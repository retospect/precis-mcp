"""``view='kinematics'`` — R2, docs/backlog/port-rotation-and-lever-
composition.md (deleted on ship; ``git show 65f11ab3:docs/backlog/
port-rotation-and-lever-composition.md`` still has the text): the
derived swing of every declared transition (:mod:`precis_se.kinematics`),
plus its two findings — ``port_override_unapplied`` (``view='validate'``,
:func:`precis_se.validate.port_override_unapplied_findings`) and
``revolute_axis_mismatch`` (``view='drc'``, :mod:`precis_se.
kinematics_drc`).

Covers the R2 acceptance criteria verbatim: a two-state 90°-about-z hinge
prints ``axis 0 0 1`` / ``angle 90°`` with the tip from its envelope; a
``revolute`` joint declared with axis x on that connect mismatches, axis
z does not; a block placed with its own ``rot`` and a WORLD-frame joint
axis consistent with that placement draws no finding (the transform
test); a pose-less port with a ``rot`` override is
``port_override_unapplied`` + ``no pose``; a sourced ``step_angle``
agreeing/disagreeing with the derived one; a block with no declared
states gets its own one-line note.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.dispatch import Hub
from precis.handlers.material import MaterialHandler
from precis.store import Store
from precis.utils.units import format_quantity
from precis_se.handler import SeHandler

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


def _apply_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _apply_se_migrations(store)
    return SeHandler(hub=hub)


@pytest.fixture
def material(store: Store) -> MaterialHandler:
    return MaterialHandler(hub=Hub(store=store))


def _lines(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln.strip()]


#: A block whose port ``p1`` (pose declared, centred in x/y at half
#: height) swings 90° about z between two declared states — the
#: acceptance criteria's own fixture. ``box:w0.02d0.02h0.02`` is base-at-
#: z=0 (:func:`precis.cad.primitives.box`), so its local AABB corners sit
#: at x/y = ±0.01 m; ``p1`` at ``(0, 0, 0.01)`` (box-centred in x/y) makes
#: the in-plane arm exactly ``0.01·√2`` m and the tip exactly ``0.02`` m
#: (``2·0.01√2·sin(45°) = 0.02``) — a clean round number to assert on.
def _hinge_ops(*, no_pose: bool = False) -> list[dict[str, Any]]:
    add_port: dict[str, Any] = {"op": "add_port", "block": "hinge", "name": "p1"}
    if not no_pose:
        add_port["pose"] = [0.0, 0.0, 0.01]
    return [
        {"op": "add_block", "name": "hinge", "envelope": "box:w0.02d0.02h0.02"},
        add_port,
        {
            "op": "declare_states",
            "block": "hinge",
            "states": [
                {"name": "trans"},
                {
                    "name": "cis",
                    "port_pose_overrides": {"p1": {"rot": [0.0, 0.0, math.pi / 2.0]}},
                },
            ],
        },
        {
            "op": "declare_transitions",
            "block": "hinge",
            "transitions": [
                {
                    "from_state": "trans",
                    "to_state": "cis",
                    "driver_kind": "mechanical",
                }
            ],
        },
    ]


# ── axis / angle / arm / tip ─────────────────────────────────────────────


def test_kinematics_view_derives_axis_angle_arm_tip(handler: SeHandler) -> None:
    handler.put(id="hinge1", text=json.dumps({"ops": _hinge_ops()}))
    body = handler.get(id="hinge1", view="kinematics").body
    row = next(
        ln for ln in _lines(body) if "hinge" in ln and "p1" in ln and "trans" in ln
    )
    assert "0.000 0.000 1.000" in row  # axis, block frame, 3 dp
    assert format_quantity(math.pi / 2.0, "angle") in row  # angle: 90°
    arm_m = 0.01 * math.sqrt(2.0)
    assert format_quantity(arm_m, "length") in row  # arm (envelope)
    assert format_quantity(0.02, "length") in row  # tip = 2·arm·sin(45°) = 0.02 m


def test_unchanged_port_reads_as_no_change(handler: SeHandler) -> None:
    """A second port on the same block, present in neither state's
    ``port_pose_overrides`` — its frame never moves, so it reads ``—``,
    never ``no pose`` (it DOES carry a pose)."""
    ops = _hinge_ops() + [
        {"op": "add_port", "block": "hinge", "name": "p_static", "pose": [0.01, 0, 0]}
    ]
    handler.put(id="hinge_static", text=json.dumps({"ops": ops}))
    body = handler.get(id="hinge_static", view="kinematics").body
    row = next(ln for ln in _lines(body) if "p_static" in ln)
    assert "no pose" not in row
    assert "— (no change)" in row or row.count("—") >= 2


# ── the two findings' precondition: pose-less port ───────────────────────


def test_pose_less_port_override_is_unapplied_and_says_no_pose(
    handler: SeHandler,
) -> None:
    ops = _hinge_ops(no_pose=True)
    handler.put(id="hinge_nopose", text=json.dumps({"ops": ops}))
    validate_body = handler.get(id="hinge_nopose", view="validate").body
    assert "port_override_unapplied" in validate_body
    assert "declare set_port_pose first" in validate_body
    kinematics_body = handler.get(id="hinge_nopose", view="kinematics").body
    row = next(ln for ln in _lines(kinematics_body) if "p1" in ln)
    assert "no pose" in row


# ── revolute joint axis vs. derived port axis ────────────────────────────


def test_revolute_axis_mismatch_and_agreement(handler: SeHandler) -> None:
    ops = _hinge_ops() + [
        {"op": "add_block", "name": "wall_x", "envelope": "box:w0.01d0.01h0.01"},
        {"op": "add_port", "block": "wall_x", "name": "q"},
        {
            "op": "connect",
            "a": "wall_x.q",
            "b": "hinge.p1",
            "joint": {"class": "revolute", "axis": [1.0, 0.0, 0.0]},
        },
        {"op": "add_block", "name": "wall_z", "envelope": "box:w0.01d0.01h0.01"},
        {"op": "add_port", "block": "wall_z", "name": "q"},
        {
            "op": "connect",
            "a": "wall_z.q",
            "b": "hinge.p1",
            "joint": {"class": "revolute", "axis": [0.0, 0.0, 1.0]},
        },
    ]
    handler.put(id="hinge_revolute", text=json.dumps({"ops": ops}))
    body = handler.get(id="hinge_revolute", view="drc").body
    # The finding is subject-keyed off the endpoint that actually carries
    # a derived swing (hinge.p1) — neither wall block declares states, so
    # only ONE mismatch fires, for the wall_x connect (axis x vs. the
    # derived z); the wall_z connect (axis z, agrees exactly) fires none.
    mismatch_lines = [ln for ln in _lines(body) if "revolute_axis_mismatch" in ln]
    assert len(mismatch_lines) == 1
    assert "hinge.p1" in mismatch_lines[0]


def test_revolute_axis_mismatch_transforms_through_the_block_placement(
    handler: SeHandler,
) -> None:
    """The block itself is placed with its own ``rot`` (90° about y) — the
    port's BLOCK-frame axis (0, 0, 1) maps to WORLD (1, 0, 0) through that
    placement (``Ry(90°) @ (0,0,1) = (1,0,0)``); a joint axis declared
    consistently in world coordinates draws no finding (the transform
    itself is under test, not just its absence)."""
    ops = _hinge_ops()
    ops[0] = {**ops[0], "rot": [0.0, math.pi / 2.0, 0.0]}
    ops += [
        {"op": "add_block", "name": "wall", "envelope": "box:w0.01d0.01h0.01"},
        {"op": "add_port", "block": "wall", "name": "q"},
        {
            "op": "connect",
            "a": "wall.q",
            "b": "hinge.p1",
            "joint": {"class": "revolute", "axis": [1.0, 0.0, 0.0]},
        },
    ]
    handler.put(id="hinge_rotated", text=json.dumps({"ops": ops}))
    body = handler.get(id="hinge_rotated", view="drc").body
    assert "revolute_axis_mismatch" not in body


def test_revolute_axis_mismatch_absent_without_a_declared_axis(
    handler: SeHandler,
) -> None:
    """A ``revolute`` joint that never declares ``axis`` has nothing to
    compare against — silently skipped, not a crash."""
    ops = _hinge_ops() + [
        {"op": "add_block", "name": "wall", "envelope": "box:w0.01d0.01h0.01"},
        {"op": "add_port", "block": "wall", "name": "q"},
        {
            "op": "connect",
            "a": "wall.q",
            "b": "hinge.p1",
            "joint": {"class": "revolute"},
        },
    ]
    handler.put(id="hinge_no_axis", text=json.dumps({"ops": ops}))
    body = handler.get(id="hinge_no_axis", view="drc").body
    assert "revolute_axis_mismatch" not in body


# ── sourced step_angle vs. derived angle ─────────────────────────────────


def _link_step_angle(
    handler: SeHandler, material: MaterialHandler, store: Store, slug: str, rad: float
) -> None:
    mat = f"mat-{slug}"
    material.put(id=mat, title=mat)
    material.put(id=mat, property="step_angle", value=rad, unit="rad")
    design_ref = store.get_ref(kind="se", id=slug)
    mat_ref = store.get_ref(kind="material", id=mat)
    assert design_ref is not None and mat_ref is not None
    store.add_link(
        src_ref_id=design_ref.id,
        dst_ref_id=mat_ref.id,
        relation="made-of",
        meta={"block": "hinge"},
    )


def test_sourced_step_angle_agreeing_shows_no_disagreement(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    handler.put(id="hinge_agree", text=json.dumps({"ops": _hinge_ops()}))
    # 1.8 % off the derived pi/2 — inside the 10 % band.
    _link_step_angle(handler, material, store, "hinge_agree", 1.6)
    body = handler.get(id="hinge_agree", view="kinematics").body
    row = next(ln for ln in _lines(body) if "p1" in ln and "trans" in ln)
    assert format_quantity(1.6, "angle") in row
    assert "disagrees" not in row


def test_sourced_step_angle_disagreeing_is_flagged(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    handler.put(id="hinge_disagree", text=json.dumps({"ops": _hinge_ops()}))
    # 57 % off the derived pi/2 — past the 10 % band.
    _link_step_angle(handler, material, store, "hinge_disagree", 1.0)
    body = handler.get(id="hinge_disagree", view="kinematics").body
    row = next(ln for ln in _lines(body) if "p1" in ln and "trans" in ln)
    assert format_quantity(1.0, "angle") in row
    assert "disagrees" in row


# ── a block without declared states ──────────────────────────────────────


def test_block_without_declared_states_gets_one_line_note(handler: SeHandler) -> None:
    ops = _hinge_ops() + [
        {"op": "add_block", "name": "plain", "envelope": "box:w0.01d0.01h0.01"}
    ]
    handler.put(id="hinge_plain", text=json.dumps({"ops": ops}))
    body = handler.get(id="hinge_plain", view="kinematics").body
    assert "plain: no declared states" in body
    # The state-carrying block still renders its table alongside the note.
    assert any("hinge" in ln and "p1" in ln for ln in _lines(body))
