"""Graph-tier design DRC over a loaded :class:`~precis_se.ops.SeTree` —
the pre-geometry checks (se-kind.md L4 "Design DRC, graph tier") backing
``view='drc'``, plus the two L2→L4 evaluations that need only envelopes:
tolerance **stack-up** (:mod:`precis_se.measures`) and the
**declared-vs-derived DOF probe** (renting
:func:`precis.cad.relate.translational_dof` as-is).

Like :mod:`precis_se.validate` this is a read-time re-check over stored
data, never the op-time gate restated — a pre-slice-3 free-form joint, a
hand-corrected row, or a relation whose source got removed must surface
as a *finding*, never a crash. And per suggestive-by-contract, absence
stays warn-tier at most; **error** is reserved for declarations that are
structurally unintelligible or self-contradictory (unknown kinematic
class, rigid-vs-moving contradiction, unresolvable/cyclic relation,
measure on a missing block).

The DOF probe is deliberately modest (spec: "the rotational probe is its
missing twin … may slip a slice" — it did): only ``revolute`` (axis travel
expected *bounded* — a pin that can slide out of its bore axially is a
finding) and ``prismatic``/``cylindrical``/``screw`` (axis travel expected
*free* — a slide blocked at zero travel is a finding, and one unbounded in
*both* directions has no end stop, the same slides-off physics), and only
when the declared axis aligns with a principal axis (±x/y/z —
``translational_dof`` probes exactly those; an off-principal axis is
reported as skipped, honestly, rather than approximated). Advisory
warn-tier throughout: envelope geometry legitimately understates a joint
(circlips, shoulders and threads live at L3).
"""

from __future__ import annotations

from dataclasses import dataclass

from precis.cad import bulk as cad_bulk
from precis.cad import relate as cad_relate
from precis.cad.graph import Design as CadDesign
from precis_se import joints as se_joints
from precis_se import modes as se_modes
from precis_se.measures import StackupResult, stackup
from precis_se.ops import SeTree, effective_envelope
from precis_se.validate import ValidationIssue, _posed_component

#: kinematic classes the axis-travel probe covers, with the expectation.
_PROBE_BOUNDED = frozenset({"revolute"})
_PROBE_FREE = frozenset({"prismatic", "cylindrical", "screw"})

#: classes that permit relative motion — a ``rigid`` connect between the
#: same block pair contradicts any of these (the spec's "a block both
#: fixed and revolute to the same partner"). ``compliant`` counts: motion
#: with stiffness is still motion (reviewer finding). ``captive`` is
#: deliberately left out — it states topology (interlocked), not a motion
#: posture; rigid+captive is redundant, not contradictory.
_MOVING_CLASSES = frozenset(
    {"revolute", "prismatic", "cylindrical", "screw", "planar", "ball", "compliant"}
)

#: A measure value this many times the whole design's posed extent is
#: almost certainly a unit slip (15 "metres" of axle on a 0.25 m design =
#: millimetres entered as metres — the single most common agent error).
_MAGNITUDE_FACTOR = 10.0


@dataclass
class DofProbe:
    """One axis-travel probe outcome (or an honest skip), rendered in the
    ``view='drc'`` report next to any finding it produced."""

    subject: str  # 'a.port—b.port'
    klass: str
    outcome: str


@dataclass
class DrcReport:
    findings: list[ValidationIssue]
    stackup: list[StackupResult]
    dof_probes: list[DofProbe]


def _resolved_block(tree: SeTree, name: str) -> str:
    """A connect endpoint's *measure-owning* block: itself, or — for an
    instance/array — its template (measures, like ports, live on ordinary
    blocks only)."""
    node = tree.blocks.get(name)
    if node is not None and node.template is not None:
        return node.template
    return name


def _principal_axis(axis: list[float]) -> str | None:
    """``'+z'``-style label when ``axis`` (unit) aligns with a principal
    axis within ~2.5°, else ``None`` (probe skipped)."""
    for i, label in enumerate(("x", "y", "z")):
        if axis[i] >= 0.999:
            return f"+{label}"
        if axis[i] <= -0.999:
            return f"-{label}"
    return None


def _flip(direction: str) -> str:
    return ("-" if direction[0] == "+" else "+") + direction[1]


