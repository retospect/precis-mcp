"""The axial member + Maxwell/Calladine stability classifier
(:mod:`precis_se.stability`, ``view='stability'`` — docs/backlog/
structural-solution-space.md slice 1): the canonical structures a
structural engineer would grade the implementation on — a determinate
triangle, an unstabilizable four-bar, and the classic 3-strut/9-cable
tensegrity prism at its equilibrium twist (30°) — plus the joint/objective
vetting, the DRC seams, and the rung-5 prestress check (declared preloads
vs the self-stress space, slice 3)."""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis_se import drc as se_drc
from precis_se import joints as se_joints
from precis_se import stability as se_stability
from precis_se.handler import SeHandler, _render_stability
from precis_se.ops import ConnectSpec, OpError, SeTree, apply_ops


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
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
        ops.append({"op": "add_block", "name": name, "pose": pose, "pose_unit": "m"})
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


def test_bare_number_force_gets_a_concrete_vector_hint() -> None:
    """gr332020: the common slip is passing the load's magnitude instead
    of a 3-vector along an axis — the error should hand back a
    copy-pasteable example, not just name the wrong shape."""
    with pytest.raises(
        se_joints.JointError,
        match=r"a downward 800 N load is \[0, 0, -800\]",
    ):
        se_joints.validate_objectives({"force": 800})
    with pytest.raises(
        se_joints.JointError,
        match=r"an 800 N·m moment about z is \[0, 0, 800\]",
    ):
        se_joints.validate_objectives({"torque": 800})


def test_non_numeric_force_keeps_the_plain_message() -> None:
    """A non-numeric offender (wrong-length list, string, …) gets no
    vector-magnitude hint — there's no single number to build one from."""
    with pytest.raises(se_joints.JointError, match="must be a 3-vector") as exc_info:
        se_joints.validate_objectives({"force": [1, 2]})
    assert "downward" not in str(exc_info.value)


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
        {"op": "add_block", "name": "c", "pose": "[2.0 0.0 0.0] m"},
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


def test_dof_probe_labels_rod_bilateral_not_unilateral() -> None:
    # gripe 334789 (part 1): a rod (both capacities > 0) is bilateral —
    # the skip label must not call it a unilateral member.
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_ROD))],
    )
    report = se_drc.drc(tree)
    (probe,) = [p for p in report.dof_probes if p.klass == "axial"]
    assert "axial member (rod)" in probe.outcome
    assert "unilateral" not in probe.outcome


def test_dof_probe_labels_tie_and_strut_unilateral() -> None:
    for params in (_TIE, _STRUT):
        tree = _pin_structure(
            {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
            [("a", "b", dict(params))],
        )
        report = se_drc.drc(tree)
        (probe,) = [p for p in report.dof_probes if p.klass == "axial"]
        assert "unilateral member" in probe.outcome
        assert "(rod)" not in probe.outcome


# ── preload tensioning consistency (gripe 334782) ────────────────────────


def test_preload_without_free_length_warns() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.05]},
        [("a", "b", {**_ROD, "preload": 50.0})],
    )
    (detail,) = _rules(tree)["preload_consistency"]
    assert "no free_length and rate" in detail
    assert "preload = rate ×" in detail


def test_preload_without_rate_only_warns() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.05]},
        [("a", "b", {**_ROD, "preload": 50.0, "free_length": 0.04})],
    )
    (detail,) = _rules(tree)["preload_consistency"]
    assert "no rate" in detail
    assert "no free_length and rate" not in detail


def test_consistent_free_length_rate_preload_triple_is_quiet() -> None:
    # installed length 0.05 m; free 0.04 m; rate 5000 N/m → 50 N exactly.
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.05]},
        [
            (
                "a",
                "b",
                {**_ROD, "preload": 50.0, "free_length": 0.04, "rate": 5000.0},
            )
        ],
    )
    assert "preload_consistency" not in _rules(tree)


def test_inconsistent_free_length_rate_preload_triple_warns_with_both_numbers() -> None:
    # rate × (installed − free) = 5000 × (0.05 − 0.04) = 50 N, but the
    # declared preload is 200 N — well outside the 1% tolerance.
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.05]},
        [
            (
                "a",
                "b",
                {**_ROD, "preload": 200.0, "free_length": 0.04, "rate": 5000.0},
            )
        ],
    )
    (detail,) = _rules(tree)["preload_consistency"]
    assert "200 N" in detail
    assert "50 N" in detail
    assert "disagrees" in detail


def test_no_preload_member_stays_quiet_about_tensioning() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.05]},
        [("a", "b", dict(_ROD))],
    )
    assert "preload_consistency" not in _rules(tree)


# ── stability scope honesty (gripe 334783) ───────────────────────────────


def test_loaded_block_outside_subgraph_appears_in_the_header() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_ROD))],
        fixed={"a": True, "b": ["y", "z"]},
    )
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "saddle", "pose": "[2.0 0.0 0.0] m"},
            {"op": "set_load", "block": "saddle", "force": [10.0, 0.0, 0.0]},
        ],
    )
    report = se_stability.classify(tree)
    (note,) = [n for n in report.notes if "outside the analysed" in n]
    assert "saddle" in note
    assert "not checked" in note


