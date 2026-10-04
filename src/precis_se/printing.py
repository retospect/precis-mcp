"""Engine 3 — process DRC + export (docs/backlog/se-print-implementer.md).

Answers the third printing question: *will it print, and what do I send?*
Composes Engine 1's solid (:mod:`precis_se.printsolid`) with Engine 2's
orientation search (:mod:`precis.cad.printability`, se-free) into one
:class:`BlockPrintReport` per fdm-family block — the one place
``handler.py``'s ``view='print'`` (no-args summary, per-block detail, and
export) all read from, so the three renders can never quietly disagree
about a block's chosen frame or its findings.

**The rules dict** is built once, here, from :func:`precis_se.
capabilities.resolve` over :func:`precis_se.capabilities.known_fields`
(the block's mode family's full field roster — a field null in this exact
material's row is still asked for, the same "the model overrides if it
wants" posture ``set_process_override`` already takes), length fields
rescaled mm → m (:func:`precis.cad.printability.rules_mm_to_m`) since
``se_capabilities.json`` is authored in millimetres and the cad kernel is
metres throughout. Every numeric threshold below traces back to this one
dict — no literal figure lives in this module.

**se-only rules** — the ones Engine 2's cad-level ``process_findings``
cannot know because they need the se tree, not just a mesh:

- ``unrealized`` (info) — the block's mode says ``fdm``, its solid says
  nothing (:func:`precis_se.printsolid.printed_solid` returned ``None``).
- ``abstract_joint`` (warn) — a connect touching this block whose
  mechanism implies real hardware nothing has been named for yet
  (:func:`precis_se.fasten.abstract_joints`, filtered to this block).
- ``hole_undersize`` / ``hole_shrink_absorbed`` — a stamped
  :class:`~precis_se.fasten.Hole` (:func:`precis_se.fasten.features_for`)
  below ``min_hole``, and the printed-hole compensation already folded
  into every non-bought hole's diameter (a receipt, not a new number).
- ``min_feature`` — primitive-level (module docstring's honesty: box/
  cylinder/frustum *parameters* of the printed solid's own node set, never
  a mesh thin-wall search) against ``min_wall`` (preferred) or
  ``min_feature``.
- ``layer_vs_load`` — the chosen frame's ``load_vs_layer`` score term
  (:func:`precis.cad.printability.score`) against ``strength_z_ratio``,
  fired whenever the block declares a load with *any* component along the
  chosen build-z (the layer-normal, weak direction) — not a pass/fail
  verdict (this module invents no comparison the capability data doesn't
  already carry), a receipt that the two numbers are worth reading
  together.

Every rule above is skipped outright when its threshold doesn't resolve —
the same "no rule runs on a ``None`` threshold" honesty
:mod:`precis.cad.printability` already keeps for its own terms.

**Floating regions** — ``floating_island`` (error), every branch (SIMP
included, beside its voxel rule): the mesh that is actually written
(:func:`build_print_mesh` — build frame, welded, sub-layer tails lifted by
:mod:`precis.cad.mesh_check` with ``tol = max(layer, source field pitch) /
2``: a mesh tessellated from a voxel field cannot carry a real feature
under half that field's pitch, so a shallower dip is a meshing artefact;
a B-rep block gets half a layer) is sliced at the house ``layer_height``
*after* the lift, and every layer polygon overlapping neither layer below
it is reported, the slicer's own SharpTail test — what survives is never
hidden. :func:`write_mesh` ships that same mesh (the
report carries it, so it is computed once), plus a ``mesh_cleanup`` info
line counting welded/dropped degenerate triangles, remaining slivers and
lifted tail vertices.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.cad import printability as cad_printability
from precis.cad.export import (
    _MM_PER_M,
    ExportError,
    _component_meshes,
    _scaled_for_export,
    _solid_mesh,
    _write_3mf,
    _write_binary_stl,
    manifold_available,
)
from precis.cad.fieldops import flat_bed
from precis.cad.mesh_check import (
    CANTILEVER_WARN_MM,
    Cantilever,
    Island,
    count_slivers,
    floating_islands,
    lift_rejection,
    lift_sharp_tails_checked,
    slicer_cantilevers,
    weld_and_drop_degenerate,
)
from precis.cad.printability import BuildCandidate, rotate_to_frame
from precis.cad.scene import NodeSpec, SceneSpec
from precis.cad.vec import Vec3, as_vec3
from precis.cad.vec import pose as cad_pose
from precis.structsolve.simp import overhang_violations
from precis_se import capabilities as se_caps
from precis_se import fasten as se_fasten
from precis_se import modes as se_modes
from precis_se.ops import SeBlock, SeTree, pinned_down
from precis_se.printsolid import PrintedSolid, printed_solid
from precis_se.validate import ValidationIssue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

#: ``build_frame.origin`` of a SIMP-realized block — the AM filter baked
#: the direction in at solve time (:mod:`precis_se.simp_bridge`), so the
#: view verifies that frame instead of searching. Spelled here rather than
#: imported so this module stays free of the bridge (which imports the
#: engine and the store-facing halves this read path never needs).
SIMP_FRAME_ORIGIN = "simp"

#: ``view='print' args={'block': ...}``'s candidate-table depth — the same
#: top-N the cad kind's own ``view='printability'`` renders
#: (``handlers/cad.py::_PRINTABILITY_TOP_N``); a render-width judgment,
#: not a capability figure.
CANDIDATE_TABLE_N = 5


#: The first layer's thickness Bambu Studio slices with (it is 0.2 mm
#: whatever the layer height) and the default line width of its 0.4 mm
#: nozzle — the figures :func:`~precis.cad.mesh_check.slicer_cantilevers`
#: mirrors when the block's capability row gives no ``line_width``.
SLICER_FIRST_LAYER_MM = 0.2
SLICER_LINE_WIDTH_MM = 0.42

#: ``floating_island`` findings listed individually per block; the rest are
#: summarised in one line (the total is always stated).
MAX_LISTED_ISLANDS = 10


class PrintUnsupported(RuntimeError):
    """``manifold3d`` is not installed — the handler turns this into the
    same ``Unsupported`` + install hint every mesh path in this repo
    raises (:func:`precis.cad.export.manifold_available`)."""


@dataclass
class BlockPrintReport:
    """Everything ``view='print'`` renders for one fdm-family block, all
    composed once (module docstring). ``candidates`` is Engine 2's
    best-first search (empty when there is no solid to search over);
    ``chosen_down``/``chosen_score`` are the frame actually reported on —
    the pinned one when :attr:`pinned`, else the best candidate.
    ``best_other`` is set only when a pin scores worse than the best
    candidate, an already-formatted sentence for a finding's
    ``suggested_fix``."""

    block: str
    mode: str
    printed: PrintedSolid | None
    candidates: list[BuildCandidate]
    chosen_down: Vec3 | None
    chosen_score: cad_printability.Score | None
    pinned: bool
    best_other: str | None
    findings: list[ValidationIssue] = field(default_factory=list)
    #: The house ``layer_height`` (metres) the block's mode resolves to —
    #: the sample pitch the cad field-export backend meshes an ``rd``/
    #: ``blend`` design at (:func:`write_mesh`); ``None`` when the
    #: capability doesn't resolve (export then takes cad's own default).
    pitch: float | None = None
    #: Set on a SIMP-realized block (``build_frame.origin == 'simp'``): why
    #: no orientation search ran and what the overhang verification on
    #: the stored field measured — rendered as its own line, so the
    #: report never reads as a search that happened to agree.
    search_skipped: str | None = None
    #: The post-processed build-frame mesh the findings above were judged on
    #: and :func:`write_mesh` will ship (``None`` when nothing was built).
    print_mesh: PrintMesh | None = field(default=None, repr=False)
    #: Pitch (metres) of the stored field a field-rooted block was solved at,
    #: ``None`` for a B-rep block or an unrecoverable field — what
    #: :func:`write_mesh` needs to rebuild the same mesh the report judged.
    source_pitch: float | None = None
    #: ``True`` when the mesh above was built and the floating-region check
    #: ran on it (``report_for(mesh_checks=True)``, the default).
    mesh_checked: bool = False


@dataclass
class PrintMesh:
    """The mesh a block ships: ``(name, verts, tris)`` per part in the build
    frame (millimetres, bed at ``z = 0``), already welded and tail-lifted
    (:func:`build_print_mesh`), with what that cleanup did."""

    parts: list[tuple[str, np.ndarray, np.ndarray]]
    #: The tail-lift tolerance used, mm (``None`` when none could be set).
    tol_mm: float | None = None
    n_dropped: int = 0
    n_slivers: int = 0
    n_lifted: int = 0
    #: Plateaus the lift left alone: too little material above / a face
    #: would flip or collapse (:func:`~precis.cad.mesh_check.lift_sharp_tails_checked`).
    n_kept_thin: int = 0
    n_kept_flip: int = 0
    #: Per part, why the lift was abandoned and the welded mesh shipped
    #: unlifted (:func:`~precis.cad.mesh_check.lift_rejection`).
    lift_abandoned: list[str] = field(default_factory=list)
    #: Flat bed contact (:func:`flat_bed_spec`): a receipt when it was applied
    #: to the field, or why it was skipped on a field-rooted block.
    flat_bed: str | None = None
    flat_bed_skipped: str | None = None
    #: Extrusion line width (mm) the slicer-cantilever check drops narrower
    #: features by; ``None`` takes :data:`SLICER_LINE_WIDTH_MM`.
    line_width_mm: float | None = None


def format_down(v: Vec3) -> str:
    """One build-down direction, agent/human-readable — ``handler.py``'s
    own renders share this rather than each spelling the format."""
    return f"[{v[0]:.4g}, {v[1]:.4g}, {v[2]:.4g}]"


def _rules_for(tree: SeTree, node: SeBlock) -> dict[str, Any]:
    """Every field ``node``'s mode family defines, resolved through
    :func:`~precis_se.capabilities.resolve` (block override → house tier,
    clamped to the physical figure), length fields rescaled mm → m — the
    one rules dict Engine 2's ``orient``/``process_findings`` and this
    module's own se-only checks both read, never a second lookup."""
    raw: dict[str, Any] = {}
    for name in sorted(se_caps.known_fields(node.mode)):
        resolved = se_caps.resolve(tree, node, name)
        if resolved is not None:
            raw[name] = resolved.value
    return cad_printability.rules_mm_to_m(raw)


