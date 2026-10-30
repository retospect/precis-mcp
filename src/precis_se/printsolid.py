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

- ``bound_kind in ('component', 'part', 'pcb')`` — bought or fabbed
  elsewhere, not printed.
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
Unless the air is the feature — a void with material standing just
outside the tool's radial wall means the authored cad already carries
the hole (``feature_already_cut``, info; gr525812).
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.cad import bulk as cad_bulk
from precis.cad import dsl as cad_dsl
from precis.cad.export import needs_field_backend
from precis.cad.fold import Expr
from precis.cad.graph import Design as CadDesign
from precis.cad.scene import (
    NodeSpec,
    SceneError,
    SceneSpec,
    build_design,
    expand_instances,
)
from precis.cad.vec import Transform, Vec3, euler_rad_from_matrix
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import normalize as cad_normalize
from precis.cad.vec import pose as cad_pose
from precis.cad.vec import translation as cad_translation
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


def _feature_already_cut(
    block_name: str, hole: Hole, components: list[str]
) -> ValidationIssue:
    """The info-severity receipt for a stamped hole whose void the bound
    cad already carries — precis-se-fasten-help §5 tells the builder to
    cut the stamped features into the authored geometry, so
    ``intersect ≈ 0`` with material standing around the void is success
    (gr525812), not the missing-feature error it used to emit."""
    return ValidationIssue(
        rule="feature_already_cut",
        subject=block_name,
        detail=(
            f"stamped {hole.kind} hole {hole.name!r} removes nothing from "
            f"{', '.join(repr(c) for c in components)} — material stands "
            "in a thin shell just outside the void, so the bound cad "
            "already carries this feature and the cut is a no-op"
        ),
        severity="info",
    )


#: Ray grid for the per-hole "did this cut remove anything" probe —
#: quadrature over the tool's own (small) AABB, so a coarse grid is exact
#: enough to tell air from material. Field-backed designs use
#: :func:`precis.cad.bulk.volume_field` over the same box instead — see
#: :func:`_solid_volume`.
_CUT_PROBE_GRID = 24

#: Floor of the annulus probed just outside a cutter that removed
#: nothing — the witness that tells "the bound cad already carries this
#: hole" (a void with material standing around it) apart from "the tool
#: is in air" (nothing around it at all). The probe's real thickness is
#: ``max(_CUT_SHELL_M, 0.15 × cutter radius)``: a fixed 0.5 mm is nearly
#: the whole wall of a thin boss and invisible inside a fat one.
_CUT_SHELL_M = 5e-4

#: The annulus probe's ray grid — finer than ``_CUT_PROBE_GRID`` because
#: the shell is thin by construction and the wall around a void can be
#: thinner still (a counterbore in a boss leaves a sub-millimetre
#: annulus), so a coarse grid could step over the only material there.
_CUT_SHELL_GRID = 48

#: How much solid material along the stamped axis depth still reads as
#: "the void is open" — a hole the cad already carries is open the whole
#: way; anything more than this sliver of coverage is a shallower or
#: partial feature, not the stamped one.
_CUT_VOID_OPEN_M = 1e-6

#: Sign-grid cells for the same probe on a field-backed design:
#: presence/absence over a hole-sized box needs far less than
#: :data:`precis.cad.bulk.FIELD_VOLUME_CELLS`' 250k — a design with many
#: stamped holes would spend its whole wall-clock budget on probes
#: (gr529278), while 20k still resolves a cut tool's volume.
PROBE_FIELD_CELLS = 20_000


