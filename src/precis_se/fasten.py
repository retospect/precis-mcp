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
one in a nutless stack gets whatever its **thread strategy** says. And the
walk itself has an end: the connect's other endpoint is the last member
the joint owns (a trailing nut aside) — whatever the ray grazes beyond it
is not this joint's business.

**The far end is a declared choice, not a default** (rung 3c). Rung 3a
gave the terminal member a cut thread (``d − P``) whatever it was made
of, which is right in aluminium and wrong in a printed boss — a tapped
thread in plastic strips after a few assemblies. So the terminal member's
*mode* now decides what may be stamped: metal still takes a cut thread,
and a printed member takes what ``params.thread_strategy`` names — a nut,
a nut trap, a heat-set insert or a thread-forming core hole
(:mod:`precis.thread_forming`). Undeclared on a printed member, **nothing
is stamped** and the finding lists the four: a plausible-looking hole is
worse than no hole, because it prints.

**The head now stamps too** (rung 3c): a countersunk head cuts a 90° cone
in the near member and a cap/pan/button head gets an optional counterbore,
which the flat `fastener` category could not express until migration 0163
gave a row its ``head_form``.

**And the tool has to reach** (rung 3b): a hex key turning an M3 sweeps a
66 mm circle, so :mod:`precis_se.toolaccess` stands each candidate driver
on the drive face and asks whether it clears the assembly. The answer is
*which* tool, not whether — "long-arm hex key only" is an instruction.

Deferred, named so they are not re-derived:
assembly-order existence — including whether a nut trap can be *reached*;
edge distance (needs hole positions in a member's outline, which arrives
with the profile tier); washers as load-spreaders in the stack-up
(they are members here, which is geometrically right and mechanically
silent); **a screw bottoming out** is now answered for the two BLIND
strategies — ``tapped``/``core`` are drilled past the engaged thread by
the house tip-clearance/tap-chamfer allowance
(:func:`precis.thread_forming.blind_hole`, gr343427) and come back
*through* instead when that reaches the far face, rather than stamping
the whole member thickness regardless of how little thread the screw
actually needs. What is still checked separately is the other half, a
pocket deeper than the member it sits in (``pocket_too_deep``);
the printed **boss** as geometry rather than prose — this pass only ever
subtracts, and adding material to a member someone else authored is the
hand-edit collision the derived-feature rule exists to avoid; and the
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

from precis import component_series
from precis import fit_classes as core_fit
from precis import thread_forming as core_tf
from precis.cad import probe as cad_probe
from precis.cad.graph import Design as CadDesign
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis.utils.units import format_quantity
from precis_se import capabilities as se_caps
from precis_se import catalog as se_catalog
from precis_se import joints as se_joints
from precis_se import modes as se_modes
from precis_se import toolaccess as se_toolaccess
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
#: Deliberately kept absolute (the units policy documented in
#: :mod:`precis.utils.units`'s module docstring: an author-stated
#: tolerance is accepted absolute-with-unit and never silently
#: relativized): a fastener stack is real catalog hardware — bolts,
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
#: intent). Radians internal per the angle ruling in
#: :mod:`precis.utils.units`'s module docstring — this compares directly
#: against :func:`_angle_rad`'s radian output.
_AXIS_TOL_RAD = math.radians(2.5)

#: Minimum thread engagement into a tapped member, in nominal diameters.
#: 1×D is the steel-into-steel rule of thumb; softer materials want more,
#: which is a material-capability refinement this does not yet have (so
#: the finding says "at least", never "exactly").
_MIN_ENGAGEMENT_D = 1.0

#: Threads of protrusion past a nut — the assembly convention that the
#: nut is fully engaged, expressed in pitches.
_PROTRUSION_PITCHES = 2.0

#: Drives this shop builds with (Reto, 2026-09-15): the two internal
#: drives that take real torque. ``'socket'`` is `component`'s spelling of
#: hexagon socket (a hex key) and ``'torx'`` of hexalobular — migration
#: 0093's `drive_type` vocabulary, used verbatim so the check compares
#: stored values rather than a second spelling of them.
_PREFERRED_DRIVES: frozenset[str] = frozenset({"socket", "torx", "allen"})

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
    #: The member's manufacturing mode (``'fdm/asa'``), verbatim. Carried
    #: because *what a member is made of* decides what the far end of the
    #: stack may be given: a cut thread is right in aluminium and wrong in
    #: a printed boss, and rung 3a stamped one without ever asking.
    mode: str | None = None