def _local_loads(node: SeBlock) -> list[Vec3]:
    """The block's declared force load, rotated from the world frame
    ``set_load`` declares it in into the block's own local frame —
    ``printed_solid``'s frame (pose identity), the same inverse-pose
    transform :mod:`precis_se.printsolid` already uses for a stamped
    hole's axis. Only ``force`` feeds Engine 2's orientation scoring —
    ``torque`` has no representation in :func:`precis.cad.printability.
    orient`'s ``loads: Sequence[Vec3]`` today."""
    force = (node.objectives or {}).get("force")
    if not force:
        return []
    xform = cad_pose(as_vec3(node.pose), as_vec3(node.rot))
    inv = xform.inverse()
    return [inv.apply_dir(as_vec3(force))]


def _abstract_joint_findings(
    tree: SeTree, block: str, *, fused: Callable[[str, str], bool] | None = None
) -> list[ValidationIssue]:
    """``abstract_joint`` (warn), filtered to the connects touching
    ``block`` — module docstring. ``fused`` is the manufacture group's
    fusion predicate (:func:`precis_se.fasten.abstract_joints`)."""
    out: list[ValidationIssue] = []
    for connect, mechanism in se_fasten.abstract_joints(tree, fused=fused):
        if block not in (connect.a_block, connect.b_block):
            continue
        subject = (
            f"{connect.a_block}.{connect.a_port}—{connect.b_block}.{connect.b_port}"
        )
        out.append(
            ValidationIssue(
                rule="abstract_joint",
                subject=subject,
                detail=(
                    f"the {mechanism!r} mechanism between {connect.a_block!r} "
                    f"and {connect.b_block!r} implies real hardware, but "
                    "nothing has been named for it yet — the requirements "
                    "say a joint exists, nothing realizes it"
                ),
                severity="warn",
                suggested_fix=(
                    "mint the fastener from the series registry, bind it "
                    "(set_binding kind='component'), and re-issue the "
                    "connect naming it"
                ),
            )
        )
    return out


