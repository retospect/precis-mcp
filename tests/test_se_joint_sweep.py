"""Joint sweep over a declared continuous range
(``precis_se.kinematics_drc.sweep_findings``).

The fixture: a base ``frame_box`` below the pivot, a rotor ``arm`` (a box
envelope offset +x from the pivot, so NOT rotation-invariant) joined to
it by a ``revolute`` connect about +z through the world origin, and a
small ``post`` on +y that the arm reaches at 90° and clears at 67.5°
(the arm's centreline passes 11.5 mm from the post centre there, against
5 mm arm half-width + 3.9 mm post half-extent along the normal).
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from precis.design import states as design_states
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis.utils.units import format_quantity
from precis_se import joints as se_joints
from precis_se import kinematics_drc, persist
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _rotor_ops(
    joint_params: dict[str, Any] | None,
    *,
    arm_port_pose: bool = True,
    arm_envelope: bool = True,
) -> list[dict[str, Any]]:
    arm: dict[str, Any] = {"op": "add_block", "name": "arm", "pose": [0.03, 0.0, 0.0]}
    if arm_envelope:
        arm["envelope"] = "box:w0.04d0.01h0.01"
    arm_port: dict[str, Any] = {"op": "add_port", "block": "arm", "name": "p"}
    if arm_port_pose:
        arm_port["pose"] = [-0.03, 0.0, 0.0]  # the pivot: world origin
    joint: dict[str, Any] = {"class": "revolute", "axis": [0.0, 0.0, 1.0]}
    if joint_params is not None:
        # The arm is the end that turns; a test passing 'moves' overrides.
        joint["params"] = {"moves": "arm", **joint_params}
    return [
        {
            "op": "add_block",
            "name": "frame_box",
            "envelope": "box:w0.01d0.01h0.01",
            "pose": [0.0, 0.0, -0.012],
        },
        {"op": "add_port", "block": "frame_box", "name": "q", "pose": [0, 0, 0.012]},
        arm,
        arm_port,
        {
            "op": "add_block",
            "name": "post",
            "envelope": "box:w0.006d0.006h0.01",
            "pose": [0.0, 0.03, 0.0],
        },
        {"op": "connect", "a": "frame_box.q", "b": "arm.p", "joint": joint},
    ]


def _put(handler: SeHandler, slug: str, ops: list[dict[str, Any]]) -> None:
    handler.put(id=slug, text=json.dumps({"ops": ops}))


def _sweep_lines(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if "joint_sweep" in ln]


# ── criteria 1 + 2: the range finds the 90° collision; a short one doesn't ──


def test_range_through_ninety_degrees_flags_arm_against_post(
    handler: SeHandler,
) -> None:
    _put(handler, "rotor_full", _rotor_ops({"range": [0.0, math.pi], "samples": 9}))
    body = handler.get(id="rotor_full", view="drc").body
    hits = [ln for ln in _sweep_lines(body) if "joint_sweep_interference" in ln]
    assert len(hits) == 1, body
    row = hits[0]
    assert "arm" in row and "post" in row
    assert "arm.p—frame_box.q" in row  # subject = the stored (sorted) connect
    ninety = format_quantity(math.pi / 2.0, "angle")
    assert f"collide at {ninety}, within" in row
    assert "1 of 9 samples" in row
    assert "warn" in row


def test_range_short_of_the_post_draws_no_sweep_finding(handler: SeHandler) -> None:
    _put(handler, "rotor_short", _rotor_ops({"range": [0.0, math.pi / 4.0]}))
    body = handler.get(id="rotor_short", view="drc").body
    assert [ln.split("\t")[1] for ln in _sweep_lines(body)] == ["joint_sweep_clean"]


# ── criterion 3: discrete states 0 and π pass view='sweep' — the gap ──────


def _arm_states(
    handler: SeHandler, store: Store, slug: str, poses: dict[str, dict[str, Any]]
) -> None:
    ops = _rotor_ops(None) + [
        {
            "op": "declare_states",
            "block": "arm",
            "states": [{"name": "rest"}] + [{"name": n} for n in poses],
        }
    ]
    _put(handler, slug, ops)
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    uid = persist.load_tree(store, ref.id).blocks["arm"].uid
    assert uid is not None
    for name, pose in poses.items():
        design_states.set_state_pose(store, ref.id, uid, name, pose)


def test_discrete_states_at_zero_and_pi_pass_sweep_without_a_range(
    handler: SeHandler, store: Store
) -> None:
    # 'flipped' = the arm rotated π about the pivot (world origin).
    flipped = {"xyz": [-0.03, 0.0, 0.0], "rot": [0.0, 0.0, math.pi]}
    _arm_states(handler, store, "rotor_states", {"flipped": flipped})
    sweep = handler.get(id="rotor_states", view="sweep").body
    assert "2/2 combination(s) checked" in sweep
    assert "no interference in any checked state" in sweep
    body = handler.get(id="rotor_states", view="drc").body
    assert [ln.split("\t")[1] for ln in _sweep_lines(body)] == ["joint_sweep_not_run"]


def test_state_posing_control_a_quarter_turn_state_is_caught_by_sweep(
    handler: SeHandler, store: Store
) -> None:
    """Control for the test above: the state poses really do move the
    arm, so its clean result is a real 0/π answer, not a no-op."""
    quarter = {"xyz": [0.0, 0.03, 0.0], "rot": [0.0, 0.0, math.pi / 2.0]}
    _arm_states(handler, store, "rotor_quarter", {"quarter": quarter})
    sweep = handler.get(id="rotor_quarter", view="sweep").body
    assert "arm ↔ post" in sweep


# ── criterion 4: no pose / no envelope contribute nothing ───────────────────


@pytest.mark.parametrize(
    ("slug", "kwargs"),
    [
        ("rotor_nopose", {"arm_port_pose": False}),
        ("rotor_noenv", {"arm_envelope": False}),
    ],
)
def test_pose_less_port_or_envelope_less_block_contributes_nothing(
    handler: SeHandler, slug: str, kwargs: dict[str, bool]
) -> None:
    _put(handler, slug, _rotor_ops({"range": [0.0, math.pi]}, **kwargs))
    body = handler.get(id=slug, view="drc").body
    assert _sweep_lines(body) == []


# ── criterion 5: write-time refusals ────────────────────────────────────────


def test_range_on_a_rigid_connect_is_refused_at_write(handler: SeHandler) -> None:
    ops = _rotor_ops(None)
    ops[-1] = {
        "op": "connect",
        "a": "frame_box.q",
        "b": "arm.p",
        "joint": {"class": "rigid", "params": {"range": [0.0, 1.0]}},
    }
    with pytest.raises(BadInput, match="revolute"):
        _put(handler, "rotor_rigid", ops)


@pytest.mark.parametrize(
    ("joint", "match"),
    [
        ({"class": "rigid", "params": {"range": [0, 1]}}, "prismatic | revolute"),
        (
            {"class": "cylindrical", "axis": [0, 0, 1], "params": {"range": [0, 1]}},
            "only",
        ),
        (
            {"class": "revolute", "axis": [0, 0, 1], "params": {"range": [1, 1]}},
            "lo < hi",
        ),
        (
            {"class": "revolute", "axis": [0, 0, 1], "params": {"range": [2, 1]}},
            "lo < hi",
        ),
        ({"class": "revolute", "axis": [0, 0, 1], "params": {"range": [0]}}, "lo, hi"),
        (
            {
                "class": "revolute",
                "axis": [0, 0, 1],
                "params": {"range": [0, 1], "moves": "x", "samples": 1},
            },
            "samples",
        ),
        (
            {
                "class": "revolute",
                "axis": [0, 0, 1],
                "params": {"range": [0, 1], "moves": "x", "samples": 2.5},
            },
            "samples",
        ),
        (
            {"class": "revolute", "params": {"range": [0, 1], "moves": "x"}},
            "needs the joint's 'axis'",
        ),
        (
            {"class": "revolute", "axis": [0, 0, 1], "params": {"samples": 5}},
            "'samples' needs a 'range'",
        ),
        (
            {"class": "revolute", "axis": [0, 0, 1], "params": {"moves": "x"}},
            "'moves' needs a 'range'",
        ),
        (
            {"class": "revolute", "axis": [0, 0, 1], "params": {"range": [0, 1]}},
            "needs 'moves'",
        ),
    ],
)
def test_range_param_vetting(joint: dict[str, Any], match: str) -> None:
    with pytest.raises(se_joints.JointError, match=match):
        se_joints.validate_joint(joint)


def test_range_param_normalizes() -> None:
    out = se_joints.validate_joint(
        {
            "class": "prismatic",
            "axis": [0, 0, 2],
            "params": {"range": [0, 1], "moves": " slider ", "samples": 4.0},
        }
    )
    assert out["params"] == {"range": [0.0, 1.0], "moves": "slider", "samples": 4}


# ── prismatic: a slide into the post, reported in mm ────────────────────────


def test_prismatic_slide_reports_first_colliding_travel_in_length(
    handler: SeHandler,
) -> None:
    ops: list[dict[str, Any]] = [
        {
            "op": "add_block",
            "name": "rail",
            "envelope": "box:w0.01d0.01h0.01",
            "pose": [0.0, 0.0, -0.012],
        },
        {"op": "add_port", "block": "rail", "name": "q", "pose": [0, 0, 0.012]},
        {"op": "add_block", "name": "slider", "envelope": "box:w0.01d0.01h0.01"},
        {"op": "add_port", "block": "slider", "name": "p", "pose": [0, 0, 0]},
        {
            "op": "add_block",
            "name": "post",
            "envelope": "box:w0.006d0.006h0.01",
            "pose": [0.03, 0.0, 0.0],
        },
        {
            "op": "connect",
            "a": "rail.q",
            "b": "slider.p",
            "joint": {
                "class": "prismatic",
                "axis": [1.0, 0.0, 0.0],
                "params": {"range": [0.0, 0.04], "moves": "slider", "samples": 5},
            },
        },
    ]
    _put(handler, "slide", ops)
    body = handler.get(id="slide", view="drc").body
    hits = [ln for ln in _sweep_lines(body) if "joint_sweep_interference" in ln]
    assert len(hits) == 1, body
    assert f"collide at {format_quantity(0.03, 'length')}, within" in hits[0]
    assert "slider" in hits[0] and "post" in hits[0]


# ── criterion 6: the 256-evaluation budget names what it skipped ────────────


def test_joints_past_the_evaluation_budget_are_named(
    handler: SeHandler, store: Store
) -> None:
    ops = _rotor_ops({"range": [0.0, math.pi / 4.0], "samples": 200})
    # A second rotor 200 mm clear of the first.
    ops += [
        {
            "op": "add_block",
            "name": "arm2",
            "envelope": "box:w0.01d0.01h0.01",
            "pose": [0.2, 0.0, 0.0],
        },
        {"op": "add_port", "block": "arm2", "name": "p", "pose": [0, 0, 0]},
        {
            "op": "add_block",
            "name": "base2",
            "envelope": "box:w0.01d0.01h0.01",
            "pose": [0.2, 0.0, -0.012],
        },
        {"op": "add_port", "block": "base2", "name": "q", "pose": [0, 0, 0.012]},
        {
            "op": "connect",
            "a": "base2.q",
            "b": "arm2.p",
            "joint": {
                "class": "revolute",
                "axis": [0.0, 0.0, 1.0],
                "params": {"range": [0.0, 1.0], "moves": "arm2", "samples": 60},
            },
        },
    ]
    _put(handler, "rotor_budget", ops)
    assert kinematics_drc.SWEEP_EVAL_BUDGET < 200 + 60
    ref = store.get_ref(kind="se", id="rotor_budget")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    before = {n: (list(b.pose), list(b.rot)) for n, b in tree.blocks.items()}
    found = kinematics_drc.sweep_findings(tree)
    after = {n: (list(b.pose), list(b.rot)) for n, b in tree.blocks.items()}
    assert after == before  # the sweep's poses are transient
    unchecked = [f for f in found if f.rule == "joint_sweep_unchecked"]
    assert len(unchecked) == 1
    assert unchecked[0].severity == "info"
    assert "arm2.p—base2.q" in unchecked[0].detail
    assert "arm.p—frame_box.q" not in unchecked[0].detail
    assert not [f for f in found if f.rule == "joint_sweep_interference"]


# ── the moving end is declared, not read off endpoint order ─────────────────


def test_moves_naming_neither_end_is_refused_at_write(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="'moves' is 'post'"):
        _put(
            handler,
            "rotor_badmoves",
            _rotor_ops({"range": [0.0, 1.0], "moves": "post"}),
        )


def test_endpoint_order_does_not_pick_the_moving_end() -> None:
    """Persist stores a connect with its endpoints sorted, so the stored
    ``b`` end is an accident of naming. Writing the connect both ways
    round must sweep the same (declared) end and find the same hit."""
    rows = []
    for flip in (False, True):
        ops = _rotor_ops({"range": [0.0, math.pi], "samples": 9})
        if flip:
            ops[-1] = {**ops[-1], "a": "arm.p", "b": "frame_box.q"}
        tree = apply_ops(SeTree(), ops)
        hits = [
            f
            for f in kinematics_drc.sweep_findings(tree)
            if f.rule == "joint_sweep_interference"
        ]
        assert len(hits) == 1
        rows.append(hits[0].detail)
    assert rows[0] == rows[1]
    assert "arm ↔ post" in rows[0]


def test_moves_drifted_off_both_ends_is_reported_unchecked() -> None:
    tree = apply_ops(SeTree(), _rotor_ops({"range": [0.0, math.pi]}))
    assert tree.connects[0].joint is not None
    tree.connects[0].joint["params"]["moves"] = "gone"
    found = kinematics_drc.sweep_findings(tree)
    assert [f.rule for f in found] == ["joint_sweep_unchecked"]
    assert "'gone'" in found[0].detail


# ── not-run / clean lines (gr464669) ────────────────────────────────────────


def _rules(tree: SeTree) -> list[str]:
    return [f.rule for f in kinematics_drc.sweep_findings(tree)]


def test_revolute_joints_without_a_range_say_the_sweep_did_not_run() -> None:
    tree = apply_ops(SeTree(), _rotor_ops(None))
    found = kinematics_drc.sweep_findings(tree)
    assert [f.rule for f in found] == ["joint_sweep_not_run"]
    assert found[0].severity == "info"
    assert found[0].detail.startswith("1 revolute/prismatic joints, 0 declare")


def test_design_without_joints_draws_no_sweep_finding() -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "box:w0.01d0.01h0.01"},
        {"op": "add_block", "name": "b", "envelope": "box:w0.01d0.01h0.01"},
    ]
    assert _rules(apply_ops(SeTree(), ops)) == []


def test_one_ranged_joint_sweeping_clean_reports_the_swept_count() -> None:
    tree = apply_ops(SeTree(), _rotor_ops({"range": [0.0, math.pi / 4.0]}))
    found = kinematics_drc.sweep_findings(tree)
    assert [f.rule for f in found] == ["joint_sweep_clean"]
    assert found[0].severity == "info"
    assert found[0].detail.startswith("1 joint(s) swept")
    assert found[0].subject == "1 joint(s)"


def test_interference_suppresses_the_clean_line() -> None:
    tree = apply_ops(SeTree(), _rotor_ops({"range": [0.0, math.pi], "samples": 9}))
    rules = _rules(tree)
    assert "joint_sweep_interference" in rules
    assert "joint_sweep_clean" not in rules


# ── rigid riders (gr462067) ─────────────────────────────────────────────────


def _rider_ops(rider_joint: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The rotor plus a ``rider`` 20 mm above the arm's tip and an
    ``obstacle`` 20 mm above the post. Authored, all clear; the arm turns
    clear of the obstacle, but a rider carried with it lands on it."""
    ops = _rotor_ops({"range": [0.0, math.pi], "samples": 9})
    ops += [
        {
            "op": "add_block",
            "name": "rider",
            "envelope": "box:w0.006d0.006h0.01",
            "pose": [0.03, 0.0, 0.02],
        },
        {"op": "add_port", "block": "rider", "name": "p", "pose": [0, 0, 0]},
        {
            "op": "add_block",
            "name": "obstacle",
            "envelope": "box:w0.006d0.006h0.01",
            "pose": [0.0, 0.03, 0.02],
        },
    ]
    if rider_joint is not None:
        ops += [
            {"op": "add_port", "block": "arm", "name": "r", "pose": [0, 0, 0.0]},
            {"op": "connect", "a": "arm.r", "b": "rider.p", "joint": rider_joint},
        ]
    return ops


