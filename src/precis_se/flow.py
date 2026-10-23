"""se → precis_flow adapter — the slice-2 half of
docs/backlog/se-flow-kernel.md.

A thin chamber arrives as a block's posed cad **envelope** (or, for the
kernel-facing entry point, any ray-queryable solid — a ``Placed``
primitive or a ``(Design, Expr)`` pair, which is how a ``cad`` kind
solid's ``body add`` unions carry floor topography). This module turns
it into a :class:`precis_flow.Domain`:

- **thin axis** — the smallest local-AABB extent, refused when it is
  not unique (the two lateral extents must each clear
  :data:`THIN_MIN_RATIO` × the gap; Q3 keeps 3-D chambers out);
- **mask** — the footprint, cell centres whose thin-axis ray meets
  solid;
- **depth map** — ``h*(x*, y*)``, the local chord through the solid
  over the reference gap ``h0`` (median inside chord — a drilled floor
  digs deeper, a flat slab reads ``h* = 1``; the ceiling is whichever
  thin-axis end is the flatter boundary, ``z_flip`` records when it is
  the low end);
- **ports** — ``patch:`` selectors resolved through
  :mod:`precis_se.datums` (``_face_frame`` gives the face's local
  rectangle; a patch on *any* block is re-posed into the chamber's
  local frame). A patch claims the inside cells within
  :data:`PORT_SNAP_DX` cells of its lateral projection — a side-face
  patch is a ~zero-width ribbon on the boundary, so the claim reaches
  the nearest inside column (cfdtest's ports are the first ~1.5 cells
  of inside within the port band).

``view='flow'`` (:func:`render_flow`) runs solve + trace and serves two
shapes: a text summary (field, streamline/statistics, regime card) and
``format='json'`` — the slice-2b player payload: chamber outline +
floor depth map, ``trace(record_frames=K)`` frames as one base64
float32 ``(K, n, 3)`` blob (z* is the local gap fraction, 0 = floor,
1 = ceiling), port masks, the tracking plane, per-particle outcomes,
the scale triple ``(L, h0, u0)`` and the regime card (``size=``
re-dimensionalises the same shape). Over :data:`PAYLOAD_CAP_BYTES` the
payload subsamples particles first, then frames, and says by how much.
The run splits into :func:`flow_chamber` (envelope → Domain, the costly
half), :func:`flow_trace` and :func:`flow_scale` so the web Flow tab
(``precis_web/routes/se_flow.py``) can memoise the first two.

**Measures through COMPUTERS.** A *chamber spec* is a pocket whose
``shape`` is the ``flow:`` token —
``flow:in=<patch>;out=<patch>;dv=<d_v*>[;profile=plug|parabolic][;zplane=<z*>]``
— ``dv`` is the required vertical-dispersion input (the kernel never
defaults it). A measure on a region of that pocket gets a realised
value from :data:`FLOW_COMPUTERS` (breakthrough fraction, residence
time in ``t0``, resistance ratio against the undrilled chamber,
evenness ``1 - nonuniformity`` over the datum region's lateral
footprint).
"""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass
from typing import Any, NamedTuple

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.cad.bulk import expr_aabb
from precis.cad.fold import Expr, material_intervals
from precis.cad.graph import Design
from precis.cad.primitives import Placed
from precis.cad.vec import as_vec3
from precis.cad.vec import pose as cad_pose
from precis.errors import BadInput
from precis.utils.units import format_quantity
from precis_flow import (
    AtomWall,
    CylinderLumen,
    Domain,
    Field,
    Paths,
    Ports,
    RegimeCard,
    Seed,
    nonuniformity,
    propagate,
    solve,
    trace,
)
from precis_flow.regime import KN_CONTINUUM_MAX, KN_FREE_MIN

#: A chamber's thin axis is unique only when both lateral extents clear
#: this multiple of the gap (a square tube or a cube has no thin axis;
#: Q3 keeps 3-D chambers out of the kernel).
THIN_MIN_RATIO = 2.0

#: A port patch claims inside cells this many cells from its lateral
#: ribbon (a side-face patch is a ~zero-width ribbon on the boundary;
#: the claim must reach the nearest inside column — cfdtest's ports are
#: the first ~1.5 cells of inside within the port band).
PORT_SNAP_DX = 1.75

#: The slice-2b player payload stays under this byte budget; over it the
#: payload subsamples particles first, then frames (it reports the
#: strides it took).
PAYLOAD_CAP_BYTES = 20_000_000

#: Default lateral cell count along x* (the domain spans ``x*∈[0,1]``,
#: so this also sets the reduced cell size ``dx* = 1/nx``).
DEFAULT_NX = 200

#: Evenness histogram bins over the measure's datum region (cfdtest
#: used 12×4 over its central rectangle; here the region is the
#: datum's own footprint).
EVENNESS_BINS = (12, 4)

_AXES = ("x", "y", "z")


class _Solid(NamedTuple):
    """A ray-queryable solid in one frame: its AABB plus inside-interval
    hits ``(o, d) -> [(t_in, t_out), ...]``."""

    lo: np.ndarray
    hi: np.ndarray
    hits: Any


def _wrap_solid(solid: Any) -> _Solid:
    """Normalise ``Placed`` or ``(Design, Expr)`` to :class:`_Solid` —
    all queries land in the solid's own frame (the envelope's local
    frame for ``Placed``; the design frame for a ``cad`` scene)."""
    if isinstance(solid, Placed):
        prim = solid.prim
        lo, hi = prim.aabb_local()
        return _Solid(
            lo=np.asarray(lo, dtype=float),
            hi=np.asarray(hi, dtype=float),
            hits=prim.ray_hits_local,
        )
    if isinstance(solid, tuple) and len(solid) == 2:
        design, expr = solid
        if not isinstance(design, Design) or not isinstance(expr, Expr):
            raise BadInput(f"a cad solid is (Design, Expr), got {type(solid)}")
        lo, hi = expr_aabb(design, expr)

        def hits(o: Any, d: Any) -> Any:
            return material_intervals(expr, o, d, design.instances)

        return _Solid(
            lo=np.asarray(lo, dtype=float),
            hi=np.asarray(hi, dtype=float),
            hits=hits,
        )
    raise BadInput(
        "a flow chamber's solid is a Placed envelope or a (Design, Expr) "
        f"cad solid, got {type(solid).__name__}"
    )


@dataclass(frozen=True)
class PortPatch:
    """A port opening: a rectangle in the chamber's local frame
    (metres), resolved from a ``patch:`` selector or built directly."""

    point: np.ndarray  # (3,) rectangle centre
    normal: np.ndarray  # (3,) unit outward normal of the carrying face
    u: np.ndarray  # (3,) unit in-plane axis
    v: np.ndarray  # (3,) unit in-plane axis
    half_u: float  # half extent along u
    half_v: float  # half extent along v
    selector: str = ""  # the patch: text it came from (diagnostics)


@dataclass(frozen=True)
class FlowSpec:
    """A chamber spec's parsed ``flow:`` pocket-shape token:

    ``flow:in=<patch>;out=<patch>;dv=<d_v*>[;profile=plug|parabolic]
    [;zplane=<z*>]`` — ``in``/``out`` are ``patch:`` selectors tagging
    the ports, ``dv`` the reduced vertical dispersion over deepened
    floor (required — the kernel never defaults it), ``profile`` the
    horizontal velocity profile (Q4; the se default is ``parabolic``),
    ``zplane`` the tracking plane in reduced height (default 0.99)."""

    inlet: str
    outlet: str
    d_v_star: float
    profile: str = "parabolic"
    zplane: float = 0.99