def _hole_findings(
    node: SeBlock, block: str, holes: list[se_fasten.Hole], rules: dict[str, Any]
) -> list[ValidationIssue]:
    """``hole_undersize`` (warn, per undersize hole) and
    ``hole_shrink_absorbed`` (info, one receipt for the block) — module
    docstring."""
    if not holes:
        return []
    out: list[ValidationIssue] = []
    min_hole = rules.get("min_hole")
    if min_hole is not None:
        for hole in holes:
            if hole.diameter_m < min_hole:
                out.append(
                    ValidationIssue(
                        rule="hole_undersize",
                        subject=block,
                        detail=(
                            f"{hole.name}: a {hole.diameter_m * 1000:.2f} mm "
                            f"stamped {hole.kind} hole is below what this "
                            "process can reliably print"
                        ),
                        severity="warn",
                        measured=f"{hole.diameter_m * 1000:.2f} mm",
                        expected=f">= {min_hole * 1000:.2f} mm (min_hole)",
                        suggested_fix="drill after printing",
                    )
                )
    bump, cap = se_caps.hole_compensation_m(node.mode)
    if bump > 0.0:
        out.append(
            ValidationIssue(
                rule="hole_shrink_absorbed",
                subject=block,
                detail=(
                    f"+{bump * 1000:.2f} mm printed-hole compensation "
                    f"({cap.mode if cap is not None else node.mode}) already "
                    f"applied to {len(holes)} stamped hole(s): "
                    + ", ".join(h.name for h in holes)
                ),
                severity="info",
                measured=f"+{bump * 1000:.2f} mm",
            )
        )
    return out


def _thinnest_dim(alias: str, params: dict[str, float]) -> float | None:
    """One primitive node's thinnest box/cylinder/frustum dimension —
    module docstring's ``min_feature`` honesty: cheap, primitive-level,
    never a mesh thin-wall search. ``None`` for a shape this v1 check
    doesn't cover (sphere, torus, hex, ngon, pyramid, chamfer — none of
    them a designed member's dominant wall shape in practice yet). A
    frustum/truncated-cone radius of ``0`` is the shape's own point, not a
    thin wall, so it is excluded from the comparison rather than read as a
    zero-thickness feature."""
    if alias == "box":
        return min(params["w"], params["d"], params["h"])
    if alias in ("cyl", "cone"):
        return min(2.0 * params["r"], params["h"])
    if alias in ("tcone", "frustum"):
        radii = [2.0 * params[k] for k in ("rb", "rt") if params.get(k, 0.0) > 0.0]
        return min([*radii, params["h"]]) if radii else params["h"]
    return None


def _min_feature_finding(
    block: str, printed: PrintedSolid, rules: dict[str, Any]
) -> ValidationIssue | None:
    """``min_feature`` (warn) — the worst (thinnest) offending ``add``
    node in the printed solid's own node set, against ``min_wall``
    (preferred) or ``min_feature``. ``None`` when neither threshold
    resolves, or nothing is thinner than whichever does."""
    threshold = rules.get("min_wall")
    threshold_name = "min_wall"
    if threshold is None:
        threshold = rules.get("min_feature")
        threshold_name = "min_feature"
    if threshold is None:
        return None
    worst: tuple[float, str] | None = None
    for node_spec in printed.spec.nodes:
        if node_spec.op != "add":
            continue  # a cut tool's own size says nothing about a wall
        try:
            parsed = cad_dsl.parse(node_spec.config)
        except (cad_dsl.DslError, ValueError):  # pragma: no cover - defensive
            continue
        dim = _thinnest_dim(parsed.alias, parsed.params)
        if dim is None:
            continue
        if worst is None or dim < worst[0]:
            worst = (dim, node_spec.name)
    if worst is None or worst[0] >= threshold:
        return None
    dim, name = worst
    return ValidationIssue(
        rule="min_feature",
        subject=block,
        detail=(
            f"node {name!r}'s thinnest dimension is {dim * 1000:.2f} mm, "
            f"below the process {threshold_name}"
        ),
        severity="warn",
        measured=f"{dim * 1000:.2f} mm",
        expected=f">= {threshold * 1000:.2f} mm ({threshold_name})",
        suggested_fix=f"thicken {name!r}'s envelope parameter",
    )


def _layer_vs_load_finding(
    block: str, score: cad_printability.Score, rules: dict[str, Any]
) -> ValidationIssue | None:
    """``layer_vs_load`` (warn) — module docstring: fired whenever the
    chosen frame's ``load_vs_layer`` score term is non-zero (some
    component of a declared load crosses layers) and ``strength_z_ratio``
    resolves; a receipt, not an invented pass/fail line."""
    ratio = rules.get("strength_z_ratio")
    if ratio is None or score.load_vs_layer <= 0.0:
        return None
    return ValidationIssue(
        rule="layer_vs_load",
        subject=block,
        detail=(
            f"{score.load_vs_layer:.3f} of the declared load's magnitude "
            "runs along the chosen build-z (the weak, layer-normal "
            "direction)"
        ),
        severity="warn",
        measured=f"{score.load_vs_layer:.3f}",
        expected=f"{ratio:g} (strength_z_ratio, tensile-across-layers ÷ in-plane)",
        suggested_fix="reorient / reroute the load through a rib",
    )


def frame_findings(
    block: str,
    mesh: tuple[np.ndarray, np.ndarray],
    down: Vec3,
    rules: dict[str, Any],
    *,
    score: cad_printability.Score | None,
    best_other: str | None = None,
) -> list[ValidationIssue]:
    """The findings that depend on WHICH build frame a solid prints in —
    Engine 2's ``process_findings`` (overhang/bridge/bed contact/build
    volume) mapped onto se's issue shape, plus ``layer_vs_load``. Shared
    with :mod:`precis_se.printgroup`, which judges every member at the
    group's one frame rather than at the member's own best."""
    out: list[ValidationIssue] = [
        ValidationIssue(
            rule=f.rule,
            subject=block,
            detail=f.detail,
            severity=f.severity,
            measured=f.measured,
            expected=f.expected,
            suggested_fix=f.suggested_fix,
        )
        for f in cad_printability.process_findings(
            mesh, down, rules, best_other=best_other
        )
    ]
    if score is not None:
        lv = _layer_vs_load_finding(block, score, rules)
        if lv is not None:
            out.append(lv)
    return out