@dataclass(frozen=True)
class Hole:
    """A feature this joint stamps into one member. **Derived**: named
    after the connect that made it, recomputed on every read, stored
    nowhere.

    ``kind`` is one of:

    - ``'clearance'`` — the screw passes through (rung 3a).
    - ``'tapped'`` — a cut thread, ``d − P``.
    - ``'core'`` — a thread-*forming* hole a pointy screw deforms.
    - ``'insert-pocket'`` — the stepped bore a heat-set insert melts into.
    - ``'nut-pocket'`` — a hex pocket holding a nut captive.
    - ``'counterbore'`` / ``'countersink'`` — head clearance in the near
      member, from the head form (rung 3c).

    ``across_flats_m`` is set only for a hex pocket, where a diameter
    alone would describe a hole the nut spins in. ``source`` is the
    provenance sentence of the *number* — a shop rule and a published
    table must not read the same.

    ``through`` says whether the feature exits the member's far face:
    true for every clearance hole (the screw has to pass), and for a
    ``'tapped'``/``'core'`` terminal feature only when the blind depth
    computed from engagement would have exceeded the member's own
    thickness and was capped there — at that point it is not a blind
    hole any more, it is a through hole that happens to be threaded, and
    a view should say so rather than imply a bottom that isn't there.
    False (a genuine blind pocket) for every other kind."""

    name: str
    block: str
    kind: str
    diameter_m: float
    depth_m: float
    origin: list[float]
    axis: list[float]
    across_flats_m: float | None = None
    chamfer_m: float | None = None
    source: str | None = None
    #: How much of a ``tapped``/``core`` far-end hole is full-form thread —
    #: the rest of ``depth_m`` is the house tip-clearance/tap-chamfer
    #: allowance that lets a blind hole drill past it
    #: (:func:`precis.thread_forming.blind_hole`). ``None`` for every other
    #: kind, and when the far end has no catalog pitch to size it from.
    thread_depth_m: float | None = None
    #: True when the feature exits the member's far face — every clearance
    #: hole, and a ``tapped``/``core`` hole whose blind depth would have
    #: reached the far face and was capped at the thickness instead.
    through: bool = False


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
    #: ``params.thread_strategy`` as declared, or None. What the screw
    #: threads into at the far end (`precis.thread_forming`).
    thread_strategy: str | None = None
    #: The terminal member's material class, derived from its mode. None
    #: means the block never said what it is made of — which is a finding,
    #: not a default.
    material_class: str | None = None
    #: The best driver that clears the assembly (rung 3b), or None when
    #: none does / the drive has no tool listed. Prose, because what a
    #: builder needs is the tool's name, not its envelope.
    tool: str | None = None
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
                    mode=getattr(node, "mode", None),
                )
            )
    members.sort(key=lambda m: (m.t_in, m.block))
    return members


