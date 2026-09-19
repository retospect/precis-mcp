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
tracking as ``_fret_chromophores``'s)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis_se.atomic.validate import PORT_ROT_MISMATCH_RAD
from precis_se.kinematics import KinematicsRow, derive
from precis_se.ops import SeTree
from precis_se.validate import ValidationIssue


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
    return out
