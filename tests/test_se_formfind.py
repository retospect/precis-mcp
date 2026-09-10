"""The se ``formfind`` op — force-density form-finding over the axial
subgraph, poses written back ``origin: 'proposed'``
(docs/backlog/structural-solution-space.md slice 2;
:mod:`precis_se.formfind` bridging :mod:`precis.structsolve`)."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import precis_se
from precis.dispatch import Hub
from precis.store import Store
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


_TIE = {"tension_capacity": 100.0, "compression_capacity": 0.0}
_STRUT = {"tension_capacity": 0.0, "compression_capacity": 100.0}


def _pin_structure(
    nodes: dict[str, list[float]],
    members: Sequence[tuple[str, str, dict[str, float] | None]],
    fixed: dict[str, Any] | None = None,
) -> SeTree:
    ops: list[dict[str, Any]] = []
    for name, pose in nodes.items():
        ops.append({"op": "add_block", "name": name, "pose": pose})
        ops.append({"op": "add_port", "block": name, "name": "pin"})
    for a, b, params in members:
        joint: dict[str, Any] = {"class": "axial"}
        if params is not None:
            joint["params"] = params
        ops.append({"op": "connect", "a": f"{a}.pin", "b": f"{b}.pin", "joint": joint})
    for name, spec in (fixed or {}).items():
        ops.append({"op": "set_load", "block": name, "fixed": spec})
    return apply_ops(SeTree(), ops)


_BASE = {
    "b0": [0.0, 0.0, 0.0],
    "b1": [2.0, 0.0, 0.0],
    "b2": [1.0, 2.0, 0.0],
}
_ALL_FIXED = {name: True for name in _BASE}


def _tripod(apex_pose: list[float] | None = None) -> SeTree:
    """Three fully fixed anchors + one free apex on three ties."""
    nodes = dict(_BASE, apex=apex_pose or [0.0, 0.0, 5.0])
    members = [(n, "apex", dict(_TIE)) for n in _BASE]
    return _pin_structure(nodes, members, fixed=_ALL_FIXED)


_CENTROID = [1.0, 2.0 / 3.0, 0.0]


# ── the happy path ───────────────────────────────────────────────────────


def test_solved_pose_lands_at_equilibrium_and_is_stamped_proposed() -> None:
    tree = _tripod()
    apply_ops(tree, [{"op": "formfind", "move": "all"}])
    apex = tree.blocks["apex"]
    assert np.allclose(apex.pose, _CENTROID)
    assert apex.origins.get("pose") == "proposed"
    # anchors untouched, stamp-free
    for name, pose in _BASE.items():
        assert tree.blocks[name].pose == pose
        assert "pose" not in tree.blocks[name].origins


def test_already_proposed_poses_move_without_explicit_authorization() -> None:
    tree = _tripod()
    apply_ops(
        tree,
        [
            {
                "op": "set_pose",
                "block": "apex",
                "pose": [9.0, 9.0, 9.0],
                "origin": "proposed",
            }
        ],
    )
    apply_ops(tree, [{"op": "formfind"}])
    assert np.allclose(tree.blocks["apex"].pose, _CENTROID)


def test_user_poses_are_contract_by_default() -> None:
    tree = _tripod()
    with pytest.raises(OpError, match="no movable nodes"):
        apply_ops(tree, [{"op": "formfind"}])
    assert tree.blocks["apex"].pose == [0.0, 0.0, 5.0]  # untouched


def test_member_q_override_weights_the_equilibrium() -> None:
    tree = _tripod()
    apply_ops(
        tree,
        [
            {
                "op": "formfind",
                "move": ["apex"],
                "q": [{"a": "b0.pin", "b": "apex.pin", "q": 8.0}],
            }
        ],
    )
    weights = np.array([8.0, 1.0, 1.0])
    anchors = np.array(list(_BASE.values()))
    assert np.allclose(
        tree.blocks["apex"].pose, (weights[:, None] * anchors).sum(axis=0) / 10.0
    )


def test_partial_fixity_rides_the_rail() -> None:
    tree = _tripod()
    apply_ops(tree, [{"op": "set_load", "block": "apex", "fixed": ["z"]}])
    apply_ops(tree, [{"op": "formfind", "move": "all"}])
    assert tree.blocks["apex"].pose[2] == 5.0  # prescribed z kept verbatim
    assert np.allclose(tree.blocks["apex"].pose[:2], _CENTROID[:2])


def test_rot_and_non_axial_blocks_are_untouched() -> None:
    tree = _tripod()
    apply_ops(
        tree,
        [
            {"op": "set_pose", "block": "apex", "pose": [0, 0, 5], "rot": [0, 0, 45]},
            {"op": "add_block", "name": "bystander", "pose": [7.0, 7.0, 7.0]},
        ],
    )
    apply_ops(tree, [{"op": "formfind", "move": "all"}])
    assert tree.blocks["apex"].rot == [0.0, 0.0, 45.0]
    assert tree.blocks["bystander"].pose == [7.0, 7.0, 7.0]


def test_node_already_at_equilibrium_keeps_its_user_stamp() -> None:
    tree = _tripod(apex_pose=list(_CENTROID))
    apply_ops(tree, [{"op": "formfind", "move": "all"}])
    assert "pose" not in tree.blocks["apex"].origins  # unmoved → still contract


def test_prism_with_struts_solves_to_a_checked_equilibrium() -> None:
    s32 = math.sqrt(3.0) / 2.0
    nodes = {
        "b0": [1.0, 0.0, 0.0],
        "b1": [-0.5, s32, 0.0],
        "b2": [-0.5, -s32, 0.0],
        "t0": [0.9, 0.1, 1.1],  # rough guesses — the solve ignores them
        "t1": [-0.4, 0.8, 0.9],
        "t2": [0.1, -1.2, 1.0],
    }
    cables = [
        ("t0", "t1"),
        ("t1", "t2"),
        ("t2", "t0"),
        ("b0", "t0"),
        ("b1", "t1"),
        ("b2", "t2"),
    ]
    struts = [("b0", "t1"), ("b1", "t2"), ("b2", "t0")]
    members = [(a, b, dict(_TIE)) for a, b in cables] + [
        (a, b, dict(_STRUT)) for a, b in struts
    ]
    tree = _pin_structure(nodes, members, fixed={n: True for n in ("b0", "b1", "b2")})
    # default q_strut=−1 exactly cancels each top node's vertical cable —
    # the honest singular refusal, not a garbage shape
    with pytest.raises(OpError, match="singular"):
        apply_ops(tree, [{"op": "formfind", "move": "all"}])
    apply_ops(tree, [{"op": "formfind", "move": "all", "q_strut": -0.5}])
    # independent equilibrium recheck at each solved top node
    q_of = {
        frozenset(m): (1.0 if m in {frozenset(c) for c in cables} else -0.5)
        for m in map(frozenset, cables + struts)
    }
    poses = {n: np.asarray(tree.blocks[n].pose) for n in nodes}
    for top in ("t0", "t1", "t2"):
        balance = np.zeros(3)
        for a, b in cables + struts:
            if top not in (a, b):
                continue
            other = b if a == top else a
            balance += q_of[frozenset((a, b))] * (poses[other] - poses[top])
        assert np.allclose(balance, 0.0, atol=1e-9)
        assert tree.blocks[top].origins.get("pose") == "proposed"


# ── refusal paths ────────────────────────────────────────────────────────


def test_no_axial_members_rejects() -> None:
    tree = apply_ops(SeTree(), [{"op": "add_block", "name": "lonely"}])
    with pytest.raises(OpError, match="no axial members"):
        apply_ops(tree, [{"op": "formfind", "move": "all"}])


def test_stray_keys_reject() -> None:
    tree = _tripod()
    with pytest.raises(OpError, match="unknown key"):
        apply_ops(tree, [{"op": "formfind", "move": "all", "qtie": 2.0}])


def test_move_typo_and_anchor_listing_reject() -> None:
    tree = _tripod()
    with pytest.raises(OpError, match="names no axial node"):
        apply_ops(tree, [{"op": "formfind", "move": ["apeks"]}])
    with pytest.raises(OpError, match="fully fixed"):
        apply_ops(tree, [{"op": "formfind", "move": ["b0"]}])
    with pytest.raises(OpError, match="empty list"):
        apply_ops(tree, [{"op": "formfind", "move": []}])


def test_wrong_sign_densities_reject_by_role() -> None:
    tree = _tripod()
    with pytest.raises(OpError, match="tension-only"):
        apply_ops(tree, [{"op": "formfind", "move": "all", "q_tie": -1.0}])
    with pytest.raises(OpError, match="compression-only"):
        apply_ops(tree, [{"op": "formfind", "move": "all", "q_strut": 1.0}])
    with pytest.raises(OpError, match="tension-only"):
        apply_ops(
            tree,
            [
                {
                    "op": "formfind",
                    "move": "all",
                    "q": [{"a": "b0.pin", "b": "apex.pin", "q": -2.0}],
                }
            ],
        )


def test_duplicate_q_override_rejects_instead_of_last_wins() -> None:
    tree = _tripod()
    with pytest.raises(OpError, match="twice"):
        apply_ops(
            tree,
            [
                {
                    "op": "formfind",
                    "move": "all",
                    "q": [
                        {"a": "b0.pin", "b": "apex.pin", "q": 2.0},
                        # same member, endpoints reversed — still a dup
                        {"a": "apex.pin", "b": "b0.pin", "q": 3.0},
                    ],
                }
            ],
        )


def test_q_override_naming_no_member_rejects_with_roster() -> None:
    tree = _tripod()
    with pytest.raises(OpError, match="names no live axial member"):
        apply_ops(
            tree,
            [
                {
                    "op": "formfind",
                    "move": "all",
                    "q": [{"a": "b0.pin", "b": "b1.pin", "q": 2.0}],
                }
            ],
        )


def test_undeclared_role_needs_an_explicit_q() -> None:
    nodes = dict(_BASE, apex=[0.0, 0.0, 5.0])
    members: list[tuple[str, str, dict[str, float] | None]] = [
        ("b0", "apex", dict(_TIE)),
        ("b1", "apex", dict(_TIE)),
        ("b2", "apex", None),  # no capacity pair → no honest default sign
    ]
    tree = _pin_structure(nodes, members, fixed=_ALL_FIXED)
    with pytest.raises(OpError, match="no honest default"):
        apply_ops(tree, [{"op": "formfind", "move": "all"}])
    apply_ops(
        tree,
        [
            {
                "op": "formfind",
                "move": "all",
                "q": [{"a": "b2.pin", "b": "apex.pin", "q": 1.0}],
            }
        ],
    )
    assert np.allclose(tree.blocks["apex"].pose, _CENTROID)


def test_collapse_refuses_and_writes_nothing_back() -> None:
    nodes = {"a0": [0.0, 0.0, 0.0], "a1": [0.0, 0.0, 0.0], "free": [1.0, 1.0, 1.0]}
    members: list[tuple[str, str, dict[str, float] | None]] = [
        ("a0", "free", dict(_TIE)),
        ("a1", "free", dict(_TIE)),
    ]
    tree = _pin_structure(nodes, members, fixed={"a0": True, "a1": True})
    with pytest.raises(OpError, match="collapses"):
        apply_ops(tree, [{"op": "formfind", "move": "all"}])
    assert tree.blocks["free"].pose == [1.0, 1.0, 1.0]
    assert "pose" not in tree.blocks["free"].origins


def test_no_anchors_at_all_surfaces_the_solver_refusal() -> None:
    tree = _pin_structure(
        {"x": [0.0, 0.0, 0.0], "y": [1.0, 0.0, 0.0]},
        [("x", "y", dict(_TIE))],
    )
    with pytest.raises(OpError, match="objectives.fixed"):
        apply_ops(tree, [{"op": "formfind", "move": "all"}])


def test_self_loop_axial_member_rejects() -> None:
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "solo", "pose": [0.0, 0.0, 0.0]},
        {"op": "add_port", "block": "solo", "name": "p1"},
        {"op": "add_port", "block": "solo", "name": "p2"},
        {
            "op": "connect",
            "a": "solo.p1",
            "b": "solo.p2",
            "joint": {"class": "axial", "params": dict(_TIE)},
        },
    ]
    tree = apply_ops(SeTree(), ops)
    with pytest.raises(OpError, match="no usable line of action"):
        apply_ops(tree, [{"op": "formfind", "move": "all"}])


# ── through the handler (persist round-trip + the freedom stamp) ─────────


def test_formfind_round_trips_through_the_store(handler: SeHandler) -> None:
    ops: list[dict[str, Any]] = []
    for name, pose in dict(_BASE, apex=[0.0, 0.0, 5.0]).items():
        ops.append({"op": "add_block", "name": name, "pose": pose})
        ops.append({"op": "add_port", "block": name, "name": "pin"})
    for name in _BASE:
        ops.append(
            {
                "op": "connect",
                "a": f"{name}.pin",
                "b": "apex.pin",
                "joint": {"class": "axial", "params": dict(_TIE)},
            }
        )
    for name in _BASE:
        ops.append({"op": "set_load", "block": name, "fixed": True})
    handler.put(id="tripod", text=json.dumps({"ops": ops}))
    handler.edit(id="tripod", ops=[{"op": "formfind", "move": "all"}])
    body = handler.get(id="tripod", view="block", args={"name": "apex"}).body
    assert "pose: [1, 0.666667, 0] m" in body
    freedom = handler.get(id="tripod", view="freedom").body
    assert "apex.pose" in freedom  # stamped proposed, visible as revisable
    # and the checker agrees the result is a structure it can classify
    stability = handler.get(id="tripod", view="stability").body
    assert "verdict:" in stability
