"""``revolute_axis_mismatch`` (warn) — R2, docs/backlog/
port-rotation-and-lever-composition.md: a connect's ``revolute`` joint
names its rotation axis in the WORLD frame (:mod:`precis_se.joints`); the
endpoint port carries the SAME rotation in the BLOCK frame, derived from
its declared states/transitions (:mod:`precis_se.kinematics`). Both are
claims about the same physical hinge, so a disagreement past
:data:`~precis_se.atomic.validate.PORT_ROT_MISMATCH_RAD` is a finding —
the joint/port sibling of R1's ``port_rot_mismatch`` (a bound structure's
measured frame vs. a declared one), appended by the handler's
``_render_drc`` after :func:`precis_se.drc.drc`'s own findings, exactly
where :mod:`precis_se.precedent`'s slice-5 findings are (store-aware:
:func:`precis_se.kinematics.derive` needs it).

**World placement.** A block's own ``pose``/``rot`` ARE its world
placement, never composed through a parent chain — the same v1
convention :mod:`precis_se.validate` (``_posed_component``) and
``view='fret'`` (``_fret_chromophores``) already use; "parents included"
just means this is the one placement chain every consumer agrees on,
whether or not the block happens to have a ``parent``.

**Ordinary endpoints only.** Only a connect endpoint block with
``template is None`` is checked — the same "instances skip, templates
own states" rule :mod:`precis_se.kinematics` follows. An instance's own
world placement can differ from its template's, and deriving a
per-instance axis is a later round (nested-frame inheritance, same
tracking as ``_fret_chromophores``'s).

**Joint sweep** (:func:`sweep_findings`): a revolute/prismatic joint's declared
continuous ``params.range`` is swept sample by sample through
:func:`precis_se.validate.envelope_overlaps` — ``joint_sweep_interference``
(warn) for a collision inside the range, ``joint_sweep_unchecked`` (info)
for what the evaluation/time budgets did not reach. ``view='sweep'``
(:func:`precis_se.handler._render_sweep`) crosses DISCRETE states; this is
the continuous counterpart, store-free."""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.vec import Transform
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import euler_rad_from_matrix as cad_euler_rad
from precis.cad.vec import pose as cad_pose
from precis.utils.units import format_quantity
from precis_se import joints as se_joints
from precis_se.atomic.validate import PORT_ROT_MISMATCH_RAD
from precis_se.kinematics import KinematicsRow, derive
from precis_se.ops import ConnectSpec, SeTree, effective_envelope, effective_ports
from precis_se.validate import ValidationIssue, envelope_overlaps


def _fmt_axis(v: NDArray[np.float64]) -> str:
    return " ".join(f"{float(c):.3f}" for c in v)


def findings(store: Any, tree: SeTree, ref_id: int) -> list[ValidationIssue]:
    """One ``revolute_axis_mismatch`` per ``(connect, transition)`` whose
    endpoint port's derived swing axis disagrees with the connect's own
    declared ``joint.axis`` by more than :data:`PORT_ROT_MISMATCH_RAD`,
    compared AS LINES (``min(θ, π−θ)`` — an axis and its negation mean
    the same hinge). A connect with no ``revolute`` joint, no declared
    axis, or an endpoint with nothing derived (no pose, no declared
    states/transitions, or an instance) contributes nothing — this is a
    check over what both sides actually claim, never a demand that they
    claim it."""
    rows_by_block_port: dict[tuple[str, str], list[KinematicsRow]] = {}
    for row in derive(store, tree, ref_id).rows:
        if row.axis is None:
            continue
        rows_by_block_port.setdefault((row.block, row.port), []).append(row)
    out: list[ValidationIssue] = []
    for c in tree.connects:
        joint = c.joint or {}
        if joint.get("class") != "revolute":
            continue
        axis_raw = joint.get("axis")
        if not axis_raw:
            continue
        j_axis = np.asarray([float(x) for x in axis_raw], dtype=np.float64)
        j_norm = float(np.linalg.norm(j_axis))
        if j_norm < 1e-12:
            continue
        j_axis = j_axis / j_norm
        for block, port in ((c.a_block, c.a_port), (c.b_block, c.b_port)):
            node = tree.blocks.get(block)
            if node is None or node.template is not None:
                continue
            derived_rows = rows_by_block_port.get((block, port))
            if not derived_rows:
                continue
            xform = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
            for row in derived_rows:
                assert row.axis is not None
                axis_world = xform.apply_dir(np.asarray(row.axis, dtype=np.float64))
                axis_norm = float(np.linalg.norm(axis_world))
                if axis_norm < 1e-12:
                    continue
                axis_world = axis_world / axis_norm
                dot = float(np.clip(np.dot(axis_world, j_axis), -1.0, 1.0))
                theta = math.acos(abs(dot))  # lines, not rays: min(θ, π−θ)
                if theta <= PORT_ROT_MISMATCH_RAD:
                    continue
                out.append(
                    ValidationIssue(
                        rule="revolute_axis_mismatch",
                        subject=f"{block}.{port} ({row.transition})",
                        detail=(
                            f"joint axis {_fmt_axis(j_axis)} (world) vs. "
                            f"derived port axis {_fmt_axis(axis_world)} "
                            f"(world, from transition {row.transition!r}) — "
                            f"{math.degrees(theta):.3g}° apart, more than "
                            f"{math.degrees(PORT_ROT_MISMATCH_RAD):.3g}° — "
                            "the joint names the axis, the port carries the "
                            "rotation; both must agree"
                        ),
                        severity="warn",
                    )
                )
    out.extend(sweep_findings(tree))
    return out


