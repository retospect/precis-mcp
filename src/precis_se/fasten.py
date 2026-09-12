"""Mechanism → geometry: what a `screw` joint does to the parts it joins
(``se-off-the-shelf-fabrication.md`` engine 2, rung 3).

Slice 3 made a mechanism *demand a relation*. It never made a mechanism
**change a part**, and off-the-shelf assembly is almost entirely that: a
screw is not a line in a diagram, it is a hole through every member it
passes, a length that either reaches or doesn't, and a thread that
converts turns into travel. This module is the first instance of the one
engine — a joint, given its mechanism and the posed geometry of the
blocks around it, says what features it stamps and whether the fastener
it names can actually do the job.

**The axis comes from the fastener, not from the declaration.** A screw
knows which way it points: the catalog envelope runs head-at-local-origin
to thread-end at ``+z`` (:mod:`precis_se.catalog`), so the drive
direction is the block's own rotation applied to ``+z`` and the ray
origin is its pose. A declared ``joint.axis`` is then something to
*check* against that, not something to trust — which is the whole point
of having both.

**The stack is walked, not assumed.** Members come from casting that ray
through the design (:func:`precis.cad.probe.probe_ray`, one probe per
posed envelope) and keeping the material intervals on the thread side of
the head. That is what makes the grip a measurement rather than a
guess — nobody has to declare the order of a stack that the poses
already state.

**Stamped features are derived and regenerable**, never stored: a
:class:`Hole` is recomputed from the connect on every read, named after
the connect that made it (the copper-derived rule the rest of the tree
follows). Nothing here writes.

**What is a hole and what isn't.** A member that is *bought* — a nut, a
washer, anything ``component``-bound — already has whatever hole it has;
you do not drill a nut. Only designed members get stamped, and the last
one in a nutless stack gets a **tapped** hole rather than a clearance
one, because that is what a screw with no nut means.

Deferred, named so they are not re-derived: tool access (a swept driver
envelope per drive type × size — rung 3b, it needs capability data);
assembly-order existence; edge distance (needs hole positions in a
member's outline, which arrives with the profile tier); counterbores and
countersinks (a head-form question, and the head forms are one flat
`fastener` category today); washers as load-spreaders in the stack-up
(they are members here, which is geometrically right and mechanically
silent); **a screw bottoming out in a blind hole** — the checks below are
all "is it long enough", never "is it too long", because nothing in the
tree says whether a tapped hole is blind or through, and warning on every
through-hole would train designers to ignore the finding; and the
**position-tolerance relation** each stamped hole should carry
(``se-feasibility-and-cost.md``: a joint stamps features *and the
relations that make them meaningful*) — blocked on the measure layer
having no derived-row concept, so :func:`_pattern_findings` reports the
slack in prose as a stopgap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis import fit_classes as core_fit
from precis.cad import probe as cad_probe
from precis.cad.graph import Design as CadDesign
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis.utils.units import format_quantity
from precis_se import catalog as se_catalog
from precis_se import joints as se_joints
from precis_se.ops import SeTree, effective_envelope
from precis_se.validate import ValidationIssue, _posed_component

#: The ray-span state that means material (``precis.cad.fold`` spells the
#: pair ``'solid' | 'void'``). Named rather than inlined because the miss
#: is silent: a wrong literal here yields an empty stack, which reads
#: exactly like a screw pointing into thin air.
_SOLID = "solid"

#: Material shorter than this along the axis is a graze, not a member —
#: two envelopes that merely touch at a face would otherwise each
#: contribute a zero-thickness "member" to the stack (metres).
#:
#: Deliberately kept absolute (units-policy-cutover.md item 4: "author-
#: stated tolerances accepted as ... absolute-with-unit, never silently
#: relativized"): a fastener stack is real catalog hardware — bolts,
#: washers, tapped holes — whose manufacturing tolerances are a physical
#: micron-scale fact independent of the *design's* drawing scale, not a
#: kernel numerical-degeneracy artifact like ``LINEAR_EPS`` was. Relative
#: to a nanometre-scale envelope this would demand implausible sub-Å
#: member thickness; that mismatch is real information (a screw joint at
#: that scale is not, physically, a machine screw), not a bug to paper
#: over by relativizing the threshold.
_MIN_MEMBER_M = 1e-6

#: Declared-vs-derived axis tolerance, radians (~2.5°, the same order the
#: DOF probe uses for principal-axis alignment, for the same reason: a
#: designer types round numbers, and a joint axis is a statement of
#: intent). Radians internal per the units-policy-cutover angle ruling —
#: this compares directly against :func:`_angle_rad`'s radian output.
_AXIS_TOL_RAD = math.radians(2.5)

#: Minimum thread engagement into a tapped member, in nominal diameters.
#: 1×D is the steel-into-steel rule of thumb; softer materials want more,
#: which is a material-capability refinement this does not yet have (so
#: the finding says "at least", never "exactly").
_MIN_ENGAGEMENT_D = 1.0

#: Threads of protrusion past a nut — the assembly convention that the
#: nut is fully engaged, expressed in pitches.
_PROTRUSION_PITCHES = 2.0

#: Relative disagreement tolerated between a declared ``params.lead`` and
#: the catalog pitch before it is a finding (1%: a declared 1.0 mm lead
#: against a 1.0 mm pitch must not fire on float noise, a declared 2 mm
#: against 1.0 must).
_LEAD_TOL_REL = 0.01


@dataclass(frozen=True)
class Member:
    """One block the fastener's axis passes through, on the thread side of
    the head. ``t_in``/``t_out`` are metres along the axis from the head
    bearing face; ``thickness_m`` is the material between them."""

    block: str
    t_in: float
    t_out: float
    thickness_m: float
    #: catalog form when the member is a bought fastener ('nut',
    #: 'washer', 'screw'), else None — a nut terminates a stack.
    form: str | None = None
    #: True when the member is `component`/`part`-bound: bought, so it
    #: comes with its own holes and must not be stamped.
    bought: bool = False


@dataclass(frozen=True)
class Hole:
    """A feature this joint stamps into one member. **Derived**: named
    after the connect that made it, recomputed on every read, stored
    nowhere. ``kind`` is ``'clearance'`` (the screw passes through) or
    ``'tapped'`` (the screw threads into it)."""

    name: str
    block: str
    kind: str
    diameter_m: float
    depth_m: float
    origin: list[float]
    axis: list[float]


@dataclass(frozen=True)
class Thread:
    """The thread as a lead screw: **distance per turn, with limits**.

    ``lead_m`` is the axial travel of one full revolution (pitch × starts
    — single-start for every series in the catalog today, which is why
    ``starts`` is carried explicitly rather than assumed away).
    ``engagement_m`` is the limit: how much thread is actually in the nut
    or tapped hole, so ``turns`` is the number of revolutions from first
    engagement to seated. Both are ``None`` when the stack could not be
    walked — a lead with no limits is half the fact.

    Everything here is *derived from the catalog row*. What the joint
    declared lives on :class:`FastenResult` instead, so the two can be
    compared rather than merged."""

    pitch_m: float
    starts: int
    lead_m: float
    engagement_m: float | None = None
    turns: float | None = None


@dataclass
class FastenResult:
    """One screw joint's propagation. ``why_not`` set means the geometry
    could not be derived at all and every geometric field is empty — the
    same absence-is-reported contract the catalog generators keep."""

    subject: str
    klass: str
    mechanism: str | None
    fastener: str | None = None
    component: str | None = None
    thread_size: str | None = None
    #: the class the joint asked for (``params.fit_class``), or None for
    #: the house default — recorded so a view can say which it was.
    fit_class: str | None = None
    fit: core_fit.Fit | None = None
    #: ``params.lead``, metres per revolution, exactly as declared.
    declared_lead_m: float | None = None
    #: 'nut' (a nut terminates the stack) | 'tapped' (the last member is
    #: threaded) | None (no stack walked).
    termination: str | None = None
    members: list[Member] = field(default_factory=list)
    #: clamped material — everything before the terminating member.
    grip_m: float | None = None
    #: all material on the axis, including the nut or tapped member.
    stack_m: float | None = None
    length_m: float | None = None
    required_length_m: float | None = None
    thread: Thread | None = None
    holes: list[Hole] = field(default_factory=list)
    findings: list[ValidationIssue] = field(default_factory=list)
    why_not: str | None = None


def _specs(node: Any) -> dict[str, Any]:
    """A block's catalog spec set, or ``{}`` — total over unbound blocks,
    fake stores and pre-rung-2b trees alike."""
    specs = getattr(getattr(node, "derived", None), "specs", None)
    return specs if isinstance(specs, dict) else {}


def _num(specs: dict[str, Any], key: str) -> float | None:
    """One positive finite spec value, else ``None``."""
    try:
        v = float(specs.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0.0 else None


def _form(tree: SeTree, name: str) -> str | None:
    node = tree.blocks.get(name)
    if node is None or node.bound_kind != "component":
        return None
    return se_catalog.fastener_form(_specs(node))


def _drive_axis(node: Any) -> tuple[list[float], list[float]]:
    """``(origin, direction)`` of the screw's own axis, world frame: the
    head bearing face and the head→thread direction.

    Read off the block's pose, because the catalog envelope's local frame
    already fixes it (module docstring) — no declaration is consulted, so
    a joint that declares the axis backwards is a *finding* rather than a
    stack walked in the wrong direction."""
    xform = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
    direction = xform.apply_dir(np.array([0.0, 0.0, 1.0]))
    return [float(v) for v in node.pose], [float(v) for v in direction]


def _angle_rad(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return math.acos(max(-1.0, min(1.0, dot)))


def _walk_axis(
    tree: SeTree, fastener: str, origin: list[float], axis: list[float]
) -> list[Member]:
    """Every block whose material the fastener's axis crosses on the
    thread side of the head, in axis order.

    One posed design, one ray, one probe per component — the probe is
    analytic interval arithmetic, so this is cheap next to the DOF
    probe's contact scan. Material behind the head (``t < 0``) is not a
    member: the screw does not pass through it."""
    design = CadDesign()
    posed: dict[str, Any] = {}
    for name, node in sorted(tree.blocks.items()):
        if name == fastener:
            continue
        env = effective_envelope(tree, node)
        if not env:
            continue
        expr = _posed_component(design, name, env, node)
        if expr is not None:
            posed[name] = expr
    members: list[Member] = []
    for name in sorted(posed):
        try:
            result = cad_probe.probe_ray(
                design, cad_as_vec3(origin), cad_as_vec3(axis), component=name
            )
        except (ValueError, ArithmeticError):  # pragma: no cover — defensive
            continue
        for seg in result.segments:
            if seg.state != _SOLID:
                continue
            t_in = max(float(seg.t_in), 0.0)
            t_out = float(seg.t_out)
            if not math.isfinite(t_out) or t_out - t_in <= _MIN_MEMBER_M:
                continue
            node = tree.blocks[name]
            members.append(
                Member(
                    block=name,
                    t_in=t_in,
                    t_out=t_out,
                    thickness_m=t_out - t_in,
                    form=_form(tree, name),
                    bought=node.bound_kind in ("component", "part")
                    and bool(node.bound),
                )
            )
    members.sort(key=lambda m: (m.t_in, m.block))
    return members


def _find_fastener(tree: SeTree, connect: Any) -> str | None:
    """The screw among a connect's endpoints, or ``None``.

    Endpoints only, deliberately: a BOM line names *which* fastener but
    not *where* it is, and every number below is positional. Reporting
    that gap by name beats inventing a pose."""
    for name in sorted({connect.a_block, connect.b_block}):
        if _form(tree, name) == "screw":
            return name
    return None


def _thread(specs: dict[str, Any]) -> Thread | None:
    """The catalog thread. ``starts`` is explicit rather than folded into
    the lead because every series in the catalog today is single-start —
    an assumption worth being able to see, and to change in one place."""
    pitch = _num(specs, "thread_pitch")
    if pitch is None:
        return None
    starts = 1
    return Thread(pitch_m=pitch, starts=starts, lead_m=pitch * starts)


def _hole_for(
    member: Member,
    *,
    subject: str,
    kind: str,
    diameter_m: float,
    depth_m: float,
    origin: list[float],
    axis: list[float],
) -> Hole:
    start = [o + member.t_in * a for o, a in zip(origin, axis, strict=True)]
    return Hole(
        name=f"{subject}#{member.block}.{kind}",
        block=member.block,
        kind=kind,
        diameter_m=diameter_m,
        depth_m=depth_m,
        origin=[float(v) for v in start],
        axis=[float(v) for v in axis],
    )


def _one(tree: SeTree, connect: Any, joint: dict[str, Any]) -> FastenResult:
    subject = f"{connect.a_block}.{connect.a_port}—{connect.b_block}.{connect.b_port}"
    params = joint.get("params") or {}
    res = FastenResult(
        subject=subject,
        klass=str(joint["class"]),
        mechanism=joint.get("mechanism"),
        fit_class=params.get("fit_class"),
        declared_lead_m=(
            float(params["lead"]) if params.get("lead") is not None else None
        ),
    )
    fastener = _find_fastener(tree, connect)
    if fastener is None:
        res.why_not = (
            "neither endpoint is a block bound to a screw-form `component` "
            "— the stack-up is positional, so the fastener has to be placed "
            "as a block (a BOM line names which screw, not where it is): "
            "add_block + set_binding(kind='component')"
        )
        return res
    node = tree.blocks[fastener]
    specs = _specs(node)
    res.fastener = fastener
    res.component = node.bound
    res.thread_size = (
        str(specs["thread_size"]) if specs.get("thread_size") is not None else None
    )
    res.length_m = _num(specs, "length")
    res.thread = _thread(specs)

    origin, axis = _drive_axis(node)
    declared_axis = joint.get("axis")
    if declared_axis is not None:
        off = _angle_rad([float(v) for v in declared_axis], axis)
        # 180° apart is the same line, and a joint axis is a line: only
        # flag a genuine misalignment, not a sign convention.
        off = min(off, math.pi - off)
        if off > _AXIS_TOL_RAD:
            res.findings.append(
                ValidationIssue(
                    rule="fastener_axis",
                    subject=subject,
                    detail=(
                        f"the joint declares an axis {format_quantity(off, 'angle')} "
                        f"away from {fastener!r}'s own head→thread direction — the "
                        "stack-up follows the screw's pose; set_joint's axis "
                        "or the block's rot is wrong"
                    ),
                    severity="warn",
                )
            )
    res.members = _walk_axis(tree, fastener, origin, axis)
    if not res.members:
        res.why_not = (
            f"{fastener!r}'s axis passes through nothing — no other block's "
            "envelope lies on the thread side of its head (check its pose "
            "and rot; the screw drives along its own +z)"
        )
        return res

    last = res.members[-1]
    res.termination = "nut" if last.form == "nut" else "tapped"
    res.stack_m = sum(m.thickness_m for m in res.members)
    res.grip_m = res.stack_m - last.thickness_m

    nominal = _num(specs, "outer_diameter")
    pitch = res.thread.pitch_m if res.thread else None
    if res.termination == "nut" and pitch is not None:
        res.required_length_m = res.stack_m + _PROTRUSION_PITCHES * pitch
    elif res.termination == "tapped" and nominal is not None:
        res.required_length_m = res.grip_m + _MIN_ENGAGEMENT_D * nominal

    if res.required_length_m is not None and res.length_m is not None:
        engagement = (
            last.thickness_m
            if res.termination == "nut"
            else max(0.0, min(res.length_m - res.grip_m, last.thickness_m))
        )
        if res.thread is not None:
            res.thread = Thread(
                pitch_m=res.thread.pitch_m,
                starts=res.thread.starts,
                lead_m=res.thread.lead_m,
                engagement_m=engagement,
                turns=engagement / res.thread.lead_m,
            )
        if res.length_m < res.required_length_m:
            short = res.required_length_m - res.length_m
            need = (
                f"{res.stack_m * 1000:.1f} mm of stack + "
                f"{_PROTRUSION_PITCHES:g} threads of protrusion past the nut"
                if res.termination == "nut"
                else f"{res.grip_m * 1000:.1f} mm of grip + "
                f"{_MIN_ENGAGEMENT_D:g}×D of thread engagement"
            )
            res.findings.append(
                ValidationIssue(
                    rule="screw_too_short",
                    subject=subject,
                    detail=(
                        f"{res.component or fastener} is "
                        f"{res.length_m * 1000:.1f} mm under the head but "
                        f"this joint needs {res.required_length_m * 1000:.1f} "
                        f"mm ({need}) — {short * 1000:.1f} mm short"
                    ),
                    severity="warn",
                )
            )
    if (
        res.termination == "tapped"
        and nominal is not None
        and last.thickness_m < _MIN_ENGAGEMENT_D * nominal
    ):
        res.findings.append(
            ValidationIssue(
                rule="thread_engagement",
                subject=subject,
                detail=(
                    f"the tapped member {last.block!r} is only "
                    f"{last.thickness_m * 1000:.1f} mm thick, less than the "
                    f"{_MIN_ENGAGEMENT_D:g}×D ({nominal * 1000:.1f} mm) of "
                    "engagement a threaded joint wants — use a nut, a "
                    "longer boss, or a thread insert"
                ),
                severity="warn",
            )
        )

    if res.thread is not None and res.declared_lead_m is not None:
        declared = res.declared_lead_m
        derived = res.thread.lead_m
        if abs(declared - derived) > _LEAD_TOL_REL * derived:
            res.findings.append(
                ValidationIssue(
                    rule="lead_disagreement",
                    subject=subject,
                    detail=(
                        f"the joint declares a lead of {declared * 1000:.3f} "
                        f"mm/turn but {res.component or fastener}'s thread "
                        f"pitch is {derived * 1000:.3f} mm — one of the two "
                        "is wrong (a multi-start thread would explain a "
                        "whole multiple; nothing else does)"
                    ),
                    severity="warn",
                )
            )

    _stamp(res, subject=subject, origin=origin, axis=axis, specs=specs)
    return res


def _stamp(
    res: FastenResult,
    *,
    subject: str,
    origin: list[float],
    axis: list[float],
    specs: dict[str, Any],
) -> None:
    """Emit the holes into ``res`` — clearance through every designed
    member the screw passes, and a tapped hole in the terminal one when
    no nut ends the stack."""
    if res.thread_size is None:
        res.findings.append(
            ValidationIssue(
                rule="fit_unresolved",
                subject=subject,
                detail=(
                    f"{res.component or res.fastener} has no thread_size "
                    "spec, so no clearance hole can be sized — nothing was "
                    "stamped (mint it from a series, or set the spec)"
                ),
                severity="warn",
            )
        )
        return
    fit = core_fit.clearance_hole(res.thread_size, res.fit_class)
    if fit is None:
        res.findings.append(
            ValidationIssue(
                rule="fit_unresolved",
                subject=subject,
                detail=(
                    f"no clearance-hole table row for thread size "
                    f"{res.thread_size!r} (have: "
                    f"{', '.join(core_fit.sizes())}) — nothing was stamped "
                    "rather than a guessed diameter"
                ),
                severity="warn",
            )
        )
        return
    res.fit = fit
    pitch = res.thread.pitch_m if res.thread else None
    nominal = _num(specs, "outer_diameter")
    holes: list[Hole] = []
    for member in res.members:
        if member.bought:
            continue  # a bought part comes with its own hole
        terminal = member is res.members[-1] and res.termination == "tapped"
        if terminal and pitch is not None and nominal is not None:
            # The tapping drill for a metric thread is d − P (~100% thread
            # form) — the one number that is genuinely standard here, and
            # skipped entirely when either input is absent.
            holes.append(
                _hole_for(
                    member,
                    subject=subject,
                    kind="tapped",
                    diameter_m=nominal - pitch,
                    depth_m=member.thickness_m,
                    origin=origin,
                    axis=axis,
                )
            )
            continue
        holes.append(
            _hole_for(
                member,
                subject=subject,
                kind="clearance",
                diameter_m=fit.hole_m,
                depth_m=member.thickness_m,
                origin=origin,
                axis=axis,
            )
        )
    res.holes = holes


def _pattern_findings(results: list[FastenResult]) -> None:
    """The cross-joint check: two or more clearance holes of the same fit
    in one member is a *pattern*, and a pattern binds on position error
    long before one hole does.

    Reto's house rule (``d + 0.2``) is tighter than ISO 273 fine, so this
    is exactly where it bites: 0.1 mm of radial slack per hole. Said once
    per (member, fit) rather than per hole — the designer has one decision
    to make, not N — and homed on the first joint that stamped into that
    member, so it hangs off something that caused it rather than off
    whichever result happened to sort first."""
    by_member: dict[tuple[str, str], list[FastenResult]] = {}
    for res in results:
        if res.fit is None:
            continue
        for hole in res.holes:
            if hole.kind != "clearance":
                continue
            hits = by_member.setdefault((hole.block, res.fit.fit_class), [])
            # identity, not equality: two joints with identical numbers
            # are still two holes, and `FastenResult` compares by value.
            if not any(h is res for h in hits):
                hits.append(res)
    for (block, fit_class), hits in sorted(by_member.items()):
        if len(hits) < 2:
            continue
        fit = next(r.fit for r in hits if r.fit is not None)
        hits[0].findings.append(
            ValidationIssue(
                rule="hole_pattern_tolerance",
                subject=block,
                detail=(
                    f"{len(hits)} clearance holes in {block!r} at fit "
                    f"{fit_class!r} — {fit.radial_slack_mm:.2f} mm of radial "
                    "slack each, so the pattern binds on a position error "
                    "that size. Either hold the hole positions to it, or "
                    "declare a looser fit_class on the joints "
                    f"({' | '.join(sorted(core_fit.classes()))})"
                ),
                severity="warn",
            )
        )


def fasten(tree: SeTree) -> list[FastenResult]:
    """Every screw joint in ``tree``, propagated. Pure — reads the loaded
    tree (including its catalog-derived facets) and nothing else.

    Both registries are in scope, and they meet here: the `screw`
    **mechanism** (threaded fastening — holes, grip, length) and the
    `screw` **kinematic class** (the helical pair — lead and travel
    limits). A joint that declares either is analysed; one that declares
    both gets both, and the declared lead is checked against the
    fastener's actual pitch.

    Total: a malformed stored joint is skipped here (it is already
    :mod:`precis_se.drc`'s ``malformed_joint`` finding) and every other
    gap becomes ``why_not`` on its own result."""
    results: list[FastenResult] = []
    for connect in tree.connects:
        if not connect.joint:
            continue
        try:
            joint = se_joints.validate_joint(connect.joint)
        except se_joints.JointError:
            continue
        if joint.get("mechanism") != "screw" and joint["class"] != "screw":
            continue
        results.append(_one(tree, connect, joint))
    results.sort(key=lambda r: r.subject)
    _pattern_findings(results)
    return results


def findings(results: list[FastenResult]) -> list[ValidationIssue]:
    """Flatten the pass into DRC's finding list."""
    return [f for res in results for f in res.findings]