def _design_extent(tree: SeTree) -> float:
    """Diagonal of the union AABB over every posed envelope, metres — the
    design's characteristic size for the unit-slip advisory. 0.0 (callers
    skip the advisory, honestly) when no block has a boundable envelope,
    or when the tree has array nodes — ``_posed_component`` poses an
    array at its single own pose, so the extent would understate an
    array-dominated footprint and turn legitimate measures into
    false unit-slip warnings (reviewer finding)."""
    if any(node.array for node in tree.blocks.values()):
        return 0.0
    design = CadDesign()
    lo: list[float] | None = None
    hi: list[float] | None = None
    for name, node in sorted(tree.blocks.items()):
        env = effective_envelope(tree, node)
        if not env:
            continue
        expr = _posed_component(design, name, env, node)
        if expr is None:
            continue
        try:
            raw_lo, raw_hi = cad_bulk.expr_aabb(design, expr)
        except ValueError:
            # unbounded envelope (a bare half-space chamfer) — no finite
            # box to contribute; skip it like the sibling AABB consumers
            # (reviewer finding: this must never crash the drc() read).
            continue
        elo = [float(v) for v in raw_lo]
        ehi = [float(v) for v in raw_hi]
        lo = elo if lo is None else [min(a, b) for a, b in zip(lo, elo, strict=True)]
        hi = ehi if hi is None else [max(a, b) for a, b in zip(hi, ehi, strict=True)]
    if lo is None or hi is None:
        return 0.0
    return float(sum((h - x) ** 2 for h, x in zip(hi, lo, strict=True)) ** 0.5)


