"""``precis_geom.flow`` — the DB-free cad solid → :class:`precis_flow.Domain`
adapter (se-flow-kernel slice 4a).

Refactored out of ``precis_se/flow.py`` so a workspace script builds the
thin-gap domain from the cad graph it made, with no DB and no se: the
solid is a ``Placed`` primitive or a ``(Design, Expr)`` pair, the ports
are :class:`PortPatch` rectangles given directly in the solid's frame.
``precis_se.flow`` keeps what needs the tree — ``patch_port`` (``patch:``
selectors through ``precis_se.datums``), the ``flow:`` pocket token,
``FLOW_COMPUTERS`` and ``view='flow'`` — and imports this module (one
implementation; ``Chamber`` there is an alias of :class:`GapDomain`).

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
- **ports** — a :class:`PortPatch` claims the inside cells within
  :data:`PORT_SNAP_DX` cells of its lateral projection — a side-face
  patch is a ~zero-width ribbon on the boundary, so the claim reaches
  the nearest inside column (cfdtest's ports are the first ~1.5 cells
  of inside within the port band).

Names are general on purpose: a :class:`GapDomain` is any built thin
gap (``Domain`` + scale + axis map + outline + depth map), nothing in it
is chamber-specific. The physics outcomes over it live in
:mod:`precis_geom.physics`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, NamedTuple

import numpy as np

from precis.cad.bulk import expr_aabb
from precis.cad.fold import Expr, material_intervals
from precis.cad.graph import Design
from precis.cad.primitives import Placed
from precis.cad.vec import as_vec3
from precis.errors import BadInput
from precis.utils.units import format_quantity
from precis_flow import Domain, Ports

#: A gap's thin axis is unique only when both lateral extents clear
#: this multiple of the gap (a square tube or a cube has no thin axis;
#: Q3 keeps 3-D chambers out of the kernel).
THIN_MIN_RATIO = 2.0

#: A port patch claims inside cells this many cells from its lateral
#: ribbon (a side-face patch is a ~zero-width ribbon on the boundary;
#: the claim must reach the nearest inside column — cfdtest's ports are
#: the first ~1.5 cells of inside within the port band).
PORT_SNAP_DX = 1.75

#: Default lateral cell count along x* (the domain spans ``x*∈[0,1]``,
#: so this also sets the reduced cell size ``dx* = 1/nx``).
DEFAULT_NX = 200

#: Evenness histogram bins over a region (cfdtest used 12×4 over its
#: central rectangle; here the region is the caller's own footprint).
EVENNESS_BINS = (12, 4)

AXES = ("x", "y", "z")


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


_PATCH_VECTORS = ("point", "normal", "u", "v")


@dataclass(frozen=True)
class PortPatch:
    """A port opening: a rectangle in the solid's local frame (metres),
    resolved from a ``patch:`` selector or built directly."""

    point: np.ndarray  # (3,) rectangle centre
    normal: np.ndarray  # (3,) unit outward normal of the carrying face
    u: np.ndarray  # (3,) unit in-plane axis
    v: np.ndarray  # (3,) unit in-plane axis
    half_u: float  # half extent along u
    half_v: float  # half extent along v
    selector: str = ""  # the patch: text it came from (diagnostics)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (the manifest's spec carries ports this way)."""
        out: dict[str, Any] = {
            k: [float(c) for c in getattr(self, k)] for k in _PATCH_VECTORS
        }
        out["half_u"] = float(self.half_u)
        out["half_v"] = float(self.half_v)
        if self.selector:
            out["selector"] = self.selector
        return out

    @classmethod
    def coerce(cls, value: Any, what: str) -> PortPatch:
        """A :class:`PortPatch` from itself or its :meth:`to_dict` form;
        ``BadInput`` names the missing or malformed key."""
        if isinstance(value, cls):
            return value
        keys = "point, normal, u, v (3-vectors), half_u, half_v (m)"
        if not isinstance(value, dict):
            raise BadInput(
                f"{what} is a PortPatch or a dict with {keys}, "
                f"got {type(value).__name__}"
            )
        missing = [k for k in (*_PATCH_VECTORS, "half_u", "half_v") if k not in value]
        if missing:
            raise BadInput(f"{what} misses {', '.join(missing)} — needs {keys}")
        vecs: dict[str, np.ndarray] = {}
        for k in _PATCH_VECTORS:
            arr = np.asarray(value[k], dtype=float)
            if arr.shape != (3,) or not np.all(np.isfinite(arr)):
                raise BadInput(
                    f"{what}.{k} must be a finite 3-vector, got {value[k]!r}"
                )
            vecs[k] = arr
        halves = []
        for k in ("half_u", "half_v"):
            h = value[k]
            if isinstance(h, bool) or not isinstance(h, int | float) or h < 0:
                raise BadInput(f"{what}.{k} must be a length ≥ 0 in m, got {h!r}")
            halves.append(float(h))
        return cls(
            point=vecs["point"],
            normal=vecs["normal"],
            u=vecs["u"],
            v=vecs["v"],
            half_u=halves[0],
            half_v=halves[1],
            selector=str(value.get("selector") or ""),
        )