def _truncate_at_target(
    members: list[Member], target: str
) -> tuple[list[Member], str | None]:
    """Stop the stack at the block the connect actually fastens.

    ``_walk_axis`` returns every block the ray crosses, with no notion of
    which one the joint names — a bolt posed on a saddle and connected to
    ``seatpost.top`` would otherwise walk straight through the seatpost,
    the clamp and the crown, out to the axle. The connect's non-fastener
    endpoint *is* that block; a nut sitting just beyond it is still the
    joint's own hardware (the ``nut`` termination expects it as the
    terminal member) and survives the cut, but nothing further does.
    Ownership of that nut is by position only: two collinear joints
    packed so tightly that a foreign nut sits immediately past this
    joint's target are not told apart here.

    If the target never appears in the walk at all, the walk is returned
    unchanged and a detail string is returned instead of ``None`` — a
    silent guess here would hide the very axis mismatch that caused it."""
    idx = next((i for i, m in enumerate(members) if m.block == target), None)
    if idx is None:
        return members, (
            f"the connect names {target!r} as what this screw fastens into, "
            "but the screw's axis never crosses its envelope — the stack "
            "was taken as everything on the axis; check its pose/rot or "
            "the connect's far endpoint"
        )
    end = idx + 1
    if end < len(members) and members[end].form == "nut":
        end += 1
    return members[:end], None


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
    at_t: float | None = None,
    across_flats_m: float | None = None,
    chamfer_m: float | None = None,
    source: str | None = None,
    thread_depth_m: float | None = None,
    through: bool = False,
) -> Hole:
    # A printed hole comes out undersize, so the *modelled* diameter is
    # the wanted one plus the process's compensation. Applied here, at the
    # one place every feature is built, and said out loud in `source` —
    # a silently enlarged hole is indistinguishable from a wrong table.
    bump, cap = se_caps.hole_compensation_m(member.mode)
    if bump and not member.bought:
        diameter_m += bump
        across_flats_m = None if across_flats_m is None else across_flats_m + bump
        note = (
            f"+{bump * 1000:.2f} mm printed-hole compensation "
            f"({cap.mode}, confidence {cap.confidence})"
            if cap is not None
            else ""
        )
        source = f"{source}; {note}" if source else note
    # A feature normally starts where the member does; ``at_t`` is for the
    # ones that don't — a counterbore starts at the member's near face,
    # which is the same thing, but an insert pocket bored from the FAR
    # face starts at t_out.
    t = member.t_in if at_t is None else at_t
    start = [o + t * a for o, a in zip(origin, axis, strict=True)]
    return Hole(
        name=f"{subject}#{member.block}.{kind}",
        block=member.block,
        kind=kind,
        diameter_m=diameter_m,
        depth_m=depth_m,
        origin=[float(v) for v in start],
        axis=[float(v) for v in axis],
        across_flats_m=across_flats_m,
        chamfer_m=chamfer_m,
        source=source,
        thread_depth_m=thread_depth_m,
        through=through,
    )


def _engagement_d(res: FastenResult) -> float:
    """How many nominal diameters of thread this material wants.

    Falls back to the steel 1×D when the material is unknown — which is
    the *old* behaviour, kept deliberately: the honest complaint about an
    undeclared material is :func:`_resolve_strategy`'s finding, not a
    silently inflated length requirement that nobody can trace."""
    rule = core_tf.material(res.material_class) if res.material_class else None
    return rule.min_engagement_d if rule is not None else _MIN_ENGAGEMENT_D


def _resolve_strategy(res: FastenResult, *, subject: str, last: Member) -> None:
    """Decide what the far end of the stack *is*, and say so when it
    cannot be decided.

    Rung 3a had one answer — a cut thread — and applied it to whatever the
    last member happened to be. The rule now:

    - a **nut** in the stack settles it (the geometry already says so);
      a `thread_strategy` that contradicts a present nut is a finding,
      because two declarations disagree and neither is obviously stale;
    - otherwise the **declared** strategy wins;
    - otherwise **metal** takes a cut thread, which is the one material
      where that is the obvious default;
    - a member with **no mode at all** also takes a cut thread — rung
      3a's behaviour, unchanged, because refusing there would break every
      design that predates modes — but with an `info` finding saying it
      was an assumption;
    - a member that **says it is printed**, with no strategy, gets
      **nothing stamped** and a finding naming the four options. That is
      the whole point of the rung: where the tree knows the member is
      plastic, a plausible hole is worse than no hole, because it prints.
    """
    res.material_class = se_modes.thread_material_class(last.mode)
    declared = res.thread_strategy
    if last.form == "nut":
        if declared is not None and declared not in ("nut", "nut-trap"):
            res.findings.append(
                ValidationIssue(
                    rule="thread_strategy_conflict",
                    subject=subject,
                    detail=(
                        f"the stack ends in the nut {last.block!r} but the "
                        f"joint declares thread_strategy={declared!r} — the "
                        "nut is where the thread is; drop the param or "
                        "remove the nut from the axis"
                    ),
                    severity="warn",
                )
            )
        res.thread_strategy = "nut"
        return
    if declared is not None:
        if (
            declared == "tapped"
            and res.material_class
            and res.material_class.startswith("thermoplastic")
        ):
            res.findings.append(
                ValidationIssue(
                    rule="tapped_plastic",
                    subject=subject,
                    detail=(
                        f"{last.block!r} is {last.mode} and the joint asks "
                        "for a cut thread — it will hold, but a tapped "
                        "thread in plastic strips after a few assemblies; "
                        "'insert' or 'nut-trap' is the durable version and "
                        "'thread-forming' the cheap one. Declared, so this "
                        "is a note rather than a refusal"
                    ),
                    severity="info",
                )
            )
        return
    if res.material_class == "metal":
        res.thread_strategy = "tapped"
        return
    if res.material_class is None:
        # Nothing says what this member is. The cut thread is still the
        # answer for the material a nutless stack has always been assumed
        # to end in, so rung 3a's behaviour stands — but it is now
        # visibly an assumption rather than a fact, which is the whole
        # difference between this and the defect above it.
        res.thread_strategy = "tapped"
        res.findings.append(
            ValidationIssue(
                rule="member_material_undeclared",
                subject=subject,
                detail=(
                    f"nothing says what {last.block!r} is made of, so it was "
                    "given a cut thread — the metal answer. If it is printed, "
                    "that thread strips: set_mode(block=…, mode='fdm/…') and "
                    "the right strategy will be asked for"
                ),
                severity="info",
            )
        )
        return
    options = " · ".join(
        f"{k} ({v['title']})"
        for k, v in core_tf.strategies().items()
        if k in ("nut", "nut-trap", "insert", "thread-forming")
    )
    res.findings.append(
        ValidationIssue(
            rule="thread_strategy_undeclared",
            subject=subject,
            detail=(
                f"{last.block!r} is printed ({last.mode}) and no nut ends "
                "the stack, so what the screw threads into is a choice, not "
                "a default — a cut thread in plastic is the wrong answer "
                "often enough that nothing was stamped. Set "
                "params.thread_strategy: " + options
            ),
            severity="warn",
        )
    )