def test_fixed_and_forced_block_warns_force_has_no_effect() -> None:
    # set_load has replace semantics on a block, so 'fixed' and 'force'
    # must be declared together in one op — a separate call would wipe
    # the earlier 'fixed' out.
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_ROD))],
        fixed={"b": ["y", "z"]},
    )
    apply_ops(
        tree,
        [
            {
                "op": "set_load",
                "block": "a",
                "fixed": True,
                "force": [10.0, 0.0, 0.0],
            }
        ],
    )
    report = se_stability.classify(tree)
    (note,) = [n for n in report.notes if "no effect in this model" in n]
    assert "a:" in note


def test_clean_fully_analysed_design_reports_neither_scope_note() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0], "c": [0.5, _S32, 0.0]},
        [("a", "b", dict(_ROD)), ("b", "c", dict(_ROD)), ("c", "a", dict(_ROD))],
        fixed={"a": True, "b": ["y", "z"], "c": ["z"]},
    )
    report = se_stability.classify(tree)
    assert not any("outside the analysed" in n for n in report.notes)
    assert not any("no effect in this model" in n for n in report.notes)


def test_cable_mechanism_demands_a_bom_line() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_TIE))],
    )
    for conn in tree.connects:
        conn.joint = {"class": "axial", "mechanism": "cable", "params": dict(_TIE)}
    details = _rules(tree)["mechanism_bom"]
    assert any("cable / wire rope" in d for d in details)


def test_tensioner_mechanism_demands_rod_and_nipple() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_TIE))],
    )
    for conn in tree.connects:
        conn.joint = {"class": "axial", "mechanism": "tensioner", "params": dict(_TIE)}
    details = _rules(tree)["mechanism_bom"]
    assert any("nipple / turnbuckle" in d for d in details)
    assert not any("wire rope" in d for d in details)


# ── prestress: declared preloads vs the self-stress space ────────────────
# (rung 5's null-space DRC — structural-solution-space.md slice 3)


def _prism_with_preloads(
    preloads: dict[tuple[str, str], float],
    strut_params: dict[str, float] | None = None,
) -> SeTree:
    members = []
    for a, b in _PRISM_CABLES:
        params = dict(_TIE)
        if (a, b) in preloads:
            params["preload"] = preloads[(a, b)]
        members.append((a, b, params))
    for a, b in _PRISM_STRUTS:
        params = dict(strut_params or _STRUT)
        if (a, b) in preloads:
            params["preload"] = preloads[(a, b)]
        members.append((a, b, params))
    return _pin_structure(_PRISM_NODES, members, fixed=_PRISM_FIXED)


def _prism_state() -> dict[str, float]:
    """The classifier's normalized self-stress coefficients by subject —
    the ground truth the null-space check must agree with (one assembly)."""
    report = se_stability.classify(_prism(fixed=_PRISM_FIXED))
    return {
        row.subject: coeff
        for row in report.members
        if (coeff := row.self_stress) is not None
    }


def test_bolted_joint_preload_between_grounded_blocks_is_a_self_stress() -> None:
    # the rung-5 archetype: a preloaded member between fully fixed blocks
    # equilibrates through the support reactions — always compatible.
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.05]},
        [("a", "b", {**_ROD, "preload": 50.0})],
        fixed={"a": True, "b": True},
    )
    report = se_stability.prestress_report(tree)
    assert report is not None
    assert report.compatible is True
    assert report.residual == 0.0
    assert report.findings == []


def test_preload_with_a_free_unbalanced_node_is_refused() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.05]},
        [("a", "b", {**_ROD, "preload": 50.0})],
        fixed={"a": True},
    )
    report = se_stability.prestress_report(tree)
    assert report is not None
    assert report.compatible is False
    assert report.worst_node == "b"
    (finding,) = report.findings
    assert "not a self-stress state" in finding[1]
    assert "out-of-balance force 50 N at node 'b'" in finding[1]
    assert "no self-stress at all (s = 0)" in finding[1]


def test_prism_preloads_in_the_state_ratio_are_compatible() -> None:
    # declare every preload as 100 N × the classifier's coefficient,
    # rounded to 3 significant digits — rounding must stay inside the
    # tolerance, and the residual is still reported.
    state = _prism_state()
    preloads = {
        (a, b): float(f"{100.0 * state[f'{a}.pin—{b}.pin']:.3g}")
        for a, b in _PRISM_CABLES + _PRISM_STRUTS
    }
    report = se_stability.prestress_report(_prism_with_preloads(preloads))
    assert report is not None
    assert report.compatible is True
    assert report.findings == []
    assert report.residual is not None and 0.0 < report.residual <= 1.0


def test_prism_single_preload_pins_the_state_and_implies_the_rest() -> None:
    state = _prism_state()
    declared = float(f"{100.0 * state['b0.pin—t0.pin']:.6g}")
    report = se_stability.prestress_report(
        _prism_with_preloads({("b0", "t0"): declared})
    )
    assert report is not None
    assert report.compatible is True
    assert report.unique is True
    implied = {row.subject: row.implied for row in report.rows}
    for a, b in _PRISM_STRUTS:
        force = implied[f"{a}.pin—{b}.pin"]
        assert force is not None and force < 0.0
    # the completion reproduces the classifier's ratios
    assert implied["b1.pin—b2.pin"] == pytest.approx(
        100.0 * state["b1.pin—b2.pin"], rel=1e-3
    )