def frame_free_findings(
    tree: SeTree,
    node: SeBlock,
    block: str,
    printed: PrintedSolid,
    rules: dict[str, Any],
    *,
    fused: Callable[[str, str], bool] | None = None,
) -> list[ValidationIssue]:
    """The se-only findings that hold whatever frame the solid prints in —
    stamped-hole checks, ``min_feature``, ``abstract_joint`` (module
    docstring). Shared with :mod:`precis_se.printgroup` and, with
    ``fused`` (a ``screw`` demand satisfied by fusion),
    :mod:`precis_se.manufacture`."""
    out = _hole_findings(node, block, se_fasten.features_for(tree, block), rules)
    mf = _min_feature_finding(block, printed, rules)
    if mf is not None:
        out.append(mf)
    out.extend(_abstract_joint_findings(tree, block, fused=fused))
    return out


_AXIS_PERM = {"x": (1, 2, 0), "y": (2, 0, 1), "z": (0, 1, 2)}


def _simp_field_overhangs(
    printed: PrintedSolid, build_dir: str, cad_store_reader: Store
) -> int | None:
    """The 45° voxel rule (:func:`precis.structsolve.simp.
    overhang_violations`) on the STORED field a SIMP-realized block is
    bound to, in the build frame ``build_dir`` names — the same rule the
    AM filter enforced at solve time, re-measured on what was actually
    stored (morphology runs after the solve and could re-open a gap). The
    root field leaf only: stamped cuts are not in the count. ``None`` when
    the bound design's root is not a field leaf or the grid cannot be
    loaded — then the mesh-based ``overhang`` rule runs as usual."""
    if not printed.spec.nodes:
        return None
    config = str(printed.spec.nodes[0].config or "")
    if not config.startswith("field:"):
        return None
    try:
        _header, fld = cad_store_reader.get_field(config[len("field:") :])
    except Exception:
        return None
    axis, sign = build_dir[0], build_dir[1:]
    perm = _AXIS_PERM.get(axis)
    if perm is None or sign not in ("+", "-"):
        return None
    solid = np.transpose((np.asarray(fld.grid) <= 0.0).astype(float), perm)
    if sign == "-":
        solid = solid[:, :, ::-1]
    return overhang_violations(np.ascontiguousarray(solid), plate_at_first_solid=True)


def source_field_pitch(
    printed: PrintedSolid, cad_store_reader: Store
) -> tuple[bool, float | None]:
    """``(field_rooted, pitch)`` — whether the printed solid's root leaf is a
    stored sampled field (SIMP / any ``field:<sha>`` block), and that
    field's own pitch in metres (the resolution the solver produced it at,
    NOT the narrow-band remesh pitch the export meshes it at), read from the
    stored header without loading the samples
    (:meth:`~precis.store.Store.field_header`). ``pitch`` is ``None`` when
    the root is not a field, the store has no such grid (or the prefix is
    ambiguous), or the header carries no usable pitch; a database error
    propagates."""
    if not printed.spec.nodes:
        return False, None
    config = str(printed.spec.nodes[0].config or "")
    if not config.startswith("field:"):
        return False, None
    header = cad_store_reader.field_header(config[len("field:") :])
    raw = (header or {}).get("pitch_m")
    if raw is None:
        return True, None
    try:
        pitch = float(raw)
    except (TypeError, ValueError):
        return True, None
    return True, (pitch if pitch > 0.0 else None)


