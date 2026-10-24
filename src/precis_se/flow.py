"""se → precis_flow adapter — the slice-2 half of
docs/backlog/se-flow-kernel.md.

A thin chamber arrives as a block's posed cad **envelope** (or, for the
kernel-facing entry point, any ray-queryable solid — a ``Placed``
primitive or a ``(Design, Expr)`` pair, which is how a ``cad`` kind
solid's ``body add`` unions carry floor topography). The DB-free half —
thin axis, mask, depth map, port claiming (:func:`domain_from_solid`,
:class:`PortPatch`, :class:`GapDomain`) — lives in
:mod:`precis_geom.flow` (se-flow-kernel slice 4a) so a workspace script
runs the same code; ``Chamber`` here is an alias of ``GapDomain`` for
one release. This module keeps what needs the tree: ``patch:``
selectors resolved through :mod:`precis_se.datums` (``_face_frame``
gives the face's local rectangle; a patch on *any* block is re-posed
into the chamber's local frame) in :func:`patch_port`.

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
footprint). The chamber computers call
:func:`precis_geom.physics.evaluate` — the script's stated number and
the server's re-check are one implementation.
"""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.cad.primitives import Placed
from precis.cad.vec import as_vec3
from precis.cad.vec import pose as cad_pose
from precis.errors import BadInput
from precis.utils.units import format_quantity
from precis_flow import (
    AtomWall,
    CylinderLumen,
    Field,
    Paths,
    RegimeCard,
    Seed,
    nonuniformity,
    propagate,
    solve,
    trace,
)
from precis_flow.regime import KN_CONTINUUM_MAX, KN_FREE_MIN
from precis_geom import physics as geom_physics
from precis_geom.flow import AXES as _AXES
from precis_geom.flow import (
    DEFAULT_NX,
    EVENNESS_BINS,
    GapDomain,
    PortPatch,
    domain_from_solid,
)
from precis_geom.physics import card_dict as _card_dict
from precis_se.measures import SCOPE_SEP

#: ``Chamber`` is the pre-4a name of :class:`precis_geom.flow.GapDomain`
#: — kept for one release (se-flow-kernel slice 4a).
Chamber = GapDomain

#: The slice-2b player payload stays under this byte budget; over it the
#: payload subsamples particles first, then frames (it reports the
#: strides it took).
PAYLOAD_CAP_BYTES = 20_000_000