def _tool_access(
    res: FastenResult,
    tree: SeTree,
    *,
    subject: str,
    specs: dict[str, Any],
    origin: list[float],
    axis: list[float],
) -> None:
    """Rung 3b: can any driver actually turn this screw where it sits.

    The drive face is the top of the head for a proud head and the flush
    face for a countersunk one, and the tool comes from ``−axis`` — the
    screw's own frame answers both, which is why this lives here rather
    than in the tool module."""
    head_h = se_catalog.head_height(specs) or 0.0
    sunk = str(specs.get("head_form") or "") == "countersunk"
    offset = 0.0 if sunk else -head_h
    face = [o + offset * a for o, a in zip(origin, axis, strict=True)]
    result = se_toolaccess.access(
        tree,
        fastener=res.fastener or "",
        subject=subject,
        drive_origin=face,
        drive_axis=[-a for a in axis],
        drive_type=(
            str(specs["drive_type"]) if specs.get("drive_type") is not None else None
        ),
        drive_size_mm=_mm_spec(specs, "drive_size"),
    )
    if result is None:
        return
    res.tool = result.fits[0].title if result.fits else None
    issue = se_toolaccess.finding(result)
    if issue is not None:
        res.findings.append(issue)


def _mm_spec(specs: dict[str, Any], key: str) -> float | None:
    """A spec that arrives in metres, back in the millimetres the tool
    tables are keyed by. The round-trip is deliberate and narrow: the
    driver data is catalogue sizes ("a 4 mm key"), not lengths, so it is
    keyed the way the tool is stamped."""
    value = _num(specs, key)
    return None if value is None else round(value * 1000.0, 4)