@dataclass(frozen=True)
class LumenSpec:
    """A lumen spec's parsed ``flow:`` pocket-shape token:

    ``flow:lumen;alpha=<a>`` — the Knudsen propagator on a
    structure-bound block: the wall is the bound structure's atom sites
    as vdW spheres (``generated.sites`` in the spec's words), the ports
    the lumen's two open axial ends, ``alpha`` the required
    accommodation coefficient (the propagator never defaults it)."""

    alpha: float


def parse_flow_shape(text: str) -> FlowSpec | LumenSpec:
    """Parse a ``flow:`` pocket-shape token. ``ValueError`` names the
    grammar on any malformed term."""
    s = (text or "").strip()
    grammar = (
        "'flow:in=<patch>;out=<patch>;dv=<d_v*>[;profile=plug|parabolic][;zplane=<z*>]'"
        " or 'flow:lumen;alpha=<a>'"
    )
    if not s.startswith("flow:"):
        raise ValueError(f"flow spec is {grammar}, got {text!r}")
    if s[len("flow:") :].split(";")[0] == "lumen":
        kv_l: dict[str, str] = {}
        for part in s[len("flow:") :].split(";")[1:]:
            key, sep, val = part.partition("=")
            if not sep or key.strip() != "alpha":
                raise ValueError(
                    f"flow lumen spec: unexpected term {part!r} — "
                    "'flow:lumen;alpha=<a>'"
                )
            kv_l["alpha"] = val.strip()
        if "alpha" not in kv_l:
            raise ValueError("flow lumen spec needs alpha= — 'flow:lumen;alpha=<a>'")
        try:
            alpha = float(kv_l["alpha"])
        except ValueError:
            raise ValueError(
                f"flow lumen spec: alpha must be a number in [0, 1], "
                f"got {kv_l['alpha']!r}"
            ) from None
        if not math.isfinite(alpha) or not 0.0 <= alpha <= 1.0:
            raise ValueError(
                f"flow lumen spec: alpha must lie in [0, 1], got {alpha!r}"
            )
        return LumenSpec(alpha=alpha)
    kv: dict[str, str] = {}
    for part in s[len("flow:") :].split(";"):
        key, sep, val = part.partition("=")
        if not sep or not key.strip() or not val.strip():
            raise ValueError(f"flow chamber spec: malformed term {part!r} — {grammar}")
        if key.strip() in kv:
            raise ValueError(f"flow chamber spec: {key.strip()!r} given twice")
        kv[key.strip()] = val.strip()
    stray = sorted(set(kv) - {"in", "out", "dv", "profile", "zplane"})
    if stray:
        raise ValueError(
            f"flow chamber spec: unknown key(s) {', '.join(stray)} — "
            "keys are in, out, dv, profile, zplane"
        )
    for required in ("in", "out", "dv"):
        if required not in kv:
            raise ValueError(f"flow chamber spec needs {required!r}= — {grammar}")
    try:
        dv = float(kv["dv"])
    except ValueError:
        raise ValueError(
            f"flow chamber spec: dv must be a number (D_v* = D_v t0/h0^2), "
            f"got {kv['dv']!r}"
        ) from None
    if not math.isfinite(dv) or dv < 0.0:
        raise ValueError(f"flow chamber spec: dv must be a finite ≥ 0, got {dv!r}")
    profile = kv.get("profile", "parabolic").lower()
    if profile not in ("plug", "parabolic"):
        raise ValueError(
            f"flow chamber spec: profile is 'plug' or 'parabolic', got {profile!r}"
        )
    zplane = 0.99
    if "zplane" in kv:
        try:
            zplane = float(kv["zplane"])
        except ValueError:
            raise ValueError(
                f"flow chamber spec: zplane must be a number in (0, 1], "
                f"got {kv['zplane']!r}"
            ) from None
        if not 0.0 < zplane <= 1.0:
            raise ValueError(
                f"flow chamber spec: zplane must lie in (0, 1], got {zplane!r}"
            )
    return FlowSpec(
        inlet=kv["in"], outlet=kv["out"], d_v_star=dv, profile=profile, zplane=zplane
    )


@dataclass(frozen=True)
class Chamber:
    """A built flow chamber: the :class:`Domain` plus the geometry
    provenance the stored result and player payload need."""

    domain: Domain
    L: float  # m — lateral extent along the x* axis
    h0: float  # m — reference gap (median inside chord)
    thin_axis: int  # local axis index of the gap
    x_axis: int  # local axis mapped to x*
    y_axis: int  # local axis mapped to y*
    z_ceiling: float  # thin-axis coordinate of the flat ceiling (local, m)
    z_flip: bool  # True when the ceiling sits at the low end of the thin axis
    lo: np.ndarray  # (3,) local AABB lo
    hi: np.ndarray  # (3,) local AABB hi
    outline: np.ndarray  # (K, 2) footprint ring in reduced coords
    depth_star: np.ndarray  # (ny, nx) floor topography h* - 1


def _inside_intervals(
    solid: _Solid, o: np.ndarray, d: np.ndarray, axis: int
) -> list[tuple[float, float]]:
    """The solid's inside intervals along ``d`` from ``o``, clipped to
    the thin AABB, slivers dropped. Hits are ray parameters measured
    from ``o``; the AABB band is ``o[axis]`` offset to the box ends."""
    lo_z, hi_z = float(solid.lo[axis]), float(solid.hi[axis])
    scale = max(hi_z - lo_z, 1e-30)
    eps = max(1e-12, 1e-9 * scale)
    t_lo = lo_z - float(o[axis]) - eps
    t_hi = hi_z - float(o[axis]) + eps
    out: list[tuple[float, float]] = []
    for a, b in solid.hits(as_vec3(o), as_vec3(d)) or []:
        a, b = max(float(a), t_lo), min(float(b), t_hi)
        if b - a > eps:
            out.append((a, b))
    return out


def _mask_outline(mask: np.ndarray) -> np.ndarray:
    """The footprint's boundary ring in reduced coords — the union of
    inside-cell edges facing outside, chained into a closed polygon.
    Cell-resolution (jagged) by construction."""
    ny, nx = mask.shape
    dx = 1.0 / nx
    edges: dict[tuple[float, float], list[tuple[float, float]]] = {}
    for j in range(ny):
        for i in range(nx):
            if not mask[j, i]:
                continue
            x0, x1 = i * dx, (i + 1) * dx
            y0, y1 = j * dx, (j + 1) * dx
            if j == 0 or not mask[j - 1, i]:
                edges.setdefault((x0, y0), []).append((x1, y0))
            if j == ny - 1 or not mask[j + 1, i]:
                edges.setdefault((x1, y1), []).append((x0, y1))
            if i == 0 or not mask[j, i - 1]:
                edges.setdefault((x0, y1), []).append((x0, y0))
            if i == nx - 1 or not mask[j, i + 1]:
                edges.setdefault((x1, y0), []).append((x1, y1))
    if not edges:
        return np.empty((0, 2))
    start = min(edges)
    ring = [start]
    cur = start
    while True:
        nxts = edges.get(cur, [])
        nxt = nxts.pop(0) if nxts else None
        if nxt is None or nxt == start:
            break
        ring.append(nxt)
        cur = nxt
    return np.asarray(ring, dtype=float)