@dataclass(frozen=True)
class GapDomain:
    """A built thin gap: the :class:`Domain` plus the geometry
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


def gap_cells(solid: Any, nx: int = DEFAULT_NX) -> tuple[int, int]:
    """``(nx, ny)`` the domain of ``solid`` would have at ``nx`` — the
    budget check reads it before any ray is cast."""
    s = _wrap_solid(solid)
    extents = np.asarray(s.hi - s.lo, dtype=float)
    if not np.all(np.isfinite(extents)):
        return int(nx), 0
    lateral = sorted((float(extents[a]) for a in range(3)), reverse=True)[:2]
    L, W = lateral
    return int(nx), max(1, round(int(nx) * W / L)) if L > 0 else 0


def domain_from_solid(
    solid: Any,
    inlet: PortPatch,
    outlet: PortPatch,
    *,
    nx: int = DEFAULT_NX,
    z_plane_star: float = 0.99,
) -> GapDomain:
    """Build the reduced :class:`Domain` (plus geometry provenance) from
    a ray-queryable solid and its two port rectangles.

    ``solid`` is a ``Placed`` envelope or a ``(Design, Expr)`` cad solid;
    all coordinates are the solid's own frame. ``inlet``/``outlet`` are
    :class:`PortPatch` rectangles in that frame (``precis_se.flow.
    patch_port`` resolves them from ``patch:`` selectors)."""
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
            f"{AXES[a]} {format_quantity(float(extents[a]), 'length')}"
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
    inlet_m = _claim_port(mask, inlet, (xa, ya), s.lo, L)
    outlet_m = _claim_port(mask, outlet, (xa, ya), s.lo, L)
    for name, port in (("inlet", inlet_m), ("outlet", outlet_m)):
        if not port.any():
            raise BadInput(
                f"the {name} port claims no inside cells — a solve needs "
                "each port to land on the footprint",
                next="place the port rectangle so its lateral projection "
                f"reaches inside cells (a side-face patch claims the nearest "
                f"inside column within {PORT_SNAP_DX:g} cells)",
            )
    if (inlet_m & outlet_m).any():
        raise BadInput(
            "the inlet and outlet ports claim the same cells — a solve "
            "needs two disjoint ports",
            next="move the two port rectangles apart; they may be at most "
            "a few cells wide each",
        )
    domain = Domain(
        mask=mask,
        h_star=h_star,
        ports=Ports(inlet=inlet_m, outlet=outlet_m),
        z_plane_star=z_plane_star,
    )
    return GapDomain(
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


def region_ranges(
    gap: GapDomain, patch: PortPatch
) -> tuple[tuple[float, float], tuple[float, float]] | str:
    """A region rectangle's lateral footprint in reduced coords —
    ``(x_range, y_range)`` — or a reason string when it projects to a
    degenerate region (a side-face patch has no lateral area)."""
    xa, ya = gap.x_axis, gap.y_axis
    lo = gap.lo
    # axis-aligned bbox of the rotated rectangle: centre ± |hu·u_a| + |hv·v_a|
    ex = abs(patch.half_u * patch.u[xa]) + abs(patch.half_v * patch.v[xa])
    ey = abs(patch.half_u * patch.u[ya]) + abs(patch.half_v * patch.v[ya])
    x_range = (
        float((patch.point[xa] - ex - lo[xa]) / gap.L),
        float((patch.point[xa] + ex - lo[xa]) / gap.L),
    )
    y_range = (
        float((patch.point[ya] - ey - lo[ya]) / gap.L),
        float((patch.point[ya] + ey - lo[ya]) / gap.L),
    )
    if x_range[1] - x_range[0] <= 0 or y_range[1] - y_range[0] <= 0:
        label = f"datum {patch.selector!r}" if patch.selector else "the region"
        return (
            f"{label} projects to a degenerate lateral region — "
            "evenness needs a patch on a floor/lid face"
        )
    return x_range, y_range


__all__ = [
    "AXES",
    "DEFAULT_NX",
    "EVENNESS_BINS",
    "PORT_SNAP_DX",
    "THIN_MIN_RATIO",
    "GapDomain",
    "PortPatch",
    "domain_from_solid",
    "gap_cells",
    "region_ranges",
]
