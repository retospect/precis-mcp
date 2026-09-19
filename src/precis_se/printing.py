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
"""

from __future__ import annotations

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
from precis.cad.printability import BuildCandidate, rotate_to_frame
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


def _abstract_joint_findings(tree: SeTree, block: str) -> list[ValidationIssue]:
    """``abstract_joint`` (warn), filtered to the connects touching
    ``block`` — module docstring."""
    out: list[ValidationIssue] = []
    for connect, mechanism in se_fasten.abstract_joints(tree):
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
) -> list[ValidationIssue]:
    """The se-only findings that hold whatever frame the solid prints in —
    stamped-hole checks, ``min_feature``, ``abstract_joint`` (module
    docstring). Shared with :mod:`precis_se.printgroup`."""
    out = _hole_findings(node, block, se_fasten.features_for(tree, block), rules)
    mf = _min_feature_finding(block, printed, rules)
    if mf is not None:
        out.append(mf)
    out.extend(_abstract_joint_findings(tree, block))
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


def report_for(
    tree: SeTree, block: str, *, cad_store_reader: Store
) -> BlockPrintReport | None:
    """Everything ``view='print'`` needs for one block, or ``None`` when
    the block's resolved mode family isn't ``fdm`` at all — not this
    engine's block (``view='fab'`` covers it instead).

    Raises :class:`PrintUnsupported` when the ``manifold3d`` backend is
    missing and a solid needs tessellating (mirrors ``handlers/cad.py``'s
    own ``view='printability'`` gate) — never for a block with nothing to
    tessellate (``unrealized``/net-empty), which reports honestly with no
    backend needed at all."""
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
    )


def write_mesh(
    printed: PrintedSolid,
    down: Vec3,
    fmt: str,
    out_path: str | Path,
    *,
    pitch: float | None = None,
) -> Path:
    """Write ``printed``'s solid to ``out_path`` (``'stl'``/``'3mf'``) in
    the build frame — rotated so ``down`` is ``-z``, bed at ``z = 0``,
    millimetres. ``pitch`` (metres — the house ``layer_height``,
    :attr:`BlockPrintReport.pitch`) is the field-backend sample spacing
    for an ``rd``/``blend`` design; ``None`` takes cad's own default and a
    sharp design ignores it either way.

    Reuses the cad export seam exactly rather than re-scaling or
    re-tessellating (module docstring): mm-scale first
    (:func:`precis.cad.export._scaled_for_export` — never scale twice),
    fold the already-mm design to its final triangle mesh with the same
    private helpers ``handlers/cad.py``'s own printability probe already
    reuses cross-module (:func:`precis.cad.export._solid_mesh`/
    ``_component_meshes``), *then* rotate that **finished** mesh into the
    build frame (:func:`precis.cad.printability.rotate_to_frame`) — never
    the node tree, which would have to re-derive the pattern/intersect
    fold semantics :func:`precis.cad.export._component_solids` already
    owns.

    Raises :class:`PrintUnsupported` when ``manifold3d`` is missing, and
    :class:`precis.cad.export.ExportError` on a malformed/empty fold —
    mirrors the cad kind's own export path exactly."""
    if not manifold_available():
        raise PrintUnsupported(
            "view='print' export needs the manifold3d backend (core "
            "dependency — a broken venv?)"
        )
    out = Path(out_path)
    scaled = _scaled_for_export(printed.spec)
    mm_pitch = None if pitch is None else pitch * _MM_PER_M
    if fmt == "stl":
        verts, tris = _solid_mesh(scaled, pitch=mm_pitch)
        verts = rotate_to_frame(verts, down)
        _write_binary_stl(out, verts, tris)
    elif fmt == "3mf":
        parts = [
            (name, rotate_to_frame(v, down), t)
            for name, v, t in _component_meshes(scaled, pitch=mm_pitch)
        ]
        _write_3mf(out, parts)
    else:
        raise ExportError(f"unknown mesh format {fmt!r}; supported: stl, 3mf")
    return out
