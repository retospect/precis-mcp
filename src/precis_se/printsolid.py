"""Engine 1 — the printed solid (docs/backlog/se-print-implementer.md).

Answers the first of the three printing questions: *what solid gets
printed?* A block's L3 realization (its bound ``cad`` design), minus the
derived features other layers stamp into it (:mod:`precis_se.fasten`'s
holes, pockets, counterbores) — composed at read time, stored nowhere,
like every other derived thing in se. Engines 2/3 (orientation search,
process DRC/export) are later rungs of the same item and are not built
here.

**Eligibility** runs on ``bound_kind`` first, mirroring
:mod:`precis_se.fasten`'s own precedent (``Member.bought`` is
``bound_kind in ('component', 'part')``, and ``_stamp`` skips those):

- ``bound_kind in ('component', 'part')`` — bought, not printed.
- ``bound_kind == 'structure'`` — an atomic realization, not a solid.
- resolved mode family isn't ``fdm`` (unset mode → no family) — another
  implementer's block, or unassigned.
- ``bound_kind != 'cad'`` — the block never got a printable realization at
  all: an abstract requirements block is not an implementation.

Every ineligible case (and a dangling/malformed cad binding) returns
``None`` rather than a distinguishing sentinel — the caller
(``view='print'``, rung 4) already has to re-derive *why* from the tree
directly to render ``unrealized``/``planned, not checked``/etc, so a
second classification here would just be a second place to keep in sync.

**Feature cuts.** Every :class:`~precis_se.fasten.Hole` stamped into this
block becomes a subtractive primitive — a round hole a ``cyl:``, a hex nut
pocket a ``hex:`` sized by across-flats, a countersink a ``cone:`` — built
in **world frame** (a ``Hole``'s ``origin``/``axis`` already are, per
``fasten._drive_axis``) and then carried into the block's own local frame
by the inverse of the block's world pose, since the bound cad design's own
node coordinates are authored in that local frame (pose identity — the
build frame, Engine 2, is a later, separate transform applied only at
export time). A counterbore is not a distinct shape: it *is* the "second,
wider cyl" the module docstring above describes, already expressed as an
ordinary ``Hole`` with a bigger diameter and shallower depth than the
shank clearance hole underneath it — the two just happen to share an axis.

Every top-level component the tool's bounding box meets is cut — a
multi-part ``use``-based binding (one block bound to an assembly) gets
the hole in whichever part it lands in, not in the first part regardless
(gr344816). A hole whose tool meets no part, or meets one and still
removes nothing (it sits in a bounding-box corner's air), is a
``feature_not_cut`` finding: the cut is never allowed to succeed on air.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from precis.cad import bulk as cad_bulk
from precis.cad import dsl as cad_dsl
from precis.cad.fold import Expr
from precis.cad.graph import Design as CadDesign
from precis.cad.scene import (
    NodeSpec,
    SceneError,
    SceneSpec,
    build_design,
    expand_instances,
)
from precis.cad.vec import Transform, euler_rad_from_matrix
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import normalize as cad_normalize
from precis.cad.vec import pose as cad_pose
from precis.cad_resolve import design_resolver
from precis.utils.units import format_dsl_number
from precis_se import fasten as se_fasten
from precis_se import modes as se_modes
from precis_se.fasten import Hole
from precis_se.ops import SeTree
from precis_se.validate import ValidationIssue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

#: A printed part's bbox diagonal outside this band (metres) is a
#: ``mode_scale_mismatch`` finding — you do not FDM a 5 nm part, and a
#: 10+ m one is not a single print either. se-print-implementer.md's
#: numbers verbatim.
_MIN_BBOX_DIAG_M = 1e-4
_MAX_BBOX_DIAG_M = 10.0

#: ``sqrt(3)`` — across-flats (flat-to-flat) → circumradius for a regular
#: hexagon: ``circumradius = across_flats / sqrt(3)`` (apothem =
#: circumradius · cos(30°) = circumradius · sqrt(3)/2, and across_flats is
#: twice the apothem). Named so the conversion reads as geometry, not a
#: magic constant.
_HEX_ACROSS_FLATS_TO_CIRCUMRADIUS = 1.0 / math.sqrt(3.0)

#: Node names in a stamped :class:`~precis_se.fasten.Hole` carry
#: ``#``/``.``/``—`` (the connect-subject grammar) — harmless as a display
#: label, but not a token this module wants to promise is safe everywhere
#: a cad node name is used (e.g. re-parsed from stored text later). Kept
#: distinct from the Hole's own ``name`` (returned verbatim in
#: :attr:`PrintedSolid.features` — that's the identity a DRC finding names
#: "which connect made this feature" by).
_UNSAFE_NODE_CHARS = re.compile(r"[^A-Za-z0-9_]+")


def _safe_node_name(name: str) -> str:
    return _UNSAFE_NODE_CHARS.sub("_", name).strip("_") or "cut"


@dataclass
class PrintedSolid:
    """One block's printed solid — Engine 1's whole answer. ``design`` is
    the live, evaluable :class:`~precis.cad.graph.Design` (already cut);
    ``spec`` is the equivalent :class:`~precis.cad.scene.SceneSpec` — the
    base design's own nodes plus one ``cut`` :class:`~precis.cad.scene.
    NodeSpec` per applied feature, in the block's local frame — carried for
    a later rung's export, not consumed here. ``features`` names every
    :class:`~precis_se.fasten.Hole` actually cut (its own ``.name``, the
    connect-derived identity), in application order."""

    design: CadDesign
    spec: SceneSpec
    features: list[str]
    volume_before_m3: float
    volume_after_m3: float
    findings: list[ValidationIssue] = field(default_factory=list)
    block: str = ""
    mode: str = ""


def _feature_not_cut(block_name: str, hole: Hole, why: str) -> ValidationIssue:
    """The error-severity receipt for a stamped hole the solid does not
    carry — the file must not leave looking complete."""
    return ValidationIssue(
        rule="feature_not_cut",
        subject=block_name,
        detail=(
            f"stamped {hole.kind} hole {hole.name!r} was NOT cut from "
            f"{block_name!r}'s printed solid — {why}; the exported part is "
            "missing this feature"
        ),
        severity="error",
    )


#: Ray grid for the per-hole "did this cut remove anything" probe —
#: quadrature over the tool's own (small) AABB, so a coarse grid is exact
#: enough to tell air from material.
_CUT_PROBE_GRID = 24


def _components_met(design: CadDesign, cutter: Expr) -> list[str]:
    """The components whose AABB overlaps the cutting tool's — the parts a
    hole can possibly cut, in the design's component order."""
    lo, hi = cad_bulk.expr_aabb(design, cutter)
    met: list[str] = []
    for name, expr in design.components.items():
        clo, chi = cad_bulk.expr_aabb(design, expr)
        if all(lo[i] <= chi[i] and hi[i] >= clo[i] for i in range(3)):
            met.append(name)
    return met


def _apply_cuts(
    design: CadDesign,
    block_name: str,
    holes: list[Hole],
    inv: Transform,
    findings: list[ValidationIssue],
) -> tuple[list[NodeSpec], list[str], bool]:
    """Subtract every stamped hole from the component(s) it lands in.

    Each hole's tool is cut from every top-level component whose bounding
    box it meets, and the cut must remove material from at least one of
    them — a hole that meets no part, or only a part's bounding-box air,
    is a ``feature_not_cut`` finding rather than a silently whole solid
    (gr344816: cutting ``components[0]`` regardless put a later part's
    hole into the first part, and the union re-filled it). Returns the
    ``cut`` node specs (one per component actually cut), the names of the
    holes cut, and whether any component changed.
    """
    cutters_by_component: dict[str, list[Expr]] = {}
    cut_nodes: list[NodeSpec] = []
    feature_names: list[str] = []
    for hole in holes:
        # A stamped hole that cannot be cut is never dropped silently: the
        # solid would leave without a screw hole and nobody would know —
        # exactly what process DRC exists to catch (pre-ship review).
        if hole.diameter_m <= 0.0 or hole.depth_m <= 0.0:
            findings.append(
                _feature_not_cut(
                    block_name,
                    hole,
                    f"non-positive size (d={hole.diameter_m:g} m, "
                    f"depth={hole.depth_m:g} m)",
                )
            )
            continue
        try:
            config = _cut_config(hole)
            primitive = cad_dsl.build_config(config)
        except (cad_dsl.DslError, ValueError) as exc:
            findings.append(
                _feature_not_cut(block_name, hole, f"unbuildable tool geometry: {exc}")
            )
            continue
        xform, loc, rot = _cut_placement(hole, inv)
        cutter = design.prim(_safe_node_name(hole.name), primitive, xform)
        removing = [
            name
            for name in _components_met(design, cutter)
            if cad_bulk.volume(
                design,
                expr=design.intersect(design.components[name], cutter),
                grid=_CUT_PROBE_GRID,
            ).volume
            > 0.0
        ]
        if not removing:
            findings.append(
                _feature_not_cut(
                    block_name,
                    hole,
                    "the tool meets no material — it cuts air (hole origin "
                    "or axis outside every component of the bound design)",
                )
            )
            continue
        for name in removing:
            cutters_by_component.setdefault(name, []).append(cutter)
            # Node names are unique per SceneSpec (the parser refuses a
            # duplicate); a fastener spanning two mated parts cuts both, so
            # the second and later cut nodes carry their component's name.
            node_name = _safe_node_name(hole.name)
            if len(removing) > 1:
                node_name = _safe_node_name(f"{hole.name}_{name}")
            cut_nodes.append(
                NodeSpec(
                    name=node_name,
                    op="cut",
                    config=config,
                    component=name,
                    loc=loc,
                    rot=rot,
                )
            )
        feature_names.append(hole.name)

    for name, cutters in cutters_by_component.items():
        design.add_component(name, design.subtract(design.components[name], *cutters))
    return cut_nodes, feature_names, bool(cutters_by_component)


def _cut_config(hole: Hole) -> str:
    """The cutting tool's canonical ``config`` text for one stamped hole —
    default a round ``cyl:``, a hex nut pocket a ``hex:`` sized from
    across-flats, a countersink a ``cone:`` (module docstring: a
    counterbore is already just a wider ``cyl:``, no special case)."""
    radius = format_dsl_number(hole.diameter_m / 2.0)
    depth = format_dsl_number(hole.depth_m)
    if hole.kind == "nut-pocket" and hole.across_flats_m:
        circumradius = hole.across_flats_m * _HEX_ACROSS_FLATS_TO_CIRCUMRADIUS
        return f"hex:r{format_dsl_number(circumradius)}h{depth}"
    if hole.kind == "countersink":
        return f"cone:r{radius}h{depth}"
    return f"cyl:r{radius}h{depth}"


def _cut_placement(
    hole: Hole, inv: Transform
) -> tuple[Transform, tuple[float, float, float], tuple[float, float, float]]:
    """A stamped hole's world origin/axis, carried into the block's own
    local frame (``inv`` — the inverse of the block's world pose) as a
    placement for the cutting primitive: the local point its base sits at,
    plus an orthonormal basis whose local ``+z`` is the hole's axis (every
    cutting shape here is a body of revolution about its own local ``+z``,
    so the roll about that axis is free — picked deterministically, never
    left to fill with whatever the first candidate basis vector happens to
    be, so a re-read is bit-identical)."""
    origin_local = inv.apply(cad_as_vec3(hole.origin))
    axis_local = cad_normalize(inv.apply_dir(cad_as_vec3(hole.axis)))
    reference = (
        np.array([1.0, 0.0, 0.0])
        if abs(float(np.dot(axis_local, [1.0, 0.0, 0.0]))) < 0.9
        else np.array([0.0, 1.0, 0.0])
    )
    x_basis = cad_normalize(np.cross(reference, axis_local))
    y_basis = np.cross(axis_local, x_basis)
    basis = np.column_stack([x_basis, y_basis, axis_local])
    rot = euler_rad_from_matrix(basis)
    xform = cad_pose(origin_local, cad_as_vec3(rot))
    loc = (float(origin_local[0]), float(origin_local[1]), float(origin_local[2]))
    return xform, loc, rot


def printed_solid(
    tree: SeTree,
    block_name: str,
    *,
    cad_store_reader: Store,
    exclude: frozenset[str] = frozenset(),
) -> PrintedSolid | None:
    """The printed solid for ``block_name`` — the bound cad design minus
    every stamped hole it carries — or ``None`` when the block is not an
    fdm implementation at all (module docstring's eligibility order).
    ``exclude`` (fastener block names) drops those fasteners' holes from
    the cut set — :func:`precis_se.fasten.features_for`'s parameter, the
    manufacture fuse's elided screws; every other caller passes nothing.

    ``cad_store_reader`` is anything offering the ``cad`` kind's own
    store-level ``get_ref``/``cad_load`` (a plain :class:`~precis.store.
    Store` in production) — deliberately not the handler class: printsolid
    reaches the stored design through the same store-level seam
    :func:`precis.cad_resolve.design_resolver` already uses for
    ``use <slug>`` instancing, so this module never imports
    ``precis.handlers.cad``.
    """
    node = tree.blocks.get(block_name)
    if node is None:
        return None
    if node.bound_kind in ("component", "part"):
        return None
    if node.bound_kind == "structure":
        return None
    family = se_modes.family_of(node.mode)
    if family is None or family.key != "fdm":
        return None
    if node.bound_kind != "cad" or not node.bound:
        return None

    ref = cad_store_reader.get_ref(kind="cad", id=node.bound)
    if ref is None:
        return None  # a dangling binding is DRC's finding, not this one's
    base_spec, _handles = cad_store_reader.cad_load(ref.id)
    resolver = design_resolver(cad_store_reader)
    try:
        expanded = expand_instances(base_spec, resolver)
        design = build_design(expanded)
    except SceneError:
        return None
    if not design.components:
        return None

    findings: list[ValidationIssue] = []
    base_expr = design.whole()
    volume_before = cad_bulk.volume(design).volume
    lo, hi = cad_bulk.expr_aabb(design, base_expr)
    diag = float(np.linalg.norm(np.asarray(hi) - np.asarray(lo)))
    if diag < _MIN_BBOX_DIAG_M or diag > _MAX_BBOX_DIAG_M:
        findings.append(
            ValidationIssue(
                rule="mode_scale_mismatch",
                subject=block_name,
                detail=(
                    f"{block_name!r}'s printed solid has a bbox diagonal of "
                    f"{diag:g} m, outside the [{_MIN_BBOX_DIAG_M:g}, "
                    f"{_MAX_BBOX_DIAG_M:g}] m band this shop's fdm process "
                    "covers — you do not FDM a nanoscale or building-scale "
                    "part"
                ),
                severity="warn",
            )
        )

    world_xform = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
    inv = world_xform.inverse()

    cut_nodes, feature_names, cut_any = _apply_cuts(
        design,
        block_name,
        se_fasten.features_for(tree, block_name, exclude=exclude),
        inv,
        findings,
    )
    volume_after = cad_bulk.volume(design).volume if cut_any else volume_before

    if volume_after <= 0.0:
        findings.append(
            ValidationIssue(
                rule="net_empty",
                subject=block_name,
                detail=(
                    f"{block_name!r}'s printed solid has zero volume after "
                    "its stamped cuts — a feature consumed the whole body; "
                    "check the offending hole depths/diameters against the "
                    "envelope"
                ),
                severity="error",
            )
        )

    spec = SceneSpec(
        nodes=[*expanded.nodes, *cut_nodes],
        components=list(expanded.components),
        meta=dict(expanded.meta),
        # A field-rooted design (realize strategy='simp') resolves its
        # ``field:<sha>`` leaf through the loader ``cad_load`` attached;
        # the export/mesh path rebuilds the design from this spec and
        # would otherwise refuse the leaf.
        field_loader=expanded.field_loader,
    )
    return PrintedSolid(
        design=design,
        spec=spec,
        features=feature_names,
        volume_before_m3=volume_before,
        volume_after_m3=volume_after,
        findings=findings,
        block=block_name,
        mode=node.mode or "",
    )