def _claim_port(
    mask: np.ndarray,
    patch: PortPatch,
    lateral: tuple[int, int],
    lo: np.ndarray,
    L: float,
) -> np.ndarray:
    """Rasterise a port patch to a cell mask: inside cells within
    :data:`PORT_SNAP_DX` cells of the patch's lateral projection."""
    ny, nx = mask.shape
    xa, ya = lateral
    dx_star = 1.0 / nx
    cell_m = L / nx  # one reduced cell in metres along either lateral axis
    n_u = max(1, math.ceil(2.0 * patch.half_u / (cell_m / 2.0)))
    n_v = max(1, math.ceil(2.0 * patch.half_v / (cell_m / 2.0)))
    us = np.linspace(-patch.half_u, patch.half_u, n_u + 1)
    vs = np.linspace(-patch.half_v, patch.half_v, n_v + 1)
    centre_x = (np.arange(nx) + 0.5) * dx_star
    centre_y = (np.arange(ny) + 0.5) * dx_star
    snap = PORT_SNAP_DX * dx_star
    reach = 2
    claimed = np.zeros((ny, nx), dtype=bool)
    for su in us:
        for sv in vs:
            s = patch.point + su * patch.u + sv * patch.v
            xs = (float(s[xa]) - float(lo[xa])) / L
            ys = (float(s[ya]) - float(lo[ya])) / L
            gi = math.floor(xs / dx_star)
            gj = math.floor(ys / dx_star)
            best: tuple[float, int, int] | None = None
            for j in range(gj - reach, gj + reach + 1):
                if j < 0 or j >= ny:
                    continue
                for i in range(gi - reach, gi + reach + 1):
                    if i < 0 or i >= nx or not mask[j, i]:
                        continue
                    dist = math.hypot(xs - centre_x[i], ys - centre_y[j])
                    if dist <= snap and (best is None or dist < best[0]):
                        best = (dist, j, i)
            if best is not None:
                claimed[best[1], best[2]] = True
    return claimed


def domain_from_solid(
    solid: Any,
    inlet: PortPatch,
    outlet: PortPatch,
    *,
    nx: int = DEFAULT_NX,
    z_plane_star: float = 0.99,
) -> Chamber:
    """Build the reduced :class:`Domain` (plus geometry provenance) from
    a ray-queryable solid and its two port rectangles.

    ``solid`` is a ``Placed`` envelope or a ``(Design, Expr)`` cad solid;
    all coordinates are the solid's own frame. ``inlet``/``outlet`` are
    :class:`PortPatch` rectangles in that frame (``patch_port`` resolves
    them from ``patch:`` selectors)."""
    s = _wrap_solid(solid)
    extents = np.asarray(s.hi - s.lo, dtype=float)
    if not np.all(np.isfinite(extents)):
        raise BadInput(
            "the chamber's solid has an unbounded extent — a flow domain "
            "needs a bounded envelope",
            next="an unbounded leaf (chamfer:) cannot bound a chamber",
        )
    thin = int(np.argmin(extents))
    lateral = sorted(a for a in range(3) if a != thin)
    if extents[lateral[0]] < THIN_MIN_RATIO * extents[thin]:
        dims = " × ".join(
            f"{_AXES[a]} {format_quantity(float(extents[a]), 'length')}"
            for a in range(3)
        )
        raise BadInput(
            f"the chamber is not thin — its extents ({dims}) have no "
            "uniquely smallest axis (both lateral extents must be at "
            f"least {THIN_MIN_RATIO:g}× the gap)",
            next="the flow kernel is 2-D Hele-Shaw: a 3-D (non-thin) "
            "chamber needs an external Stokes solve, which this layer "
            "does not do (se-flow-kernel.md Q3)",
        )
    # x* along the longer lateral axis (cfdtest convention: flow along L).
    xa, ya = (lateral[0], lateral[1])
    if extents[ya] > extents[xa]:
        xa, ya = ya, xa
    L = float(extents[xa])
    W = float(extents[ya])
    nx = int(nx)
    if nx < 8:
        raise BadInput(
            f"flow nx={nx} is too coarse — the domain needs ≥ 8 cells along x*"
        )
    ny = max(1, round(nx * W / L))
    d = np.zeros(3)
    d[thin] = 1.0
    pad = max(float(extents[thin]), 1e-9)
    mask = np.zeros((ny, nx), dtype=bool)
    top = np.full((ny, nx), np.nan)  # highest inside t per cell
    bot = np.full((ny, nx), np.nan)  # lowest inside t per cell
    height = np.zeros((ny, nx))
    for j in range(ny):
        for i in range(nx):
            o = np.array(s.lo, dtype=float)
            o[xa] = float(s.lo[xa]) + (i + 0.5) * (L / nx)
            o[ya] = float(s.lo[ya]) + (j + 0.5) * (L / nx)
            o[thin] = float(s.lo[thin]) - pad
            spans = _inside_intervals(s, o, d, thin)
            if not spans:
                continue
            a0 = min(a for a, _b in spans)
            b0 = max(b for _a, b in spans)
            mask[j, i] = True
            bot[j, i] = a0 + float(o[thin])
            top[j, i] = b0 + float(o[thin])
            height[j, i] = b0 - a0
    if not mask.any():
        raise BadInput(
            "the chamber's solid has no inside cells — the thin-axis "
            "projection is empty",
            next="check the envelope actually contains material",
        )
    h0 = float(np.median(height[mask]))
    if h0 <= 0.0:
        raise BadInput("the chamber's reference gap is zero — degenerate envelope")
    # The ceiling is the flatter thin-axis boundary (floor topography is
    # the other end's); z_flip when the flat end is the low end.
    spread_top = float(np.nanmax(top) - np.nanmin(top))
    spread_bot = float(np.nanmax(bot) - np.nanmin(bot))
    z_flip = spread_bot < spread_top
    z_ceiling = float(np.nanmedian(bot if z_flip else top))
    h_star = np.where(mask, height / h0, 0.0)
    domain = Domain(
        mask=mask,
        h_star=h_star,
        ports=Ports(
            inlet=_claim_port(mask, inlet, (xa, ya), s.lo, L),
            outlet=_claim_port(mask, outlet, (xa, ya), s.lo, L),
        ),
        z_plane_star=z_plane_star,
    )
    return Chamber(
        domain=domain,
        L=L,
        h0=h0,
        thin_axis=thin,
        x_axis=xa,
        y_axis=ya,
        z_ceiling=z_ceiling,
        z_flip=z_flip,
        lo=np.asarray(s.lo, dtype=float),
        hi=np.asarray(s.hi, dtype=float),
        outline=_mask_outline(mask),
        depth_star=h_star - 1.0,
    )


def _chamber_placed(store: Any, tree: Any, name: str) -> Placed | None:
    """The block's envelope as a world-posed primitive — the
    ``se_datums._placed`` path with a ``field_loader`` so ``field:<sha>``
    envelopes resolve through the store. With no store (the flow
    COMPUTERS' store-free signature) the loader wired onto the loaded
    tree by :mod:`precis_se.persist` answers instead (gr503066); a bare
    tree leaves a ``field:`` envelope unbuilt."""
    from precis_se.ops import effective_envelope

    node = tree.blocks.get(name)
    if node is None:
        return None
    env = effective_envelope(tree, node)
    if env is None:
        return None
    loader = (
        store.field_loader()
        if store is not None
        else getattr(tree, "field_loader", None)
    )
    try:
        prim = cad_dsl.build(cad_dsl.parse(env), field_loader=loader)
    except (cad_dsl.DslError, ValueError):
        return None
    xform = cad_pose(as_vec3(node.pose), as_vec3(node.rot))
    return Placed(prim=prim, xform=xform)