def _solid_volume(
    design: CadDesign,
    expr: Expr | None = None,
    *,
    field_backend: bool,
    box: tuple[Vec3, Vec3] | None = None,
    probe_grid: int | None = None,
    probe_cells: int | None = None,
) -> float:
    """One volume answer for both mesh backends. The ray quadrature
    (:func:`precis.cad.bulk.volume`) is exact-per-ray and cheap on analytic
    leaves, but on a field-backed design (``rd``/``blend``/``field``) every
    ray's hit search iterates the scalar SDF — ~100+ ``distance_local``
    calls — so its ~58k rays cost minutes no caller-side deadline can
    interrupt (gr529278's residual ``view='validate'`` hang: the deadline
    gates which checks *start*, it cannot cut a quadrature in flight).
    Field-backed designs therefore take
    :func:`~precis.cad.bulk.volume_field`'s bounded sign grid — the same
    vectorized ``expr_sdf_np`` the field mesher already pays, sized to the
    box it is given. ``probe_cells`` shrinks that grid for the per-hole
    presence probe (:data:`PROBE_FIELD_CELLS`); the reported before/after
    volumes keep the full :data:`~precis.cad.bulk.FIELD_VOLUME_CELLS`."""
    if field_backend:
        return cad_bulk.volume_field(
            design,
            expr=expr,
            box=box,
            cells=(cad_bulk.FIELD_VOLUME_CELLS if probe_cells is None else probe_cells),
        ).volume
    return cad_bulk.volume(design, expr=expr, grid=probe_grid).volume


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
    field_backend: bool = False,
    deadline: float | None = None,
) -> tuple[list[NodeSpec], list[str], bool]:
    """Subtract every stamped hole from the component(s) it lands in.

    Each hole's tool is cut from every top-level component whose bounding
    box it meets, and the cut must remove material from at least one of
    them — a hole that meets no part, or only a part's bounding-box air,
    is a ``feature_not_cut`` finding rather than a silently whole solid
    (gr344816: cutting ``components[0]`` regardless put a later part's
    hole into the first part, and the union re-filled it). A hole the
    tool removes nothing from is *not* automatically missing, though:
    ``_wall_around_void`` checks the thin shell just outside the tool's
    radial wall, and material standing there means the bound cad already
    carries the void (``feature_already_cut``, an info note — the cut
    node is still emitted, a no-op subtract); only a tool with nothing
    around it — genuinely in air — stays ``feature_not_cut``.
    ``deadline`` (``time.monotonic()`` — the validate view's budget,
    gr529278) is checked once per hole *before* it is probed: a hole the
    budget never reached is reported "not run: budget exceeded" — still
    an uncut stamped feature, so the same error receipt as the geometry
    cases — never probed anyway. Returns the ``cut`` node specs (one per
    component actually cut), the names of the holes cut, and whether any
    component changed.
    """
    cutters_by_component: dict[str, list[Expr]] = {}
    cut_nodes: list[NodeSpec] = []
    feature_names: list[str] = []
    for hole in holes:
        # A stamped hole that cannot be cut is never dropped silently: the
        # solid would leave without a screw hole and nobody would know —
        # exactly what process DRC exists to catch (pre-ship review).
        if deadline is not None and time.monotonic() >= deadline:
            findings.append(
                _feature_not_cut(
                    block_name,
                    hole,
                    "not run: budget exceeded — the view's wall-clock "
                    "budget was spent before this hole could be probed",
                )
            )
            continue
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
        cutter_box = cad_bulk.expr_aabb(design, cutter)
        met = _components_met(design, cutter)
        removing = [
            name
            for name in met
            if _solid_volume(
                design,
                design.intersect(design.components[name], cutter),
                field_backend=field_backend,
                box=cutter_box,
                probe_grid=_CUT_PROBE_GRID,
                probe_cells=PROBE_FIELD_CELLS,
            )
            > 0.0
        ]
        if not removing:
            removing, why = _wall_around_void(design, hole, cutter, xform, met)
            if not removing:
                findings.append(
                    _feature_not_cut(
                        block_name,
                        hole,
                        why
                        or (
                            "the tool meets no material — it cuts air (hole "
                            "origin or axis outside every component of the "
                            "bound design)"
                        ),
                    )
                )
                continue
            findings.append(_feature_already_cut(block_name, hole, removing))
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


def _cutter_radius_m(hole: Hole) -> float:
    """The tool's radius about its own axis — half the diameter for a
    round tool, half the across-flats (the inradius) for a hex pocket."""
    return max(hole.diameter_m, hole.across_flats_m or 0.0) / 2.0


def _void_cover_m(
    design: CadDesign, o: Any, d: Any, depth_m: float, *, component: str | None
) -> float:
    """Metres of material along the ray inside ``[0, depth_m]`` — how
    much of the cutter's stamped depth is NOT open void. The whole
    design's material when ``component`` is ``None``."""
    covered = 0.0
    for lo, hi in design.ray_material(o, d, component=component):
        covered += max(0.0, min(float(hi), depth_m) - max(float(lo), 0.0))
    return covered


def _quadrant_probes(
    design: CadDesign,
    hole: Hole,
    radius_m: float,
    shell_t: float,
    xform: Transform,
) -> list[tuple[Expr, Expr]]:
    """Build each quadrant's near/far probes once for all met components."""
    reach = radius_m + shell_t
    half = format_dsl_number(reach)
    depth = format_dsl_number(hole.depth_m)
    try:
        quad_prim = cad_dsl.build_config(f"box:w{half}d{half}h{depth}")
        width = format_dsl_number(2.0 * reach)
        half_depth = format_dsl_number(hole.depth_m / 2.0)
        slab_prim = cad_dsl.build_config(f"box:w{width}d{width}h{half_depth}")
    except (cad_dsl.DslError, ValueError):  # pragma: no cover — defensive
        return []
    # A full-depth positive volume cannot distinguish a complete wall from
    # one that exists only near the opening or only at the far end.
    slabs = [
        design.prim(
            f"{_safe_node_name(hole.name)}_depth{i}",
            slab_prim,
            xform.compose(cad_translation(0.0, 0.0, i * hole.depth_m / 2.0)),
        )
        for i in range(2)
    ]
    probes: list[tuple[Expr, Expr]] = []
    for i, (sx, sy) in enumerate(((1, 1), (1, -1), (-1, 1), (-1, -1))):
        # DSL boxes are centred in x/y, but their base is at local z=0.
        qx = xform.compose(cad_translation(sx * reach / 2.0, sy * reach / 2.0, 0.0))
        qbox = design.prim(f"{_safe_node_name(hole.name)}_q{i}", quad_prim, qx)
        probes.append(
            (design.intersect(qbox, slabs[0]), design.intersect(qbox, slabs[1]))
        )
    return probes


