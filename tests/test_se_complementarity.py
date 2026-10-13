"""The se side of the complementarity solver (docs/backlog/
complementarity-solver.md slices 1–3): :mod:`precis_se.stability`'s
bridge into :mod:`precis.structsolve.complementarity` — the taut/slack
``view='stability'`` section (:func:`load_path_report`), the warn-first
``completeness`` findings folded into ``view='drc'``/``validate.validate``
(:func:`completeness_findings`), and the slice-3 bistability entry
(:func:`probe_member_bistability` surfaced via
``args={'bistable': ...}``). The load-case vocabulary is the view-layer
``load_cases`` argument (the spec's open question, resolved 2026-10-13 —
no objectives-schema change)."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import drc as se_drc
from precis_se import stability as se_stability
from precis_se import validate as se_validate
from precis_se.handler import SeHandler, _render_stability
from precis_se.ops import SeTree, apply_ops


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


_TIE = {"tension_capacity": 100.0, "compression_capacity": 0.0}
_STRUT = {"tension_capacity": 0.0, "compression_capacity": 100.0}
_ROD = {"tension_capacity": 100.0, "compression_capacity": 100.0}
_RATE = {"rate": 10.0}

_S32 = math.sqrt(3.0) / 2.0


def _pin_structure(
    nodes: dict[str, list[float]],
    members: list[tuple[str, str, dict[str, float]]],
    fixed: dict[str, Any] | None = None,
    forces: dict[str, list[float]] | None = None,
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
    for name, vec in (forces or {}).items():
        ops.append({"op": "set_load", "block": name, "force": vec})
    return apply_ops(SeTree(), ops)


def _tripod() -> SeTree:
    """The core's anchored tripod as an se design: strut A—D, cables
    B—D/C—D, apex D riding the z rail with a lateral load — the solve's
    hand-checked answer is strut bearing, cable B taut, cable C slack."""
    return _pin_structure(
        {
            "a": [1.0, 0.0, 0.0],
            "b": [-0.5, _S32, 0.0],
            "c": [-0.5, -_S32, 0.0],
            "d": [0.0, 0.0, 1.0],
        },
        [
            ("a", "d", dict(_STRUT, **_RATE)),
            ("b", "d", dict(_TIE, **_RATE)),
            ("c", "d", dict(_TIE, **_RATE)),
        ],
        fixed={"a": True, "b": True, "c": True, "d": ["z"]},
        forces={"d": [1.0, -1.0, 0.0]},
    )


def _tie_driven_into_compression(**kw: Any) -> SeTree:
    """A(0,0,0) and B(1,0,0) grounded; P(0.5,0,1) held in-plane by a tie
    to A and a rod to B. A downward load on P drives the tie
    compressive — the sign-blind solve carries it, the sign-aware one
    cannot: ``incomplete``, culprit = the tie."""
    return _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0], "p": [0.5, 0.0, 1.0]},
        [("a", "p", dict(_TIE, **_RATE)), ("b", "p", dict(_ROD, **_RATE))],
        fixed={"a": True, "b": True, "p": ["y"]},
        **kw,
    )


def _bistable_tree() -> SeTree:
    """The core's two-equilibria fixture as an se design: moving node m
    tied to grounded anchors g1 (the photoswitch member) and g2. Under
    cis the problem admits two consistent active sets — the trans
    equilibrium is also a stable equilibrium of cis: one structure, two
    equilibria."""
    return _pin_structure(
        {"g1": [0.4, 0.5, 0.0], "g2": [0.6, 1.4, 0.0], "m": [-0.4, 0.3, 0.0]},
        [
            ("g1", "m", dict(_TIE, rate=3.0, free_length=0.99)),
            ("g2", "m", dict(_TIE, rate=2.0, free_length=1.25)),
        ],
        fixed={"g1": True, "g2": True, "m": ["z"]},
        forces={"m": [-1.0, -2.0, 0.0]},
    )


# ── slice 1: the taut/slack section of view='stability' ─────────────────


def test_load_path_matches_the_core_tripod_solve() -> None:
    report = se_stability.load_path_report(_tripod())
    assert report is not None and report.checked
    (case,) = report.cases
    assert case.name == "declared" and case.solved
    by_subject = {m.subject: m for m in case.members}
    assert by_subject["a.pin—d.pin"].status == "bearing"
    assert by_subject["a.pin—d.pin"].force < 0.0
    assert by_subject["b.pin—d.pin"].status == "taut"
    assert by_subject["c.pin—d.pin"].status == "slack"
    assert by_subject["c.pin—d.pin"].force == 0.0


def test_load_path_skips_undeclared_rate_and_reports() -> None:
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0], "c": [0.0, 1.0, 0.0]},
        [
            ("a", "c", dict(_TIE, **_RATE)),
            ("b", "c", dict(_TIE)),  # no rate — skip-and-report
        ],
        fixed={"a": True, "b": True, "c": True},
    )
    report = se_stability.load_path_report(tree)
    assert report is not None
    assert [s for s, _ in report.skipped] == ["b.pin—c.pin"]
    assert "rate" in report.skipped[0][1]
    assert report.cases[0].solved


def test_load_path_is_silent_without_axial_members() -> None:
    tree = apply_ops(
        SeTree(),
        [{"op": "add_block", "name": "a", "pose": [0, 0, 0], "pose_unit": "m"}],
    )
    assert se_stability.load_path_report(tree) is None
    assert se_stability.completeness_findings(tree) == []


def test_load_path_reports_refusals_never_a_bare_verdict() -> None:
    # tie driven into compression: the sign-aware solve refuses — the
    # case carries the refusal text, not a fabricated member table.
    report = se_stability.load_path_report(
        _tie_driven_into_compression(forces={"p": [0.0, 0.0, -10.0]})
    )
    assert report is not None
    case = report.cases[0]
    assert not case.solved
    assert case.error and "no legal path" in case.error


def test_load_path_named_cases_layer_on_the_declared_case() -> None:
    report = se_stability.load_path_report(
        _tripod(), load_cases=[("shock", {"d": [2.0, -2.0, 0.0]})]
    )
    assert report is not None
    assert [c.name for c in report.cases] == ["declared", "shock"]
    shock = report.cases[1]
    assert shock.solved
    by_subject = {m.subject: m.status for m in shock.members}
    assert by_subject["c.pin—d.pin"] == "slack"


def test_load_path_named_case_on_a_non_node_block_is_dropped_with_note() -> None:
    tree = _tripod()
    apply_ops(
        tree,
        [{"op": "add_block", "name": "island", "pose": [9, 9, 9], "pose_unit": "m"}],
    )
    report = se_stability.load_path_report(
        tree, load_cases=[("shock", {"island": [0, 0, -5.0], "d": [1.0, -1.0, 0.0]})]
    )
    assert report is not None
    assert "island" in report.cases[1].note


def test_load_path_notes_carry_the_honesty_scoping() -> None:
    report = se_stability.load_path_report(_tripod())
    assert report is not None
    joined = "\n".join(report.notes)
    assert "NOT an independent physics" in joined
    assert "unstressed at their installed length" in joined


def test_stability_view_renders_the_taut_slack_section() -> None:
    body = _render_stability(_tripod())
    assert "## load path" in body
    assert "case 'declared'" in body
    assert "slack" in body and "bearing" in body and "taut" in body
    assert "NOT an independent physics" in body


def test_stability_view_named_case_and_bistable_args(handler: SeHandler) -> None:
    handler.put(
        id="tripod",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": n, "pose": p, "pose_unit": "m"}
                    for n, p in {
                        "a": [1.0, 0.0, 0.0],
                        "b": [-0.5, _S32, 0.0],
                        "c": [-0.5, -_S32, 0.0],
                        "d": [0.0, 0.0, 1.0],
                    }.items()
                ]
                + [
                    {"op": "add_port", "block": n, "name": "pin"}
                    for n in ("a", "b", "c", "d")
                ]
                + [
                    {
                        "op": "connect",
                        "a": f"{a}.pin",
                        "b": f"{b}.pin",
                        "joint": {"class": "axial", "params": p},
                    }
                    for a, b, p in [
                        ("a", "d", dict(_STRUT, **_RATE)),
                        ("b", "d", dict(_TIE, **_RATE)),
                        ("c", "d", dict(_TIE, **_RATE)),
                    ]
                ]
                + [
                    {"op": "set_load", "block": n, "fixed": True}
                    for n in ("a", "b", "c")
                ]
                + [
                    {"op": "set_load", "block": "d", "fixed": ["z"]},
                    {"op": "set_load", "block": "d", "force": [1.0, -1.0, 0.0]},
                ]
            }
        ),
    )
    body = handler.get(
        id="tripod",
        view="stability",
        args={"load_cases": [{"name": "shock", "loads": {"d": [2, -2, 0]}}]},
    ).body
    assert "case 'shock'" in body
    assert "case 'declared'" in body


# ── slice 2: sign-aware completeness findings in validate/drc ────────────


def test_tie_driven_into_compression_is_a_warn_finding_naming_member_and_case() -> None:
    tree = _tie_driven_into_compression(forces={"p": [0.0, 0.0, -10.0]})
    for findings in (
        se_drc.drc(tree).findings,
        se_validate.validate(tree),
    ):
        hits = [
            f
            for f in findings
            if f.rule == "completeness" and f.subject == "a.pin—p.pin"
        ]
        assert hits, "expected a completeness finding naming the member"
        assert hits[0].severity == "warn"
        assert "'declared'" in hits[0].detail
        assert "illegal sign" in hits[0].detail


def test_completeness_clears_when_the_member_is_re_signed() -> None:
    # the same geometry with the a—p member declared a strut: compression
    # is its legal sign, the case completes, no finding.
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0], "p": [0.5, 0.0, 1.0]},
        [("a", "p", dict(_STRUT, **_RATE)), ("b", "p", dict(_ROD, **_RATE))],
        fixed={"a": True, "b": True, "p": ["y"]},
        forces={"p": [0.0, 0.0, -10.0]},
    )
    # cleared = no warn-tier completeness finding; the info scope notes
    # ride every run once the solve checked (they are standing honesty,
    # not a finding against the design).
    assert not [
        f
        for f in se_drc.drc(tree).findings
        if f.rule == "completeness" and f.severity == "warn"
    ]


def test_completeness_clears_when_the_load_is_removed() -> None:
    tree = _tie_driven_into_compression()  # no forces at all
    assert not [
        f
        for f in se_drc.drc(tree).findings
        if f.rule == "completeness" and f.severity == "warn"
    ]


def test_named_load_case_finding_names_the_case() -> None:
    tree = _tie_driven_into_compression()
    hits = [
        f
        for f in se_drc.drc(
            tree, load_cases=[("shock", {"p": [0.0, 0.0, -10.0]})]
        ).findings
        if f.rule == "completeness" and f.subject == "a.pin—p.pin"
    ]
    assert hits and "'shock'" in hits[0].detail


def test_dropped_case_load_is_a_finding_not_a_silent_skip() -> None:
    tree = _tie_driven_into_compression()
    apply_ops(
        tree,
        [{"op": "add_block", "name": "island", "pose": [9, 9, 9], "pose_unit": "m"}],
    )
    hits = [
        f
        for f in se_drc.drc(
            tree, load_cases=[("shock", {"island": [0.0, 0.0, -10.0]})]
        ).findings
        if f.rule == "completeness"
    ]
    assert any("island" in f.detail and "not applied" in f.detail for f in hits)


def test_mechanism_verdict_names_the_case_and_the_cause() -> None:
    # a lone member with a fully free loaded node — even the sign-blind
    # solve finds no path (a mechanism, not a sign problem).
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "p": [0.0, 0.0, 1.0]},
        [("a", "p", dict(_ROD, **_RATE))],
        fixed={"a": True},
        forces={"p": [1.0, 0.0, 0.0]},
    )
    hits = [f for f in se_drc.drc(tree).findings if f.rule == "completeness"]
    assert any(
        f.subject == "load case 'declared'"
        and "mechanism" in f.detail
        and f.severity == "warn"
        for f in hits
    )


def test_no_solvable_member_reports_not_checked_at_info_not_warn() -> None:
    # an axial design without a declared rate is honestly UNCHECKED —
    # info-tier, not a warn (review: unchecked ≠ finding-worthy).
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0]},
        [("a", "b", dict(_TIE))],  # no rate — the solve cannot run
        fixed={"a": True},
    )
    hits = [f for f in se_drc.drc(tree).findings if f.rule == "completeness"]
    assert hits and all(f.severity == "info" for f in hits)
    assert any("not checked" in f.detail for f in hits)
    assert any("UNCHECKED" in f.detail for f in hits)


def test_completeness_scope_notes_are_surfaced_at_info() -> None:
    # the core's standing notes (rep.notes — "'complete' means a legal
    # load path exists, not that members are adequately sized") ride
    # every run at info tier, complete cases included.
    hits = [f for f in se_drc.drc(_tripod()).findings if f.rule == "completeness"]
    assert any(f.severity == "info" and "adequately sized" in f.detail for f in hits)


def test_case_named_declared_is_refused() -> None:
    # 'declared' is the implicit stored-objectives case's name — a user
    # case colliding with it is refused, not silently shadowed.
    with pytest.raises(ValueError, match="declared"):
        se_stability.load_path_report(_tripod(), load_cases=[("declared", {})])


def test_view_refuses_case_named_declared(handler: SeHandler) -> None:
    handler.put(id="empty2", text=json.dumps({"ops": []}))
    with pytest.raises(BadInput, match="declared"):
        handler.get(
            id="empty2",
            view="stability",
            args={"load_cases": [{"name": "declared", "loads": {}}]},
        )


# ── slice 3: the bistability probe entry ─────────────────────────────────


def test_bistable_member_reports_two_equilibria_and_barrier() -> None:
    probe = se_stability.probe_member_bistability(
        _bistable_tree(), "g1.pin—m.pin", 0.99, 0.66
    )
    assert probe.error is None
    res = probe.result
    assert res is not None and res.bistable
    assert res.barrier is not None
    assert any("holds both" in n for n in res.notes)
    assert all(s.solved and s.stable for s in res.states)


def test_identical_assignments_are_one_equilibrium_honestly() -> None:
    probe = se_stability.probe_member_bistability(
        _bistable_tree(), "g1.pin—m.pin", 0.99, 0.99
    )
    res = probe.result
    assert res is not None and not res.bistable
    assert any("coincide" in n for n in res.notes)


def test_monostable_assignment_reports_one_state_honestly() -> None:
    # cis so long the tie can never engage: the sign-aware solve finds no
    # equilibrium for assignment B — one state reported, no barrier.
    tree = _pin_structure(
        {"a": [0.0, 0.0, 0.0], "b": [1.0, 0.0, 0.0], "p": [0.5, 0.0, 1.0]},
        [
            ("a", "p", dict(_TIE, **_RATE)),
            ("b", "p", dict(_ROD, **_RATE)),
        ],
        fixed={"a": True, "b": True, "p": ["y"]},
        forces={"p": [0.0, 0.0, -10.0]},
    )
    probe = se_stability.probe_member_bistability(tree, "a.pin—p.pin", 1.1, 10.0)
    res = probe.result
    assert res is not None and not res.bistable
    assert res.barrier is None
    assert any("not bistable" in n for n in res.notes)


def test_bistability_probe_names_unknown_and_skipped_members() -> None:
    probe = se_stability.probe_member_bistability(
        _bistable_tree(), "g1.pin—nope.pin", 0.99, 0.66
    )
    assert probe.error and "no axial member" in probe.error


def test_stability_view_renders_the_probe_with_notes_verbatim(
    handler: SeHandler,
) -> None:
    handler.put(
        id="bistable1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": n, "pose": p, "pose_unit": "m"}
                    for n, p in {
                        "g1": [0.4, 0.5, 0.0],
                        "g2": [0.6, 1.4, 0.0],
                        "m": [-0.4, 0.3, 0.0],
                    }.items()
                ]
                + [
                    {"op": "add_port", "block": n, "name": "pin"}
                    for n in ("g1", "g2", "m")
                ]
                + [
                    {
                        "op": "connect",
                        "a": "g1.pin",
                        "b": "m.pin",
                        "joint": {
                            "class": "axial",
                            "params": dict(_TIE, rate=3.0, free_length=0.99),
                        },
                    },
                    {
                        "op": "connect",
                        "a": "g2.pin",
                        "b": "m.pin",
                        "joint": {
                            "class": "axial",
                            "params": dict(_TIE, rate=2.0, free_length=1.25),
                        },
                    },
                ]
                + [
                    {"op": "set_load", "block": "g1", "fixed": True},
                    {"op": "set_load", "block": "g2", "fixed": True},
                    {"op": "set_load", "block": "m", "fixed": ["z"]},
                    {"op": "set_load", "block": "m", "force": [-1.0, -2.0, 0.0]},
                ]
            }
        ),
    )
    body = handler.get(
        id="bistable1",
        view="stability",
        args={"bistable": {"member": "g1.pin—m.pin", "trans": 0.99, "cis": 0.66}},
    ).body
    assert "## bistability probe" in body
    assert "bistable: YES" in body
    # the core's notes surfaced verbatim
    assert "one structure, two equilibria" in body
    assert "ESTIMATE, not a certified saddle-point energy" in body


def test_view_args_vetting(handler: SeHandler) -> None:
    handler.put(id="empty1", text=json.dumps({"ops": []}))
    with pytest.raises(BadInput, match="load_cases"):
        handler.get(id="empty1", view="stability", args={"load_cases": "x"})
    with pytest.raises(BadInput, match="bistable"):
        handler.get(id="empty1", view="stability", args={"bistable": {"member": "a"}})
    with pytest.raises(BadInput, match="unknown args key"):
        handler.get(id="empty1", view="stability", args={"cases": []})