def patch_port(tree: Any, chamber: str, selector: str) -> PortPatch:
    """Resolve a ``patch:`` selector to a :class:`PortPatch` in the
    chamber block's local frame — the patch may sit on a different
    block's face (a port plate), re-posed through the two poses."""
    from precis_se import datums as se_datums

    sel = se_datums.parse_selector(selector)
    if sel.kind != "patch":
        raise BadInput(
            f"a flow port must be a 'patch:<instance>.<tag>@<u>,<v>+<w>x<h>' "
            f"selector, got {selector!r}"
        )
    chamber_node = tree.blocks.get(chamber)
    if chamber_node is None:
        raise BadInput(f"no block {chamber!r} — the chamber names a block")
    # Only the chamber's pose is needed here — its envelope may be a
    # field: grid that only a store-bound loader can build.
    chamber_xform = cad_pose(as_vec3(chamber_node.pose), as_vec3(chamber_node.rot))
    target = sel.instance or chamber
    placed = se_datums._placed(tree, target)
    if placed is None:
        raise BadInput(f"block {target!r} has no parseable envelope")
    frame = se_datums._face_frame(placed, sel.tag or "")
    if frame is None:
        tags = ", ".join(sorted(se_datums._face_geometry(placed))) or "none"
        raise BadInput(f"no face tagged {sel.tag!r} on block {target!r} (has: {tags})")
    centre_l = (
        frame.point + float(sel.u or 0.0) * frame.u + float(sel.v or 0.0) * frame.v
    )
    # Re-pose the rectangle into the chamber's local frame.
    rel = chamber_xform.inverse().compose(placed.xform)
    return PortPatch(
        point=np.asarray(rel.apply(centre_l), dtype=float),
        normal=np.asarray(rel.apply_dir(frame.normal), dtype=float),
        u=np.asarray(rel.apply_dir(frame.u), dtype=float),
        v=np.asarray(rel.apply_dir(frame.v), dtype=float),
        half_u=float(sel.w or 0.0) / 2.0,
        half_v=float(sel.h or 0.0) / 2.0,
        selector=selector,
    )


def _bound_scene(store: Any, tree: Any, node: Any) -> Any:
    """The block's bound structure as a ``BoundScene`` — the
    ``tree.structures`` resolver first, else a store-backed one."""
    if node.bound_kind != "structure" or not node.bound:
        raise BadInput(
            f"block {node.name!r} is not structure-bound — a flow lumen "
            "needs a bound structure design (its atom sites wall the lumen)"
        )
    resolver = tree.structures
    if resolver is None and store is not None:
        from precis_se.persist import structure_scene_resolver

        resolver = structure_scene_resolver(store)
    bound = resolver(node.bound) if resolver is not None else None
    if bound is None or bound.scene is None:
        raise BadInput(
            f"bound design {node.bound!r} on block {node.name!r} does not "
            "resolve to a structure scene",
            next="the tree needs a structures resolver (persist wires one "
            "on load); a hand-built tree sets tree.structures",
        )
    return bound


def lumen_from_scene(bound: Any) -> AtomWall:
    """The bound scene's atoms as the lumen's wall: every generated atom
    site a vdW sphere (Bondi radii, ``precis_se.atomic.clearance`` —
    ``generated.sites`` in the spec's words). The scene's own frame and
    Å units — the propagator reduces by the axial extent."""
    from precis_se.atomic.clearance import VDW_DEFAULT_A, VDW_RADII_A

    scene = bound.scene
    atoms = list(scene.atoms.values())
    if not atoms:
        raise BadInput("the bound structure has no atoms — no lumen")
    pos = np.array([scene.cell.frac_to_cart(a.frac) for a in atoms])
    rad = np.array([VDW_RADII_A.get(a.element, VDW_DEFAULT_A) for a in atoms])
    try:
        return AtomWall.from_atoms(pos, rad)
    except ValueError as exc:
        raise BadInput(
            f"the bound structure is not a lumen: {exc}",
            next="a lumen is an elongated hollow structure with open ends "
            "(a hexfold tube); a sheet, blob or capped tunnel is not",
        ) from exc


def _card_from_args(
    L: float, h0: float, args: dict[str, Any]
) -> tuple[RegimeCard | None, float | None]:
    """``(card, u0)`` when the args carry the physical inputs; ``None``
    card (with a note in the render) otherwise — the reduced solve needs
    none of this."""
    have = {k: args.get(k) for k in ("mu", "rho", "dp", "u0")}
    if have["u0"] is not None and have["dp"] is not None:
        raise BadInput("view='flow': state exactly one of dp or u0")
    if have["u0"] is None and have["dp"] is None:
        if any(v is not None for v in (have["mu"], have["rho"])):
            raise BadInput(
                "view='flow': a regime card needs u0 (m/s) or dp (Pa) on top of mu, rho"
            )
        return None, None
    missing = [k for k in ("mu", "rho") if have[k] is None]
    if missing:
        raise BadInput(
            f"view='flow': a regime card needs {', '.join(missing)} too "
            "(mu in Pa·s, rho in kg/m³, plus u0 or dp)"
        )
    card = RegimeCard.from_physical(
        L=L,
        h0=h0,
        mu=float(have["mu"] or 0.0),
        rho=float(have["rho"] or 0.0),
        dp=float(have["dp"]) if have["dp"] is not None else None,
        u0=float(have["u0"]) if have["u0"] is not None else None,
        particle_d=(
            float(args["particle_d"]) if args.get("particle_d") is not None else None
        ),
        rho_p=float(args["rho_p"]) if args.get("rho_p") is not None else None,
        molecular_d=(
            float(args["molecular_d"]) if args.get("molecular_d") is not None else None
        ),
        mean_free_path=(
            float(args["mean_free_path"])
            if args.get("mean_free_path") is not None
            else None
        ),
    )
    return card, card.u0


def _finite(x: Any) -> float | None:
    """A JSON-safe number: NaN/inf (a line the inputs cannot evaluate)
    becomes ``null`` — strict JSON parsers (the web player) refuse NaN."""
    if x is None:
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _card_dict(card: RegimeCard | None) -> dict[str, Any] | None:
    if card is None:
        return None
    return {
        "ok": card.ok,
        "failures": list(card.failures),
        "lines": [
            {
                "name": ln.name,
                "value": _finite(ln.value),
                "threshold": _finite(ln.threshold),
                "ok": ln.ok,
                "holds_when": ln.holds_when,
                "otherwise": ln.otherwise,
                "applicable": ln.applicable,
            }
            for ln in card.lines
        ],
    }