def report_for(
    tree: SeTree,
    block: str,
    *,
    cad_store_reader: Store,
    mesh_checks: bool = True,
) -> BlockPrintReport | None:
    """Everything ``view='print'`` needs for one block, or ``None`` when
    the block's resolved mode family isn't ``fdm`` at all — not this
    engine's block (``view='fab'`` covers it instead).

    Raises :class:`PrintUnsupported` when the ``manifold3d`` backend is
    missing and a solid needs tessellating (mirrors ``handlers/cad.py``'s
    own ``view='printability'`` gate) — never for a block with nothing to
    tessellate (``unrealized``/net-empty), which reports honestly with no
    backend needed at all.

    ``mesh_checks`` (default on) builds the shipped mesh and runs the
    floating-region check on it (:func:`build_print_mesh`) — seconds on a
    big field mesh, so the cheap status callers (the ``drc``/``validate``
    pointers, the all-blocks summary, ``view='fab'``) pass ``False`` and
    keep their old cost; the per-block report and the export keep it on."""
    node = tree.blocks.get(block)
    if node is None:
        return None
    family = se_modes.family_of(node.mode)
    if family is None or family.key != "fdm":
        return None
    mode = node.mode or ""
    pin = pinned_down(node)
    pinned = pin is not None
    printed = printed_solid(tree, block, cad_store_reader=cad_store_reader)
    findings: list[ValidationIssue] = []

    if printed is None:
        findings.append(
            ValidationIssue(
                rule="unrealized",
                subject=block,
                detail=(
                    f"{block!r} declares mode {mode!r} but has no bound cad "
                    "design yet — nothing to print"
                ),
                severity="info",
                suggested_fix=f"realize(block={block!r}, mode={mode!r})",
            )
        )
        findings.extend(_abstract_joint_findings(tree, block))
        return BlockPrintReport(
            block=block,
            mode=mode,
            printed=None,
            candidates=[],
            chosen_down=None,
            chosen_score=None,
            pinned=pinned,
            best_other=None,
            findings=findings,
        )

    findings.extend(printed.findings)
    if printed.volume_after_m3 <= 0.0:
        # net_empty already named the problem (in printed.findings) —
        # there is no solid left to search an orientation over.
        findings.extend(_abstract_joint_findings(tree, block))
        return BlockPrintReport(
            block=block,
            mode=mode,
            printed=printed,
            candidates=[],
            chosen_down=None,
            chosen_score=None,
            pinned=pinned,
            best_other=None,
            findings=findings,
        )

    if not manifold_available():
        raise PrintUnsupported(
            "view='print' needs the manifold3d backend (core dependency — "
            "a broken venv?)"
        )
    rules = _rules_for(tree, node)
    policy = se_caps.orientation_policy(node.mode) or {}
    # An rd/blend design meshes from its SDF at the house layer height —
    # the resolution the DRC below judges it at; a sharp design's analytic
    # fold ignores the pitch.
    pitch = rules.get("layer_height")
    mesh = _solid_mesh(printed.spec, pitch=pitch)
    loads = _local_loads(node)

    # A SIMP-realized block: the AM filter baked build_dir in before the
    # solve, so an orientation search after the fact would be proposing a
    # frame the field was never optimised for. Verify the declared frame
    # instead — the 45° voxel rule on the stored field replaces the mesh
    # `overhang` rule (a marching-cubes surface over a coarse staircase
    # has facets on both sides of 45° whatever the voxels did).
    simp_frame = (
        node.build_frame
        if pinned and (node.build_frame or {}).get("origin") == SIMP_FRAME_ORIGIN
        else None
    )
    search_skipped: str | None = None
    if simp_frame is not None:
        candidates = []
        build_dir = str(simp_frame.get("build_dir") or "?")
        voxels = _simp_field_overhangs(printed, build_dir, cad_store_reader)
        if voxels is not None:
            rules = {k: v for k, v in rules.items() if k != "max_overhang"}
            if voxels > 0:
                findings.append(
                    ValidationIssue(
                        rule="overhang",
                        subject=block,
                        detail=(
                            f"{voxels} voxel(s) of the stored SIMP field are "
                            "unsupported under the 45° rule in the declared "
                            f"build frame ({build_dir}) — a post-solve "
                            "morphology re-opened a gap the AM filter had closed"
                        ),
                        severity="warn",
                        measured=f"{voxels} unsupported voxel(s)",
                        expected="0 (the AM filter's own rule)",
                        suggested_fix="re-realize without open=/close=, or coarser",
                    )
                )
            verified = f"{voxels} unsupported voxel(s) on the stored field"
        else:
            verified = "stored field unreadable — mesh overhang rule ran instead"
        search_skipped = (
            f"orientation search skipped: build_dir {build_dir!r} was baked in "
            "by the SIMP AM filter at solve time; verified at that frame — "
            f"{verified}"
        )
    else:
        candidates = (
            cad_printability.orient(mesh, rules, policy, loads) if policy else []
        )

    best_other: str | None = None
    chosen_down: Vec3 | None = None
    chosen_score: cad_printability.Score | None = None
    if pin is not None:
        chosen_down = as_vec3(pin)
        chosen_score = cad_printability.score(mesh, chosen_down, rules, policy, loads)
        if candidates and not np.allclose(chosen_down, candidates[0].down, atol=1e-6):
            best = candidates[0]
            worse_by = chosen_score.total - best.score
            best_other = (
                f"down={format_down(best.down)} scores {best.score:.4g} vs the "
                f"pinned down={format_down(chosen_down)}'s "
                f"{chosen_score.total:.4g} ({worse_by:+.4g})"
            )
    elif candidates:
        chosen_down = candidates[0].down
        chosen_score = cad_printability.score(mesh, chosen_down, rules, policy, loads)

    if chosen_down is not None:
        findings.extend(
            frame_findings(
                block,
                mesh,
                chosen_down,
                rules,
                score=chosen_score,
                best_other=best_other,
            )
        )
    print_mesh: PrintMesh | None = None
    source_pitch: float | None = None
    if chosen_down is not None and mesh_checks:
        # The mesh the file will carry, judged the way a slicer will: the
        # SIMP voxel rule above cannot see a marching-cubes tail, and the
        # mesh `overhang` rule is blind to a sub-facet one.
        field_rooted, source_pitch = source_field_pitch(printed, cad_store_reader)
        if field_rooted and source_pitch is None:
            findings.append(
                ValidationIssue(
                    rule="mesh_cleanup",
                    subject=block,
                    detail=(
                        "the block's root field has no readable pitch, so its "
                        "solve pitch is unknown — the sub-voxel tail lift fell "
                        "back to half a layer"
                    ),
                    severity="info",
                )
            )
        try:
            print_mesh = build_print_mesh(
                printed,
                chosen_down,
                pitch=pitch,
                source_pitch=source_pitch,
                line_width=rules.get("line_width"),
            )
            findings.extend(_mesh_findings(block, print_mesh, pitch))
        except (ExportError, ValueError) as exc:
            findings.append(
                ValidationIssue(
                    rule="floating_island",
                    subject=block,
                    detail=f"floating-region check skipped: {exc}",
                    severity="info",
                )
            )
    findings.extend(frame_free_findings(tree, node, block, printed, rules))

    return BlockPrintReport(
        block=block,
        mode=mode,
        printed=printed,
        candidates=candidates,
        chosen_down=chosen_down,
        chosen_score=chosen_score,
        pinned=pinned,
        best_other=best_other,
        findings=findings,
        pitch=pitch,
        search_skipped=search_skipped,
        print_mesh=print_mesh,
        source_pitch=source_pitch,
        mesh_checked=print_mesh is not None,
    )


#: Name of the half-space node :func:`flat_bed_spec` inserts after the
#: field leaf (``intersect`` with everything beyond the bed plane).
FLAT_BED_NODE = "_flat_bed"
#: Where the mesher's first sample plane sits past the cut, in mesh pitches.
FLAT_BED_PHASE = 0.25