def test_prism_preloads_off_the_state_ratio_are_refused() -> None:
    report = se_stability.prestress_report(
        _prism_with_preloads({("b0", "t0"): 100.0, ("b0", "b1"): 100.0})
    )
    assert report is not None
    assert report.compatible is False
    assert any("not a self-stress state" in d for _s, d in report.findings)
    # the s=1 geometry does admit self-stress — the diagnostic must not
    # claim otherwise
    assert not any("s = 0" in d for _s, d in report.findings)


def test_implied_sign_violation_under_an_incompatible_preload_set_is_flagged() -> None:
    # gripe 334780: an incompatible declared set (equilibrium already
    # failing) still least-squares-completes the undeclared members, and a
    # zero-tension-capacity strut forced into implied tension there was
    # silently dropped — the vetting loop only ran when `compatible`. It
    # must fire here too, conditionally worded, with a per-row marker.
    report = se_stability.prestress_report(
        _prism_with_preloads({("b0", "t0"): -50.0, ("b0", "b1"): 10.0})
    )
    assert report is not None
    assert report.compatible is False
    strut_findings = [
        d
        for _s, d in report.findings
        if "tension in this compression-only member (strut)" in d
    ]
    assert strut_findings, report.findings
    assert all(
        "least-squares completion of an incompatible preload set" in d
        and "cannot be realised" in d
        for d in strut_findings
    )
    strut_rows = [row for row in report.rows if row.role == "strut"]
    assert strut_rows and all(
        row.flag == "sign violated (strut in tension)" for row in strut_rows
    )


def test_implied_compression_in_a_tie_is_flagged() -> None:
    # struts declared as ties: the state needs them in compression, which
    # a tension-only member cannot supply — three sign findings.
    state = _prism_state()
    declared = float(f"{100.0 * state['b0.pin—t0.pin']:.6g}")
    report = se_stability.prestress_report(
        _prism_with_preloads({("b0", "t0"): declared}, strut_params=dict(_TIE))
    )
    assert report is not None
    assert report.compatible is True
    sign_findings = [
        d for _s, d in report.findings if "compression in this tension-only" in d
    ]
    assert len(sign_findings) == 3


def test_determinate_structure_admits_no_prestress() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0], "c": [0.5, _S32, 0.0]},
        [
            ("a", "b", {**_ROD, "preload": 10.0}),
            ("b", "c", dict(_ROD)),
            ("c", "a", dict(_ROD)),
        ],
        fixed={"a": True, "b": ["y", "z"], "c": ["z"]},
    )
    report = se_stability.prestress_report(tree)
    assert report is not None
    assert report.compatible is False
    assert any("no self-stress at all (s = 0)" in d for _s, d in report.findings)


def test_implied_forces_beyond_capacity_are_flagged() -> None:
    # 5× the 100 N-capacity scale: the implied tensions blow through the
    # cables' capacity and the implied compressions through the struts'.
    state = _prism_state()
    declared = float(f"{500.0 * state['b0.pin—t0.pin']:.6g}")
    report = se_stability.prestress_report(
        _prism_with_preloads({("b0", "t0"): declared})
    )
    assert report is not None
    assert report.compatible is True
    details = [d for _s, d in report.findings]
    assert any(
        "N tension against this member's 100 N tension capacity" in d for d in details
    )
    assert any("buckling/crush ceiling" in d for d in details)


def test_no_declared_preloads_is_not_a_finding() -> None:
    assert se_stability.prestress_report(_prism(fixed=_PRISM_FIXED)) is None


def test_all_zero_preloads_report_nothing_to_verify() -> None:
    # scale 0 → tolerance 0: residual 0 ≤ tolerance 0 would otherwise
    # "pass" vacuously (gripe 334781) — a zero declared vector must read
    # as unchecked, never as a verified self-stress state.
    report = se_stability.prestress_report(_prism_with_preloads({("b0", "t0"): 0.0}))
    assert report is not None
    assert report.nothing_to_verify is True
    assert report.compatible is None
    assert report.residual is None
    assert report.tolerance is None
    assert any("nothing" in n for n in report.notes)


def test_preload_on_a_skipped_member_is_reported_unchecked() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.0]},
        [("a", "b", {**_ROD, "preload": 25.0})],
    )
    report = se_stability.prestress_report(tree)
    assert report is not None
    assert report.compatible is None  # no analysable system to check against
    (finding,) = report.findings
    assert "cannot be analysed" in finding[1]
    assert "does not cover it" in finding[1]


def test_drc_carries_prestress_state_findings() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.05]},
        [("a", "b", {**_ROD, "preload": 50.0})],
        fixed={"a": True},
    )
    details = _rules(tree)["prestress_state"]
    assert any("not a self-stress state" in d for d in details)


# ── the view, through the handler (persist round-trip included) ─────────