def _drive_findings(res: FastenResult, *, subject: str, specs: dict[str, Any]) -> None:
    """House drive policy: hex socket and hexalobular (Torx) only.

    A finding, never a refusal — the design is allowed to be
    mid-thought, and a slotted screw is a real part someone may
    deliberately want. The preferred set is a module constant rather than
    a data file because it is a *shop tooling* fact with exactly one
    consumer; it moves to capability data the day a second shop needs a
    different one."""
    drive = specs.get("drive_type")
    if drive is None or str(drive).strip().lower() in _PREFERRED_DRIVES:
        return
    res.findings.append(
        ValidationIssue(
            rule="drive_not_preferred",
            subject=subject,
            detail=(
                f"{res.component or res.fastener} has a "
                f"{str(drive).strip()!r} drive — this shop builds with "
                f"{' and '.join(sorted(_PREFERRED_DRIVES))} only (they take "
                "torque without camming out, and the tools are on the "
                "bench). ISO 4762/10642/7380 are the hex-socket families, "
                "ISO 14579/14581/14583 the Torx ones"
            ),
            severity="info",
        )
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
        thread_strategy=params.get("thread_strategy"),
    )
    fastener = _find_fastener(tree, connect)
    if fastener is None:
        res.why_not = (
            "neither endpoint is a block bound to a screw-form `component` "
            "— the stack-up is positional, so the fastener has to be placed "
            "as a block (a BOM line names which screw, not where it is): "
            "add_block + set_binding(kind='component'), then connect the "
            "SCREW BLOCK to what it fastens (a='bolt.thread', "
            "b='bracket.boss'). A connect between the two members alone "
            "says they are joined; it does not say by what, and the grip, "
            "the holes and the tool are all read off the screw's own pose"
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

    target = connect.a_block if connect.b_block == fastener else connect.b_block
    res.members, missed = _truncate_at_target(res.members, target)
    if missed is not None:
        res.findings.append(
            ValidationIssue(
                rule="fastener_target_missed",
                subject=subject,
                detail=missed,
                severity="warn",
            )
        )

    last = res.members[-1]
    res.termination = "nut" if last.form == "nut" else "tapped"
    res.stack_m = sum(m.thickness_m for m in res.members)
    res.grip_m = res.stack_m - last.thickness_m
    _resolve_strategy(res, subject=subject, last=last)
    _drive_findings(res, subject=subject, specs=specs)
    _tool_access(res, tree, subject=subject, specs=specs, origin=origin, axis=axis)

    nominal = _num(specs, "outer_diameter")
    pitch = res.thread.pitch_m if res.thread else None
    engage_d = _engagement_d(res)
    if res.termination == "nut" and pitch is not None:
        res.required_length_m = res.stack_m + _PROTRUSION_PITCHES * pitch
    elif res.termination == "tapped" and nominal is not None:
        res.required_length_m = res.grip_m + engage_d * nominal

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
                f"{engage_d:g}×D of thread engagement"
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
        and last.thickness_m < engage_d * nominal
    ):
        rule = core_tf.material(res.material_class)
        why = (
            f" ({rule.title.split(' — ')[0].lower()} wants "
            f"{engage_d:g}×D, against 1×D in steel)"
            if rule is not None and engage_d > 1.0
            else ""
        )
        res.findings.append(
            ValidationIssue(
                rule="thread_engagement",
                subject=subject,
                detail=(
                    f"the threaded member {last.block!r} is only "
                    f"{last.thickness_m * 1000:.1f} mm thick, less than the "
                    f"{engage_d:g}×D ({engage_d * nominal * 1000:.1f} mm) of "
                    f"engagement this joint wants{why} — use a nut, a "
                    "longer boss, or a heat-set insert"
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

    _stamp(res, subject=subject, origin=origin, axis=axis, specs=specs, params=params)
    return res


def _stamp(
    res: FastenResult,
    *,
    subject: str,
    origin: list[float],
    axis: list[float],
    specs: dict[str, Any],
    params: dict[str, Any],
) -> None:
    """Emit the holes into ``res``: head clearance where the head form
    demands it, a clearance hole through every designed member the screw
    passes, and whatever the thread strategy puts at the far end."""
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
    holes: list[Hole] = []
    for member in res.members:
        if member.bought:
            continue  # a bought part comes with its own hole
        if member is res.members[-1] and res.termination == "tapped":
            far = _far_end_feature(res, member, subject=subject, specs=specs)
            if far is not None:
                thread_depth_m: float | None = None
                far_source = far.source
                far_through = False
                if far.kind in ("tapped", "core"):
                    # Neither leaves depth_mm set — the tapping-drill and
                    # core-hole tables are cross-sectional, so a BLIND
                    # hole's depth is sized here from engagement
                    # (gr343427). Capped at the member's thickness it is a
                    # through hole, and says so.
                    depth_m, thread_depth_m, far_source = _blind_hole_depth(
                        res, member, far, specs=specs
                    )
                    far_through = depth_m >= member.thickness_m
                elif far.depth_m is None:
                    depth_m = member.thickness_m
                else:
                    depth_m = min(far.depth_m, member.thickness_m)
                if far.kind == "nut-pocket":
                    # The screw has to REACH the nut, so the member is
                    # drilled through and the pocket is recessed from its
                    # far face — which is also where the nut takes the
                    # load. A pocket at the entry face would put the nut
                    # under the head, on the wrong side of the material.
                    holes.append(
                        _hole_for(
                            member,
                            subject=subject,
                            kind="clearance",
                            diameter_m=fit.hole_m,
                            depth_m=member.thickness_m,
                            origin=origin,
                            axis=axis,
                            source=fit.source,
                            through=True,
                        )
                    )
                holes.append(
                    _hole_for(
                        member,
                        subject=subject,
                        kind=far.kind,
                        diameter_m=far.diameter_m,
                        depth_m=depth_m,
                        origin=origin,
                        axis=axis,
                        at_t=(
                            member.t_out - depth_m if far.kind == "nut-pocket" else None
                        ),
                        across_flats_m=(
                            None
                            if far.across_flats_mm is None
                            else far.across_flats_mm / 1000.0
                        ),
                        chamfer_m=(
                            None if far.chamfer_mm is None else far.chamfer_mm / 1000.0
                        ),
                        source=far_source,
                        thread_depth_m=thread_depth_m,
                        through=far_through,
                    )
                )
                _pocket_findings(res, member, far, subject=subject)
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
                source=fit.source,
                through=True,
            )
        )
    head = _head_feature(
        res,
        subject=subject,
        specs=specs,
        origin=origin,
        axis=axis,
        counterbore=bool(params.get("counterbore")),
    )
    if head is not None:
        holes.insert(0, head)
    res.holes = holes


def _far_end_feature(
    res: FastenResult,
    member: Member,
    *,
    subject: str,
    specs: dict[str, Any],
) -> core_tf.Feature | None:
    """The feature the terminal member gets, by strategy. ``None`` means
    the strategy is undeclared (already reported) or its inputs are
    missing — and a missing input is reported here rather than turned into
    a guessed diameter."""
    strategy = res.thread_strategy
    if strategy is None or res.thread_size is None:
        return None
    size = res.thread_size
    # No ``nut`` branch: a nut in the stack makes the termination 'nut',
    # and this is only reached on 'tapped'. The nut is a part, and the
    # members it clamps get ordinary clearance holes.
    if strategy == "tapped":
        pitch = res.thread.pitch_m if res.thread else None
        feature = (
            core_tf.tapping_drill(size, pitch * 1000.0) if pitch is not None else None
        )
    elif strategy == "thread-forming":
        feature = core_tf.core_hole(size, material_class=res.material_class)
    elif strategy == "insert":
        feature = _insert_feature(res, size, subject=subject)
    elif strategy == "nut-trap":
        feature = _nut_trap_feature(res, size, subject=subject)
    else:  # pragma: no cover — the param is contract-classed at write time
        feature = None
    if feature is None and strategy in ("tapped", "thread-forming"):
        res.findings.append(
            ValidationIssue(
                rule="thread_feature_unresolved",
                subject=subject,
                detail=(
                    f"thread_strategy={strategy!r} needs a number this tree "
                    f"cannot supply for thread size {size!r} "
                    f"(material class {res.material_class or 'undeclared'}) "
                    "— nothing was stamped rather than a guessed diameter"
                ),
                severity="warn",
            )
        )
    _boss_finding(res, member, subject=subject, specs=specs)
    return feature


def _blind_hole_depth(
    res: FastenResult, member: Member, far: core_tf.Feature, *, specs: dict[str, Any]
) -> tuple[float, float | None, str]:
    """The depth a ``tapped`` or thread-forming ``core`` far-end hole is
    actually drilled to — ``core_tf.tapping_drill``/``core_hole`` size the
    *diameter* and leave ``depth_mm`` unset, so without this the caller's
    only fallback is the whole member thickness (gr343427: a 150 mm
    seatpost stamped 150 mm deep for 10 mm of real engagement).

    House rule (:func:`precis.thread_forming.blind_hole`): drill past the
    thread the screw actually engages by a tip-clearance allowance so it
    never bottoms on thread runout, plus — for a cut thread only — a
    tap-chamfer allowance for the plug tap's lead-in. Returns
    ``(drill_depth_m, thread_depth_m, source)``, the depth capped at the
    member's thickness (through instead of blind) when the rule would
    otherwise reach the far face.

    ``res.thread`` absent (no catalog pitch) falls back to the earlier
    behaviour unchanged: the whole member thickness, source untouched."""
    if res.thread is None:
        return member.thickness_m, None, far.source or ""
    pitch_m = res.thread.pitch_m
    nominal = _num(specs, "outer_diameter")
    actual_m = (
        max(0.0, res.length_m - (res.grip_m or 0.0))
        if res.length_m is not None
        else 0.0
    )
    if actual_m > 0.0:
        # The screw's own declared length says how much thread it puts
        # into this member — capped at the member, which is the most any
        # engagement can be.
        engagement_m = min(actual_m, member.thickness_m)
    else:
        # No screw, or one too short to engage at all (`screw_too_short`
        # already names that as the defect) — size the hole for the
        # engagement the joint *needs*, not for the wrong screw.
        engagement_m = _engagement_d(res) * nominal if nominal is not None else 0.0
    rule = core_tf.blind_hole()
    thread_depth_m = engagement_m + rule.tip_clearance_pitches * pitch_m
    drill_depth_m = (
        thread_depth_m + rule.tap_chamfer_pitches * pitch_m
        if far.kind == "tapped"
        else thread_depth_m
    )
    if drill_depth_m >= member.thickness_m:
        return (
            member.thickness_m,
            thread_depth_m,
            f"{far.source} — through: the blind depth would reach the far face",
        )
    return (
        drill_depth_m,
        thread_depth_m,
        f"{far.source} — blind: {thread_depth_m * 1000:.1f} mm of full "
        f"thread, drilled to {drill_depth_m * 1000:.1f} mm ({rule.source})",
    )


def _insert_feature(
    res: FastenResult, size: str, *, subject: str
) -> core_tf.Feature | None:
    """The heat-set pocket, sized from the insert series row — which the
    design must also carry as a BOM line, because a pocket with no insert
    in it is a hole."""
    series = component_series.find_series(core_tf.insert_series_id())
    row = series.size(size) if series is not None else None
    length_mm = None if row is None else row.specs.get("height")
    if length_mm is None:
        res.findings.append(
            ValidationIssue(
                rule="insert_unavailable",
                subject=subject,
                detail=(
                    f"no heat-set insert is catalogued for {size} "
                    f"(have: {', '.join(s.key for s in series.sizes)})"
                    if series is not None
                    else "the heat-set insert series is missing from the registry"
                ),
                severity="warn",
            )
        )
        return None
    res.findings.append(
        ValidationIssue(
            rule="insert_bom",
            subject=subject,
            detail=(
                f"this joint needs a {size} heat-set insert as well as the "
                f"screw — add it to the BOM ({core_tf.insert_series_id()}); "
                "the pocket alone is just a hole"
            ),
            severity="info",
        )
    )
    return core_tf.insert_pocket(size, insert_length_mm=float(length_mm))


def _nut_trap_feature(
    res: FastenResult, size: str, *, subject: str
) -> core_tf.Feature | None:
    """The hex pocket, sized from the plain-nut series row for the same
    thread. A nut trap that is not in the BOM is the same omission as a
    missing insert, and says so."""
    series = component_series.find_series("iso-4032")
    row = series.size(size) if series is not None else None
    if row is None:
        res.findings.append(
            ValidationIssue(
                rule="nut_trap_unavailable",
                subject=subject,
                detail=(
                    f"no ISO 4032 nut is catalogued for {size}, so the "
                    "pocket cannot be sized — nothing was stamped"
                ),
                severity="warn",
            )
        )
        return None
    res.findings.append(
        ValidationIssue(
            rule="nut_trap_bom",
            subject=subject,
            detail=(
                f"this joint needs an ISO 4032 {size} nut captive in the "
                "pocket — add it to the BOM. The pocket is stamped "
                "recessed from the member's FAR face (where the nut takes "
                "the load), with a clearance hole through to reach it; "
                "whether you can get the nut in there is an assembly-order "
                "question this does not check yet"
            ),
            severity="info",
        )
    )
    return core_tf.nut_pocket(
        across_flats_mm=float(row.specs["across_flats"]),
        nut_height_mm=float(row.specs["height"]),
    )


def _boss_finding(
    res: FastenResult, member: Member, *, subject: str, specs: dict[str, Any]
) -> None:
    """How much material has to surround a thread formed in plastic. Prose
    rather than a stamped solid: the boss is *added* material, and this
    pass only ever subtracts — stamping a solid into a member someone else
    authored is the hand-edit collision the derived-feature rule avoids."""
    if res.thread_strategy not in ("thread-forming", "tapped"):
        return
    if not (res.material_class or "").startswith("thermoplastic"):
        return
    if res.thread_size is None:
        return
    boss = core_tf.boss(res.thread_size, material_class=res.material_class)
    if boss is None:
        return
    res.findings.append(
        ValidationIssue(
            rule="boss_required",
            subject=subject,
            detail=(
                f"a thread formed in {member.block!r} needs at least "
                f"{boss.diameter_mm:g} mm of material around the hole "
                f"({boss.source}) — model the boss if the wall there is "
                "thinner; this pass subtracts holes, it never adds material"
            ),
            severity="info",
        )
    )


def _pocket_findings(
    res: FastenResult, member: Member, feature: core_tf.Feature, *, subject: str
) -> None:
    """A pocket deeper than the member it is in comes out the far face.

    This is the *checkable* half of rung 3a's deferred bottoming-out
    question. The other half — whether a screw is too long for a blind
    hole — stays deferred on purpose: a stamped hole's depth says how far
    the feature goes, never whether the material below it ends, so
    warning on every through-hole would train designers to ignore the
    finding (the docstring's original reasoning, unchanged)."""
    if feature.depth_m is None:
        return
    if feature.depth_m > member.thickness_m + _MIN_MEMBER_M:
        res.findings.append(
            ValidationIssue(
                rule="pocket_too_deep",
                subject=subject,
                detail=(
                    f"the {feature.kind} wants {feature.depth_m * 1000:.1f} mm "
                    f"of depth but {member.block!r} is only "
                    f"{member.thickness_m * 1000:.1f} mm thick on this axis "
                    "— it would break through the far face"
                ),
                severity="warn",
            )
        )


def _head_feature(
    res: FastenResult,
    *,
    subject: str,
    specs: dict[str, Any],
    origin: list[float],
    axis: list[float],
    counterbore: bool,
) -> Hole | None:
    """Head clearance in the **first designed member** — the deferral
    `fasten.py` opened with ("a head-form question, and the head forms are
    one flat `fastener` category today"), closed by migration 0163's
    ``head_form``.

    **A countersink is required and a counterbore is a choice**, and the
    difference is physical: a countersunk screw does not seat without its
    cone, while a cap head is equally happy standing proud. So the cone is
    stamped and the bore is only stamped when ``params.counterbore`` asks
    — otherwise the head's protrusion is *reported*, the same posture the
    thread strategy takes at the far end. A head form nobody declared
    gets nothing, which is rung 3a's behaviour, now because the row is
    silent rather than because nothing could ask."""
    form = str(specs.get("head_form") or "").strip().lower()
    if form not in ("countersunk", "cap", "pan", "button"):
        return None
    near = next((m for m in res.members if not m.bought), None)
    if near is None or res.fit is None:
        return None
    head_d = _num(specs, "head_diameter")
    if head_d is None:
        return None
    if form == "countersunk":
        # The theoretical sharp cone: sized so the real head, which is
        # smaller by its edge radius, lands at or below flush.
        return _hole_for(
            near,
            subject=subject,
            kind="countersink",
            diameter_m=head_d,
            depth_m=(head_d - res.fit.nominal_mm / 1000.0) / 2.0,
            origin=origin,
            axis=axis,
            source=(
                f"90° cone to the theoretical head Ø "
                f"{head_d * 1000:.2f} mm ({specs.get('head_form')} head)"
            ),
        )
    head_h = _num(specs, "head_height")
    if head_h is None:
        return None
    # A counterbore is head Ø plus the same clearance the shank gets: the
    # head has to drop in, not press in.
    slack_m = (res.fit.hole_mm - res.fit.nominal_mm) / 1000.0
    if not counterbore:
        res.findings.append(
            ValidationIssue(
                rule="head_stands_proud",
                subject=subject,
                detail=(
                    f"the {form} head stands {head_h * 1000:.1f} mm above "
                    f"{near.block!r} — fine unless something has to pass over "
                    f"it. To bury it, set params.counterbore=true and this "
                    f"joint will stamp a "
                    f"{(head_d + slack_m) * 1000:.1f} mm × "
                    f"{head_h * 1000:.1f} mm bore instead"
                ),
                severity="info",
            )
        )
        return None
    return _hole_for(
        near,
        subject=subject,
        kind="counterbore",
        diameter_m=head_d + slack_m,
        depth_m=head_h,
        origin=origin,
        axis=axis,
        source=(
            f"head Ø + the {res.fit.fit_class} fit's {slack_m * 1000:.1f} mm, "
            f"deep enough to bury a {head_h * 1000:.1f} mm head "
            "(params.counterbore asked for it)"
        ),
    )


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