def flat_bed_spec(
    scaled: SceneSpec, down: Vec3, mesh_pitch: float | None = None
) -> tuple[SceneSpec, str | None, str | None, tuple[int, float] | None]:
    """``(spec, applied, skipped)`` — flat bed contact for a field-rooted
    block, done in FIELD space before any tessellation (the mesh is output
    only and never edited).

    A voxel field's lowest surface bevels in at the wall columns: a 1-voxel
    fin stands on a knife edge a few tenths of a millimetre wide, narrower
    than the nozzle line, and the slicer drops its first layer.
    :func:`precis.cad.fieldops.flat_bed` pads the field one voxel-slab
    further along ``down`` (walls run straight on); this intersects the
    root field leaf — and only it, the node goes right after it — with the
    half-space on the material side of the old bed plane, so the bottom is
    a flat cut and the walls meet the bed square.

    The mesher's sample lattice is phased so a plane sits ``FLAT_BED_PHASE * mesh_pitch``
    on the material side of the cut: marching cubes then closes the foot
    within a twentieth of a cell instead of up to a whole one (the 4th
    return, ``(axis, coord)``, for ``_component_meshes(lattice=)``; ``None``
    without ``mesh_pitch``).

    ``scaled`` is the millimetre spec; ``down`` is in the block frame. Only
    an axis-aligned ``down`` in an unrotated field frame can be done: any
    other returns the spec unchanged with ``skipped`` naming why. A spec
    whose root is not a field returns ``(scaled, None, None, None)``."""
    if not scaled.nodes:
        return scaled, None, None, None
    node = scaled.nodes[0]
    if not str(node.config).startswith("field:"):
        return scaled, None, None, None
    d = np.asarray(down, dtype=np.float64)
    axis = int(np.argmax(np.abs(d)))
    if abs(d[axis]) < 1.0 - 1e-6:
        return (
            scaled,
            None,
            f"build-down {format_down(down)} is not axis-aligned in the field's "
            "frame, so the field cannot be padded along it",
            None,
        )
    if any(abs(r) > 1e-12 for r in node.rot) or node.pattern is not None:
        return (
            scaled,
            None,
            "the root field leaf is rotated or patterned in the block frame",
            None,
        )
    ref = cad_dsl.parse(node.config).ref
    if scaled.field_loader is None or ref is None:
        return scaled, None, "the field cannot be loaded (no loader attached)", None
    sign = 1 if d[axis] > 0 else -1
    fld = scaled.field_loader(ref)
    padded, bed = flat_bed(fld, axis, sign)
    lo, hi = padded.aabb_local()
    off = np.asarray(node.loc, dtype=np.float64)
    lo, hi, bed_w = lo + off, hi + off, bed + float(off[axis])
    pad = float(padded.pitch)
    blo, bhi = lo - pad, hi + pad
    span = float(bhi[axis] - blo[axis])
    if sign < 0:
        blo[axis], bhi[axis] = bed_w, bed_w + span
    else:
        blo[axis], bhi[axis] = bed_w - span, bed_w
    size = bhi - blo
    box = cad_dsl.ShapeSpec(
        alias="box",
        params={"w": float(size[0]), "d": float(size[1]), "h": float(size[2])},
    )
    cut = NodeSpec(
        name=FLAT_BED_NODE,
        op="intersect",
        config=cad_dsl.format_spec(box),
        component=node.component,
        loc=(
            float((blo[0] + bhi[0]) / 2),
            float((blo[1] + bhi[1]) / 2),
            float(blo[2]),
        ),
    )
    loader = scaled.field_loader

    def patched(r: str) -> Any:
        return padded if r == ref else loader(r)

    spec = SceneSpec(
        nodes=[scaled.nodes[0], cut, *scaled.nodes[1:]],
        components=list(scaled.components),
        meta=dict(scaled.meta),
        field_loader=patched,
    )
    lattice: tuple[int, float] | None = None
    if mesh_pitch is not None:
        lattice = (axis, bed_w - sign * FLAT_BED_PHASE * mesh_pitch)
    applied = (
        f"field extruded two voxel slabs along {'-+'[sign > 0]}{'xyz'[axis]} and cut flat at "
        f"{'xyz'[axis]} = {bed_w:.4g} mm (the old bed plane)"
    )
    return spec, applied, None, lattice


def build_print_mesh(
    printed: PrintedSolid,
    down: Vec3,
    *,
    pitch: float | None = None,
    source_pitch: float | None = None,
    welded: bool = False,
    flat_bed_contact: bool = True,
    line_width: float | None = None,
) -> PrintMesh:
    """The mesh ``printed`` ships in the build frame ``down`` names: folded
    in millimetres (:func:`precis.cad.export._scaled_for_export` first —
    never scale twice), each part rotated to ``-z`` = ``down`` with its
    lowest point on the bed (:func:`precis.cad.printability.rotate_to_frame`),
    then :func:`~precis.cad.mesh_check.weld_and_drop_degenerate` and
    :func:`~precis.cad.mesh_check.lift_sharp_tails` with ``tol = max(layer/2,
    source_pitch/2)``: a mesh tessellated from a voxel field cannot carry a
    real feature smaller than half that field's pitch, so a dip shallower
    than that is a meshing artefact by construction. ``pitch`` is the house
    layer height and ``source_pitch`` the pitch (both metres) of the field
    the solver produced — ``None`` for a B-rep / non-field block, which
    gets half a layer. ``welded`` folds every component into ONE body (STL has no
    parts) instead of one part per component (3MF). The one place the
    report and the writer both take their mesh from.

    A field-rooted block first gets flat bed contact in field space
    (:func:`flat_bed_spec`; ``flat_bed_contact=False`` disables it): ``PrintMesh.
    flat_bed`` / ``flat_bed_skipped`` say what happened."""
    scaled = _scaled_for_export(printed.spec)
    mm_pitch = None if pitch is None else pitch * _MM_PER_M
    applied = skipped = lattice = None
    if flat_bed_contact:
        scaled, applied, skipped, lattice = flat_bed_spec(scaled, down, mm_pitch)
    source_mm = None if source_pitch is None else source_pitch * _MM_PER_M
    if welded:
        raw = [("design", *_solid_mesh(scaled, pitch=mm_pitch, lattice=lattice))]
    else:
        raw = _component_meshes(scaled, pitch=mm_pitch, lattice=lattice)
    halves = [h / 2.0 for h in (mm_pitch, source_mm) if h is not None]
    tol_mm = max(halves) if halves else None
    out = PrintMesh(
        parts=[],
        tol_mm=tol_mm,
        flat_bed=applied,
        flat_bed_skipped=skipped,
        line_width_mm=None if line_width is None else line_width * _MM_PER_M,
    )
    for name, v, t in raw:
        v = rotate_to_frame(v, down)
        v, t, dropped = weld_and_drop_degenerate(v, t)
        out.n_dropped += dropped
        if tol_mm is not None and len(v):
            res = lift_sharp_tails_checked(v, t, bed_z=float(v[:, 2].min()), tol=tol_mm)
            why = lift_rejection(v, res.vertices, t) if res.n_lifted else None
            if why is not None:
                out.lift_abandoned.append(f"{name}: {why}")
            else:
                v = res.vertices
                out.n_lifted += res.n_lifted
                out.n_kept_thin += res.n_kept_thin
                out.n_kept_flip += res.n_kept_flip
        out.n_slivers += count_slivers(v, t)
        out.parts.append((name, v, t))
    return out


def _island_issue(
    block: str, isl: Island, index: int, total: int, layer_mm: float
) -> ValidationIssue:
    return ValidationIssue(
        rule="floating_island",
        subject=block,
        detail=(
            f"island {index}/{total}: a {isl.area:.3g} mm² layer polygon at "
            f"z = {isl.z:.2f} mm, centred ({isl.x:.1f}, {isl.y:.1f}) mm, "
            "overlaps neither of the two layers below it — the slicer sees "
            "it as a floating region (SharpTail) with nothing to print on"
        ),
        severity="error",
        measured=f"z = {isl.z:.2f} mm, {isl.area:.3g} mm²",
        expected=f"0 islands at {layer_mm:.3g} mm layers",
        suggested_fix=(
            "add support under it, change the build frame, or reshape "
            "the feature so each layer grows out of the one below"
        ),
    )