def test_stability_view_round_trips_through_the_store(handler: SeHandler) -> None:
    ops: list[dict[str, Any]] = []
    for name, pose in _PRISM_NODES.items():
        ops.append({"op": "add_block", "name": name, "pose": pose, "pose_unit": "m"})
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
    # no member declares a preload — the prestress section must not appear
    assert "## prestress" not in body


def test_stability_view_renders_the_prestress_section(handler: SeHandler) -> None:
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "a", "pose": [0.0, 0.0, 0.0]},
        {"op": "add_port", "block": "a", "name": "pin"},
        {"op": "add_block", "name": "b", "pose": "[0.0 0.0 0.05] m"},
        {"op": "add_port", "block": "b", "name": "pin"},
        {
            "op": "connect",
            "a": "a.pin",
            "b": "b.pin",
            "joint": {"class": "axial", "params": {**_ROD, "preload": 50.0}},
        },
        {"op": "set_load", "block": "a", "fixed": True},
        {"op": "set_load", "block": "b", "fixed": True},
    ]
    handler.put(id="bolted", text=json.dumps({"ops": ops}))
    body = handler.get(id="bolted", view="stability").body
    assert "## prestress — declared preloads vs the self-stress space" in body
    assert "ARE a self-stress state" in body
    assert "50 N" in body  # the declared column carries the number


def test_stability_view_zero_preload_reports_nothing_to_verify(
    handler: SeHandler,
) -> None:
    # gripe 334781: an all-zero preload must never render as "ARE a
    # self-stress state" — the tolerance scales with the declared
    # magnitude, so zero-vs-zero is vacuous, not verified.
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "a", "pose": [0.0, 0.0, 0.0]},
        {"op": "add_port", "block": "a", "name": "pin"},
        {"op": "add_block", "name": "b", "pose": "[0.0 0.0 0.05] m"},
        {"op": "add_port", "block": "b", "name": "pin"},
        {
            "op": "connect",
            "a": "a.pin",
            "b": "b.pin",
            "joint": {"class": "axial", "params": {**_ROD, "preload": 0.0}},
        },
        {"op": "set_load", "block": "a", "fixed": True},
        {"op": "set_load", "block": "b", "fixed": True},
    ]
    handler.put(id="unpreloaded", text=json.dumps({"ops": ops}))
    body = handler.get(id="unpreloaded", view="stability").body
    assert "## prestress — declared preloads vs the self-stress space" in body
    assert "no preload declared — nothing to verify" in body
    assert "ARE a self-stress state" not in body


def test_stability_view_in_unknown_view_roster(handler: SeHandler) -> None:
    handler.put(id="empty1", text=json.dumps({"ops": []}))
    from precis.errors import BadInput

    with pytest.raises(BadInput) as exc_info:
        handler.get(id="empty1", view="nope")
    assert "view='stability'" in str(exc_info.value.next)


# ── rigid connects merge blocks into one rigid body (gr334788) ───────────


def _with_rigid(tree: SeTree, a: str, b: str) -> SeTree:
    return apply_ops(
        tree,
        [
            {
                "op": "connect",
                "a": f"{a}.pin",
                "b": f"{b}.pin",
                "joint": {"class": "rigid"},
            }
        ],
    )


#: Wall anchor points (grounded pin nodes) for the grouped-body tests.
_WALLS = {
    "w1": [2.0, 0.3, -0.2],
    "w2": [-1.5, 1.2, 0.4],
    "w3": [0.3, -2.0, 0.9],
    "w4": [0.8, 1.7, -1.6],
    "w5": [-0.7, -0.9, 2.1],
    "w6": [1.1, -1.4, -1.3],
}


def _walls_tree() -> SeTree:
    ops: list[dict[str, Any]] = []
    for name, pose in _WALLS.items():
        ops += [
            {"op": "add_block", "name": name, "pose": pose, "pose_unit": "m"},
            {"op": "add_port", "block": name, "name": "pin"},
            {"op": "set_load", "block": name, "fixed": True},
        ]
    return apply_ops(SeTree(), ops)


def _bar(a: str, port_a: str, b: str) -> dict[str, Any]:
    return {
        "op": "connect",
        "a": f"{a}.{port_a}",
        "b": f"{b}.pin",
        "joint": {"class": "axial", "params": dict(_ROD)},
    }


def _grouped(n_bars: int) -> SeTree:
    """g1 + g2 rigidly joined, bars 0..n-1 alternate g1/g2 to the walls."""
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "g1", "pose": [0.0, 0.0, 0.0]},
        {"op": "add_port", "block": "g1", "name": "pin"},
        {"op": "add_block", "name": "g2", "pose": "[0.4 0.1 -0.2] m"},
        {"op": "add_port", "block": "g2", "name": "pin"},
    ]
    for k, wall in enumerate(list(_WALLS)[:n_bars]):
        ops.append(_bar("g1" if k % 2 == 0 else "g2", "pin", wall))
    return _with_rigid(apply_ops(_walls_tree(), ops), "g1", "g2")


def _single(n_bars: int) -> SeTree:
    """The same body as ONE block: g2's attachment as a port offset."""
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "g1", "pose": [0.0, 0.0, 0.0]},
        {"op": "add_port", "block": "g1", "name": "pin"},
        {"op": "add_port", "block": "g1", "name": "q", "pose": "[0.4 0.1 -0.2] m"},
    ]
    for k, wall in enumerate(list(_WALLS)[:n_bars]):
        ops.append(_bar("g1", "pin" if k % 2 == 0 else "q", wall))
    return apply_ops(_walls_tree(), ops)


