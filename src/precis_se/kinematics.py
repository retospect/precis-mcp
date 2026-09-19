"""The swing of every declared transition — R2, docs/backlog/
port-rotation-and-lever-composition.md (the file was deleted on R1's
ship; ``git show 65f11ab3:docs/backlog/port-rotation-and-lever-
composition.md`` still has it).

R1 (``bind_structure``, :mod:`precis_se.atomic.bind`) measures a port's
frame off a bound structure's atoms. R2 derives something different but
related: nothing new is *declared* — two states already carry the port's
frame in each (``port_pose_overrides[port].rot``, composed onto the
port's own ``rot`` exactly the way :func:`precis_se.handler.
_apply_port_delta` composes it for a transient pose), so the rotation a
transition performs is ``R_to · R_fromᵀ``, read as axis-angle through
:func:`~precis.cad.vec.axis_angle_from_matrix`.

**Precondition:** the port carries a pose. ``_apply_port_delta`` drops
every override on a pose-less port (nothing to displace), so a
pose-less port's row here is ``no_pose=True`` rather than a bogus "no
rotation" — the whole reason an undeclared pivot must never look like a
non-rotating one. :mod:`precis_se.validate`'s ``port_override_unapplied``
finding is this same precondition's write-time echo.

Ordinary blocks only — templates own declared states, an instance/array
inherits (:func:`precis.design.states.states_for` is keyed by
``block_uid``, and only an ordinary/template block's uid ever carries a
row; same "instances skip" rule :mod:`precis_se.order`'s walk and
:mod:`precis_se.precedent`'s slice-5 findings both already follow).

Sourced facts (``step_angle``, ``rotation_rate``, ``rotation_barrier``)
resolve through the star schema exactly the way
:mod:`precis_se.compose`'s ``delta_length`` does —
:func:`precis_se.library._resolve_star_value` over a one-block
:class:`~precis_se.library._Candidate`, the same "material/component
property, unknown key mints proposed-tier on first write" path, no new
machinery needed here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad import dsl as cad_dsl
from precis.cad.vec import aabb_corners, axis_angle_from_matrix
from precis.cad.vec import rotation as cad_rotation
from precis.design import states as design_states
from precis_se.library import (
    _Candidate,
    _ReadCache,
    _resolve_star_value,
    value_row_number,
)
from precis_se.ops import PortSpec, SeBlock, SeTree, effective_envelope, effective_ports

#: The three sourced facts a rotary transition may carry, resolved
#: through the star schema like :data:`precis_se.compose.DELTA_KEY` —
#: radians, hertz, electronvolts respectively (module docstring).
STEP_ANGLE_KEY = "step_angle"
ROTATION_RATE_KEY = "rotation_rate"
ROTATION_BARRIER_KEY = "rotation_barrier"

#: A sourced ``step_angle`` disagreeing with the derived one by more than
#: this relative fraction is flagged (R2 acceptance criteria).
STEP_ANGLE_DISAGREE_FRACTION = 0.10


@dataclass(frozen=True)
class KinematicsRow:
    """One ``(block, transition, port)`` row of ``view='kinematics'``.

    ``axis``/``angle_rad`` are ``None``/``0.0`` together whenever the
    port's frame does not change between the two states (renders as
    ``—``, never confused with :attr:`no_pose` — see the module
    docstring). ``arm_m``/``tip_m`` are ``None`` exactly when ``axis`` is
    (no rotation, nothing to lever) or the block has no usable envelope.
    ``step_angle_rad``/``step_angle_source`` are the sourced facts
    (:data:`STEP_ANGLE_KEY`), independent of whether a swing was derived
    — the whole POINT of :attr:`disagree` is comparing the two."""

    block: str
    transition: str
    port: str
    axis: tuple[float, float, float] | None
    angle_rad: float
    arm_m: float | None
    tip_m: float | None
    step_angle_rad: float | None
    step_angle_source: str | None
    no_pose: bool = False
    disagree: bool = False


@dataclass(frozen=True)
class DeriveResult:
    """:func:`derive`'s return: the rows, plus which ordinary blocks
    ``design_states.states_for`` found ≥1 declared state for — regardless
    of whether that block ALSO declared a transition (a states-but-no-
    transitions block contributes no row, but still isn't a "no declared
    states" block). The handler's ``view='kinematics'`` render used to
    re-query ``states_for`` per block just to answer this same question a
    second time; :attr:`state_carrying_blocks` is the answer derive()
    already had, at no extra store cost."""

    rows: list[KinematicsRow]
    state_carrying_blocks: frozenset[str]


def compose_port_rot(
    base_rot: list[float] | tuple[float, float, float] | None,
    delta_rot: list[float] | tuple[float, float, float] | None,
) -> NDArray[np.float64]:
    """The rotation MATRIX a state's ``rot`` delta composes onto a port's
    own base ``rot``, in the BLOCK frame — ``R_delta @ R_base``, factored
    out of :func:`precis_se.handler._apply_port_delta` so this module can
    read the matrix directly instead of round-tripping through Euler
    radians on every state (that function still owns *storing* the
    result back as Euler radians for a transient pose; the arithmetic
    lives here so the two can never drift apart). See
    ``_apply_port_delta``'s docstring for the frame-order rationale —
    this is NOT the port-local ``R_base @ R_delta`` a scene mate's spin
    would use."""
    base = cad_rotation(*(float(x) for x in (base_rot or (0.0, 0.0, 0.0)))).R
    if delta_rot is None:
        return base
    delta = cad_rotation(*(float(x) for x in delta_rot)).R
    return delta @ base


def _state_port_rot(
    port: PortSpec, state: design_states.BlockState
) -> NDArray[np.float64]:
    override = (state.port_pose_overrides or {}).get(port.name)
    delta_rot = override.get("rot") if isinstance(override, dict) else None
    return compose_port_rot(port.rot, delta_rot)


def _port_arm_m(
    tree: SeTree, node: SeBlock, port: PortSpec, axis: tuple[float, float, float]
) -> float | None:
    """The distance from ``port``'s origin to the block envelope's
    farthest extent, measured in the plane normal to ``axis`` — a
    geometric UPPER BOUND on the lever arm the block itself offers (the
    module docstring's "arm (envelope)"). ``None`` when the block has no
    envelope, or it doesn't parse — the caller shows ``—``, never a
    fabricated number."""
    env = effective_envelope(tree, node)
    if not env:
        return None
    try:
        prim = cad_dsl.build_config(env)
    except cad_dsl.DslError:
        return None
    lo, hi = prim.aabb_local()
    if not (np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
        return None
    origin = np.asarray(port.pose, dtype=np.float64)
    axis_arr = np.asarray(axis, dtype=np.float64)
    best = 0.0
    for corner in aabb_corners(lo, hi):
        d = np.asarray(corner, dtype=np.float64) - origin
        d_perp = d - float(np.dot(d, axis_arr)) * axis_arr
        dist = float(np.linalg.norm(d_perp))
        if dist > best:
            best = dist
    return best


def derive(store: Any, tree: SeTree, ref_id: int) -> DeriveResult:
    """Every ``(block, transition, port)`` kinematics row in ``tree`` —
    ordinary blocks with ≥1 declared state AND ≥1 declared transition
    only (module docstring); a block with declared states but no
    transitions, or none at all, contributes no ROW here, but
    :attr:`DeriveResult.state_carrying_blocks` still names the former —
    the "say so" one-liner for a block with NEITHER is
    :mod:`precis_se.handler`'s render, off that set, rather than a
    second ``states_for`` query per block."""
    rows: list[KinematicsRow] = []
    state_carrying: set[str] = set()
    cache = _ReadCache(store)
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        if node.template is not None:
            continue
        if node.uid is None:
            continue
        states = design_states.states_for(store, ref_id, node.uid)
        if not states:
            continue
        state_carrying.add(name)
        transitions = design_states.transitions_for(store, ref_id, node.uid)
        if not transitions:
            continue
        by_name = {s.name: s for s in states}
        ports = effective_ports(tree, node)
        cand = _Candidate(tree.own_slug or "", name, node, tree, ref_id)
        step_hit = _resolve_star_value(cand, STEP_ANGLE_KEY, cache)
        step_angle_rad = value_row_number(step_hit[0]) if step_hit is not None else None
        step_angle_source = step_hit[2] if step_hit is not None else None
        for t in transitions:
            state_from = by_name.get(t.from_state)
            state_to = by_name.get(t.to_state)
            if state_from is None or state_to is None:
                continue  # FK-enforced at write time; defense in depth only
            label = f"{t.from_state} → {t.to_state}"
            for port_name in sorted(ports):
                port = ports[port_name]
                if port.pose is None:
                    rows.append(
                        KinematicsRow(
                            block=name,
                            transition=label,
                            port=port_name,
                            axis=None,
                            angle_rad=0.0,
                            arm_m=None,
                            tip_m=None,
                            step_angle_rad=None,
                            step_angle_source=None,
                            no_pose=True,
                        )
                    )
                    continue
                R_from = _state_port_rot(port, state_from)
                R_to = _state_port_rot(port, state_to)
                R_swing = R_to @ R_from.T
                axis, angle = axis_angle_from_matrix(R_swing)
                arm_m: float | None = None
                tip_m: float | None = None
                if axis is not None:
                    arm_m = _port_arm_m(tree, node, port, axis)
                    if arm_m is not None:
                        tip_m = 2.0 * arm_m * math.sin(angle / 2.0)
                disagree = False
                if (
                    axis is not None
                    and step_angle_rad is not None
                    and abs(step_angle_rad) > 1e-12
                ):
                    rel = abs(angle - abs(step_angle_rad)) / abs(step_angle_rad)
                    disagree = rel > STEP_ANGLE_DISAGREE_FRACTION
                rows.append(
                    KinematicsRow(
                        block=name,
                        transition=label,
                        port=port_name,
                        axis=axis,
                        angle_rad=angle if axis is not None else 0.0,
                        arm_m=arm_m,
                        tip_m=tip_m,
                        step_angle_rad=step_angle_rad,
                        step_angle_source=step_angle_source,
                        disagree=disagree,
                    )
                )
    return DeriveResult(rows=rows, state_carrying_blocks=frozenset(state_carrying))