#: The rules judged on the mesh that is actually written. An STL of a
#: multi-component block ships the welded union, which is judged again — its
#: findings replace every one of these from the per-component report.
SHIPPED_MESH_RULES = ("floating_island", "slicer_cantilever", "mesh_cleanup")


def _shared_bed(built: PrintMesh) -> float:
    """The bed height of the whole plate: the lowest point of ALL parts, so
    a component resting on another is not judged as if it sat on the bed."""
    return min(float(v[:, 2].min()) for _n, v, _t in built.parts)


def _cantilever_findings(
    block: str, built: PrintMesh, layer_mm: float
) -> list[ValidationIssue]:
    """``slicer_cantilever`` — Bambu Studio's floating-cantilever test
    (:func:`~precis.cad.mesh_check.slicer_cantilevers`): ``error`` for a
    region more than :data:`~precis.cad.mesh_check.CANTILEVER_WARN_MM` from
    what supports it in a layer tree support cannot reach (it skips the
    first layers off the bed, so supports=on adds nothing), ``warn`` for the
    same distance higher up (supports can fix it). Errors first, then by
    distance; at most :data:`MAX_LISTED_ISLANDS` listed, the total stated."""
    lw = built.line_width_mm or SLICER_LINE_WIDTH_MM
    hits: list[Cantilever] = []
    bed = _shared_bed(built)
    for _name, v, t in built.parts:
        hits.extend(
            slicer_cantilevers(
                v,
                t,
                layer_mm,
                first_layer=SLICER_FIRST_LAYER_MM,
                line_width=lw,
                bed_z=bed,
            )
        )
    hits = [c for c in hits if c.distance > CANTILEVER_WARN_MM]
    hits.sort(key=lambda c: (not c.unreachable, -c.distance))
    out: list[ValidationIssue] = []
    for i, c in enumerate(hits[:MAX_LISTED_ISLANDS], start=1):
        out.append(
            ValidationIssue(
                rule="slicer_cantilever",
                subject=block,
                detail=(
                    f"region {i}/{len(hits)}: {c.area:.3g} mm² at z = "
                    f"{c.bottom_z:.2f}-{c.z:.2f} mm, centred ({c.x:.1f}, "
                    f"{c.y:.1f}) mm, reaches {c.distance:.1f} mm past what "
                    "supports it (features under one line width, "
                    f"{lw:.2g} mm, do not count as support) — "
                    + (
                        "tree support cannot reach this layer, so the slicer's "
                        "floating-cantilever error cannot be cured with supports"
                        if c.unreachable
                        else "the slicer warns; supports can cure it"
                    )
                ),
                severity="error" if c.unreachable else "warn",
                measured=f"{c.distance:.1f} mm",
                expected=f"<= {CANTILEVER_WARN_MM:g} mm",
                suggested_fix=(
                    "give the feature a full-width foot on the bed (a bed-"
                    "touching wall must be at least one line width wide in "
                    "its first layer), or reorient"
                    if c.unreachable
                    else "enable supports, or reshape so the span grows out "
                    "of the layer below"
                ),
            )
        )
    if len(hits) > MAX_LISTED_ISLANDS:
        out.append(
            ValidationIssue(
                rule="slicer_cantilever",
                subject=block,
                detail=(
                    f"{len(hits) - MAX_LISTED_ISLANDS} further cantilever "
                    f"region(s) not listed ({len(hits)} in total)"
                ),
                severity="error" if any(c.unreachable for c in hits) else "warn",
                measured=f"{len(hits)} regions",
            )
        )
    return out


def _mesh_findings(
    block: str, built: PrintMesh, pitch: float | None
) -> list[ValidationIssue]:
    """``floating_island`` (error, per island, at most
    :data:`MAX_LISTED_ISLANDS` listed + a count line) and ``mesh_cleanup``
    (info) for the mesh :func:`build_print_mesh` made — module docstring."""
    out: list[ValidationIssue] = []
    if pitch is not None:
        layer_mm = pitch * _MM_PER_M
        islands: list[Island] = []
        bed = _shared_bed(built)
        for _name, v, t in built.parts:
            islands.extend(floating_islands(v, t, layer_mm, bed_z=bed))
        total = len(islands)
        for i, isl in enumerate(islands[:MAX_LISTED_ISLANDS], start=1):
            out.append(_island_issue(block, isl, i, total, layer_mm))
        if total > MAX_LISTED_ISLANDS:
            out.append(
                ValidationIssue(
                    rule="floating_island",
                    subject=block,
                    detail=(
                        f"{total - MAX_LISTED_ISLANDS} further floating "
                        f"island(s) not listed ({total} in total)"
                    ),
                    severity="error",
                    measured=f"{total} islands",
                    expected=f"0 islands at {layer_mm:.3g} mm layers",
                )
            )
        try:
            out.extend(_cantilever_findings(block, built, layer_mm))
        except Exception as exc:  # the island findings above stand on their own
            out.append(
                ValidationIssue(
                    rule="slicer_cantilever",
                    subject=block,
                    detail=f"slicer-cantilever check skipped: {exc}",
                    severity="info",
                )
            )
    if built.flat_bed_skipped:
        out.append(
            ValidationIssue(
                rule="mesh_cleanup",
                subject=block,
                detail=(
                    "flat bed contact was not applied: "
                    f"{built.flat_bed_skipped} — fins and walls that stand on "
                    "the bed keep the rounded foot the voxel field gives them"
                ),
                severity="info",
            )
        )
    if built.flat_bed:
        out.append(
            ValidationIssue(
                rule="mesh_cleanup",
                subject=block,
                detail=f"flat bed contact applied: {built.flat_bed}",
                severity="info",
            )
        )
    for why in built.lift_abandoned:
        out.append(
            ValidationIssue(
                rule="mesh_cleanup",
                subject=block,
                detail=(
                    f"tail lift abandoned for {why} — the welded mesh ships "
                    "unlifted and the floating-region check ran on it"
                ),
                severity="info",
            )
        )
    if (
        built.n_dropped
        or built.n_slivers
        or built.n_lifted
        or built.n_kept_thin
        or built.n_kept_flip
    ):
        tol = "" if built.tol_mm is None else f" (tol {built.tol_mm:.3g} mm)"
        out.append(
            ValidationIssue(
                rule="mesh_cleanup",
                subject=block,
                detail=(
                    f"before export: {built.n_dropped} degenerate triangle(s) "
                    f"dropped on welding, {built.n_slivers} zero-area sliver(s) "
                    f"remain, {built.n_lifted} sub-layer tail vertex/vertices "
                    f"lifted{tol}; kept unlifted: {built.n_kept_thin} too thin, "
                    f"{built.n_kept_flip} would flip a face"
                ),
                severity="info",
                measured=(
                    f"{built.n_dropped} dropped / {built.n_slivers} slivers / "
                    f"{built.n_lifted} lifted"
                ),
            )
        )
    return out


