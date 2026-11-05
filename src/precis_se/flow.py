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
from dataclasses import dataclass, field
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
from precis_flow.physics import PHYSICS, keys
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
from precis_se.flow_registry import subscribe_computers
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
    — ``in``/``out`` are ``patch:`` or ``rect:`` selectors tagging the
    ports, ``dv``
    the reduced vertical dispersion over deepened floor (required — the
    kernel never defaults it), ``profile`` the horizontal velocity
    profile (Q4; the se default is ``parabolic``), ``zplane`` the
    tracking plane in reduced height (default 0.99), ``fluid`` a
    ``kind='material'`` slug the fluid's μ/ρ/D are read from
    (se-multiphysics.md slice 1), ``species`` the transported species
    names (measure-name scope ``<block>.<species>/<prop>``). The
    workspace-import terms (se-flow-kernel 4a) carry the script's own
    solve parameters so the import re-check repeats it exactly:
    ``cells`` (kernel nx; 0 = :data:`DEFAULT_NX`), ``seed`` (tracer
    seed), ``n`` (particles; 0 = the kernel default),
    ``seed_z=<lo>,<hi>`` (the seed band's gap fractions)."""

    inlet: str
    outlet: str
    d_v_star: float
    profile: str = "parabolic"
    zplane: float = 0.99
    fluid: str | None = None
    species: tuple[str, ...] = ()
    cells: int = 0
    seed: int = 0
    n: int = 0
    seed_z: tuple[float, float] | None = None
    # New physics retain their raw token inputs without growing this dataclass.
    physics_inputs: dict[str, str] = field(default_factory=dict, hash=False, repr=False)
    declared_keys: frozenset[str] = field(
        default_factory=frozenset, compare=False, repr=False
    )


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
    stray = sorted(set(kv) - set(keys("token_keys")))
    if stray:
        # Keep the established diagnostic order across registry groups.
        token_keys = list(keys("token_keys"))
        token_keys.remove("species")
        token_keys.insert(token_keys.index("fluid") + 1, "species")
        raise ValueError(
            f"flow chamber spec: unknown key(s) {', '.join(stray)} — "
            f"keys are {', '.join(token_keys)}"
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
    ints: dict[str, int] = {}
    for key in ("cells", "seed", "n"):
        if key not in kv:
            continue
        try:
            ints[key] = int(kv[key])
        except ValueError:
            raise ValueError(
                f"flow chamber spec: {key} must be an integer, got {kv[key]!r}"
            ) from None
        if ints[key] < 0 or (
            key == "cells" and ints[key] not in (0,) and ints[key] < 8
        ):
            raise ValueError(
                f"flow chamber spec: {key} must be "
                f"{'0 (the default) or ≥ 8' if key == 'cells' else '≥ 0'}, "
                f"got {ints[key]}"
            )
    seed_z: tuple[float, float] | None = None
    if "seed_z" in kv:
        try:
            lo_s, hi_s = kv["seed_z"].split(",")
            seed_z = (float(lo_s), float(hi_s))
        except ValueError:
            raise ValueError(
                f"flow chamber spec: seed_z must be <lo>,<hi> gap fractions, "
                f"got {kv['seed_z']!r}"
            ) from None
        if not (0.0 <= seed_z[0] <= seed_z[1] <= 1.0):
            raise ValueError(
                f"flow chamber spec: seed_z must be ordered fractions in "
                f"[0, 1], got {kv['seed_z']!r}"
            )
    return FlowSpec(
        inlet=kv["in"],
        outlet=kv["out"],
        d_v_star=dv,
        profile=profile,
        zplane=zplane,
        fluid=fluid,
        species=species,
        cells=ints.get("cells", 0),
        seed=ints.get("seed", 0),
        n=ints.get("n", 0),
        seed_z=seed_z,
        physics_inputs={
            k: v
            for k, v in kv.items()
            if k not in PHYSICS["flow"].token_keys and k != "species"
        },
        declared_keys=frozenset(kv),
    )


def _check_tree_physics(
    tree: Any,
    block: str,
    args: dict[str, Any] | None = None,
    spec: FlowSpec | None = None,
) -> None:
    """Inputs on the chamber activate physics regardless of requested output.

    Scan all its flow pockets as well as input measures: checking only the
    requested measure's pocket would silently omit another declared physics.
    Raw token keys also catch unsupported extensions on a lumen token before
    its narrower parser rejects them.
    """
    node = tree.blocks.get(block) if tree is not None else None
    tokens: set[str] = set()
    lumen = False
    for pocket in (getattr(node, "pockets", {}) or {}).values():
        shape = (pocket.shape or "").strip()
        if shape.startswith("flow:"):
            parts = shape[len("flow:") :].split(";")
            lumen = lumen or parts[0] == "lumen"
            tokens.update(p.partition("=")[0].strip() for p in parts if "=" in p)
    if spec is not None:
        tokens.update(spec.declared_keys)
        tokens.update(spec.physics_inputs)
        if spec.species:
            tokens.add("species")
    measures: set[str] = set()
    for measure in tree.measures if tree is not None else ():
        if measure.block == block:
            measures.add(measure.name.rsplit(SCOPE_SEP, 1)[-1])
            measures.update(
                value for value in (measure.measurand, measure.measurand_live) if value
            )
    geom_physics.check_flow_physics(
        token_keys=tokens | set(args or {}),
        spec_keys=args or {},
        input_measures=measures,
        lumen=lumen,
    )


def _port_on_own_envelope(chamber: str, ports: tuple[str, ...]) -> bool:
    """Does a port sit on a face of the chamber block's own envelope? A
    ``patch:`` names an envelope face; a ``rect:`` is plain block-local
    coordinates and a ``patch:`` on another block re-poses into them."""
    from precis_se import datums as se_datums
    from precis_se.measures import MeasureError

    for text in ports:
        try:
            sel = se_datums.parse_selector(text)
        except MeasureError:
            continue  # patch_port refuses it, naming the selector
        if sel.kind == "patch" and (sel.instance or chamber) == chamber:
            return True
    return False


def _chamber_placed(
    store: Any, tree: Any, name: str, ports: tuple[str, ...] = ()
) -> Any:
    """The solid a chamber solves on, the one its ``ports`` were resolved
    against. The block's envelope as a world-posed primitive — the
    ``se_datums._placed`` path with a ``field_loader`` so ``field:<sha>``
    envelopes resolve through the store. With no store (the flow
    COMPUTERS' store-free signature) the loader wired onto the loaded
    tree by :mod:`precis_se.persist` answers instead (gr503066); a bare
    tree leaves a ``field:`` envelope unbuilt. A ``cad``-bound block
    resolves through its bound design — that solid carries the floor
    topography, while a workspace macro import's envelope is only a
    bounding sphere (se-flow-kernel 4a) — unless a port is a ``patch:``
    on the block's own envelope: that port's rectangle exists only on
    the envelope, so the solve stays on the envelope too, whatever the
    bound design looks like."""
    from precis_se.ops import effective_envelope

    node = tree.blocks.get(name)
    if node is None:
        return None
    if (
        node.bound_kind == "cad"
        and node.bound
        and not _port_on_own_envelope(name, ports)
    ):
        resolver = getattr(tree, "cad_designs", None)
        got = resolver(node.bound) if resolver is not None else None
        if got is not None:
            design, _meta = got
            try:
                return design, design.whole()
            except ValueError:
                pass  # no components — fall through to the envelope path
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
    block's face (a port plate), re-posed through the two poses. A
    ``rect:`` selector is already chamber-local — a workspace import
    writes it, since a macro block's envelope carries no named face
    (se-flow-kernel 4a)."""
    from precis_se import datums as se_datums
    from precis_se.measures import MeasureError

    try:
        sel = se_datums.parse_selector(selector)
    except MeasureError as exc:
        raise BadInput(
            f"flow port: {exc}",
            next="a flow port is a 'patch:' or 'rect:' selector",
        ) from exc
    if sel.kind == "rect":
        assert sel.rect is not None
        point, normal, u, v, half_u, half_v = sel.rect
        return PortPatch(
            point=np.asarray(point, dtype=float),
            normal=np.asarray(normal, dtype=float),
            u=np.asarray(u, dtype=float),
            v=np.asarray(v, dtype=float),
            half_u=half_u,
            half_v=half_v,
            selector=selector,
        )
    if sel.kind != "patch":
        raise BadInput(
            f"a flow port must be a 'patch:<instance>.<tag>@<u>,<v>+<w>x<h>' "
            f"or 'rect:' selector, got {selector!r}"
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
    _check_tree_physics(tree, block, args)
    inlet_sel = args.get("inlet")
    outlet_sel = args.get("outlet")
    if not inlet_sel or not outlet_sel:
        raise BadInput(
            "view='flow' needs args={'inlet': <patch>, 'outlet': <patch>} — "
            "patch: selectors tagging the two ports"
        )
    placed = _chamber_placed(store, tree, block, (str(inlet_sel), str(outlet_sel)))
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
    geom_physics.check_flow_physics(token_keys=args, spec_keys=args)
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
        _check_tree_physics(tree, block, args)
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


def flow_outcome(
    solid: Any, tree: Any, block: str, spec: FlowSpec, region: str | None = None
) -> geom_physics.Outcome:
    """:func:`precis_geom.physics.evaluate` for a parsed ``flow:`` token
    on ``solid`` — the script's implementation at the token's own
    ``seed``/``cells``/``n``/``seed_z``. ``region`` is the selector
    evenness scores over; it changes no other measurand. The measure
    computers and the import re-check both call this, so they repeat
    one solve the same way."""
    _check_tree_physics(tree, block, spec=spec)
    case: dict[str, Any] = {
        "inlet": patch_port(tree, block, spec.inlet),
        "outlet": patch_port(tree, block, spec.outlet),
        "dv": spec.d_v_star,
        "profile": spec.profile,
        "zplane": spec.zplane,
        "evenness_region": patch_port(tree, block, region) if region else None,
    }
    if spec.n:
        case["n"] = spec.n
    if spec.seed_z is not None:
        case["seed_z"] = list(spec.seed_z)
    return geom_physics.evaluate(
        solid, ["flow"], case, seed=spec.seed, cells=spec.cells or DEFAULT_NX
    )


def _flow_measure(
    tree: Any,
    measure: Any,
    slug: str,
    memo: dict[Any, Any] | None = None,
) -> tuple[float | None, str | None]:
    """One flow measurand for a measure on a ``flow:`` pocket's region:
    :func:`flow_outcome` at the token's solve parameters; evenness scores
    over the measure's own datum region. A caller-owned ``memo`` shares
    outcomes and failures within a pass, never across geometry edits."""
    from precis_se import datums as se_datums

    node = tree.blocks.get(measure.block)
    if node is None:
        return None, f"measure names missing block {measure.block!r}"
    got = _flow_pocket(node, measure.datum or "")
    if isinstance(got, str):
        return None, got
    _pocket, spec = got
    if isinstance(spec, LumenSpec):
        try:
            _check_tree_physics(tree, measure.block)
        except BadInput as exc:
            return None, str(exc)
        return _lumen_measure(tree, node, spec, slug)
    placed = _chamber_placed(None, tree, measure.block, (spec.inlet, spec.outlet))
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
        if sel.kind not in ("patch", "rect"):
            return None, f"evenness scores over a patch: region, got {datum!r}"
    try:
        key = (measure.block, spec)
        if memo is None:
            out = flow_outcome(
                placed,
                tree,
                measure.block,
                spec,
                datum if slug == "evenness" else None,
            )
        else:
            if key not in memo:
                try:
                    memo[key] = flow_outcome(placed, tree, measure.block, spec)
                except (
                    Exception
                ) as exc:  # DRC reports failures without retrying each measure
                    memo[key] = f"{type(exc).__name__}: {exc}"
            out = memo[key]
            if isinstance(out, str):
                return None, out
            if slug == "evenness":
                ranges = geom_physics.region_ranges(
                    out.gap, patch_port(tree, measure.block, datum)
                )
                if isinstance(ranges, str):
                    return None, ranges
                nu, kept = nonuniformity(
                    out.paths.crossing_points(), ranges[0], ranges[1], EVENNESS_BINS
                )
                if not kept.shape[0]:
                    return (
                        None,
                        "no particle crossed the tracking plane inside the evenness region — no evenness",
                    )
                return float(1.0 - nu), None
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


def _registered_computer(module: str, function: str) -> Any:
    """Load a declared computer and apply the shared combination gate."""
    from importlib import import_module

    from precis_se.properties.computers import _Computer

    def compute(tree: Any, measure: Any) -> tuple[float | None, str | None]:
        try:
            _check_tree_physics(tree, measure.block)
        except BadInput as exc:
            return None, str(exc)
        return getattr(import_module(module), function)(tree, measure)

    return _Computer(compute)


# Slugs are declared without importing these implementations; importing species
# here would make direct imports of flow_species circular.
FLOW_COMPUTERS: dict[str, Any] = {}


def _register_flow_computers(module: str, computers: dict[str, str]) -> None:
    FLOW_COMPUTERS.update(
        {
            slug: _registered_computer(module, function)
            for slug, function in computers.items()
        }
    )


subscribe_computers(_register_flow_computers)