# ── joint sweep over a declared continuous range ─────────────────────────

#: Per-design cap on overlap evaluations (one :func:`envelope_overlaps`
#: call per joint sample). A joint whose samples do not fit what is left
#: is skipped WHOLE and named in ``joint_sweep_unchecked`` — never
#: silently dropped (``view='sweep'``'s ``_SWEEP_COMBO_BUDGET`` posture).
SWEEP_EVAL_BUDGET = 256

#: Wall-clock cap for the whole joint sweep, seconds, shared across every
#: sample's :func:`envelope_overlaps` call (each is handed what is left) —
#: the same one-deadline rule as ``view='sweep'``'s ``_SWEEP_WALL_BUDGET_S``.
SWEEP_WALL_BUDGET_S = 30.0


def _subtree(tree: SeTree, root: str) -> set[str]:
    """``root`` plus every block whose parent chain reaches it — the set
    that moves rigidly with the joint's ``b`` side."""
    out = {root}
    for name in tree.blocks:
        seen: set[str] = set()
        cur = tree.blocks[name].parent
        while cur is not None and cur not in seen and cur in tree.blocks:
            if cur == root:
                out.add(name)
                break
            seen.add(cur)
            cur = tree.blocks[cur].parent
    return out


def _sample_transform(
    klass: str, axis: NDArray[np.float64], pivot: NDArray[np.float64], value: float
) -> Transform:
    """The world-frame rigid motion of the moving subtree at joint value
    ``value``: a rotation by ``value`` radians about ``axis`` through
    ``pivot`` (revolute), or a slide of ``value`` metres along ``axis``
    (prismatic)."""
    if klass == "prismatic":
        return Transform(R=np.eye(3, dtype=np.float64), t=value * axis)
    k = np.array(
        [[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]],
        dtype=np.float64,
    )
    R = np.eye(3) + math.sin(value) * k + (1.0 - math.cos(value)) * (k @ k)
    return Transform(R=R, t=pivot - R @ pivot)


def _fmt_value(klass: str, value: float) -> str:
    return format_quantity(value, "angle" if klass == "revolute" else "length")


def _connect_subject(c: ConnectSpec) -> str:
    return f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"