def flow_payload(
    chamber: Chamber,
    paths: Paths,
    card: RegimeCard | None,
    *,
    u0: float | None,
    profile: str,
    d_v_star: float,
    block: str = "",
    scale: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The slice-2b player payload: outline + floor depth map + port
    masks, the recorded frames as one base64 float32 ``(K, n, 3)`` blob
    (z* is the local gap fraction, 0 = floor, 1 = ceiling), per-particle
    outcomes (residence ``t*``, broke/exited, first plane crossing), the
    tracking plane, the scale triple ``(L, h0, u0)`` and the regime
    card. Over :data:`PAYLOAD_CAP_BYTES` the frames blob subsamples
    particles first, then frames — ``subsampled`` reports the strides
    (the per-particle arrays take the same particle stride)."""
    frames = paths.frames
    sub_p = sub_f = 1
    blob = b""
    if frames is not None:
        sub_p = 1
        while (
            frames.shape[1] // sub_p > 1
            and (frames[:, ::sub_p].nbytes) > PAYLOAD_CAP_BYTES
        ):
            sub_p *= 2
        kept = frames[:, ::sub_p]
        while kept.shape[0] // sub_f > 1 and kept[::sub_f].nbytes > PAYLOAD_CAP_BYTES:
            sub_f *= 2
        kept = kept[::sub_f]
        blob = kept.astype("<f4", copy=False).tobytes()
        frame_shape = list(kept.shape)
        frame_t = (
            np.asarray(paths.frame_t)[::sub_f].tolist()
            if paths.frame_t is not None
            else None
        )
    else:
        frame_shape = None
        frame_t = None

    def _b64(arr: np.ndarray) -> str:
        return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode()

    d = chamber.domain
    return {
        "block": block,
        "profile": profile,
        "d_v_star": d_v_star,
        "scale": scale
        or {
            "L": chamber.L,
            "h0": chamber.h0,
            "u0": u0,
            "geometry_L": chamber.L,
            "geometry_h0": chamber.h0,
        },
        "z_plane_star": float(d.z_plane_star),
        "frame": {
            "x_axis": _AXES[chamber.x_axis],
            "y_axis": _AXES[chamber.y_axis],
            "thin_axis": _AXES[chamber.thin_axis],
            "z_flip": chamber.z_flip,
            "z_ceiling": chamber.z_ceiling,
            "aabb_lo": [float(c) for c in chamber.lo],
            "aabb_hi": [float(c) for c in chamber.hi],
        },
        "mesh": {
            "outline": [[float(x), float(y)] for x, y in chamber.outline],
            "floor_depth": {
                "shape": list(chamber.depth_star.shape),
                "dtype": "float32",
                "data_b64": base64.b64encode(
                    chamber.depth_star.astype("<f4").tobytes()
                ).decode(),
            },
            "mask": {
                "shape": list(chamber.domain.mask.shape),
                "dtype": "uint8",
                "data_b64": base64.b64encode(
                    chamber.domain.mask.astype(np.uint8).tobytes()
                ).decode(),
            },
            "ports": {
                "dtype": "uint8",
                "inlet_b64": _b64(d.ports.inlet.astype(np.uint8)),
                "outlet_b64": _b64(d.ports.outlet.astype(np.uint8)),
            },
        },
        "particles": {
            "n": int(paths.residence[::sub_p].shape[0]),
            "dtype": "float32 (residence, cross) / uint8 (broke, exited)",
            "residence_b64": _b64(paths.residence[::sub_p].astype("<f4")),
            "broke_b64": _b64(paths.broke[::sub_p].astype(np.uint8)),
            "exited_b64": _b64(paths.exited[::sub_p].astype(np.uint8)),
            "cross_b64": _b64(paths.cross_star[::sub_p].astype("<f4")),
        },
        "frames": (
            {
                "shape": frame_shape,
                "dtype": "float32",
                "encoding": "base64; little-endian; (K, n, 3) = (x*, y*, psi), "
                "psi 0 = floor, 1 = ceiling",
                "frame_t": frame_t,
                "data_b64": base64.b64encode(blob).decode(),
            }
            if frames is not None
            else None
        ),
        "subsampled": {
            "particles_stride": sub_p,
            "frames_stride": sub_f,
            "note": (
                "payload over the 20 MB cap — particles subsampled "
                f"{sub_p}×, frames {sub_f}×"
                if sub_p > 1 or sub_f > 1
                else "full resolution"
            ),
        },
        "card": _card_dict(card),
    }


def _num(args: dict[str, Any], key: str, default: Any = None) -> float | None:
    v = args.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise BadInput(f"view='flow': {key} must be a number, got {v!r}")
    return float(v)


def _lumen_kn_gate(h0_m: float, args: dict[str, Any]) -> None:
    """The card's routing on a lumen: ``mean_free_path`` (m) sets
    ``Kn = λ/h0`` against the free diameter ``h0 = 2 R_eff``. ``Kn >= 10``
    runs (this propagator); ``0.01 < Kn < 10`` refuses naming DSMC;
    ``Kn <= 0.01`` refuses — a lumen has no continuum solver. No
    ``mean_free_path`` runs too: a structure-bound lumen is intrinsically
    the free-molecular case (the card is advisory when the full physical
    set is given)."""
    mfp = _num(args, "mean_free_path")
    if mfp is None:
        return
    kn = mfp / h0_m
    if kn >= KN_FREE_MIN:
        return
    if kn > KN_CONTINUUM_MAX:
        raise BadInput(
            f"view='flow': Kn = {kn:.3g} is the transition regime "
            f"({KN_CONTINUUM_MAX:g} < Kn < {KN_FREE_MIN:g}) — neither the "
            "continuum kernel nor the nano propagator holds; DSMC is "
            "out of scope (se-flow-kernel.md slice 3)"
        )
    raise BadInput(
        f"view='flow': Kn = {kn:.3g} <= {KN_CONTINUUM_MAX:g} — the "
        "continuum kernel applies, but a structure-bound lumen is not a "
        "thin chamber",
        next="a Hele-Shaw chamber is a cad envelope block with "
        "patch: ports, not a bound structure",
    )


def _lumen_payload(
    wall: AtomWall,
    paths: Paths,
    card: RegimeCard | None,
    *,
    u0: float | None,
    alpha: float,
    scale_angstrom: float,
    block: str,
) -> dict[str, Any]:
    """The lumen counterpart of :func:`flow_payload`: same frame blob
    contract (``(K, n, 3)`` reduced ``(x*, y*, psi)``) and scale triple,
    with ``h0`` the free diameter ``2 R_eff`` and ``u0`` the thermal
    ``v̄`` when the caller supplied it. ``mesh.lumen`` carries the wall
    itself — the reduced atom sites + radii — where a chamber payload
    carries outline + floor depth; ``regime`` names the solver."""
    frames = paths.frames
    sub_p = sub_f = 1
    blob = b""
    if frames is not None:
        while (
            frames.shape[1] // sub_p > 1
            and (frames[:, ::sub_p].nbytes) > PAYLOAD_CAP_BYTES
        ):
            sub_p *= 2
        kept = frames[:, ::sub_p]
        while kept.shape[0] // sub_f > 1 and kept[::sub_f].nbytes > PAYLOAD_CAP_BYTES:
            sub_f *= 2
        kept = kept[::sub_f]
        blob = kept.astype("<f4", copy=False).tobytes()
        frame_shape = list(kept.shape)
        frame_t = (
            np.asarray(paths.frame_t)[::sub_f].tolist()
            if paths.frame_t is not None
            else None
        )
    else:
        frame_shape = None
        frame_t = None
    L_m = wall.extent * scale_angstrom
    return {
        "block": block,
        "regime": "knudsen",
        "alpha": alpha,
        "scale": {
            "L": L_m,
            "h0": 2.0 * wall.r_eff * L_m,
            "u0": u0,
            "t0": "L / v̄ — residence and frame_t count transits at v̄",
        },
        "mesh": {
            "lumen": {
                "centres": {
                    "shape": list(wall.centres.shape),
                    "dtype": "float32",
                    "data_b64": base64.b64encode(
                        wall.centres.astype("<f4").tobytes()
                    ).decode(),
                },
                "radii": {
                    "shape": list(wall.radii.shape),
                    "dtype": "float32",
                    "data_b64": base64.b64encode(
                        wall.radii.astype("<f4").tobytes()
                    ).decode(),
                },
                "r_eff": wall.r_eff,
                "r_in": wall.r_in,
                "encoding": "reduced (x* over the axial extent; "
                "centres are the wall atom sites)",
            }
        },
        "frames": (
            {
                "shape": frame_shape,
                "dtype": "float32",
                "encoding": "base64; little-endian; (K, n, 3) = (x*, y*, psi), "
                "psi the transverse fraction across the free cross-section",
                "frame_t": frame_t,
                "data_b64": base64.b64encode(blob).decode(),
            }
            if frames is not None
            else None
        ),
        "subsampled": {
            "particles_stride": sub_p,
            "frames_stride": sub_f,
            "note": (
                "payload over the 20 MB cap — particles subsampled "
                f"{sub_p}×, frames {sub_f}×"
                if sub_p > 1 or sub_f > 1
                else "full resolution"
            ),
        },
        "card": _card_dict(card),
    }


def _render_flow_lumen(store: Any, tree: Any, node: Any, args: dict[str, Any]) -> str:
    """``view='flow'`` on a structure-bound block: the Knudsen
    propagator on the bound structure's atom sites — runs without being
    asked, the card routes ``Kn >= 10`` here."""
    for key in ("inlet", "outlet", "dv", "nx", "profile", "zplane", "seed_z"):
        if args.get(key) is not None:
            raise BadInput(
                f"view='flow': {key!r} is a thin-chamber input — a "
                "structure lumen takes 'alpha' plus n/frames/seed and "
                "the physical scale args"
            )
    alpha = _num(args, "alpha")
    if alpha is None:
        raise BadInput(
            "view='flow' on a lumen needs args.alpha — the accommodation "
            "coefficient (the propagator never defaults it)",
            next="alpha = 1 is pure diffuse reflection, 0 pure specular",
        )
    if not 0.0 <= alpha <= 1.0:
        raise BadInput(f"view='flow': alpha must lie in [0, 1], got {alpha!r}")
    bound = _bound_scene(store, tree, node)
    wall = lumen_from_scene(bound)
    # the scene is Å-native; the wall's extent is in Å too
    L_m = wall.extent * 1e-10
    h0_m = 2.0 * wall.r_eff * wall.extent * 1e-10
    _lumen_kn_gate(h0_m, args)
    card, u0 = _card_from_args(L_m, h0_m, args)
    if card is not None:
        try:
            route = card.route
        except ValueError as exc:
            raise BadInput(f"view='flow': {exc}") from exc
        if route != "knudsen":
            raise BadInput(
                "view='flow': the regime card holds — but a structure "
                "lumen is not a thin chamber; the continuum kernel has "
                "no domain here",
                next="a Hele-Shaw chamber is a cad envelope block with "
                "patch: ports, not a bound structure",
            )
    n = int(_num(args, "n", 400) or 400)
    frames_k = int(_num(args, "frames", 0) or 0)
    seed_no = int(_num(args, "seed", 0) or 0)
    paths = propagate(wall, n, alpha, seed_no, record_frames=frames_k)
    payload = _lumen_payload(
        wall, paths, card, u0=u0, alpha=alpha, scale_angstrom=1e-10, block=node.name
    )
    fmt = str(args.get("format") or "text").strip().lower()
    if fmt == "json":
        return json.dumps(payload, indent=1)
    if fmt != "text":
        raise BadInput(
            f"view='flow': args.format must be 'text' or 'json', got {fmt!r}",
            options=["text", "json"],
        )
    lines = [
        "# flow — free-molecular (Knudsen) propagator on an atom-site wall",
        "",
        (
            f"lumen: L = {format_quantity(wall.extent * 1e-10, 'length')} axial, "
            f"{wall.centres.shape[0]} wall sites, R_eff = "
            f"{format_quantity(wall.r_eff * wall.extent * 1e-10, 'length')} "
            f"(inscribed {format_quantity(wall.r_in * wall.extent * 1e-10, 'length')})"
        ),
        (
            f"propagate: n = {paths.n_particles}, alpha {alpha:g}, "
            f"transmission {paths.breakthrough:.3f}, resolved "
            f"{paths.resolved_fraction:.2f} "
            f"(trapped {paths.trapped_fraction:.2f}), flights "
            f"{paths.steps_used}"
        ),
    ]
    res = paths.residence[np.isfinite(paths.residence)]
    if res.size:
        lines.append(
            f"residence t*: mean {float(res.mean()):.3g}, "
            f"std {float(res.std()):.3g} (over resolved; t* = t v̄/L)"
        )
    if paths.frames is not None:
        blob_bytes = len(base64.b64decode(payload["frames"]["data_b64"]))
        sub = payload["subsampled"]
        lines.append(
            f"player payload: frames {payload['frames']['shape']} f32 "
            f"({blob_bytes:,} B) — {sub['note']}; "
            "get(format='json') serves the full payload"
        )
    lines.append("")
    if card is None:
        lines.append(
            "regime card: not computed — pass mu, rho, u0 and "
            "mean_free_path (SI) for the six-line card"
        )
    else:
        lines.append(
            f"regime card ({'OK' if card.ok else 'FAILS: ' + ', '.join(card.failures)})"
            " — routed knudsen:"
        )
        for ln in card.lines:
            mark = "ok" if ln.ok else "FAIL"
            lines.append(
                f"  {ln.name:<9} {ln.value:.3g}  [{mark}; holds when "
                f"{ln.holds_when}; else {ln.otherwise}]"
            )
    return "\n".join(lines)


@dataclass(frozen=True)
class FlowPocket:
    """A ``flow:`` chamber spec found on a block — what the web Flow
    tab offers to play."""

    block: str
    pocket: str
    spec: FlowSpec


def flow_pockets(tree: Any) -> list[FlowPocket]:
    """Every pocket whose shape parses as a ``flow:`` chamber spec, in
    block then pocket name order."""
    out: list[FlowPocket] = []
    for bname in sorted(tree.blocks):
        node = tree.blocks[bname]
        for pname in sorted(getattr(node, "pockets", {}) or {}):
            shape = (node.pockets[pname].shape or "").strip()
            if not shape.startswith("flow:"):
                continue
            try:
                spec = parse_flow_shape(shape)
            except ValueError:
                continue
            if not isinstance(spec, FlowSpec):
                continue  # a lumen spec has no web player yet
            out.append(FlowPocket(block=bname, pocket=pname, spec=spec))
    return out


def flow_chamber(store: Any, tree: Any, args: dict[str, Any]) -> Chamber:
    """The chamber of ``view='flow'`` args (``block``, ``inlet``,
    ``outlet``, optional ``nx`` and ``zplane``) — the expensive,
    trace-independent half of a run (the web player caches it)."""
    block = str(args.get("block") or "").strip()
    if not block:
        raise BadInput(
            "view='flow' requires args={'block': <chamber block>, "
            "'inlet': <patch>, 'outlet': <patch>, 'dv': <d_v*>} — or "
            "{'block': <structure-bound block>, 'alpha': <a>} on a lumen"
        )
    if block not in tree.blocks:
        raise BadInput(f"no block {block!r} — the chamber names a block")
    inlet_sel = args.get("inlet")
    outlet_sel = args.get("outlet")
    if not inlet_sel or not outlet_sel:
        raise BadInput(
            "view='flow' needs args={'inlet': <patch>, 'outlet': <patch>} — "
            "patch: selectors tagging the two ports"
        )
    placed = _chamber_placed(store, tree, block)
    if placed is None:
        raise BadInput(
            f"block {block!r} has no parseable envelope — the chamber "
            "needs a cad envelope"
        )
    nx = int(_num(args, "nx", DEFAULT_NX) or DEFAULT_NX)
    zplane = float(_num(args, "zplane", 0.99) or 0.99)
    return domain_from_solid(
        placed,
        patch_port(tree, block, str(inlet_sel)),
        patch_port(tree, block, str(outlet_sel)),
        nx=nx,
        z_plane_star=zplane,
    )


def flow_trace(
    chamber: Chamber, args: dict[str, Any]
) -> tuple[Field, Paths, str, float]:
    """Solve + trace the chamber for ``view='flow'`` args (``dv``
    required; ``profile``, ``n``, ``frames``, ``seed``, ``seed_z``) →
    ``(field, paths, profile, d_v*)``."""
    dv = _num(args, "dv")
    if dv is None:
        raise BadInput(
            "view='flow' needs args.dv — the reduced vertical dispersion "
            "D_v* (the kernel never defaults it)"
        )
    profile = str(args.get("profile") or "parabolic").strip().lower()
    if profile not in ("plug", "parabolic"):
        raise BadInput(
            f"view='flow': profile is 'plug' or 'parabolic', got {profile!r}",
            options=["plug", "parabolic"],
        )
    n = int(_num(args, "n", 400) or 400)
    frames_k = int(_num(args, "frames", 0) or 0)
    seed_no = int(_num(args, "seed", 0) or 0)
    seed_z = args.get("seed_z") or [0.0, 0.5]
    if (
        not isinstance(seed_z, list | tuple)
        or len(seed_z) != 2
        or not all(isinstance(z, int | float) for z in seed_z)
    ):
        raise BadInput("view='flow': seed_z must be [z_lo, z_hi] gap fractions")
    z_lo, z_hi = float(seed_z[0]), float(seed_z[1])
    field = solve(chamber.domain)
    seed = Seed.across_inlet(chamber.domain, z_lo=z_lo, z_hi=z_hi)
    paths = trace(
        field,
        seed,
        n,
        d_v_star=dv,
        seed_rng=seed_no,
        record_frames=frames_k,
        profile=profile,
    )
    return field, paths, profile, dv


def flow_scale(
    chamber: Chamber, args: dict[str, Any]
) -> tuple[RegimeCard | None, dict[str, Any]]:
    """The regime card and scale triple for ``view='flow'`` args.
    ``size`` (m) re-dimensionalises the same shape at another lateral
    extent — ``L = size``, ``h0`` scaled with it — so the card judges
    that size; without it the chamber's own geometry stands."""
    size = _num(args, "size")
    if size is not None and not (math.isfinite(size) and size > 0.0):
        raise BadInput(f"view='flow': size must be a length > 0 in m, got {size!r}")
    k = (size / chamber.L) if size is not None else 1.0
    L, h0 = chamber.L * k, chamber.h0 * k
    card, u0 = _card_from_args(L, h0, args)
    if card is not None:
        try:
            route = card.route
        except ValueError as exc:
            if card.knudsen.applicable and card.knudsen.value > KN_CONTINUUM_MAX:
                raise BadInput(f"view='flow': {exc}") from exc
            # a failing continuum line is shown on the card, not refused —
            # the web player re-judges a cached trace at other sizes
            route = "continuum"
        if route != "continuum":
            raise BadInput(
                f"view='flow': the regime card routes {route} — Kn "
                f"{card.knudsen.value:.3g} puts this chamber in the "
                "free-molecular regime, and the nano propagator runs on "
                "structure lumens only (a cad envelope has no atom wall)",
                next="bind a structure lumen block and pass alpha=",
            )
    return card, {
        "L": L,
        "h0": h0,
        "u0": u0,
        "geometry_L": chamber.L,
        "geometry_h0": chamber.h0,
    }


def render_flow(store: Any, tree: Any, args: dict[str, Any]) -> str:
    """``view='flow'`` — solve + trace the chamber and render the text
    summary or the slice-2b player payload (``format='json'``); on a
    structure-bound block the Knudsen propagator runs instead (slice 3)."""
    block = str(args.get("block") or "").strip()
    if block and block in tree.blocks:
        node = tree.blocks[block]
        if node.bound_kind == "structure":
            return _render_flow_lumen(store, tree, node, args)
    fmt = str(args.get("format") or "text").strip().lower()
    if fmt not in ("text", "json"):
        raise BadInput(
            f"view='flow': args.format must be 'text' or 'json', got {fmt!r}",
            options=["text", "json"],
        )
    chamber = flow_chamber(store, tree, args)
    card, scale = flow_scale(chamber, args)  # routes before the solve
    field, paths, profile, dv = flow_trace(chamber, args)
    payload = flow_payload(
        chamber,
        paths,
        card,
        u0=scale["u0"],
        profile=profile,
        d_v_star=dv,
        block=str(args.get("block") or "").strip(),
        scale=scale,
    )
    if fmt == "json":
        return json.dumps(payload, indent=1)
    return _render_flow_text(chamber, field, paths, card, payload)


def _render_flow_text(
    chamber: Chamber,
    field: Field,
    paths: Paths,
    card: RegimeCard | None,
    payload: dict[str, Any],
) -> str:
    d = chamber.domain
    speed = np.hypot(field.ux_star, field.uy_star)
    lines = [
        f"# flow — thin-chamber Hele-Shaw on {d.nx}×{d.ny} cells",
        "",
        (
            f"chamber: L = {format_quantity(chamber.L, 'length')} along "
            f"local +{_AXES[chamber.x_axis]}, h0 = "
            f"{format_quantity(chamber.h0, 'length')} (thin axis "
            f"local {_AXES[chamber.thin_axis]}"
            f"{', ceiling at the low end' if chamber.z_flip else ''}); "
            f"{int(d.mask.sum())} inside cells, "
            f"depth* max {float(chamber.depth_star.max()):.3g}"
        ),
        (
            f"ports: inlet {int(d.ports.inlet.sum())} cells, "
            f"outlet {int(d.ports.outlet.sum())} cells"
        ),
        (f"field: R* = {field.r_star:.4g}, |U*| max {float(speed[d.mask].max()):.4g}"),
        (
            f"trace: n = {paths.n_particles}, profile "
            f"{payload['profile']}, D_v* = {payload['d_v_star']:g}, "
            f"breakthrough {paths.breakthrough:.3f}, resolved "
            f"{paths.resolved_fraction:.2f} "
            f"(trapped {paths.trapped_fraction:.2f}), steps "
            f"{paths.steps_used}"
        ),
    ]
    res = paths.residence[np.isfinite(paths.residence)]
    if res.size:
        lines.append(
            f"residence t*: mean {float(res.mean()):.3g}, "
            f"std {float(res.std()):.3g} (over resolved)"
        )
    if paths.frames is not None:
        blob_bytes = len(base64.b64decode(payload["frames"]["data_b64"]))
        sub = payload["subsampled"]
        lines.append(
            f"player payload: frames {payload['frames']['shape']} f32 "
            f"({blob_bytes:,} B) — {sub['note']}; "
            "get(format='json') serves the full payload"
        )
    lines.append("")
    if card is None:
        lines.append(
            "regime card: not computed — pass mu, rho and u0|dp (SI) for "
            "the six-line card"
        )
    else:
        lines.append(
            f"regime card ({'OK' if card.ok else 'FAILS: ' + ', '.join(card.failures)}):"
        )
        for ln in card.lines:
            mark = "ok" if ln.ok else "FAIL"
            lines.append(
                f"  {ln.name:<9} {ln.value:.3g}  [{mark}; holds when "
                f"{ln.holds_when}; else {ln.otherwise}]"
            )
    return "\n".join(lines)


# ── measures through COMPUTERS ────────────────────────────────────────


def _flow_pocket(node: Any, datum: str | None) -> Any | str:
    """The pocket on ``node`` claiming ``datum`` as a region and carrying
    a parsed ``flow:`` shape — or a reason string."""
    from precis_se import datums as se_datums

    hits = [
        p
        for p in node.pockets.values()
        if any(se_datums.same_region(r, datum) for r in p.regions)
    ]
    if not hits:
        return (
            f"no pocket on {node.name!r} claims region {datum!r} — a flow "
            "measure needs a 'flow:in=…;out=…;dv=…' pocket"
        )
    pocket = hits[0]
    shape = (pocket.shape or "").strip()
    try:
        spec = parse_flow_shape(shape)
    except ValueError:
        return (
            f"pocket {pocket.name!r} on {node.name!r} carries shape "
            f"{shape!r}, not a 'flow:in=…;out=…;dv=…' chamber spec"
        )
    return pocket, spec


def _flow_run(
    tree: Any, measure: Any
) -> tuple[Any, Paths | None, FlowSpec | LumenSpec | None, str | None]:
    """Domain + trace (chamber) or wall + propagate (lumen) for a flow
    measure — the first element is the :class:`Chamber` or the
    :class:`AtomWall`, or a reason string on failure."""
    node = tree.blocks.get(measure.block)
    if node is None:
        return None, None, None, f"measure names missing block {measure.block!r}"
    got = _flow_pocket(node, measure.datum or "")
    if isinstance(got, str):
        return None, None, None, got
    _pocket, spec = got
    if isinstance(spec, LumenSpec):
        try:
            wall = lumen_from_scene(_bound_scene(None, tree, node))
            paths = propagate(wall, 400, spec.alpha, 0)
        except BadInput as exc:
            return None, None, spec, str(exc)
        return wall, paths, spec, None
    placed = _chamber_placed(None, tree, measure.block)
    if placed is None:
        return (
            None,
            None,
            spec,
            (
                f"block {measure.block!r} has no parseable envelope — the "
                "chamber needs a cad envelope"
            ),
        )
    try:
        inlet = patch_port(tree, measure.block, spec.inlet)
        outlet = patch_port(tree, measure.block, spec.outlet)
        chamber = domain_from_solid(placed, inlet, outlet, z_plane_star=spec.zplane)
        field = solve(chamber.domain)
        seed = Seed.across_inlet(chamber.domain)
        paths = trace(
            field,
            seed,
            400,
            d_v_star=spec.d_v_star,
            seed_rng=0,
            profile=spec.profile,
        )
    except (BadInput, ValueError) as exc:
        return None, None, spec, str(exc)
    return chamber, paths, spec, None


def _flow_breakthrough(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    _ch, paths, _spec, err = _flow_run(tree, measure)
    if paths is None:
        return None, err
    return float(paths.breakthrough), None


def _flow_residence(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    _ch, paths, _spec, err = _flow_run(tree, measure)
    if paths is None:
        return None, err
    res = paths.residence[np.isfinite(paths.residence)]
    if not res.size:
        return None, "no particles resolved — no residence statistic"
    return float(res.mean()), None


def _flow_resistance_ratio(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    chamber, paths, spec, err = _flow_run(tree, measure)
    if chamber is None or paths is None:
        return None, err
    if isinstance(chamber, AtomWall):
        # free-molecular resistance ∝ 1/W — the corrugated lumen against
        # the smooth cylinder at R_eff = R_axis − r_vdW, same seed
        if paths.breakthrough <= 0.0:
            return None, "the lumen transmits nothing — no resistance ratio"
        assert isinstance(spec, LumenSpec)
        ref = propagate(CylinderLumen(chamber.r_eff), 400, spec.alpha, 0)
        if ref.breakthrough <= 0.0:
            return None, "the smooth-cylinder reference transmits nothing"
        return float(ref.breakthrough / paths.breakthrough), None
    d = chamber.domain
    flat = Domain(
        mask=d.mask,
        h_star=np.where(d.mask, 1.0, 0.0),
        ports=d.ports,
        z_plane_star=d.z_plane_star,
    )
    r_ref = solve(flat).r_star
    if not math.isfinite(r_ref) or r_ref <= 0:
        return None, "the undrilled chamber's resistance is not finite"
    r = solve(d).r_star
    return float(r / r_ref), None


def _flow_evenness(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    """``1 - nonuniformity`` of first-crossing points over the datum
    patch's lateral footprint (1 = perfectly even, 0 = all in one bin)."""
    from precis_se import datums as se_datums

    chamber, paths, spec, err = _flow_run(tree, measure)
    if chamber is None or paths is None:
        return None, err
    if isinstance(chamber, AtomWall):
        # the lumen's region is its outlet aperture: exit points mapped
        # to equal-area polar bins (u = r²/R_eff², v = θ/2π over the
        # disk — a uniform exit spread reads uniform there)
        pts = paths.crossing_points()
        if pts.shape[0] == 0:
            return None, "no transmitted particles — no evenness"
        z = pts[:, 1] * 2.0 * chamber.r_eff - chamber.r_eff
        y = pts[:, 0]
        u = np.clip((y * y + z * z) / chamber.r_eff**2, 0.0, 1.0)
        v = (np.arctan2(z, y) + np.pi) / (2.0 * np.pi)
        nu, _ = nonuniformity(
            np.column_stack([u, v]), (0.0, 1.0), (0.0, 1.0), EVENNESS_BINS
        )
        return float(1.0 - nu), None
    datum = measure.datum or ""
    try:
        sel = se_datums.parse_selector(datum)
    except Exception:
        return None, f"datum {datum!r} does not parse as a patch region"
    if sel.kind != "patch":
        return None, f"evenness scores over a patch: region, got {datum!r}"
    patch = patch_port(tree, measure.block, datum)
    xa, ya = chamber.x_axis, chamber.y_axis
    lo = chamber.lo
    u_pts = (
        patch.point[xa] + np.array([-patch.half_u, patch.half_u]) * patch.u[xa],
        patch.point[ya] + np.array([-patch.half_u, patch.half_u]) * patch.u[ya],
    )
    v_pts = (
        patch.point[xa] + np.array([-patch.half_v, patch.half_v]) * patch.v[xa],
        patch.point[ya] + np.array([-patch.half_v, patch.half_v]) * patch.v[ya],
    )
    xs = np.concatenate([u_pts[0], v_pts[0]])
    ys = np.concatenate([u_pts[1], v_pts[1]])
    x_range = (
        float((xs.min() - lo[xa]) / chamber.L),
        float((xs.max() - lo[xa]) / chamber.L),
    )
    y_range = (
        float((ys.min() - lo[ya]) / chamber.L),
        float((ys.max() - lo[ya]) / chamber.L),
    )
    if x_range[1] - x_range[0] <= 0 or y_range[1] - y_range[0] <= 0:
        return None, (
            f"datum {datum!r} projects to a degenerate lateral region — "
            "evenness needs a patch on a floor/lid face"
        )
    nu, _kept = nonuniformity(paths.crossing_points(), x_range, y_range, EVENNESS_BINS)
    return float(1.0 - nu), None


from precis_se.properties.computers import _Computer

#: measurand slug → computer, registered in
#: :data:`precis_se.properties.COMPUTERS` — the four flow measurands of
#: se-flow-kernel.md slice 2.
FLOW_COMPUTERS: dict[str, Any] = {
    "breakthrough-fraction": _Computer(_flow_breakthrough),
    "residence-time-t0": _Computer(_flow_residence),
    "resistance-ratio": _Computer(_flow_resistance_ratio),
    "evenness": _Computer(_flow_evenness),
}