@pytest.mark.parametrize("n_bars", [4, 6])
def test_two_block_rigid_group_behaves_like_one_body(n_bars: int) -> None:
    grouped = se_stability.classify(_grouped(n_bars))
    single = se_stability.classify(_single(n_bars))
    key = lambda r: (r.b, r.c, r.rank, r.m, r.rb_dim, r.m_internal, r.s)
    assert key(grouped) == key(single)
    assert grouped.verdict == single.verdict
    assert grouped.j == single.j == 1 + n_bars  # n walls + the one body
    assert grouped.m == 6 - grouped.rank > 0  # a real, non-trivial count
    assert grouped.b == n_bars
    assert any("merged into one rigid body" in n for n in grouped.notes)


def test_rigid_group_stress_state_matches_single_body() -> None:
    gsys = se_stability._assemble(_grouped(6))
    ssys = se_stability._assemble(_single(6))
    assert gsys.n == ssys.n == 24
    assert np.allclose(gsys.equilibrium, ssys.equilibrium)
    state = np.arange(1.0, 7.0)
    assert np.allclose(
        se_stability._stress_matrix(gsys, state),
        se_stability._stress_matrix(ssys, state),
    )


def test_rigid_connects_are_modelled_not_partial() -> None:
    report = se_stability.classify(_grouped(4))
    assert not report.partial and report.unmodelled_connects == []
    assert not any("PARTIAL" in n for n in report.notes)


def test_rigid_connect_to_missing_block_stays_partial() -> None:
    tree = _grouped(4)
    tree.connects.append(
        ConnectSpec(
            a_block="g1",
            a_port="pin",
            b_block="ghost",
            b_port="pin",
            joint={"class": "rigid"},
        )
    )
    report = se_stability.classify(tree)
    assert report.partial
    assert report.unmodelled_connects == ["g1.pin—ghost.pin"]
    assert any("PARTIAL" in n for n in report.notes)


def test_no_rigid_connect_is_not_partial() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]}, [("a", "b", dict(_ROD))]
    )
    report = se_stability.classify(tree)
    assert not report.partial and report.unmodelled_connects == []


def test_rigid_only_design_is_not_applicable_and_says_why() -> None:
    tree = _with_rigid(
        _pin_structure({"a": [0.0, 0.0, 0.0], "r": [1.0, 0.0, 0.0]}, []), "a", "r"
    )
    report = se_stability.classify(tree)
    assert not report.partial
    assert report.verdict.startswith("no axial members")
    assert "rigid" in report.verdict


def test_axial_between_rigid_group_members_is_internal() -> None:
    tree = apply_ops(
        _grouped(4),
        [
            {"op": "add_port", "block": "g1", "name": "x"},
            {"op": "add_port", "block": "g2", "name": "x"},
            {
                "op": "connect",
                "a": "g1.x",
                "b": "g2.x",
                "joint": {"class": "axial", "params": dict(_ROD)},
            },
        ],
    )
    report = se_stability.classify(tree)
    skipped = [r for r in report.members if r.skipped]
    assert len(skipped) == 1 and "same rigid group" in str(skipped[0].skipped)
    assert report.b == 4


def test_rigid_group_with_axial_elsewhere_classifies() -> None:
    """A rigid pair held by 4 bars plus an independent grounded triangle:
    each part keeps its own verdict contribution."""
    tree = _grouped(4)
    tree = apply_ops(
        tree,
        [
            {"op": "add_block", "name": "t", "pose": "[5.0 0.0 0.0] m"},
            {"op": "add_port", "block": "t", "name": "pin"},
            _bar("t", "pin", "w5"),
            _bar("t", "pin", "w6"),
        ],
    )
    report = se_stability.classify(tree)
    base = se_stability.classify(_grouped(4))
    assert report.b == base.b + 2
    assert report.m == base.m + 3 - 2  # pin node: 3 DOF, 2 bars
    assert not report.partial


def test_stability_view_has_no_partial_banner_for_modelled_rigid() -> None:
    text = _render_stability(_grouped(4))
    assert "PARTIAL" not in text
    assert "merged into one rigid body" in text


# ── port moment arms: 6-DOF rigid-body nodes (gr334787) ──────────────────