def sweep_findings(tree: SeTree) -> list[ValidationIssue]:
    """``joint_sweep_interference`` (warn) / ``joint_sweep_unchecked``
    (info). For each connect
    whose revolute/prismatic joint declares ``params.range``, move the
    ``params.moves`` end and its ``parent`` subtree rigidly through
    ``samples`` evenly spaced joint values (endpoints included,
    displacements from the authored pose) and run
    :func:`envelope_overlaps` on each — the static
    ``undeclared_interpenetration`` check, re-posed, never a second
    geometry engine. The moving end is declared, never inferred from
    endpoint order: persist stores a connect as an unordered pair
    (endpoints sorted). Pivot = the moving end's port origin, posed
    (block pose ∘ port pose); axis = the joint's declared ``axis``, WORLD
    frame (:mod:`precis_se.joints`' v1 contract, the same frame
    ``revolute_axis_mismatch`` reads it in).

    Only (moving, static) pairs are asked about; ancestor/nested pairs
    (inside :func:`envelope_overlaps`) and every connected pair (the
    static check's sanction) are exempt. A joint whose moving port has no
    pose, or whose moving block has no envelope, contributes nothing; a
    ``moves`` that names neither end is ``joint_sweep_unchecked``. The
    tree's world poses are restored before returning."""
    connected = {frozenset({c.a_block, c.b_block}) for c in tree.connects}
    out: list[ValidationIssue] = []
    over_budget: list[str] = []
    budget = SWEEP_EVAL_BUDGET
    deadline = time.monotonic() + SWEEP_WALL_BUDGET_S
    for c in tree.connects:
        try:
            joint = se_joints.validate_joint(c.joint or {})
        except se_joints.JointError:
            continue  # stored-shape problems are drc.py's finding
        params = joint.get("params", {})
        klass = joint["class"]
        if "range" not in params or klass not in se_joints.RANGE_CLASSES:
            continue
        subject = _connect_subject(c)
        mover_block = params.get("moves")
        if mover_block == c.a_block:
            mover_port = c.a_port
        elif mover_block == c.b_block:
            mover_port = c.b_port
        else:
            # Write time refuses this; a stored joint can still drift (an
            # end removed and re-added under another name) — say so.
            out.append(
                ValidationIssue(
                    rule="joint_sweep_unchecked",
                    subject=subject,
                    detail=(
                        f"joint param 'moves' is {mover_block!r}, not one of "
                        f"this connect's ends ({c.a_block!r}, {c.b_block!r}) "
                        "— the range is UNCHECKED; set_joint with the end "
                        "that turns/slides"
                    ),
                    severity="info",
                )
            )
            continue
        node = tree.blocks.get(mover_block)
        if node is None or not effective_envelope(tree, node):
            continue
        port = effective_ports(tree, node).get(mover_port)
        if port is None or port.pose is None:
            continue
        axis = np.asarray(joint["axis"], dtype=np.float64)
        axis_norm = float(np.linalg.norm(axis))
        if axis_norm < 1e-12:
            continue
        axis = axis / axis_norm
        lo, hi = params["range"]
        n = int(params.get("samples", se_joints.DEFAULT_RANGE_SAMPLES))
        if n > budget:
            over_budget.append(subject)
            continue
        budget -= n
        m_xform = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
        pivot = np.asarray(m_xform.apply(cad_as_vec3(port.pose)), dtype=np.float64)
        moving = _subtree(tree, mover_block)
        baseline = {
            m: (list(tree.blocks[m].pose), list(tree.blocks[m].rot)) for m in moving
        }

        def _ask(a: str, b: str, moving: set[str] = moving) -> bool:
            return (a in moving) != (b in moving) and frozenset({a, b}) not in connected

        values = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
        hits: dict[tuple[str, str], list[int]] = {}
        unchecked: set[tuple[str, str]] = set()
        timed_out = False
        try:
            for i, value in enumerate(values):
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    timed_out = True
                    break
                motion = _sample_transform(klass, axis, pivot, value)
                for m, (pose0, rot0) in baseline.items():
                    xf = motion.compose(cad_pose(cad_as_vec3(pose0), cad_as_vec3(rot0)))
                    tree.blocks[m].pose = [float(v) for v in xf.t]
                    tree.blocks[m].rot = [float(v) for v in cad_euler_rad(xf.R)]
                overlaps, cross, budget_hit = envelope_overlaps(
                    tree, budget_s=remaining, pair_filter=_ask
                )
                for a_name, b_name, _gap in overlaps:
                    pair = (a_name, b_name) if a_name in moving else (b_name, a_name)
                    hits.setdefault(pair, []).append(i)
                unchecked.update(cross)
                unchecked.update(budget_hit)
        finally:
            for m, (pose0, rot0) in baseline.items():
                tree.blocks[m].pose = pose0
                tree.blocks[m].rot = rot0
        span = f"{_fmt_value(klass, lo)}–{_fmt_value(klass, hi)}"
        for (mover, other), idx in sorted(hits.items()):
            runs: list[str] = []
            start = 0
            while start < len(idx):
                end = start
                while end + 1 < len(idx) and idx[end + 1] == idx[end] + 1:
                    end += 1
                a_val = _fmt_value(klass, values[idx[start]])
                b_val = _fmt_value(klass, values[idx[end]])
                runs.append(a_val if start == end else f"{a_val}–{b_val}")
                start = end + 1
            out.append(
                ValidationIssue(
                    rule="joint_sweep_interference",
                    subject=subject,
                    detail=(
                        f"{mover} ↔ {other}: posed envelopes collide at "
                        f"{', '.join(runs)}, within the declared {klass} "
                        f"range {span} ({len(idx)} of {n} samples collide) "
                        f"— narrow the range, or move {other} out of the "
                        "swept path"
                    ),
                    severity="warn",
                )
            )
        if timed_out or unchecked:
            pairs = ", ".join(f"{a}—{b}" for a, b in sorted(unchecked)[:5])
            why = (
                f"the {SWEEP_WALL_BUDGET_S:g}s sweep wall-clock budget ran out"
                if timed_out
                else f"pair(s) {pairs} could not be checked (cross-scale or "
                "time budget)"
            )
            out.append(
                ValidationIssue(
                    rule="joint_sweep_unchecked",
                    subject=subject,
                    detail=f"{why} — this range is partly UNCHECKED, not clear",
                    severity="info",
                )
            )
    if over_budget:
        shown = ", ".join(over_budget[:5])
        more = f" (+{len(over_budget) - 5} more)" if len(over_budget) > 5 else ""
        out.append(
            ValidationIssue(
                rule="joint_sweep_unchecked",
                subject=f"{len(over_budget)} joint(s)",
                detail=(
                    f"{shown}{more}: the joint sweep's {SWEEP_EVAL_BUDGET}-"
                    "evaluation budget (one per sample) ran out before "
                    "reaching these ranges — they are UNCHECKED, not clear; "
                    "fewer 'samples' per joint fits more joints"
                ),
                severity="info",
            )
        )
    return out
