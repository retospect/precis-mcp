"""**Connect geometric plausibility** — one DRC pass closing two gripes
that were filed together and asked to be fixed together (gr337040,
gr338426): a declared connect's endpoints are never checked against what
the connect itself *claims* about the geometry.

**gr337040 — undeclared_interpenetration's missing twin.** The existing
``undeclared_interpenetration`` check (:mod:`precis_se.validate`) flags
overlap nobody declared; nothing flagged the opposite — a connect declared
between two envelopes that never actually touch (``unicycle-printed-v1``:
26 connects, 0 DRC errors, parts floating up to 144 mm apart, because the
envelopes were authored assuming centred cylinders against the kernel's
base-at-pose ``z=0..h`` convention). :func:`findings` below's
``connect_envelope_disjoint`` rule is the direct mirror: same
:func:`~precis.cad.relate.clearance` primitive, same
``resolution``-scaled tolerance, opposite sign.

**gr338426 — mechanism/class-implied GEOMETRY, not just measures/BOM.**
:mod:`precis_se.drc`'s mechanism rules already demand a tolerance
*relation* and a BOM line (``mechanism_demand``/``mechanism_bom``); they
never checked whether the *envelopes themselves* are shaped the way the
mechanism claims — a bearing joint tangent below a fork leg (point
contact, nothing housed) passed clean, because tangent is not disjoint
(gr337040 wouldn't have caught it either) and nothing else looked at the
envelope geometry at all. The rules below close that: ``press``/``snap``
mechanisms and the ``captive`` kinematic class demand volumetric overlap;
``bearing``/``revolute``/``cylindrical`` demand the two envelopes' own
axes be coaxial and radially nested; the ``screw`` kinematic class demands
overlap along its axis.

**Severity: warn, throughout, on purpose.** Every finding here compares a
stored declaration against DERIVED envelope geometry — the exact shape of
:mod:`precis_se.drc`'s own ``dof_disagreement`` (declared kinematic class
vs. the envelopes' actual travel), which that module's docstring reasons
through explicitly: envelope geometry legitimately understates a joint
(circlips, shoulders, retention features live at L3, not yet modeled).
The same reasoning applies here — a press fit authored before its
interference allowance is dialed in, or a bearing pocket authored before
its final pose, is scaffolding-in-progress, not a broken design. Promoting
envelope interpenetration to a hard error once both blocks are BOUND is a
separate, deliberately deferred ruling (``se-atomic-round2.md``) — out of
scope here; nothing in this module changes tier based on binding state.

**Scope decisions worth naming:**

* ``screw`` is checked by KINEMATIC CLASS only, never by MECHANISM. The
  ``screw`` *mechanism* (threaded fastening) already gets a far more
  precise, dedicated rung-3 check (:mod:`precis_se.fasten`: the axis comes
  from the fastener's own catalog envelope, the grip is walked member by
  member with :func:`~precis.cad.probe.probe_ray`) — duplicating a coarse
  envelope-overlap requirement here would both re-derive machinery that
  already exists and misfire on the overwhelmingly common case of a bolted
  flange joint, where the two members are simply face-to-face with a
  clearance hole (no envelope-level interference at all, by design). The
  ``screw`` *class* (the helical lower pair — a leadscrew nut) is a
  distinct, unclaimed geometric assertion: nothing else checks that a
  declared leadscrew pair's envelopes actually engage.
* The coaxiality/radial-containment/axial-overlap checks read each
  envelope's OWN implied axis (local +z, rotated by the block's own
  ``rot`` — the base-at-pose convention, same read :mod:`precis_se.fasten`
  already does for a screw's drive direction) rather than the connect's
  declared ``joint.axis``. A single shared ``axis`` field would trivially
  be "coaxial with itself" and catch nothing — the motivating bug (bearing
  tangent below a fork leg) is exactly a case where the ENVELOPES
  themselves are misaligned, which only their own poses/rotations can
  reveal. Only circular-cross-section envelopes (``cyl``/``cone``/
  ``tcone`` — :mod:`precis.cad.dsl`) carry an inferable axis; a pairing
  with any other envelope shape is an honest skip (no finding either way),
  not a false negative — this pass never claims coverage it doesn't have.
* Tolerances are relative throughout (the units-policy-cutover ruling):
  the linear band reuses :data:`~precis.cad.relate.CONTACT_TOL_REL` times
  each pair's own governing length (:func:`~precis_se.validate.
  _characteristic_length`) or, where a real SDF query already ran, its own
  ``resolution``; the angular tolerance (2.5°, matching
  :mod:`precis_se.fasten`'s own ``_AXIS_TOL_RAD`` and the DOF probe's
  principal-axis band) is dimensionless and legitimately fixed.
* A wall-clock budget (:data:`_BUDGET_S`) bounds the per-connect SDF work,
  the same honesty pattern ``envelope_overlaps`` uses for its own O(n²)
  block-pair scan (gr337045) — sized down here because this pass is
  O(connects), not O(blocks²), but the underlying ``clearance()`` call
  costs the same ~2.3 s either way, and a design with many declared
  connects must not turn a routine ``view='drc'`` read into a multi-minute
  call. Connects the budget doesn't reach are reported UNCHECKED, never
  silently skipped.
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad import dsl as cad_dsl
from precis.cad import relate as cad_relate
from precis.cad.graph import Design as CadDesign
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis_se import joints as se_joints
from precis_se.ops import ConnectSpec, SeTree, effective_envelope
from precis_se.validate import (
    ValidationIssue,
    _characteristic_length,
    _is_ancestor,
    _posed_component,
    kernel_scale,
)

#: Flip-invariant (a line has no preferred direction) angular tolerance for
#: the coaxiality check — the same order as :mod:`precis_se.fasten`'s own
#: ``_AXIS_TOL_RAD`` and the DOF probe's principal-axis band: a designer
#: types round numbers, and small float noise from the pose/rot pipeline
#: must not misfire. Dimensionless, so a fixed radian constant is correct
#: (only LINEAR tolerances need to be scale-relative).
_AXIS_ANGLE_TOL_RAD = math.radians(2.5)

#: Envelope shapes with an inferable local +z axis (:mod:`precis.cad.dsl`'s
#: ``_ALIAS_KEYS`` — circular cross-section, base-at-pose). Any other shape
#: (box, sphere, torus, n-gon, chamfer) is an honest skip for the axis
#: checks — a designer authoring a coaxial mate out of boxes gets no
#: coverage here, which is a real gap, not a false negative.
_CIRCULAR_ALIASES = frozenset({"cyl", "cone", "tcone"})

#: Mechanisms whose entire point is volumetric interference before
#: assembly (module docstring): a press/snap fit with zero envelope
#: overlap cannot be built.
_INTERFERENCE_MECHANISMS = frozenset({"press", "snap"})

#: The mechanism/class pair the coaxial + radial-containment rule fires
#: on — a `bearing` mechanism or a `revolute`/`cylindrical` kinematic
#: class all assert the same thing about the two envelopes' own axes.
_COAXIAL_MECHANISM = "bearing"
_COAXIAL_CLASSES = frozenset({"revolute", "cylindrical"})

#: Wall-clock budget for this pass's per-connect SDF work, seconds — see
#: the module docstring's last bullet. Generous for a real design's
#: connect count (far smaller than the O(n²) block-pair scan
#: ``envelope_overlaps`` bounds with the same-sized budget).
_BUDGET_S = 30.0


def _pair_clearance(
    tree: SeTree, a_name: str, b_name: str
) -> tuple[cad_relate.ClearanceResult, float] | None:
    """The kernel's own signed clearance between ``a_name``'s and
    ``b_name``'s posed effective envelopes (metres, via
    :func:`kernel_scale`'s normalization) — the ONE geometry primitive
    every rule in this module reads, never re-derived. ``None`` when
    either envelope is absent or the pair is cross-scale (unverifiable in
    one SDF query) — the caller's per-rule handling owns that honest
    silence."""
    a_node = tree.blocks.get(a_name)
    b_node = tree.blocks.get(b_name)
    if a_node is None or b_node is None:
        return None
    a_env = effective_envelope(tree, a_node)
    b_env = effective_envelope(tree, b_node)
    if not a_env or not b_env:
        return None
    scale = kernel_scale((a_env, a_node), (b_env, b_node))
    if scale is None:
        return None
    design = CadDesign()
    a_expr = _posed_component(design, a_name, a_env, a_node, scale)
    b_expr = _posed_component(design, b_name, b_env, b_node, scale)
    if a_expr is None or b_expr is None:
        return None
    result = cad_relate.clearance(design, a_name, b_name)
    return result, scale


def _circular_axis(
    tree: SeTree, name: str
) -> tuple[NDArray[np.float64], NDArray[np.float64], float, float, str] | None:
    """``(origin, unit direction, r_min, r_max, envelope)`` of ``name``'s
    own implied axis — its envelope's local +z, base-at-pose, rotated by
    the block's own ``rot`` (module docstring: the SAME read
    :mod:`precis_se.fasten`'s ``_drive_axis`` does for a screw's drive
    direction). ``None`` when the block/envelope is absent, unparseable,
    or not one of :data:`_CIRCULAR_ALIASES` — a shape with no inherent
    axis to check."""
    node = tree.blocks.get(name)
    if node is None:
        return None
    env = effective_envelope(tree, node)
    if not env:
        return None
    try:
        spec = cad_dsl.parse(env)
    except (cad_dsl.DslError, ValueError):
        return None
    if spec.alias == "cyl":
        rb = rt = float(spec.params["r"])
    elif spec.alias == "cone":
        rb, rt = float(spec.params["r"]), 0.0
    elif spec.alias == "tcone":
        rb, rt = float(spec.params["rb"]), float(spec.params["rt"])
    else:
        return None
    origin = np.array([float(v) for v in node.pose], dtype=np.float64)
    xform = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
    direction = np.asarray(xform.apply_dir(np.array([0.0, 0.0, 1.0])), dtype=np.float64)
    norm = float(np.linalg.norm(direction))
    if norm == 0.0:
        return None
    return origin, direction / norm, min(rb, rt), max(rb, rt), env


def _axis_findings(
    tree: SeTree, c: ConnectSpec, subject: str, label: str
) -> list[ValidationIssue]:
    """The coaxial + radial-containment pair of findings (gr338426's
    bearing/revolute/cylindrical bullet) for one connect, or ``[]`` on an
    honest skip (either envelope not circular) or when both checks pass.
    Radial containment is only meaningful once the axes agree, so a
    coaxiality miss returns immediately rather than also reporting a
    radius mismatch that a misaligned pair can't meaningfully have.

    The radial-containment half compares each envelope's OWN ``r_min``/
    ``r_max`` (module-wide across the whole shape, not the local radius at
    the z the two actually overlap) — deliberately coarse. For a plain
    ``cyl`` ``r_min == r_max``, so "the smaller-radius envelope nests
    inside the larger one" is true by construction and this half can never
    fire on a cyl/cyl pair; it only has real bite once at least one side
    tapers (``cone``/``tcone``), where the narrow end can be smaller than
    what's meant to fit through it. That's a real scope limit, not a bug:
    two plain cylinders that are coaxial ARE radially nested, always — the
    interesting failure mode for them is the coaxiality half above, or the
    interference rules, not this one."""
    a = _circular_axis(tree, c.a_block)
    b = _circular_axis(tree, c.b_block)
    if a is None or b is None:
        return []
    a_origin, a_dir, a_rmin, a_rmax, a_env = a
    b_origin, b_dir, b_rmin, b_rmax, b_env = b
    dot = float(np.clip(np.dot(a_dir, b_dir), -1.0, 1.0))
    # abs() folds antiparallel into parallel directly — a line has no sign.
    angle = math.acos(abs(dot))
    governing = min(_characteristic_length(a_env), _characteristic_length(b_env))
    tol_linear = cad_relate.CONTACT_TOL_REL * governing
    if angle > _AXIS_ANGLE_TOL_RAD:
        return [
            ValidationIssue(
                rule="axis_not_coaxial",
                subject=subject,
                detail=(
                    f"{label} implies the two blocks' own envelope axes "
                    f"are coaxial, but they differ in direction by "
                    f"{math.degrees(angle):.1f}° — check pose/rot on "
                    "one of the blocks"
                ),
                severity="warn",
            )
        ]
    # Perpendicular offset between the two axis LINES: since the
    # directions are already within tolerance of parallel, the distance
    # from b's own origin (which lies exactly ON b's axis, base-at-pose)
    # to the infinite line through a's origin/direction is exact and
    # independent of how far along the line either origin sits.
    delta = b_origin - a_origin
    along = float(np.dot(delta, a_dir))
    perp = delta - along * a_dir
    offset = float(np.linalg.norm(perp))
    if offset > tol_linear:
        return [
            ValidationIssue(
                rule="axis_not_coaxial",
                subject=subject,
                detail=(
                    f"{label} implies the two blocks' own envelope axes "
                    f"are coaxial, but they are offset by {offset:g} m "
                    "perpendicular to the shared axis — check pose"
                ),
                severity="warn",
            )
        ]
    if a_rmax <= b_rmax:
        inner_rmax, outer_rmin = a_rmax, b_rmin
    else:
        inner_rmax, outer_rmin = b_rmax, a_rmin
    if inner_rmax - outer_rmin > tol_linear:
        return [
            ValidationIssue(
                rule="axis_not_radially_contained",
                subject=subject,
                detail=(
                    f"{label} implies the smaller envelope nests radially "
                    f"inside the larger one, but its max radius "
                    f"{inner_rmax:g} m exceeds the other's min radius "
                    f"{outer_rmin:g} m by {inner_rmax - outer_rmin:g} m"
                ),
                severity="warn",
            )
        ]
    return []


def findings(
    tree: SeTree, *, budget_s: float | None = _BUDGET_S
) -> list[ValidationIssue]:
    """The connect geometric plausibility pass (module docstring) — every
    finding over ``tree.connects``. Pure over ``tree``; no store access.

    ``budget_s`` bounds the per-connect SDF work (``None`` = unbounded —
    tests only); connects the budget doesn't reach are reported via
    ``geometry_plausibility_budget_exceeded``, never silently dropped."""
    out: list[ValidationIssue] = []
    deadline = time.monotonic() + budget_s if budget_s is not None else None
    skipped: list[str] = []
    for c in tree.connects:
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        if c.a_block == c.b_block:
            continue
        if _is_ancestor(tree, c.a_block, c.b_block) or _is_ancestor(
            tree, c.b_block, c.a_block
        ):
            continue
        joint: dict[str, Any] | None = None
        if c.joint:
            try:
                joint = se_joints.validate_joint(c.joint)
            except se_joints.JointError:
                joint = None  # already a malformed_joint finding (drc.py)
        klass = joint.get("class") if joint else None
        mech = joint.get("mechanism") if joint else None
        if klass == "axial":
            # A pin-ended two-force member's line of action legitimately
            # spans the open gap between its endpoints (a spoke, a tie
            # rod) — envelope contact is not what 'axial' asserts
            # (KINEMATIC_CLASSES's own docstring).
            continue
        if deadline is not None and time.monotonic() > deadline:
            skipped.append(subject)
            continue
        pair = _pair_clearance(tree, c.a_block, c.b_block)
        if pair is None:
            continue  # no envelope on one side, or cross-scale — unverifiable
        result, scale = pair
        gap_m = result.gap / scale

        # gr337040 — the mirror of undeclared_interpenetration.
        if result.gap > result.resolution:
            out.append(
                ValidationIssue(
                    rule="connect_envelope_disjoint",
                    subject=subject,
                    detail=(
                        f"declared connect but posed envelopes are "
                        f"{gap_m:g} m apart with no contact — re-pose one "
                        "of the blocks (or set_joint class 'axial' if this "
                        "is meant to be a two-force member spanning open "
                        "space), or disconnect if the relation isn't "
                        "physical"
                    ),
                    severity="warn",
                )
            )

        if joint is None:
            continue

        # gr338426 — mechanism/class-implied geometry.
        interferes = result.gap < -result.resolution
        if mech in _INTERFERENCE_MECHANISMS and not interferes:
            out.append(
                ValidationIssue(
                    rule="mechanism_no_interference",
                    subject=subject,
                    detail=(
                        f"mechanism {mech!r} means a press/snap fit, which "
                        "needs the two envelopes to volumetrically "
                        f"INTERFERE before assembly — they do not "
                        f"({gap_m:g} m gap) — a zero-overlap press is "
                        "unbuildable"
                    ),
                    severity="warn",
                )
            )
        if klass == "captive" and not interferes:
            out.append(
                ValidationIssue(
                    rule="captive_not_contained",
                    subject=subject,
                    detail=(
                        "joint class 'captive' means the two envelopes are "
                        "interlocked, which needs them to overlap — they "
                        f"do not ({gap_m:g} m gap)"
                    ),
                    severity="warn",
                )
            )
        if klass == "screw" and not interferes:
            out.append(
                ValidationIssue(
                    rule="screw_axis_no_overlap",
                    subject=subject,
                    detail=(
                        "joint class 'screw' couples rotation to "
                        "translation along a shared thread axis — the two "
                        f"envelopes do not overlap along it ({gap_m:g} m "
                        "gap)"
                    ),
                    severity="warn",
                )
            )
        if mech == _COAXIAL_MECHANISM or klass in _COAXIAL_CLASSES:
            label = (
                f"mechanism {mech!r}"
                if mech == _COAXIAL_MECHANISM
                else f"class {klass!r}"
            )
            out.extend(_axis_findings(tree, c, subject, label))

    if skipped:
        shown = ", ".join(skipped[:5])
        more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
        budget_desc = f"{budget_s:g}s" if budget_s is not None else "unbounded"
        out.append(
            ValidationIssue(
                rule="geometry_plausibility_budget_exceeded",
                subject=f"{len(skipped)} connect(s)",
                detail=(
                    f"{shown}{more}: this pass's {budget_desc} time budget "
                    "ran out before reaching these connects — they are "
                    "UNCHECKED, not clear; re-run to verify"
                ),
                severity="warn",
            )
        )
    return out