def _hub_wheel(tangential: float) -> SeTree:
    """A hub block with 8 flange-hole ports (4 angles x two flanges at
    z = ±0.02, radius 0.03) laced by axial spokes to 8 grounded rim
    points at radius 1 (z = 0). ``tangential`` shifts each rim end that
    many radians around from its hole's angle (alternating sign per
    flange); 0 = radial lacing. The hub sits in an axle bearing
    (``fixed`` x y z rx ry — a bare x/y/z on a lone body would clamp it),
    so the axle rotation is the question."""
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "hub", "pose": [0.0, 0.0, 0.0]},
        {"op": "set_load", "block": "hub", "fixed": ["x", "y", "z", "rx", "ry"]},
    ]
    k = 0
    for flange, z in ((1.0, 0.02), (-1.0, -0.02)):
        for i in range(4):
            phi = i * math.pi / 2.0
            ops.append(
                {
                    "op": "add_port",
                    "block": "hub",
                    "name": f"h{k}",
                    "pose": [0.03 * math.cos(phi), 0.03 * math.sin(phi), z],
                    "pose_unit": "m",
                }
            )
            psi = phi + flange * tangential
            ops += [
                {
                    "op": "add_block",
                    "name": f"rim{k}",
                    "pose": [math.cos(psi), math.sin(psi), 0.0],
                    "pose_unit": "m",
                },
                {"op": "add_port", "block": f"rim{k}", "name": "pin"},
                {"op": "set_load", "block": f"rim{k}", "fixed": True},
                {
                    "op": "connect",
                    "a": f"hub.h{k}",
                    "b": f"rim{k}.pin",
                    "joint": {"class": "axial", "params": dict(_ROD)},
                },
            ]
            k += 1
    return apply_ops(SeTree(), ops)


def _axle_moment_row(system: se_stability._System) -> int:
    return int(system.start[system.index["hub"]]) + 5  # rot-z row


def test_radial_spokes_carry_no_axle_moment_and_leave_the_hub_mobile() -> None:
    tree = _hub_wheel(0.0)
    system = se_stability._assemble(tree)
    assert system.dof[system.index["hub"]] == 6
    row = system.equilibrium[_axle_moment_row(system)]
    assert float(np.max(np.abs(row))) < 1e-12
    report = se_stability.classify(tree)
    # first-order mechanism about the axle; the taut radial spokes only
    # stiffen it at second order (geometric stiffness), never first order
    assert report.m_internal == 1
    assert not report.verdict.startswith("rigid")


def test_tangential_spokes_stiffen_the_hub_about_the_axle() -> None:
    tree = _hub_wheel(0.3)
    system = se_stability._assemble(tree)
    row = system.equilibrium[_axle_moment_row(system)]
    assert float(np.max(np.abs(row))) > 1e-3  # non-zero axle moment arm
    report = se_stability.classify(tree)
    assert report.m == 0 and report.m_internal == 0
    assert report.verdict.startswith("rigid")
    assert (report.j, report.b, report.rank) == (9, 8, 1)


def test_port_offset_follows_the_block_rotation() -> None:
    tree = apply_ops(
        SeTree(),
        [
            {
                "op": "add_block",
                "name": "a",
                "pose": "[1.0 0.0 0.0] m",
                "rot": [0.0, 0.0, math.pi / 2],
                "rot_unit": "rad",
            },
            {"op": "add_port", "block": "a", "name": "p", "pose": "[1.0 0.0 0.0] m"},
        ],
    )
    got = se_stability.port_world(tree, "a", "p")
    assert got == pytest.approx([1.0, 1.0, 0.0], abs=1e-9)


def test_origin_port_designs_keep_three_dof_nodes() -> None:
    system = se_stability._assemble(_prism(fixed=_PRISM_FIXED))
    assert set(system.dof.tolist()) == {3}
    assert system.n == 3 * len(system.node_names)


def _offset_bar_body(fixed: Any) -> SeTree:
    """One bar from a grounded wall pin to a body's offset port (0, 1, 0);
    the bar runs along (2, −1, 0), so its only moment about the body's
    pose is about z."""
    return apply_ops(
        SeTree(),
        [
            {"op": "add_block", "name": "wall", "pose": "[2.0 0.0 0.0] m"},
            {"op": "add_port", "block": "wall", "name": "pin"},
            {"op": "set_load", "block": "wall", "fixed": True},
            {"op": "add_block", "name": "body", "pose": [0.0, 0.0, 0.0]},
            {"op": "add_port", "block": "body", "name": "p", "pose": "[0.0 1.0 0.0] m"},
            {"op": "set_load", "block": "body", "fixed": fixed},
            {
                "op": "connect",
                "a": "body.p",
                "b": "wall.pin",
                "joint": {"class": "axial", "params": dict(_ROD)},
            },
        ],
    )


def test_lone_body_with_full_translation_support_is_clamped() -> None:
    """x/y/z on the body's only support clamps it: n = 6 + 3, c = 6 + 3,
    b = 1 → nothing free, the bar is a self-stress (m − s = −1)."""
    report = se_stability.classify(_offset_bar_body(True))
    assert (report.c, report.rank, report.m, report.s) == (9, 0, 0, 1)
    assert report.m - report.s == 9 - report.c - report.b


def test_explicit_rotation_axes_leave_the_rest_free() -> None:
    """x y z rx: c = 4 + 3; free ry, rz; the bar resists rz only → m = 1."""
    report = se_stability.classify(_offset_bar_body(["x", "y", "z", "rx"]))
    assert (report.c, report.rank, report.m, report.s) == (7, 1, 1, 0)


def test_partial_translation_support_stays_a_pin() -> None:
    """x y only: no clamp; free z, rx, ry, rz (4); the bar resists rz →
    rank 1, m = 3; c = 2 + 3, m − s = 9 − 5 − 1."""
    report = se_stability.classify(_offset_bar_body(["x", "y"]))
    assert (report.c, report.rank, report.m, report.s) == (5, 1, 3, 0)
    assert "mobile" in report.verdict


