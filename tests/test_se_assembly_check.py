"""Macro relax v1 — check-and-report (:mod:`precis_se.assembly_check`,
docs/backlog/structure-workspaces.md slice 5). Acceptance: a redundant
mate names the redundant set; an over-tight stack names the failing chain
under worst-case AND RSS; an interfering pair reports depth."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis_se import assembly_check as ac
from precis_se.handler import SeHandler
from precis_se.measures import MeasureSpec, stackup
from precis_se.ops import SeTree, apply_ops


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _tree(ops: list[dict[str, Any]]) -> SeTree:
    return apply_ops(SeTree(), ops)


_ALL6 = ["x", "y", "z", "rx", "ry", "rz"]


def _door(hinges: list[tuple[str, list[float], list[float]]]) -> SeTree:
    """A grounded frame and a door, joined by the given revolute hinges:
    ``(name, port position on both blocks (world), axis)``."""
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "frame", "pose": [0.0, 0.0, 0.0], "pose_unit": "m"},
        {"op": "add_block", "name": "door", "pose": [0.5, 0.0, 0.0], "pose_unit": "m"},
        {"op": "set_load", "block": "frame", "fixed": _ALL6},
    ]
    for name, p, axis in hinges:
        ops.append(
            {
                "op": "add_port",
                "block": "frame",
                "name": name,
                "pose": p,
                "pose_unit": "m",
            }
        )
        local = [p[0] - 0.5, p[1], p[2]]
        ops.append(
            {
                "op": "add_port",
                "block": "door",
                "name": name,
                "pose": local,
                "pose_unit": "m",
            }
        )
        ops.append(
            {
                "op": "connect",
                "a": f"frame.{name}",
                "b": f"door.{name}",
                "joint": {"class": "revolute", "axis": axis},
            }
        )
    return _tree(ops)


# ── redundancy: hand-counted fixtures ────────────────────────────────────


def test_single_hinge_is_not_redundant_and_leaves_one_dof() -> None:
    rep = ac.redundancy(_door([("h1", [0.0, 0.0, 0.0], [0, 0, 1])]))
    # frame support 6 rows + revolute 5 rows = 11; two bodies = 12 columns
    assert rep.rows == 11
    assert rep.rank == 11
    assert rep.redundant == 0
    assert rep.mobility == 1  # the door swings
    assert rep.groups == []
    assert rep.grounded


def test_two_coaxial_hinges_name_the_redundant_set() -> None:
    rep = ac.redundancy(
        _door(
            [
                ("h1", [0.0, 0.0, 0.0], [0, 0, 1]),
                ("h2", [0.0, 0.0, 1.0], [0, 0, 1]),
            ]
        )
    )
    # the second hinge repeats the first's 5 constraints: 16 rows, rank 11
    assert rep.rows == 16
    assert rep.rank == 11
    assert rep.redundant == 5
    assert rep.mobility == 1
    assert rep.groups == [["frame.h1—door.h1", "frame.h2—door.h2"]]


def test_two_skew_hinges_lock_the_door_and_are_still_redundant() -> None:
    rep = ac.redundancy(
        _door(
            [
                ("h1", [0.0, 0.0, 0.0], [0, 0, 1]),
                ("h2", [0.0, 0.0, 1.0], [0, 1, 0]),
            ]
        )
    )
    # two revolutes on different axes forbid every relative motion: rank
    # 12 (fully locked) from 16 rows → 4 redundant, both hinges involved
    assert rep.rank == 12
    assert rep.redundant == 4
    assert rep.mobility == 0
    assert rep.groups == [["frame.h1—door.h1", "frame.h2—door.h2"]]


def test_rank_tolerance_reads_micro_radian_coaxial_as_redundant() -> None:
    rep = ac.redundancy(
        _door(
            [
                ("h1", [0.0, 0.0, 0.0], [0, 0, 1]),
                ("h2", [0.0, 0.0, 1.0], [0, 1e-8, 1]),
            ]
        )
    )
    assert rep.redundant == 5


def test_unmodelled_connects_are_listed_not_guessed() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "a"},
            {"op": "add_block", "name": "b"},
            {"op": "add_port", "block": "a", "name": "p"},
            {"op": "add_port", "block": "b", "name": "q"},
            {"op": "connect", "a": "a.p", "b": "b.q", "joint": {"class": "compliant"}},
        ]
    )
    rep = ac.redundancy(tree)
    assert rep.rows == 0
    assert rep.unmodelled == [
        ("a.p—b.q", "compliant: stiffness/topology, not a rigid constraint")
    ]
    assert not rep.grounded
    assert rep.mobility == 12


def test_overconstrained_finding_names_the_group(handler: SeHandler) -> None:
    tree = _door(
        [
            ("h1", [0.0, 0.0, 0.0], [0, 0, 1]),
            ("h2", [0.0, 0.0, 1.0], [0, 0, 1]),
        ]
    )
    check = ac.assembly_check(handler.store, tree, 0, budget_s=None)
    over = [f for f in check.findings if f.rule == "assembly.overconstrained"]
    assert len(over) == 1
    assert over[0].severity == "warn"
    assert "5 redundant of 16" in over[0].subject
    assert "{frame.h1—door.h1, frame.h2—door.h2}" in over[0].detail
    assert check.method == "assembly"
    assert len(check.constraints) == 3  # two hinges + the support


# ── stack-up: worst-case and RSS ─────────────────────────────────────────


def _stack(declared_c: float) -> list[MeasureSpec]:
    # c = b + 0.1 ± 0.04, b = a + 0.1 ± 0.03, a = 1.0 → c derives 1.2;
    # worst case ±0.07, RSS ±0.05
    return [
        MeasureSpec(block="a", name="x", value=1.0),
        MeasureSpec(
            block="b", name="x", relation={"source": "a.x", "offset": 0.1, "tol": 0.03}
        ),
        MeasureSpec(
            block="c",
            name="x",
            value=declared_c,
            relation={"source": "b.x", "offset": 0.1, "tol": 0.04},
        ),
    ]


def test_stackup_accumulates_rss_beside_worst_case() -> None:
    by_key = {r.measure: r for r in stackup(_stack(1.2))}
    assert by_key["c.x"].tol_accum == pytest.approx(0.07)
    assert by_key["c.x"].tol_rss == pytest.approx(0.05)
    assert by_key["c.x"].problem is None


def _measured_tree(measures: list[MeasureSpec]) -> SeTree:
    tree = _tree(
        [
            {"op": "add_block", "name": "a"},
            {"op": "add_block", "name": "b"},
            {"op": "add_block", "name": "c"},
        ]
    )
    tree.measures.extend(measures)
    return tree


def test_over_tight_stack_fails_both_and_names_the_chain(handler: SeHandler) -> None:
    check = ac.assembly_check(
        handler.store, _measured_tree(_stack(1.3)), 0, budget_s=None
    )
    rules = {f.rule: f for f in check.findings if f.rule.startswith("stackup.")}
    assert set(rules) == {"stackup.worst_case", "stackup.rss"}
    for f in rules.values():
        assert f.subject == "c.x"
        assert "chain c.x ← b.x ← a.x" in f.detail
    assert "over-tight under both stacks" in rules["stackup.worst_case"].detail
    # drc's own tolerance_mismatch row is superseded, not duplicated
    assert not any(f.rule == "tolerance_mismatch" for f in check.findings)
    rows = {r.measure: r for r in check.stack}
    assert rows["c.x"].wc is not None and rows["c.x"].rss is not None
    assert rows["b.x"].wc is None and rows["b.x"].rss is None


def test_stack_inside_worst_case_but_outside_rss_fails_rss_only(
    handler: SeHandler,
) -> None:
    check = ac.assembly_check(
        handler.store, _measured_tree(_stack(1.26)), 0, budget_s=None
    )
    rules = [f.rule for f in check.findings if f.rule.startswith("stackup.")]
    assert rules == ["stackup.rss"]
    (f,) = [f for f in check.findings if f.rule == "stackup.rss"]
    assert "inside the worst-case band but outside the RSS band" in f.detail
    assert "chain c.x ← b.x ← a.x" in f.detail


def test_stack_within_rss_is_clean(handler: SeHandler) -> None:
    check = ac.assembly_check(
        handler.store, _measured_tree(_stack(1.24)), 0, budget_s=None
    )
    assert not any(f.rule.startswith("stackup.") for f in check.findings)
    assert all(r.wc is None and r.rss is None for r in check.stack)


# ── interference: depth on the row ───────────────────────────────────────


def test_interfering_pair_reports_depth(handler: SeHandler) -> None:
    # two 0.1 m rods along +z; the second starts 0.08 m up → 0.02 m overlap
    rod = "cyl:r0.02h0.1"
    tree = _tree(
        [
            {
                "op": "add_block",
                "name": "a",
                "envelope": rod,
                "pose": [0.0, 0.0, 0.0],
                "pose_unit": "m",
            },
            {
                "op": "add_block",
                "name": "b",
                "envelope": rod,
                "pose": [0.0, 0.0, 0.08],
                "pose_unit": "m",
            },
        ]
    )
    check = ac.assembly_check(handler.store, tree, 0, budget_s=None)
    (f,) = [f for f in check.findings if f.rule == "assembly.interference"]
    assert f.subject == "a—b"
    assert f.severity == "warn"
    depth = re.search(r"interpenetrate by ([\d.]+) mm", f.detail)
    assert depth is not None, f.detail
    assert float(depth.group(1)) == pytest.approx(20.0, rel=0.1)
    assert "no connect" in f.detail


def test_connected_interference_is_info_with_depth(handler: SeHandler) -> None:
    rod = "cyl:r0.02h0.1"
    tree = _tree(
        [
            {
                "op": "add_block",
                "name": "a",
                "envelope": rod,
                "pose": [0.0, 0.0, 0.0],
                "pose_unit": "m",
            },
            {
                "op": "add_block",
                "name": "b",
                "envelope": rod,
                "pose": [0.0, 0.0, 0.08],
                "pose_unit": "m",
            },
            {"op": "add_port", "block": "a", "name": "p"},
            {"op": "add_port", "block": "b", "name": "q"},
            {"op": "connect", "a": "a.p", "b": "b.q", "joint": {"class": "rigid"}},
        ]
    )
    check = ac.assembly_check(handler.store, tree, 0, budget_s=None)
    (f,) = [f for f in check.findings if f.rule == "assembly.interference"]
    assert f.severity == "info"
    assert "sanctioned by a connect (rigid)" in f.detail


# ── formfind where axial ─────────────────────────────────────────────────

_TIE = {"tension_capacity": 100.0, "compression_capacity": 0.0}


def _tripod(apex: list[float]) -> SeTree:
    base = {"b0": [0.0, 0.0, 0.0], "b1": [2.0, 0.0, 0.0], "b2": [1.0, 2.0, 0.0]}
    ops: list[dict[str, Any]] = []
    for name, pose in {**base, "apex": apex}.items():
        ops.append({"op": "add_block", "name": name, "pose": pose, "pose_unit": "m"})
        ops.append({"op": "add_port", "block": name, "name": "pin"})
    for name in base:
        ops.append(
            {
                "op": "connect",
                "a": f"{name}.pin",
                "b": "apex.pin",
                "joint": {"class": "axial", "params": dict(_TIE)},
            }
        )
    for name in base:
        ops.append({"op": "set_load", "block": name, "fixed": True})
    return _tree(ops)


def test_formfind_reports_off_equilibrium_without_mutating(handler: SeHandler) -> None:
    tree = _tripod([0.0, 0.0, 5.0])
    check = ac.assembly_check(handler.store, tree, 0, budget_s=None)
    (f,) = [f for f in check.findings if f.rule == "formfind.off_equilibrium"]
    assert f.subject == "apex"
    assert tree.blocks["apex"].pose == [0.0, 0.0, 5.0]  # read-only
    assert check.seed["apex"] == [0.0, 0.0, 5.0]
    assert check.geometry["apex"] == pytest.approx([1.0, 2.0 / 3.0, 0.0])


def test_formfind_at_equilibrium_says_nothing(handler: SeHandler) -> None:
    check = ac.assembly_check(
        handler.store, _tripod([1.0, 2.0 / 3.0, 0.0]), 0, budget_s=None
    )
    assert not any(f.rule.startswith("formfind.") for f in check.findings)
    assert "apex" in check.geometry


def test_no_axial_members_means_no_formfind_rows(handler: SeHandler) -> None:
    check = ac.assembly_check(
        handler.store, _door([("h1", [0.0, 0.0, 0.0], [0, 0, 1])]), 0, budget_s=None
    )
    assert not any(f.rule.startswith("formfind.") for f in check.findings)
    assert check.geometry == {}


# ── the view ─────────────────────────────────────────────────────────────


def test_view_renders_the_one_findings_list(handler: SeHandler) -> None:
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "frame", "pose": [0.0, 0.0, 0.0], "pose_unit": "m"},
        {"op": "add_block", "name": "door", "pose": [0.5, 0.0, 0.0], "pose_unit": "m"},
        {"op": "set_load", "block": "frame", "fixed": _ALL6},
    ]
    for name, z in (("h1", 0.0), ("h2", 1.0)):
        ops.append(
            {
                "op": "add_port",
                "block": "frame",
                "name": name,
                "pose": [0.0, 0.0, z],
                "pose_unit": "m",
            }
        )
        ops.append(
            {
                "op": "add_port",
                "block": "door",
                "name": name,
                "pose": [-0.5, 0.0, z],
                "pose_unit": "m",
            }
        )
        ops.append(
            {
                "op": "connect",
                "a": f"frame.{name}",
                "b": f"door.{name}",
                "joint": {"class": "revolute", "axis": [0, 0, 1]},
            }
        )
    handler.put(id="two-hinge-door", text=json.dumps({"ops": ops}))
    body = handler.get(id="two-hinge-door", view="assembly_check").body
    assert body.startswith("# assembly check (method=assembly)")
    assert "rank 11 · redundant 5 · mobility 1" in body
    assert "assembly.overconstrained" in body
    assert "## mate roster" in body
    assert "frame fixed x,y,z,rx,ry,rz" in body


def test_view_is_advertised_in_the_unknown_view_roster(handler: SeHandler) -> None:
    from precis.errors import BadInput

    handler.put(id="empty-ac", text=json.dumps({"ops": []}))
    with pytest.raises(BadInput) as exc_info:
        handler.get(id="empty-ac", view="nope")
    assert "view='assembly_check'" in str(exc_info.value.next)