def mesh_for_export(
    report: BlockPrintReport, fmt: str
) -> tuple[PrintMesh | None, list[ValidationIssue] | None]:
    """The mesh a ``fmt`` export of ``report`` must ship, and the findings
    that judged THAT mesh (``None`` = ``report.findings`` already did).

    The report judges one object per component (what 3MF ships). STL has no
    objects, so a multi-component block is welded into one body — and that
    union is what gets built here and judged again, never the per-component
    parts shipped as a union nobody checked."""
    built = report.print_mesh
    if (
        fmt == "stl"
        and built is not None
        and len(built.parts) > 1
        and report.printed is not None
        and report.chosen_down is not None
    ):
        welded = build_print_mesh(
            report.printed,
            report.chosen_down,
            pitch=report.pitch,
            source_pitch=report.source_pitch,
            welded=True,
            line_width=None
            if built.line_width_mm is None
            else built.line_width_mm / _MM_PER_M,
        )
        return welded, _mesh_findings(report.block, welded, report.pitch)
    return built, None


def write_mesh(
    printed: PrintedSolid,
    down: Vec3,
    fmt: str,
    out_path: str | Path,
    *,
    pitch: float | None = None,
    source_pitch: float | None = None,
    built: PrintMesh | None = None,
    title: str | None = None,
) -> Path:
    """Write ``printed``'s solid to ``out_path`` (``'stl'``/``'3mf'``) in
    the build frame — rotated so ``down`` is ``-z``, bed at ``z = 0``,
    millimetres. ``pitch`` (metres — the house ``layer_height``,
    :attr:`BlockPrintReport.pitch`) is the field-backend sample spacing
    for an ``rd``/``blend`` design; ``None`` takes cad's own default and a
    sharp design ignores it either way. ``source_pitch`` (metres,
    :attr:`BlockPrintReport.source_pitch`) is the solve pitch of a
    field-rooted block, widening the tail-lift tolerance
    (:func:`build_print_mesh`).

    Reuses the cad export seam exactly rather than re-scaling or
    re-tessellating (module docstring): :func:`build_print_mesh` mm-scales
    first, folds the already-mm design to its final triangle mesh with the
    same private helpers ``handlers/cad.py``'s own printability probe
    already reuses cross-module, *then* rotates that **finished** mesh into
    the build frame — never the node tree, which would have to re-derive
    the pattern/intersect fold semantics
    :func:`precis.cad.export._component_solids` already owns — and cleans it
    (weld, lift sub-layer tails). ``built`` (:attr:`BlockPrintReport.
    print_mesh`, same ``down``/``pitch``) is shipped as-is instead of
    rebuilding, so the file is byte-for-byte the mesh the report judged;
    STL reuses it only when it is a single part (STL has no parts).
    ``title`` is the 3MF ``Title`` metadata.

    Raises :class:`PrintUnsupported` when ``manifold3d`` is missing, and
    :class:`precis.cad.export.ExportError` on a malformed/empty fold —
    mirrors the cad kind's own export path exactly."""
    if not manifold_available():
        raise PrintUnsupported(
            "view='print' export needs the manifold3d backend (core "
            "dependency — a broken venv?)"
        )
    out = Path(out_path)
    if fmt == "stl":
        if built is None or len(built.parts) != 1:
            built = build_print_mesh(
                printed, down, pitch=pitch, source_pitch=source_pitch, welded=True
            )
        _name, verts, tris = built.parts[0]
        _write_binary_stl(out, verts, tris)
    elif fmt == "3mf":
        if built is None:
            built = build_print_mesh(
                printed, down, pitch=pitch, source_pitch=source_pitch
            )
        _write_3mf(out, built.parts, title=title)
    else:
        raise ExportError(f"unknown mesh format {fmt!r}; supported: stl, 3mf")
    return out


class NothingToExport(ValueError):
    """The block has no mesh to ship (unrealized, or a net-empty solid)."""


@dataclass
class MeshExport:
    """What :func:`export_block_mesh` wrote: the file, and the findings that
    judged the mesh in it (``report.findings``, except an STL of a
    multi-component block, whose welded union is judged afresh)."""

    path: Path
    findings: list[ValidationIssue]


def export_block_mesh(
    report: BlockPrintReport, fmt: str, out_path: str | Path, *, title: str
) -> MeshExport:
    """The one checked-export path for a single fdm block —
    ``view='print' fmt=`` and the web ``/se/{slug}/print/{block}.3mf``
    download both call it, so the file a browser gets and the file an
    agent gets cannot drift. ``report`` should come from
    ``report_for(..., mesh_checks=True)`` so the shipped mesh is the one
    the report judged. Raises :class:`NothingToExport`,
    :class:`PrintUnsupported`, or :class:`precis.cad.export.ExportError`."""
    if report.printed is None or report.chosen_down is None:
        raise NothingToExport(
            f"block {report.block!r} has nothing to export "
            f"({'unrealized' if report.printed is None else 'net-empty solid'})"
        )
    built, judged = mesh_for_export(report, fmt)
    path = write_mesh(
        report.printed,
        report.chosen_down,
        fmt,
        out_path,
        pitch=report.pitch,
        source_pitch=report.source_pitch,
        built=built,
        title=title,
    )
    shown = report.findings
    if judged is not None:
        # an STL of a multi-component block ships the welded union, whose
        # own floating-region findings replace the per-component ones
        shown = [f for f in shown if f.rule not in SHIPPED_MESH_RULES] + judged
    return MeshExport(path=path, findings=shown)