def test_pinned_lone_body_keeps_its_rotations_free() -> None:
    """pinned=true on the body's only x/y/z support: no clamp (a ball
    joint). c = 3 + 3; free rx, ry, rz (3); the bar resists rz → rank 1,
    m = 2; m − s = 9 − 6 − 1."""
    tree = apply_ops(
        _offset_bar_body(True), [{"op": "set_load", "block": "body", "pinned": True}]
    )
    report = se_stability.classify(tree)
    assert (report.c, report.rank, report.m, report.s) == (6, 1, 2, 0)
    assert report.m - report.s == 9 - report.c - report.b
    assert "mobile" in report.verdict


def test_pinned_false_merges_back_to_the_clamp() -> None:
    """set_load merges keys, so pinned=false must undo pinned=true."""
    tree = apply_ops(
        _offset_bar_body(True),
        [
            {"op": "set_load", "block": "body", "pinned": True},
            {"op": "set_load", "block": "body", "pinned": False},
        ],
    )
    report = se_stability.classify(tree)
    assert (report.c, report.rank, report.m, report.s) == (9, 0, 0, 1)


def test_pinned_is_refused_on_a_connect_and_must_be_a_bool() -> None:
    with pytest.raises(OpError, match="'pinned' grounds a BLOCK"):
        apply_ops(
            _offset_bar_body(True),
            [{"op": "set_load", "a": "body.p", "b": "wall.pin", "pinned": True}],
        )
    with pytest.raises(OpError, match="'pinned' must be true or false"):
        apply_ops(
            _offset_bar_body(True),
            [{"op": "set_load", "block": "body", "pinned": "yes"}],
        )


# ── supports on rigid groups: one constraint row set per foot ───────────


def _footed_group(
    feet: dict[str, Any], wall: list[float], foot_poses: list[list[float]] | None = None
) -> SeTree:
    """Rigid group {f1, f2, f3} (poses (0,0,0), (1,0,0), (0,1,0) unless
    given) with one bar from f1's origin port to a grounded wall pin, plus
    ``objectives.fixed`` per foot. Pin nodes: wall (3 DOF, 3 grounded);
    the group is one 6-DOF body. n = 9, b = 1."""
    poses = foot_poses or [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "w", "pose": wall, "pose_unit": "m"},
        {"op": "add_port", "block": "w", "name": "pin"},
        {"op": "set_load", "block": "w", "fixed": True},
    ]
    for k, pose in enumerate(poses, start=1):
        ops += [
            {"op": "add_block", "name": f"f{k}", "pose": pose, "pose_unit": "m"},
            {"op": "add_port", "block": f"f{k}", "name": "pin"},
        ]
    ops.append(_bar("w", "pin", "f1"))
    for name, spec in feet.items():
        ops.append({"op": "set_load", "block": name, "fixed": spec})
    tree = apply_ops(SeTree(), ops)
    return _with_rigid(_with_rigid(tree, "f1", "f2"), "f1", "f3")


def _counts(tree: SeTree) -> tuple[int, int, int, int]:
    report = se_stability.classify(tree)
    system = se_stability._assemble(tree)
    assert system.n == 9 and report.b == 1
    assert report.m - report.s == system.n - report.c - report.b
    return report.c, report.rank, report.m, report.s


def test_one_foot_on_a_group_clamps_the_body() -> None:
    """One foot (f2, off the bar's block) is the body's only support →
    clamp: 6 + 3 wall = 9 grounded, nothing free."""
    assert _counts(_footed_group({"f2": True}, [0.0, 0.0, 2.0])) == (9, 0, 0, 1)


def test_one_partial_foot_on_a_group_pins_its_own_point() -> None:
    """f2 fixed in z only: pins f2's point in z (1 DOF), no clamp. Free
    5; f2 is the reference, so the bar along z at f1 (arm (−1, 0, 0))
    is resisted through θ_y → rank 1, m = 4; c = 1 + 3."""
    assert _counts(_footed_group({"f2": ["z"]}, [0.0, 0.0, 2.0])) == (4, 1, 4, 0)


def test_two_feet_on_a_group_ground_five_and_leave_their_hinge() -> None:
    """f2 + f3 pinned: 5 body DOF + 3 wall = 8 grounded; one hinge about
    the f2–f3 line is left. It moves f1 along z: a bar along z resists it
    (rigid), a bar along x does not (m = 1, s = 1)."""
    feet = {"f2": True, "f3": True}
    assert _counts(_footed_group(feet, [0.0, 0.0, 2.0])) == (8, 1, 0, 0)
    assert _counts(_footed_group(feet, [-2.0, 0.0, 0.0])) == (8, 0, 1, 1)
    report = se_stability.classify(_footed_group(feet, [-2.0, 0.0, 0.0]))
    assert not any("reference only" in n for n in report.notes)


def test_three_non_collinear_feet_ground_six() -> None:
    feet = {"f1": True, "f2": True, "f3": True}
    assert _counts(_footed_group(feet, [0.0, 0.0, 2.0])) == (9, 0, 0, 1)