def _quadrant_walls(
    design: CadDesign,
    region: Expr,
    probes: list[tuple[Expr, Expr]],
) -> int:
    """Count quadrants with material in both halves, stopping at two misses.

    Only a count of at least three witnesses a surrounding wall. Once two
    quadrants miss, the partial count suffices to reject the component.
    """
    walls = misses = 0
    for halves in probes:
        if all(
            cad_bulk.volume(
                design, expr=design.intersect(region, half), grid=_CUT_SHELL_GRID
            ).volume
            > 0.0
            for half in halves
        ):
            walls += 1
        else:
            misses += 1
            if misses == 2:
                break
    return walls


def _void_radius_m(
    design: CadDesign, o_mid: Any, xform: Transform
) -> tuple[float, float] | None:
    """``(nearest, farthest)`` distance from the cutter's axis at
    mid-depth to material, over the four directions perpendicular to the
    axis — the shape of whatever void the tool sits in. ``None`` when no
    direction meets material at all (genuinely open space).

    A real hole's wall is roughly equidistant all around; a tool in air
    next to an unrelated wall is not — the ratio is what tells an
    *oversized* existing hole apart from open space."""
    radii: list[float] = []
    for local_dir in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0)):
        rd = xform.apply_dir(cad_as_vec3(local_dir))
        # ray_material is the full line — the first interval past the
        # probe point is the wall's distance in this direction.
        hit = next(
            (iv for iv in design.ray_material(o_mid, rd) if float(iv[1]) > 0.0),
            None,
        )
        if hit is not None and float(hit[0]) > 0.0:
            radii.append(float(hit[0]))
    if not radii:
        return None
    return min(radii), max(radii)


def _wall_around_void(
    design: CadDesign,
    hole: Hole,
    cutter: Expr,
    xform: Transform,
    met: list[str],
) -> tuple[list[str], str | None]:
    """``(witnessed, why)`` — the ``feature_already_cut`` witness,
    hardened.

    A cutter that removes nothing sits in one of three places: inside a
    void the authored cad already carries (precis-se-fasten-help §5's
    pre-cut — ``intersect`` reads ~0 because the hole is *there*, not
    because the tool missed), inside a *different* void (wider or
    shallower than the stamped hole), or in air. A fixed 0.5 mm shell
    plus "any material in it" could not tell those apart — a tool in
    air next to an unrelated wall, or a void bigger than the stamp,
    passed as already-cut. The witness now:

    - sizes the shell ``max(0.5 mm, 0.15 × cutter radius)``;
    - demands material in both depth halves of at least 3 of the shell's
      4 quadrants around the axis — a hole's wall surrounds the tool,
      a nearby or half-depth wall does not;
    - demands the void be open along the cutter's whole stamped depth —
      a shallower feature is not the stamped hole.

    ``witnessed`` is the ``met`` components passing all three. When it
    is empty, ``why`` is the sharper ``feature_not_cut`` reason the
    diagnostic found — a shallower void, or a size mismatch naming the
    compared radius (hex inradius) or diameter — or ``None`` for plain air."""
    cutter_r = _cutter_radius_m(hole)
    shell_t = max(_CUT_SHELL_M, 0.15 * cutter_r)
    origin = xform.apply(cad_as_vec3([0.0, 0.0, 0.0]))
    axis = xform.apply_dir(cad_as_vec3([0.0, 0.0, 1.0]))
    open_tol = _CUT_VOID_OPEN_M + 0.02 * hole.depth_m
    witnessed: list[str] = []
    shell: Expr | None = None
    probes: list[tuple[Expr, Expr]] = []
    for name in met:
        if _void_cover_m(design, origin, axis, hole.depth_m, component=name) > open_tol:
            continue
        # Reject blocked axes before building or evaluating volume probes;
        # the same geometry serves every component around this hole.
        if shell is None:
            try:
                shell_prim = cad_dsl.build_config(_cut_config(hole, shell_t))
            except (
                cad_dsl.DslError,
                ValueError,
            ):  # pragma: no cover — same shape built
                return [], None
            shell = design.prim(f"{_safe_node_name(hole.name)}_wall", shell_prim, xform)
            probes = _quadrant_probes(design, hole, cutter_r, shell_t, xform)
        region = design.subtract(
            design.intersect(design.components[name], shell), cutter
        )
        if _quadrant_walls(design, region, probes) < 3:
            continue
        witnessed.append(name)
    if witnessed:
        return witnessed, None
    # No component witnessed — diagnose once, on the whole solid.
    covered = _void_cover_m(design, origin, axis, hole.depth_m, component=None)
    if covered > open_tol:
        return [], (
            f"the void under the tool is open only "
            f"{max(0.0, hole.depth_m - covered) * 1000:.1f} mm of the stamped "
            f"{hole.depth_m * 1000:.1f} mm — a shallower feature, not the "
            "stamped hole"
        )
    radii = _void_radius_m(
        design, np.asarray(origin) + 0.5 * hole.depth_m * np.asarray(axis), xform
    )
    if (
        radii is not None
        and radii[0] > cutter_r * 0.5
        and radii[1] <= 4.0 * max(cutter_r, _CUT_SHELL_M)
    ):
        if hole.kind == "nut-pocket" and hole.across_flats_m:
            return [], (
                f"the sampled void radius is {radii[1] * 1000:.2f} mm, not the "
                f"stamped inradius {cutter_r * 1000:.2f} mm — a radius "
                "mismatch, not the stamped hex pocket"
            )
        return [], (
            f"the void there is Ø{2.0 * radii[1] * 1000:.2f} mm, not the "
            f"stamped Ø{2.0 * cutter_r * 1000:.2f} mm — a diameter "
            "mismatch, not the stamped hole"
        )
    return [], None