def _rider_hits(tree: SeTree) -> list[str]:
    return [
        f.detail
        for f in kinematics_drc.sweep_findings(tree)
        if f.rule == "joint_sweep_interference" and "rider" in f.detail
    ]


def test_rigid_rider_collides_during_the_sweep_and_is_restored() -> None:
    tree = apply_ops(SeTree(), _rider_ops({"class": "rigid"}))
    before = {n: (list(b.pose), list(b.rot)) for n, b in tree.blocks.items()}
    hits = _rider_hits(tree)
    assert len(hits) == 1, hits
    assert "rider ↔ obstacle" in hits[0]
    after = {n: (list(b.pose), list(b.rot)) for n, b in tree.blocks.items()}
    assert after == before


@pytest.mark.parametrize(
    "rider_joint",
    [None, {"class": "revolute", "axis": [0.0, 0.0, 1.0]}],
    ids=["no-connect", "non-rigid-class"],
)
def test_rider_not_rigidly_attached_stays_put_during_the_sweep(
    rider_joint: dict[str, Any] | None,
) -> None:
    tree = apply_ops(SeTree(), _rider_ops(rider_joint))
    assert _rider_hits(tree) == []


def test_rigid_closure_excludes_the_fixed_end_even_with_a_loop_back() -> None:
    ops = _rider_ops({"class": "rigid"}) + [
        {"op": "add_port", "block": "frame_box", "name": "s", "pose": [0, 0, 0]},
        {"op": "add_port", "block": "rider", "name": "f", "pose": [0, 0, 0]},
        {
            "op": "connect",
            "a": "rider.f",
            "b": "frame_box.s",
            "joint": {"class": "rigid"},
        },
    ]
    tree = apply_ops(SeTree(), ops)
    swept = tree.connects[0]
    assert swept.joint is not None and swept.joint["class"] == "revolute"
    moving = kinematics_drc._moving_set(tree, swept, "arm", "frame_box")
    assert "frame_box" not in moving
    assert {"arm", "rider"} <= moving
    before = list(tree.blocks["frame_box"].pose)
    found = kinematics_drc.sweep_findings(tree)
    assert tree.blocks["frame_box"].pose == before
    assert not [f for f in found if "frame_box" in f.detail and "↔" in f.detail]
    assert len(_rider_hits(tree)) == 1