def test_three_collinear_feet_ground_five() -> None:
    """Feet on the x axis leave the rotation about it: f1's origin is ON
    the axis, so the bar cannot resist it (m = 1); c = 5 + 3."""
    feet = {"f1": True, "f2": True, "f3": True}
    collinear = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    assert _counts(_footed_group(feet, [0.0, 0.0, 2.0], collinear)) == (8, 0, 1, 1)


def test_stability_view_header_names_clamp_semantics() -> None:
    text = _render_stability(_footed_group({"f2": True}, [0.0, 0.0, 2.0]))
    assert "clamps all 6 DOF" in text and "grounded DOF(s)" in text


def test_fixed_accepts_rotation_axes_in_canonical_order() -> None:
    assert se_joints._vet_fixed(["rz", "x", "RX"]) == ["x", "rx", "rz"]
    with pytest.raises(se_joints.JointError):
        se_joints._vet_fixed(["w"])


def test_stress_matrix_matches_finite_difference_hessian() -> None:
    """``_stress_matrix`` is the Hessian of the prestress energy
    ``Σ t·L(x)`` plus ``Σ q·g gᵀ`` (``g = ∇L``: the parallel component
    the first-order-mechanism test drops anyway). Central differences over
    the full DOF vector (rotations by exact Rodrigues), two prestressed
    bars into one offset-port body that is also a rotated block."""
    tree = apply_ops(
        SeTree(),
        [
            {
                "op": "add_block",
                "name": "body",
                "pose": "[0.1 0.2 -0.1] m",
                "rot": "[0.3 -0.4 0.5] rad",
            },
            {"op": "add_port", "block": "body", "name": "p", "pose": "[0.2 0.1 0.3] m"},
            {
                "op": "add_port",
                "block": "body",
                "name": "q",
                "pose": "[-0.3 0.2 0.1] m",
            },
            {"op": "add_block", "name": "w1", "pose": "[1.5 0.3 0.2] m"},
            {"op": "add_port", "block": "w1", "name": "pin"},
            {"op": "add_block", "name": "w2", "pose": "[-0.4 1.7 0.5] m"},
            {"op": "add_port", "block": "w2", "name": "pin"},
            {
                "op": "connect",
                "a": "body.p",
                "b": "w1.pin",
                "joint": {"class": "axial", "params": dict(_ROD)},
            },
            {
                "op": "connect",
                "a": "w2.pin",
                "b": "body.q",
                "joint": {"class": "axial", "params": dict(_ROD)},
            },
        ],
    )
    system = se_stability._assemble(tree)
    assert system.dof[system.index["body"]] == 6
    _assert_stress_matches_fd(system, np.array([2.0, 3.0]))


def _assert_stress_matches_fd(system: se_stability._System, state: np.ndarray) -> None:
    k_mat = se_stability._stress_matrix(system, state)

    def points(x: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        row = system.live[k]
        out = []
        for node, arm, pt in (
            (row.a_block, system.arms[k][0], row.a_point),
            (row.b_block, system.arms[k][1], row.b_point),
        ):
            i = system.index[node]
            r0 = int(system.start[i])
            p = system.coords[i] + arm + x[r0 : r0 + 3]
            if system.dof[i] == 6:
                th = x[r0 + 3 : r0 + 6]
                ang = float(np.linalg.norm(th))
                if ang > 0.0:
                    kx = se_stability._skew(th / ang)
                    rot = np.eye(3) + math.sin(ang) * kx + (1 - math.cos(ang)) * kx @ kx
                    p = system.coords[i] + rot @ arm + x[r0 : r0 + 3]
            out.append(p)
        return out[0], out[1]

    def length(x: np.ndarray, k: int) -> float:
        a, b = points(x, k)
        return float(np.linalg.norm(b - a))

    def energy(x: np.ndarray) -> float:
        return sum(float(state[k]) * length(x, k) for k in range(len(state)))

    n, h = system.n, 1e-4
    x0 = np.zeros(n)
    hess = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            ei, ej = np.eye(n)[i] * h, np.eye(n)[j] * h
            hess[i, j] = (
                energy(x0 + ei + ej)
                - energy(x0 + ei - ej)
                - energy(x0 - ei + ej)
                + energy(x0 - ei - ej)
            ) / (4 * h * h)
    parallel = np.zeros((n, n))
    for k in range(len(state)):
        g = np.array(
            [
                (length(x0 + np.eye(n)[i] * h, k) - length(x0 - np.eye(n)[i] * h, k))
                / (2 * h)
                for i in range(n)
            ]
        )
        parallel += float(state[k]) / float(system.lengths[k]) * np.outer(g, g)
    expected = hess + parallel
    err = float(np.max(np.abs(k_mat - expected))) / float(np.max(np.abs(expected)))
    assert err < 1e-6, err


def test_stress_matrix_of_a_rigid_group_matches_finite_difference() -> None:
    """Same Hessian check on a merged body: g2's attachment arm is
    measured from the group reference, the block poses are rotated."""
    tree = _grouped(6)
    system = se_stability._assemble(tree)
    assert system.dof[system.index["g2"]] == 6
    assert system.index["g1"] == system.index["g2"]
    _assert_stress_matches_fd(system, np.array([1.0, 2.0, 3.0, 1.5, 0.5, 2.5]))