def _cut_config(hole: Hole, radial_extra_m: float = 0.0) -> str:
    """The cutting tool's canonical ``config`` text for one stamped hole —
    default a round ``cyl:``, a hex nut pocket a ``hex:`` sized from
    across-flats, a countersink a ``cone:`` (module docstring: a
    counterbore is already just a wider ``cyl:``, no special case).
    ``radial_extra_m`` pushes the tool's radial wall out by that much —
    :func:`_wall_around_void`'s annulus probe is the same body of
    revolution, fattened."""
    radius = format_dsl_number(hole.diameter_m / 2.0 + radial_extra_m)
    depth = format_dsl_number(hole.depth_m)
    if hole.kind == "nut-pocket" and hole.across_flats_m:
        circumradius = (
            hole.across_flats_m * _HEX_ACROSS_FLATS_TO_CIRCUMRADIUS + radial_extra_m
        )
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


def scale_mismatch_finding(
    block_name: str, diag_m: float, *, scale: float = 1.0
) -> ValidationIssue | None:
    """``mode_scale_mismatch`` (warn) when the part's bbox diagonal
    (``diag_m`` metres at source size, times ``scale`` for a scaled print)
    falls outside the band the fdm process covers, else ``None``."""
    diag = diag_m * scale
    if _MIN_BBOX_DIAG_M <= diag <= _MAX_BBOX_DIAG_M:
        return None
    return ValidationIssue(
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


def printed_solid(
    tree: SeTree,
    block_name: str,
    *,
    cad_store_reader: Store,
    exclude: frozenset[str] = frozenset(),
    deadline: float | None = None,
) -> PrintedSolid | None:
    """The printed solid for ``block_name`` — the bound cad design minus
    every stamped hole it carries — or ``None`` when the block is not an
    fdm implementation at all (module docstring's eligibility order).
    ``exclude`` (fastener block names) drops those fasteners' holes from
    the cut set — :func:`precis_se.fasten.features_for`'s parameter, the
    manufacture fuse's elided screws; every other caller passes nothing.

    ``deadline`` (a ``time.monotonic()`` timestamp — the validate view's
    budget, ``None`` = unbounded) reaches inside the per-hole probe loop
    (:func:`_apply_cuts`): a hole the budget never reached is reported
    "not run: budget exceeded" rather than probed.

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
    if node.bound_kind in ("component", "part", "pcb"):
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
    field_backend = needs_field_backend(expanded)
    base_expr = design.whole()
    volume_before = _solid_volume(design, field_backend=field_backend)
    lo, hi = cad_bulk.expr_aabb(design, base_expr)
    diag = float(np.linalg.norm(np.asarray(hi) - np.asarray(lo)))
    mismatch = scale_mismatch_finding(block_name, diag)
    if mismatch is not None:
        findings.append(mismatch)

    world_xform = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
    inv = world_xform.inverse()

    cut_nodes, feature_names, cut_any = _apply_cuts(
        design,
        block_name,
        se_fasten.features_for(tree, block_name, exclude=exclude),
        inv,
        findings,
        field_backend=field_backend,
        deadline=deadline,
    )
    volume_after = (
        _solid_volume(design, field_backend=field_backend) if cut_any else volume_before
    )

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