@dataclass(frozen=True)
class FlowSpec:
    """A chamber spec's parsed ``flow:`` pocket-shape token:

    ``flow:in=<patch>;out=<patch>;dv=<d_v*>[;profile=plug|parabolic]
    [;zplane=<z*>][;fluid=<material slug>][;species=<name>[,<name>…]]``
    — ``in``/``out`` are ``patch:`` selectors tagging the ports, ``dv``
    the reduced vertical dispersion over deepened floor (required — the
    kernel never defaults it), ``profile`` the horizontal velocity
    profile (Q4; the se default is ``parabolic``), ``zplane`` the
    tracking plane in reduced height (default 0.99), ``fluid`` a
    ``kind='material'`` slug the fluid's μ/ρ/D are read from
    (se-multiphysics.md slice 1), ``species`` the transported species
    names (measure-name scope ``<block>.<species>/<prop>``)."""

    inlet: str
    outlet: str
    d_v_star: float
    profile: str = "parabolic"
    zplane: float = 0.99
    fluid: str | None = None
    species: tuple[str, ...] = ()


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
        "'flow:in=<patch>;out=<patch>;dv=<d_v*>[;profile=plug|parabolic]"
        "[;zplane=<z*>][;fluid=<material>][;species=<name>[,<name>…]]'"
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
    stray = sorted(
        set(kv) - {"in", "out", "dv", "profile", "zplane", "fluid", "species"}
    )
    if stray:
        raise ValueError(
            f"flow chamber spec: unknown key(s) {', '.join(stray)} — "
            "keys are in, out, dv, profile, zplane, fluid, species"
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
    fluid = kv.get("fluid") or None
    species = tuple(
        s.strip() for s in (kv.get("species") or "").split(",") if s.strip()
    )
    bad_sp = [s for s in species if "." in s or SCOPE_SEP in s]
    if bad_sp:
        raise ValueError(
            f"flow chamber spec: species names must not contain '.' or "
            f"{SCOPE_SEP!r} (measure scope is '<species>{SCOPE_SEP}<prop>'), "
            f"got {', '.join(bad_sp)}"
        )
    return FlowSpec(
        inlet=kv["in"],
        outlet=kv["out"],
        d_v_star=dv,
        profile=profile,
        zplane=zplane,
        fluid=fluid,
        species=species,
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


def _lumen_measure(
    tree: Any, node: Any, spec: LumenSpec, slug: str
) -> tuple[float | None, str | None]:
    """A flow measurand on a lumen pocket: wall + propagate (slice 3)."""
    try:
        wall = lumen_from_scene(_bound_scene(None, tree, node))
        paths = propagate(wall, 400, spec.alpha, 0)
    except BadInput as exc:
        return None, str(exc)
    if slug == "breakthrough-fraction":
        return float(paths.breakthrough), None
    if slug == "residence-time-t0":
        res = paths.residence[np.isfinite(paths.residence)]
        if not res.size:
            return None, "no particles resolved — no residence statistic"
        return float(res.mean()), None
    if slug == "resistance-ratio":
        # free-molecular resistance ∝ 1/W — the corrugated lumen against
        # the smooth cylinder at R_eff = R_axis − r_vdW, same seed
        if paths.breakthrough <= 0.0:
            return None, "the lumen transmits nothing — no resistance ratio"
        ref = propagate(CylinderLumen(wall.r_eff), 400, spec.alpha, 0)
        if ref.breakthrough <= 0.0:
            return None, "the smooth-cylinder reference transmits nothing"
        return float(ref.breakthrough / paths.breakthrough), None
    # evenness: the lumen's region is its outlet aperture — exit points
    # mapped to equal-area polar bins (u = r²/R_eff², v = θ/2π over the
    # disk — a uniform exit spread reads uniform there)
    pts = paths.crossing_points()
    if pts.shape[0] == 0:
        return None, "no transmitted particles — no evenness"
    z = pts[:, 1] * 2.0 * wall.r_eff - wall.r_eff
    y = pts[:, 0]
    u = np.clip((y * y + z * z) / wall.r_eff**2, 0.0, 1.0)
    v = (np.arctan2(z, y) + np.pi) / (2.0 * np.pi)
    nu, _ = nonuniformity(
        np.column_stack([u, v]), (0.0, 1.0), (0.0, 1.0), EVENNESS_BINS
    )
    return float(1.0 - nu), None


def _flow_measure(
    tree: Any, measure: Any, slug: str
) -> tuple[float | None, str | None]:
    """One flow measurand for a measure on a ``flow:`` pocket's region.
    A chamber runs :func:`precis_geom.physics.evaluate` (the script's
    implementation, ``seed=0``, ``cells=DEFAULT_NX``); evenness scores
    over the measure's own datum patch."""
    from precis_se import datums as se_datums

    node = tree.blocks.get(measure.block)
    if node is None:
        return None, f"measure names missing block {measure.block!r}"
    got = _flow_pocket(node, measure.datum or "")
    if isinstance(got, str):
        return None, got
    _pocket, spec = got
    if isinstance(spec, LumenSpec):
        return _lumen_measure(tree, node, spec, slug)
    placed = _chamber_placed(None, tree, measure.block)
    if placed is None:
        return None, (
            f"block {measure.block!r} has no parseable envelope — the "
            "chamber needs a cad envelope"
        )
    datum = measure.datum or ""
    if slug == "evenness":
        try:
            sel = se_datums.parse_selector(datum)
        except Exception:
            return None, f"datum {datum!r} does not parse as a patch region"
        if sel.kind != "patch":
            return None, f"evenness scores over a patch: region, got {datum!r}"
    try:
        case = {
            "inlet": patch_port(tree, measure.block, spec.inlet),
            "outlet": patch_port(tree, measure.block, spec.outlet),
            "dv": spec.d_v_star,
            "profile": spec.profile,
            "zplane": spec.zplane,
            "evenness_region": (
                patch_port(tree, measure.block, datum) if slug == "evenness" else None
            ),
        }
        out = geom_physics.evaluate(placed, ["flow"], case, seed=0, cells=DEFAULT_NX)
    except (BadInput, ValueError) as exc:
        return None, str(exc)
    if slug in out.measures:
        return out.measures[slug], None
    return None, out.reasons.get(slug, f"evaluate returned no {slug}")


def _flow_breakthrough(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    return _flow_measure(tree, measure, "breakthrough-fraction")


def _flow_residence(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    return _flow_measure(tree, measure, "residence-time-t0")


def _flow_resistance_ratio(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    return _flow_measure(tree, measure, "resistance-ratio")


def _flow_evenness(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    """``1 - nonuniformity`` of first-crossing points over the datum
    patch's lateral footprint (1 = perfectly even, 0 = all in one bin)."""
    return _flow_measure(tree, measure, "evenness")


# ── species BCs and the slice-1 species computers ─────────────────────
#
# (docs/backlog/se-multiphysics.md slice 1) Boundary conditions are
# patch measures — a measure whose ``datum`` is a ``patch:`` selector on
# the chamber block, scoped to one species by its name
# ``<species>/<prop>`` (the qualified ``<block>.<species>/<prop>`` of the
# spec's Q2); a bare ``<prop>`` input measure applies to every species.
# Input measurands: ``inlet-concentration`` (mol/m³, informational while
# the equations are linear), ``wall-rate-constant`` (m/s, floor/lid
# patches), ``species-diffusivity`` (m²/s — falls back to the fluid
# material's diffusivity), ``inlet-velocity`` (m/s) and ``pressure-drop``
# (Pa, needs fluid μ → u0 = h0²dp/(12μL)). A missing input is a reason
# string, never a silent number.

_SPECIES_INPUT_PROPS = (
    "inlet-concentration",
    "inlet-velocity",
    "pressure-drop",
    "species-diffusivity",
    "wall-rate-constant",
)

#: Developed Sherwood pair for parallel plates (spec table) — used for
#: the entrance-region flag and the k_s/k_m line.
_SH_WALLS = {1: 4.86, 2: 7.54}


def _scoped_prop(name: str, species: tuple[str, ...]) -> tuple[str | None, str]:
    """Split a measure name into ``(species, prop)``: ``co/wall-rate-constant``
    → ``('co', 'wall-rate-constant')``; a bare ``wall-rate-constant`` →
    ``(None, 'wall-rate-constant')``. Only the declared species names
    split — ``bt`` stays a bare name. The separator is
    :data:`~precis_se.measures.SCOPE_SEP` (``/``): ``.`` is the
    relation-source ``'block.measure'`` split and never in a name."""
    for sp in species:
        if name.startswith(f"{sp}{SCOPE_SEP}"):
            return sp, name[len(sp) + len(SCOPE_SEP) :]
    if SCOPE_SEP in name:
        head, _, rest = name.partition(SCOPE_SEP)
        if head and head not in species:
            return head, rest
    return None, name


def _bc_measures(
    tree: Any, block: str, spec: FlowSpec
) -> tuple[dict[tuple[str | None, str], Any], list[tuple[str | None, Any]]]:
    """``(species|None, prop) → measure`` for the chamber block's
    species-input patch measures, plus the ``wall-rate-constant`` list —
    scalar props collapse to one value per species (later rows win), but
    each reactive patch is its own measure, so the wall rates collect
    (per-catalyst-patch selectivity needs every one)."""
    out: dict[tuple[str | None, str], Any] = {}
    walls: list[tuple[str | None, Any]] = []
    for m in tree.measures:
        if m.block != block or m.value is None:
            continue
        sp, prop = _scoped_prop(m.name, spec.species)
        # A wall-rate measure is named '<species>/wall-rate-constant' —
        # or carries the measurand outright when the op layer had to
        # suffix the name for a second reactive patch.
        if prop == "wall-rate-constant" or (
            (m.measurand or m.measurand_live) == "wall-rate-constant"
        ):
            walls.append((sp, m))
        elif prop in _SPECIES_INPUT_PROPS:
            out[(sp, prop)] = m
    return out, walls


def _bc_value(
    bcs: dict[tuple[str | None, str], Any], species: str, prop: str
) -> tuple[float | None, Any | None]:
    """A species-scoped BC value, falling back to the unscoped measure."""
    m = bcs.get((species, prop)) or bcs.get((None, prop))
    return (m.value if m is not None else None), m


def _fluid_props(tree: Any, spec: FlowSpec) -> dict[str, float] | None:
    """The fluid material's property map (slug → ``{prop_id: value}``)
    through the tree's store-bound ``materials`` resolver — ``None`` on a
    bare tree or when the spec names no fluid."""
    if spec.fluid is None or getattr(tree, "materials", None) is None:
        return None
    return tree.materials(spec.fluid)


def _fluid_value(props: dict[str, float] | None, *ids: str) -> float | None:
    if not props:
        return None
    for pid in ids:
        v = props.get(pid)
        if isinstance(v, int | float) and v > 0:
            return float(v)
    return None


def _patch_side(chamber: Chamber, patch: PortPatch) -> str | None:
    """``'floor'``/``'lid'`` for a thin-axis face patch; ``None`` for a
    lateral (side) patch — a ``wall-rate-constant`` on a side patch has
    no wall to bind to."""
    if abs(float(patch.normal[chamber.thin_axis])) < 0.9:
        return None
    return (
        "floor"
        if (patch.point[chamber.thin_axis] > chamber.z_ceiling) == chamber.z_flip
        else "lid"
    )


def _patch_cells(chamber: Chamber, patch: PortPatch) -> np.ndarray:
    """Inside cells whose centre sits inside the patch's lateral
    rectangle — the rasteriser for floor/lid ``wall-rate-constant``
    patches (unlike ``_claim_port`` it does not snap to the boundary)."""
    d = chamber.domain
    ny, nx = d.shape
    xa, ya = chamber.x_axis, chamber.y_axis
    cells = np.zeros((ny, nx), dtype=bool)
    pu, pv = float(patch.point[xa]), float(patch.point[ya])
    for j in range(ny):
        cy = chamber.lo[ya] + (j + 0.5) * chamber.L / nx
        for i in range(nx):
            if not d.mask[j, i]:
                continue
            cx = chamber.lo[xa] + (i + 0.5) * chamber.L / nx
            du = (cx - pu) * patch.u[xa] + (cy - pv) * patch.u[ya]
            dv = (cx - pu) * patch.v[xa] + (cy - pv) * patch.v[ya]
            if abs(du) <= patch.half_u + 1e-12 and abs(dv) <= patch.half_v + 1e-12:
                cells[j, i] = True
    return cells


def _patch_streamwise_m(chamber: Chamber, patch: PortPatch) -> float:
    """The patch's extent along the chamber's flow (x*) axis, metres —
    the entrance-region flag's ``0.2·Pe·h0`` comparison length."""
    xa = chamber.x_axis
    return float(
        2.0 * patch.half_u * abs(patch.u[xa]) + 2.0 * patch.half_v * abs(patch.v[xa])
    )


@dataclass(frozen=True)
class _WallCase:
    """One reactive-wall patch bound for the solve."""

    side: str  # 'floor' | 'lid'
    k_s: float  # m/s
    da_w: float  # k_s h0 / D
    cells: np.ndarray  # (ny, nx) bool
    measure_name: str  # the measure that declared it
    streamwise_m: float  # patch extent along x*, for the entrance flag


@dataclass(frozen=True)
class _SpeciesCase:
    """The physical inputs one species' solve needs, gathered from the
    ``flow:`` spec + patch measures + the fluid material."""

    name: str
    epsilon: float  # h0/L
    pe: float  # u0 h0/D
    d_m2s: float  # the diffusivity used (measure or material)
    u0: float  # m/s
    walls: tuple[_WallCase, ...]
    c_in: float | None  # declared inlet concentration (mol/m³), informational


def _species_case_for(
    tree: Any,
    block: str,
    chamber: Chamber,
    spec: FlowSpec,
    species: str,
) -> tuple[_SpeciesCase | None, str | None]:
    """Gather the species' physical inputs, or a reason string — a
    missing input is a reason, never a silent default."""
    fluid = _fluid_props(tree, spec)
    bcs, wall_measures = _bc_measures(tree, block, spec)

    d_m2s, _dm = _bc_value(bcs, species, "species-diffusivity")
    if d_m2s is None:
        d_m2s = _fluid_value(
            fluid, "diffusivity", "species_diffusivity", "diffusion_coefficient"
        )
    if d_m2s is None or d_m2s <= 0:
        return None, (
            f"no diffusivity for species {species!r} — declare a "
            f"'{species}/species-diffusivity' patch measure (m²/s) or give "
            "the fluid material a 'diffusivity' property"
        )

    u0, _um = _bc_value(bcs, species, "inlet-velocity")
    if u0 is None:
        dp, _pm = _bc_value(bcs, species, "pressure-drop")
        mu = _fluid_value(fluid, "viscosity", "dynamic_viscosity")
        if dp is not None and dp > 0 and mu is not None:
            u0 = chamber.h0 * chamber.h0 * dp / (12.0 * mu * chamber.L)
    if u0 is None or u0 <= 0:
        return None, (
            "no operating point — declare an 'inlet-velocity' (m/s) or "
            "'pressure-drop' (Pa) patch measure on the inlet"
        )

    c_in, _cm = _bc_value(bcs, species, "inlet-concentration")

    epsilon = chamber.h0 / chamber.L
    pe = u0 * chamber.h0 / d_m2s
    walls: list[_WallCase] = []
    reasons: list[str] = []
    for sp, m in wall_measures:
        if sp is not None and sp != species:
            continue
        datum = m.datum or ""
        try:
            patch = patch_port(tree, block, datum)
        except BadInput as exc:
            reasons.append(f"{m.name}: {exc}")
            continue
        side = _patch_side(chamber, patch)
        if side is None:
            reasons.append(
                f"{m.name}: datum {datum!r} is a lateral patch — "
                "wall-rate-constant binds to floor/lid patches only"
            )
            continue
        k_s = float(m.value or 0.0)
        if k_s < 0:
            reasons.append(f"{m.name}: wall-rate-constant must be ≥ 0")
            continue
        walls.append(
            _WallCase(
                side=side,
                k_s=k_s,
                da_w=k_s * chamber.h0 / d_m2s,
                cells=_patch_cells(chamber, patch),
                measure_name=m.name,
                streamwise_m=_patch_streamwise_m(chamber, patch),
            )
        )
    if reasons:
        return None, "; ".join(reasons)
    return _SpeciesCase(
        name=species,
        epsilon=epsilon,
        pe=pe,
        d_m2s=d_m2s,
        u0=u0,
        walls=tuple(walls),
        c_in=c_in,
    ), None


def _species_of(
    tree: Any, measure: Any
) -> tuple[Any | None, FlowSpec | None, str | None, str | None]:
    """``(node, spec, species, None)`` or a reason — the measure's
    ``<species>/<prop>`` scope, defaulting to a single declared species."""
    node = tree.blocks.get(measure.block)
    if node is None:
        return None, None, None, f"measure names missing block {measure.block!r}"
    got = _flow_pocket(node, measure.datum or "")
    if isinstance(got, str):
        return None, None, None, got
    _pocket, spec = got
    sp, _prop = _scoped_prop(measure.name, spec.species)
    if sp is None:
        if len(spec.species) == 1:
            sp = spec.species[0]
        elif not spec.species:
            return (
                None,
                spec,
                None,
                (
                    f"pocket shape declares no species — the {measure.name!r} "
                    "measure has nothing to transport"
                ),
            )
        else:
            return (
                None,
                spec,
                None,
                (
                    f"measure {measure.name!r} names no species — scope it as "
                    f"'{measure.block}.<species>/<prop>' (declared: "
                    f"{', '.join(spec.species)})"
                ),
            )
    elif spec.species and sp not in spec.species:
        return (
            None,
            spec,
            None,
            (
                f"species {sp!r} is not declared in the flow: token "
                f"(species={','.join(spec.species)})"
            ),
        )
    return node, spec, sp, None


def _species_run(
    tree: Any, measure: Any, *, n_particles: int = 4000, seed_rng: int = 0
) -> tuple[Any, Any, str | None]:
    """Chamber + species solve for a species-scoped measure — Eulerian
    when ``Pe·ε ≤ 0.1``, the Lagrangian tracer otherwise (the card's
    taylor route). Returns ``(ctx, result, reason)`` where ``ctx`` is
    ``(chamber, spec, case)`` and result a ``SpeciesResult`` or
    ``Paths``."""
    from precis_flow.species import ReactiveWall, transport

    node, spec, sp, err = _species_of(tree, measure)
    if err is not None or spec is None or sp is None:
        return None, None, err
    placed = _chamber_placed(None, tree, measure.block)
    if placed is None:
        return (
            None,
            None,
            (
                f"block {measure.block!r} has no parseable envelope — the "
                "chamber needs a cad envelope"
            ),
        )
    try:
        chamber = domain_from_solid(
            placed,
            patch_port(tree, measure.block, spec.inlet),
            patch_port(tree, measure.block, spec.outlet),
            z_plane_star=spec.zplane,
        )
    except (BadInput, ValueError) as exc:
        return None, None, str(exc)
    case, err = _species_case_for(tree, measure.block, chamber, spec, sp)
    if err is not None:
        return (chamber, spec, None), None, err
    assert case is not None
    field = solve(chamber.domain)
    if case.pe * case.epsilon <= 0.1:
        walls = [
            ReactiveWall(wall=w.side, da_w=w.da_w, cells=w.cells) for w in case.walls
        ]
        try:
            res = transport(
                field,
                epsilon=case.epsilon,
                pe=case.pe,
                walls=walls,
                limiter=True,
            )
        except ValueError as exc:
            return (chamber, spec, case), None, str(exc)
        return (chamber, spec, case), res, None
    # Lagrangian route: the same walls as per-cell Da_w fields.
    wall_fields: dict[str, np.ndarray] = {}
    for w in case.walls:
        arr = wall_fields.setdefault(
            w.side, np.zeros(chamber.domain.shape, dtype=float)
        )
        arr[w.cells] = w.da_w
    walls_arg: dict[str, float | np.ndarray] = dict(wall_fields)
    paths = trace(
        field,
        Seed.across_inlet(chamber.domain, z_lo=0.0, z_hi=1.0),
        n_particles,
        d_v_star=spec.d_v_star,
        seed_rng=seed_rng,
        profile="parabolic",
        pe=case.pe,
        epsilon=case.epsilon,
        walls=walls_arg,
        max_steps=20000,
    )
    return (chamber, spec, case), paths, None


def _wall_patch_index(
    case: _SpeciesCase, measure: Any, chamber: Chamber, tree: Any, block: str
) -> int | None:
    """Which of the case's walls the measure's datum patch names — by
    cell overlap (the measure's patch need not be the rate measure's
    exact patch)."""
    datum = measure.datum or ""
    try:
        patch = patch_port(tree, block, datum)
    except BadInput:
        return None
    cells = _patch_cells(chamber, patch)
    if not cells.any():
        return None
    side = _patch_side(chamber, patch)
    best: tuple[int, int] | None = None
    for i, w in enumerate(case.walls):
        overlap = int((w.cells & cells).sum())
        if overlap and (side is None or w.side == side):
            if best is None or overlap > best[1]:
                best = (i, overlap)
    return best[0] if best else None


def _entrance_reason(case: _SpeciesCase, w: _WallCase, chamber: Chamber) -> str | None:
    """The slice-1 entrance-region flag: a short reactive patch the
    developed-Sh coefficient under-reads."""
    n_side = sum(1 for x in case.walls if x.cells.any())
    sh = _SH_WALLS.get(n_side, _SH_WALLS[2])
    k_m = sh * case.d_m2s / (2.0 * chamber.h0)
    if w.streamwise_m < 0.2 * case.pe * chamber.h0 and k_m > 0 and w.k_s / k_m >= 0.1:
        return (
            f"{w.measure_name}: patch streamwise length "
            f"{w.streamwise_m:g} m < 0.2·Pe·h0 "
            f"({0.2 * case.pe * chamber.h0:g} m) with k_s/k_m "
            f"{w.k_s / k_m:.3g} ≥ 0.1 — the developed-Sh value under-reads "
            "the entrance region; the Lagrangian is the alternative "
            "(slice 1b)"
        )
    return None


def _flow_outlet_conversion(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    ctx, res, err = _species_run(tree, measure)
    if res is None:
        return None, err
    if isinstance(res, Paths):
        absorbed = res.absorbed
        if absorbed is None:
            return 0.0, None
        return float(absorbed.mean()), None
    return float(res.conversion), None


def _flow_outlet_concentration(
    tree: Any, measure: Any
) -> tuple[float | None, str | None]:
    ctx, res, err = _species_run(tree, measure)
    if res is None:
        return None, err
    if isinstance(res, Paths):
        absorbed = res.absorbed
        return float(1.0 - absorbed.mean()) if absorbed is not None else 1.0, None
    return float(res.outlet_mean_c), None


def _flow_wall_flux_fraction(
    tree: Any, measure: Any
) -> tuple[float | None, str | None]:
    """Fraction of the inlet species flux consumed on the measure's
    patch — per-catalyst-patch selectivity."""
    ctx, res, err = _species_run(tree, measure)
    if ctx is None or res is None:
        return None, err
    chamber, spec, case = ctx
    i = _wall_patch_index(case, measure, chamber, tree, measure.block)
    if i is None:
        return None, (
            f"datum {measure.datum!r} names no reactive patch of species "
            f"{case.name!r} — wall-flux-fraction scores a "
            "wall-rate-constant patch"
        )
    w = case.walls[i]
    if isinstance(res, Paths):
        absorbed = res.absorbed
        if absorbed is None or res.absorb_star is None:
            return 0.0, None
        dx = chamber.domain.dx_star
        ii = np.clip(
            np.nan_to_num(res.absorb_star[:, 0] / dx, nan=0.0).astype(int),
            0,
            chamber.domain.nx - 1,
        )
        jj = np.clip(
            np.nan_to_num(res.absorb_star[:, 1] / dx, nan=0.0).astype(int),
            0,
            chamber.domain.ny - 1,
        )
        in_patch = absorbed & np.isfinite(res.absorb_star[:, 0]) & w.cells[jj, ii]
        value = float(in_patch.sum() / res.n_particles)
    else:
        value = (
            float(res.per_wall_sink[i] / res.inlet_flux) if res.inlet_flux > 0 else 0.0
        )
    return value, _entrance_reason(case, w, chamber)


def _flow_damkohler(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    """``Da_I = Σ_w k_s,w·f_w·L/(h0·u0)`` — the transit Damköhler (gauge);
    a patch covers fraction ``f_w`` of the chamber's cells."""
    node, spec, sp, err = _species_of(tree, measure)
    if err is not None or spec is None or sp is None:
        return None, err
    placed = _chamber_placed(None, tree, measure.block)
    if placed is None:
        return None, (
            f"block {measure.block!r} has no parseable envelope — the "
            "chamber needs a cad envelope"
        )
    try:
        chamber = domain_from_solid(
            placed,
            patch_port(tree, measure.block, spec.inlet),
            patch_port(tree, measure.block, spec.outlet),
            z_plane_star=spec.zplane,
        )
    except (BadInput, ValueError) as exc:
        return None, str(exc)
    case, err = _species_case_for(tree, measure.block, chamber, spec, sp)
    if err is not None:
        return None, err
    assert case is not None
    if not case.walls:
        return None, (
            f"no 'wall-rate-constant' patch measure for species {case.name!r} "
            "— Da_I needs a reactive wall"
        )
    n_cells = float(chamber.domain.mask.sum())
    da_i = sum(
        w.k_s * (float(w.cells.sum()) / n_cells) * chamber.L / (chamber.h0 * case.u0)
        for w in case.walls
        if w.cells.any()
    )
    return float(da_i), None


from precis_se.properties.computers import _Computer

#: measurand slug → computer, registered in
#: :data:`precis_se.properties.COMPUTERS` — the four flow measurands of
#: se-flow-kernel.md slice 2 plus the four species measurands of
#: se-multiphysics.md slice 1.
FLOW_COMPUTERS: dict[str, Any] = {
    "breakthrough-fraction": _Computer(_flow_breakthrough),
    "residence-time-t0": _Computer(_flow_residence),
    "resistance-ratio": _Computer(_flow_resistance_ratio),
    "evenness": _Computer(_flow_evenness),
    "outlet-conversion": _Computer(_flow_outlet_conversion),
    "outlet-concentration": _Computer(_flow_outlet_concentration),
    "wall-flux-fraction": _Computer(_flow_wall_flux_fraction),
    "damkohler": _Computer(_flow_damkohler),
}
