"""The axial member + Maxwell/Calladine stability classifier
(:mod:`precis_se.stability`, ``view='stability'`` — docs/backlog/
structural-solution-space.md slice 1): the canonical structures a
structural engineer would grade the implementation on — a determinate
triangle, an unstabilizable four-bar, and the classic 3-strut/9-cable
tensegrity prism at its equilibrium twist (30°) — plus the joint/objective
vetting and the DRC seams."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.dispatch import Hub
from precis.store import Store
from precis_se import drc as se_drc
from precis_se import joints as se_joints
from precis_se import stability as se_stability
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
_ROD = {"tension_capacity": 100.0, "compression_capacity": 100.0}


def _pin_structure(
    nodes: dict[str, list[float]],
    members: list[tuple[str, str, dict[str, float]]],
    fixed: dict[str, Any] | None = None,
) -> SeTree:
    """A pin-jointed structure: one block per node (one shared ``pin``
    port), one axial connect per member."""
    ops: list[dict[str, Any]] = []
    for name, pose in nodes.items():
        ops.append({"op": "add_block", "name": name, "pose": pose})
        ops.append({"op": "add_port", "block": name, "name": "pin"})
    for a, b, params in members:
        ops.append(
            {
                "op": "connect",
                "a": f"{a}.pin",
                "b": f"{b}.pin",
                "joint": {"class": "axial", "params": params},
            }
        )
    for name, spec in (fixed or {}).items():
        ops.append({"op": "set_load", "block": name, "fixed": spec})
    return apply_ops(SeTree(), ops)


_S32 = math.sqrt(3.0) / 2.0

#: The classic triplex at its equilibrium twist: bottom triangle at
#: 0/120/240°, top at +30°, cables = both triangles + verticals, struts =
#: b_i—t_{i+1}. At exactly this twist the equilibrium matrix drops rank:
#: one internal mechanism, one self-stress state, prestress-stabilized.
_PRISM_NODES = {
    "b0": [1.0, 0.0, 0.0],
    "b1": [-0.5, _S32, 0.0],
    "b2": [-0.5, -_S32, 0.0],
    "t0": [_S32, 0.5, 1.0],
    "t1": [-_S32, 0.5, 1.0],
    "t2": [0.0, -1.0, 1.0],
}
_PRISM_CABLES = [
    ("b0", "b1"),
    ("b1", "b2"),
    ("b2", "b0"),
    ("t0", "t1"),
    ("t1", "t2"),
    ("t2", "t0"),
    ("b0", "t0"),
    ("b1", "t1"),
    ("b2", "t2"),
]
_PRISM_STRUTS = [("b0", "t1"), ("b1", "t2"), ("b2", "t0")]


def _prism(strut_params: dict[str, float] | None = None, **kw: Any) -> SeTree:
    members = [(a, b, dict(_TIE)) for a, b in _PRISM_CABLES]
    members += [(a, b, dict(strut_params or _STRUT)) for a, b in _PRISM_STRUTS]
    return _pin_structure(_PRISM_NODES, members, **kw)


#: 3-2-1 supports on the bottom triangle — statically determinate
#: grounding, no rigid-body modes left, nothing overconstrained.
_PRISM_FIXED = {"b0": True, "b1": ["y", "z"], "b2": ["z"]}


# ── the canonical verdicts ───────────────────────────────────────────────


def test_grounded_triangle_is_rigid_determinate() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0], "c": [0.5, _S32, 0.0]},
        [("a", "b", dict(_ROD)), ("b", "c", dict(_ROD)), ("c", "a", dict(_ROD))],
        fixed={"a": True, "b": ["y", "z"], "c": ["z"]},
    )
    report = se_stability.classify(tree)
    assert (report.j, report.b, report.c) == (3, 3, 6)
    assert (report.m_internal, report.s) == (0, 0)
    assert report.verdict == "rigid (statically determinate)"


def test_open_four_bar_is_an_unstabilizable_mechanism() -> None:
    # ground link omitted: three bars, both ground nodes fully fixed,
    # coupler nodes held in-plane — one mechanism, no self-stress.
    tree = _pin_structure(
        {
            "g0": [0.0, 0.0, 0.0],
            "g1": [1.0, 0.0, 0.0],
            "n2": [1.0, 1.0, 0.0],
            "n3": [0.0, 1.0, 0.0],
        },
        [
            ("g1", "n2", dict(_ROD)),
            ("n2", "n3", dict(_ROD)),
            ("n3", "g0", dict(_ROD)),
        ],
        fixed={"g0": True, "g1": True, "n2": ["z"], "n3": ["z"]},
    )
    report = se_stability.classify(tree)
    assert (report.m_internal, report.s) == (1, 0)
    assert "NOT stabilized: no self-stress state exists" in report.verdict


def test_tensegrity_prism_is_prestress_stabilized() -> None:
    report = se_stability.classify(_prism(fixed=_PRISM_FIXED))
    assert (report.j, report.b, report.c) == (6, 12, 6)
    assert (report.m_internal, report.s) == (1, 1)
    assert report.verdict.startswith("prestress-stabilized")
    # the reported state is sign-feasible: every cable in tension, every
    # strut in compression.
    by_subject = {row.subject: row for row in report.members}
    for a, b in _PRISM_CABLES:
        assert (coeff := by_subject[f"{a}.pin—{b}.pin"].self_stress) is not None
        assert coeff > 0.0
    for a, b in _PRISM_STRUTS:
        assert (coeff := by_subject[f"{a}.pin—{b}.pin"].self_stress) is not None
        assert coeff < 0.0


def test_free_floating_prism_reports_rigid_body_modes_honestly() -> None:
    report = se_stability.classify(_prism())
    assert report.c == 0
    assert (report.rb_dim, report.m_internal, report.s) == (6, 1, 1)
    assert report.verdict.startswith("prestress-stabilized")
    assert any("free-floating" in note for note in report.notes)


def test_prism_with_inverted_member_reports_infeasible_self_stress() -> None:
    # declare the struts as ties too: the only self-stress state needs
    # them in compression, so no sign-feasible orientation exists.
    report = se_stability.classify(_prism(strut_params=dict(_TIE), fixed=_PRISM_FIXED))
    assert report.s == 1
    assert "infeasible for the declared members" in report.verdict
    assert "tie asked to carry compression" in report.verdict


def test_twisted_off_equilibrium_prism_is_rigid() -> None:
    # rotate the top triangle to 90° twist: generic geometry, full rank —
    # isostatic, no self-stress, no mechanism.
    nodes = dict(_PRISM_NODES)
    for i, name in enumerate(("t0", "t1", "t2")):
        angle = math.radians(120.0 * i + 90.0)
        nodes[name] = [math.cos(angle), math.sin(angle), 1.0]
    members = [(a, b, dict(_TIE)) for a, b in _PRISM_CABLES]
    members += [(a, b, dict(_STRUT)) for a, b in _PRISM_STRUTS]
    report = se_stability.classify(_pin_structure(nodes, members, fixed=_PRISM_FIXED))
    assert (report.m_internal, report.s) == (0, 0)
    assert report.verdict == "rigid (statically determinate)"


def test_no_axial_members_says_not_applicable() -> None:
    tree = apply_ops(SeTree(), [{"op": "add_block", "name": "solo"}])
    report = se_stability.classify(tree)
    assert report.verdict == "no axial members — stability analysis does not apply"


def test_degenerate_members_are_skipped_with_reasons() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.0]},
        [("a", "b", dict(_ROD))],
    )
    report = se_stability.classify(tree)
    (row,) = report.members
    assert row.skipped is not None and "zero length" in row.skipped
    assert report.verdict == "no axial members — stability analysis does not apply"


# ── joint / objective vetting ────────────────────────────────────────────


def test_axial_rejects_declared_axis() -> None:
    with pytest.raises(se_joints.JointError, match="takes no 'axis'"):
        se_joints.validate_joint({"class": "axial", "axis": [0, 0, 1]})


def test_axial_rejects_capacity_pair_of_zeros() -> None:
    with pytest.raises(se_joints.JointError, match="carries nothing"):
        se_joints.validate_joint(
            {
                "class": "axial",
                "params": {"tension_capacity": 0, "compression_capacity": 0},
            }
        )


@pytest.mark.parametrize(
    "params",
    [
        {"free_length": 0},
        {"free_length": -1},
        {"rate": 0},
        {"tension_capacity": -5},
        {"compression_capacity": "lots"},
        {"preload": float("inf")},
    ],
)
def test_axial_rejects_bad_params(params: dict[str, Any]) -> None:
    with pytest.raises(se_joints.JointError):
        se_joints.validate_joint({"class": "axial", "params": params})


def test_axial_accepts_tie_strut_and_rod() -> None:
    for params in (_TIE, _STRUT, _ROD):
        joint = se_joints.validate_joint({"class": "axial", "params": dict(params)})
        assert joint["class"] == "axial"
    # a strut's preload is compressive — negative is legal by convention.
    joint = se_joints.validate_joint(
        {"class": "axial", "params": {**_STRUT, "preload": -20}}
    )
    assert joint["params"]["preload"] == -20.0


def test_fixed_objective_normalizes_and_rejects_junk() -> None:
    assert se_joints.validate_objectives({"fixed": True})["fixed"] == ["x", "y", "z"]
    assert se_joints.validate_objectives({"fixed": ["z", "y"]})["fixed"] == ["y", "z"]
    for bad in (False, [], ["q"], "x"):
        with pytest.raises(se_joints.JointError, match="'fixed'"):
            se_joints.validate_objectives({"fixed": bad})


def test_fixed_on_a_connect_is_rejected_on_both_write_paths() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_ROD))],
    )
    with pytest.raises(OpError, match="no meaning on a connect"):
        apply_ops(
            tree,
            [{"op": "set_load", "a": "a.pin", "b": "b.pin", "fixed": True}],
        )
    # the second write path: connect's own objectives= (reviewer finding —
    # a stored 'fixed' on a connect would be read by nothing, forever).
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "c", "pose": [2.0, 0.0, 0.0]},
        {"op": "add_port", "block": "c", "name": "pin"},
        {
            "op": "connect",
            "a": "b.pin",
            "b": "c.pin",
            "objectives": {"fixed": True},
        },
    ]
    with pytest.raises(OpError, match="no meaning on a connect"):
        apply_ops(tree, ops)


# ── the DRC seams ────────────────────────────────────────────────────────


def _rules(tree: SeTree) -> dict[str, list[str]]:
    report = se_drc.drc(tree)
    out: dict[str, list[str]] = {}
    for f in report.findings:
        out.setdefault(f.rule, []).append(f.detail)
    return out


def test_drc_flags_undeclared_capacity_pair() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", {"free_length": 1.0})],
    )
    (detail,) = _rules(tree)["axial_capacity"]
    assert "missing tension_capacity and compression_capacity" in detail


def test_drc_flags_preload_beyond_capacities() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", {**_TIE, "preload": 150.0})],
    )
    details = _rules(tree)["axial_capacity"]
    assert any("exceeds the member's tension capacity" in d for d in details)
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", {**_STRUT, "preload": -150.0})],
    )
    details = _rules(tree)["axial_capacity"]
    assert any("buckling/crush ceiling" in d for d in details)


def test_drc_flags_axial_load_component_beyond_best_capacity() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_TIE))],
    )
    apply_ops(
        tree,
        [{"op": "set_load", "a": "a.pin", "b": "b.pin", "force": [400.0, 0.0, 0.0]}],
    )
    details = _rules(tree)["axial_capacity"]
    assert any("best-case capacity" in d for d in details)


def test_dof_probe_skips_axial_with_a_pointer() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_ROD))],
    )
    report = se_drc.drc(tree)
    (probe,) = [p for p in report.dof_probes if p.klass == "axial"]
    assert "view='stability'" in probe.outcome


def test_cable_mechanism_demands_a_bom_line() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_TIE))],
    )
    for conn in tree.connects:
        conn.joint = {"class": "axial", "mechanism": "cable", "params": dict(_TIE)}
    details = _rules(tree)["mechanism_bom"]
    assert any("cable / wire rope" in d for d in details)


# ── the view, through the handler (persist round-trip included) ─────────


def test_stability_view_round_trips_through_the_store(handler: SeHandler) -> None:
    ops: list[dict[str, Any]] = []
    for name, pose in _PRISM_NODES.items():
        ops.append({"op": "add_block", "name": name, "pose": pose})
        ops.append({"op": "add_port", "block": name, "name": "pin"})
    for a, b in _PRISM_CABLES:
        ops.append(
            {
                "op": "connect",
                "a": f"{a}.pin",
                "b": f"{b}.pin",
                "joint": {
                    "class": "axial",
                    "mechanism": "cable",
                    "params": dict(_TIE),
                },
            }
        )
    for a, b in _PRISM_STRUTS:
        ops.append(
            {
                "op": "connect",
                "a": f"{a}.pin",
                "b": f"{b}.pin",
                "joint": {"class": "axial", "params": dict(_STRUT)},
            }
        )
    for name, spec in _PRISM_FIXED.items():
        ops.append({"op": "set_load", "block": name, "fixed": spec})
    handler.put(
        id="triplex",
        text=json.dumps({"description": "3-strut tensegrity prism", "ops": ops}),
    )
    body = handler.get(id="triplex", view="stability").body
    assert "prestress-stabilized" in body
    assert "j=6 node(s)  b=12 member(s)  c=6" in body
    assert "axial subgraph" in body  # the model-assumption header
    # the state column carries signs, not bare scores — normalized to the
    # largest magnitude, which is a strut (compression, negative)
    assert "self_stress" in body and "-1.000" in body and "+0." in body


def test_stability_view_in_unknown_view_roster(handler: SeHandler) -> None:
    handler.put(id="empty1", text=json.dumps({"ops": []}))
    from precis.errors import BadInput

    with pytest.raises(BadInput) as exc_info:
        handler.get(id="empty1", view="nope")
    assert "view='stability'" in str(exc_info.value.next)