def drc(tree: SeTree) -> DrcReport:
    """Run every graph-tier check + stack-up + the DOF probe. Pure over
    ``tree`` (plus its loaded measures); no store access."""
    findings: list[ValidationIssue] = []

    # 1. stored joints re-checked through the one schema (op-time gates new
    # writes; pre-slice-3 rows and hand corrections land here).
    classes_by_pair: dict[frozenset[str], set[str]] = {}
    for c in tree.connects:
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        if c.joint is None:
            continue
        try:
            joint = se_joints.validate_joint(c.joint)
        except se_joints.JointError as exc:
            findings.append(
                ValidationIssue(
                    rule="malformed_joint",
                    subject=subject,
                    detail=f"stored joint does not fit the schema: {exc} — "
                    "set_joint to repair it",
                    severity="error",
                )
            )
            continue
        classes_by_pair.setdefault(frozenset({c.a_block, c.b_block}), set()).add(
            joint["class"]
        )

    # 2. rigid-vs-moving contradiction between the same block pair.
    for pair, classes in sorted(classes_by_pair.items(), key=lambda kv: sorted(kv[0])):
        moving = sorted(classes & _MOVING_CLASSES)
        if "rigid" in classes and moving:
            a, *rest = sorted(pair)
            b = rest[0] if rest else a
            findings.append(
                ValidationIssue(
                    rule="joint_contradiction",
                    subject=f"{a}—{b}",
                    detail=(
                        f"connect(s) declare both 'rigid' and "
                        f"{'/'.join(moving)} between the same two blocks — "
                        "a joint cannot forbid and permit motion at once"
                    ),
                    severity="error",
                )
            )

    # 3. mechanism-implied demands (joints.MECHANISMS): a mechanism that
    # demands a tolerance relation needs a live measure relation linking
    # the two endpoint blocks (in either direction).
    related_pairs: set[frozenset[str]] = set()
    for m in tree.measures:
        if m.relation is not None:
            src_block = str(m.relation.get("source", "")).rpartition(".")[0]
            if src_block:
                related_pairs.add(frozenset({m.block, src_block}))
    for c in tree.connects:
        if not c.joint:
            continue
        mech = c.joint.get("mechanism")
        spec = se_joints.MECHANISMS.get(mech) if isinstance(mech, str) else None
        if spec is None or spec.get("demands_relation") is None:
            continue
        a_res = _resolved_block(tree, c.a_block)
        b_res = _resolved_block(tree, c.b_block)
        if frozenset({a_res, b_res}) not in related_pairs:
            findings.append(
                ValidationIssue(
                    rule="mechanism_demand",
                    subject=f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}",
                    detail=(
                        f"mechanism {mech!r} implies {spec['demands_relation']} "
                        f"between measures of {a_res!r} and {b_res!r} — none "
                        "declared (add_measure with a relation)"
                    ),
                    severity="warn",
                )
            )

    # 3b. mechanism-implied BOM demands (live since se_bom): a mechanism
    # realized by a bought thing needs a line saying which one — on the
    # connect itself, or on either endpoint block (a designer may well
    # hang "2 bearings" on the hub rather than on the joint; both state
    # the same purchase, so either satisfies the demand).
    bom_targets: set[str] = set()
    block_bom_targets: set[str] = set()
    for line in tree.bom:
        if line.block is not None:
            bom_targets.add(line.block)
            block_bom_targets.add(line.block)
        else:
            bom_targets.add(str(line.a_block))
            bom_targets.add(str(line.b_block))
    for c in tree.connects:
        if not c.joint:
            continue
        mech = c.joint.get("mechanism")
        spec = se_joints.MECHANISMS.get(mech) if isinstance(mech, str) else None
        if spec is None or spec.get("demands_bom") is None:
            continue
        if {c.a_block, c.b_block} & bom_targets:
            continue
        findings.append(
            ValidationIssue(
                rule="mechanism_bom",
                subject=f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}",
                detail=(
                    f"mechanism {mech!r} is realized by a bought part "
                    f"({spec['demands_bom']}) — nothing is on the BOM for "
                    "this joint or its blocks (add_bom with the component)"
                ),
                severity="warn",
            )
        )

    # 3c. a mode whose realization *is* a bought thing (purchase,
    # stock-cut) needs one: a component/part binding, or a BOM line on the
    # block. Assigning the mode is the claim; this is the receipt.
    for name, block in sorted(tree.blocks.items()):
        family = se_modes.family_of(block.mode)
        if block.mode and family is None:
            findings.append(
                ValidationIssue(
                    rule="unknown_mode",
                    subject=name,
                    detail=(
                        f"stored mode {block.mode!r} has no known family — "
                        f"known: {' | '.join(se_modes.MODE_FAMILIES)} "
                        "(set_mode to repair)"
                    ),
                    severity="error",
                )
            )
            continue
        if family is None or not family.demands_item:
            continue
        # Only a line hung on the block *itself* counts here: a bearing
        # bought for a joint this block happens to sit on says nothing
        # about what the block itself is.
        bound = block.bound_kind in ("component", "part") and bool(block.bound)
        if bound or name in block_bom_targets:
            continue
        findings.append(
            ValidationIssue(
                rule="mode_without_item",
                subject=name,
                detail=(
                    f"mode {block.mode!r} means this block is bought, not "
                    "made — but it names nothing to buy (set_binding to a "
                    "component/part, or add_bom)"
                ),
                severity="warn",
            )
        )

    # 4. stored objectives re-checked — an unregistered key is a
    # declared-but-unchecked facet (warn, the annotations contract-class
    # posture; op-time rejects new ones).
    carriers: list[tuple[str, dict]] = [
        (name, node.objectives)
        for name, node in sorted(tree.blocks.items())
        if node.objectives
    ]
    carriers += [
        (f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}", c.objectives)
        for c in tree.connects
        if c.objectives
    ]
    for subject, objectives in carriers:
        strays = sorted(set(objectives) - set(se_joints.OBJECTIVE_KEYS))
        if strays:
            findings.append(
                ValidationIssue(
                    rule="unchecked_objective",
                    subject=subject,
                    detail=(
                        f"objective key(s) {', '.join(strays)} are not in the "
                        "registered loads vocabulary — stored but consumed by "
                        "nothing (set_load rejects new ones; re-set to repair)"
                    ),
                    severity="warn",
                )
            )

    # 5. measures graph: a measure on a block that doesn't exist, and the
    # stack-up problems (dangling/cyclic relation = error; declared-vs-
    # derived mismatch = warn — the numbers disagree, the graph is intact).
    for m in tree.measures:
        if _resolved_block(tree, m.block) not in tree.blocks:
            findings.append(
                ValidationIssue(
                    rule="measure_on_missing_block",
                    subject=f"{m.block}.{m.name}",
                    detail=f"measure names block {m.block!r}, which does not "
                    "exist — remove_measure or restore the block",
                    severity="error",
                )
            )

    # 5b. unit-slip advisory: a declared value dwarfing the whole posed
    # design is millimetres-as-metres until proven otherwise. Honest skip
    # (no finding either way) when nothing has an envelope to scale by.
    extent = _design_extent(tree)
    if extent > 0.0:
        for m in tree.measures:
            if m.value is not None and abs(m.value) > _MAGNITUDE_FACTOR * extent:
                findings.append(
                    ValidationIssue(
                        rule="implausible_magnitude",
                        subject=f"{m.block}.{m.name}",
                        detail=(
                            f"value {m.value:g} m is ~{abs(m.value) / extent:.0f}× "
                            f"the whole design's posed extent ({extent:g} m) — "
                            "millimetres entered as metres? (measures are "
                            "metres; set_measure to repair)"
                        ),
                        severity="warn",
                    )
                )
    stack = stackup(tree.measures)
    _STACK_RULES = {
        "mismatch": ("tolerance_mismatch", "warn"),
        "malformed": ("malformed_relation", "error"),
    }
    for res in stack:
        if res.problem is None:
            continue
        rule, severity = _STACK_RULES.get(
            res.problem_kind or "", ("unresolvable_relation", "error")
        )
        findings.append(
            ValidationIssue(
                rule=rule,
                subject=res.measure,
                detail=res.problem,
                severity=severity,
            )
        )

    # 6. declared-vs-derived DOF probe (module docstring scope).
    probes: list[DofProbe] = []
    for c in tree.connects:
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        if not c.joint:
            continue
        try:
            joint = se_joints.validate_joint(c.joint)
        except se_joints.JointError:
            continue  # already a malformed_joint finding above
        klass = joint["class"]
        if klass not in (_PROBE_BOUNDED | _PROBE_FREE):
            continue
        axis = joint.get("axis")
        if axis is None:
            probes.append(
                DofProbe(subject, klass, "skipped — no axis declared (set_joint)")
            )
            continue
        if c.a_block == c.b_block:
            probes.append(DofProbe(subject, klass, "skipped — same block"))
            continue
        direction = _principal_axis(axis)
        if direction is None:
            probes.append(
                DofProbe(
                    subject,
                    klass,
                    "skipped — axis is not principal-aligned (±x/y/z); the "
                    "arbitrary-direction probe is a later kernel increment",
                )
            )
            continue
        design = CadDesign()
        posed = []
        for name in (c.a_block, c.b_block):
            node = tree.blocks.get(name)
            env = effective_envelope(tree, node) if node is not None else None
            posed.append(
                _posed_component(design, name, env, node)
                if node is not None and env
                else None
            )
        if posed[0] is None or posed[1] is None:
            probes.append(
                DofProbe(subject, klass, "skipped — both blocks need envelopes")
            )
            continue
        result = cad_relate.translational_dof(
            design,
            moving=c.b_block,
            fixed=c.a_block,
            tol=1e-4,
            # only the declared axis is read — the other four directions
            # each cost a full contact scan (the 104 s leadscrew tree).
            dirs=(direction, _flip(direction)),
        )
        fwd = result.travel.get(direction, 0.0)
        back = result.travel.get(_flip(direction), 0.0)
        travel_txt = f"travel {direction}={fwd:g} {_flip(direction)}={back:g} m"
        if klass in _PROBE_BOUNDED and (fwd == float("inf") or back == float("inf")):
            probes.append(DofProbe(subject, klass, f"FINDING — {travel_txt}"))
            findings.append(
                ValidationIssue(
                    rule="dof_disagreement",
                    subject=subject,
                    detail=(
                        f"declared {klass!r} but the envelopes leave axis "
                        f"translation unbounded ({travel_txt}) — the geometry "
                        "does not yet constrain what the joint declares "
                        "(advisory: retention features often live at L3)"
                    ),
                    severity="warn",
                )
            )
        elif klass in _PROBE_FREE and fwd == 0.0 and back == 0.0:
            probes.append(DofProbe(subject, klass, f"FINDING — {travel_txt}"))
            findings.append(
                ValidationIssue(
                    rule="dof_disagreement",
                    subject=subject,
                    detail=(
                        f"declared {klass!r} but the envelopes block axis "
                        f"translation at zero travel ({travel_txt}) — the "
                        "declared slide cannot move"
                    ),
                    severity="warn",
                )
            )
        elif klass in _PROBE_FREE and fwd == float("inf") and back == float("inf"):
            # same slides-off-the-end physics the revolute check catches —
            # a slide with no end stop in either direction (the rack
            # carriage that sails off a finite rail).
            probes.append(DofProbe(subject, klass, f"FINDING — {travel_txt}"))
            findings.append(
                ValidationIssue(
                    rule="dof_disagreement",
                    subject=subject,
                    detail=(
                        f"declared {klass!r} but nothing limits the stroke in "
                        f"either direction ({travel_txt}) — the slide can "
                        "leave its rail (advisory: end stops often live at L3)"
                    ),
                    severity="warn",
                )
            )
        else:
            probes.append(DofProbe(subject, klass, f"ok — {travel_txt}"))

    return DrcReport(findings=findings, stackup=stack, dof_probes=probes)
