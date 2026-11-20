"""gr593137: soft evidence ranks with warnings; physical failures never rank."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.quest import compute, frontier
from precis.utils import trust_gate


def _meta(*, blocker: str | None = "seeds:1-independent") -> dict[str, Any]:
    return {
        "autocatpath_version": "1.0.0",
        "results": {
            "trust_schema": 2,
            "route_steps": ["R->P"],
            "trust": [
                {
                    "id": check,
                    "check": check,
                    "step": "R->P",
                    "seed": 0,
                    "verdict": "pass",
                    "severity": "fatal",
                }
                for check in ("neb_convergence", "saddle_verified")
            ],
        },
        "barrier": 0.5,
        "barrier_trusted": blocker is None,
        "barrier_seed_blocker": blocker,
    }


def _stamp(monkeypatch: pytest.MonkeyPatch, meta: dict[str, Any]) -> dict[str, Any]:
    monkeypatch.setattr(compute, "_autocatpath_current_version", lambda: "1.0.0")
    measures = {k: v for k, v in meta.items() if k.startswith("barrier")}
    compute._stamp_barrier_assurance(meta, measures)
    return measures


@pytest.mark.parametrize("blocker", ["seeds:1-independent", "engine:stale", None])
def test_assurance_stamps_soft_evidence(
    monkeypatch: pytest.MonkeyPatch, blocker: str | None
) -> None:
    measures = _stamp(monkeypatch, _meta(blocker=blocker))
    expected = "PASS" if blocker is None else "WARN"
    assert measures["barrier_assurance"] == expected
    assert measures["barrier_trusted"] is (blocker is None)
    if blocker:
        assert blocker in measures["barrier_assurance_reasons"]


@pytest.mark.parametrize(
    "check",
    [
        "neb_convergence",
        "saddle_verified",
        "endpoint_agreement",
        "detachment",
        "wrong_binder",
    ],
)
def test_physical_failure_overrides_soft_evidence(
    monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    meta = _meta()
    meta["results"]["trust"].append(
        {
            "id": "bad",
            "check": check,
            "step": "R->P",
            "seed": 0,
            "verdict": "fail",
            "severity": "fatal",
        }
    )
    assert _stamp(monkeypatch, meta)["barrier_assurance"] == "FAIL"


@pytest.mark.parametrize(
    "blocker", ["seeds:unknown", "seeds:0-independent", "endpoint:union-disagree"]
)
def test_unknown_or_split_evidence_fails(
    monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    assert _stamp(monkeypatch, _meta(blocker=blocker))["barrier_assurance"] == "FAIL"


def test_missing_saddle_and_offroute_saddle_cannot_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _meta()
    meta["results"]["trust"][1]["step"] = "X->Y"
    assert _stamp(monkeypatch, meta)["barrier_assurance"] == "FAIL"


def test_absurd_barrier_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = _meta()
    meta["barrier"] = 20.0
    assert _stamp(monkeypatch, meta)["barrier_assurance"] == "FAIL"


def test_bep_estimate_warns_with_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = _meta(blocker=None)
    meta["results"]["edges"] = {"R->P": {"bep": {"alpha": 0.5, "beta": 0.2}}}
    measures = _stamp(monkeypatch, meta)
    assert measures["barrier_assurance"] == "WARN"
    assert "bep_estimated" in measures["barrier_assurance_reasons"]


def _candidate(ref_id: int, meta: dict[str, Any]) -> frontier.Candidate:
    store: Any = None
    return frontier._candidate_from_structure(
        store,
        SimpleNamespace(id=ref_id, title="candidate", meta=meta),
        runs=[{"converged": True, "energy": -1.0}],
    )


def test_warning_ranks_beside_separate_pass_frontier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warning = _candidate(2, _stamp(monkeypatch, _meta()))
    passed = _candidate(
        1, {"barrier": 1.0, "barrier_trusted": True, "barrier_assurance": "PASS"}
    )
    failed = _candidate(
        3, {"barrier": 0.1, "barrier_trusted": False, "barrier_assurance": "FAIL"}
    )
    fr = frontier.pareto_split([passed, warning, failed], [("barrier", "min")])
    assert fr.frontier == [warning]
    assert fr.pass_frontier == [passed]
    assert failed in fr.unevaluated
    assert "barrier" not in failed.measures
    assert frontier.candidate_trusted(warning) is False
    rows, schema = frontier.leaderboard(fr)
    row = next(r for r in rows if r["design"] == warning.handle)
    assert row["barrier"] == "≈0.5"
    assert "seeds:1-independent" in row["quality"]
    assert row["PASS+WARN"] == "★" and row["PASS-only"] == ""
    assert "PASS-only" in schema


def test_failure_never_makes_provisional_frontier() -> None:
    c = _candidate(
        1, {"barrier": 0.1, "barrier_trusted": False, "barrier_assurance": "FAIL"}
    )
    provisional, _ = frontier._provisional_split([], [c], [("barrier", "min")])
    assert not provisional[0].on_frontier


def test_soft_seed_row_requires_a_known_positive_seed() -> None:
    rows = [
        {"gate": k, "verdict": "pass", "blocking": 0}
        for k in (
            "neb_convergence",
            "saddle_verified",
        )
    ]
    rows.append(
        {
            "gate": "seed independence (precis)",
            "verdict": "fail",
            "blocking": 1,
            "input": "seed set unknowable",
        }
    )
    assert trust_gate.barrier_assurance(rows, trusted=False)[0] == "FAIL"


def test_direct_neb_single_seed_carries_warning_to_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.structure import neb

    monkeypatch.setattr(neb, "neb_engine_token", lambda: "1.0.0")
    store: Any = SimpleNamespace(struct_neb_edge_pool=lambda *args, **kwargs: [])
    records = _meta()["results"]["trust"]
    records += [
        {"id": check, "check": check, "verdict": "pass", "severity": "fatal"}
        for check in ("frequency", "detachment", "collapsed_band")
    ]
    records.append(
        {
            "id": "multistart",
            "check": "multistart",
            "verdict": "fail",
            "severity": "fatal",
        }
    )
    run: dict[str, Any] = {
        "id": 1,
        "is_ref_id": 1,
        "fs_ref_id": 2,
        "engine": "1.0.0",
        "barrier_fwd_ev": 0.5,
        "trusted": False,
        "trust": records,
        "multistart": {"starts": [{"init_seed": 0, "barrier_eV": 0.5, "valid": True}]},
    }
    measures = compute._struct_neb_measures_from_run(store, run)
    assert measures["barrier_assurance"] == "WARN"
    entry = compute._neb_edge_entry(run, measures)
    stamp = compute._neb_candidate_stamp({"edge": entry}, {})
    assert stamp is not None and stamp["barrier_assurance"] == "WARN"
    assert _candidate(1, stamp).measures["barrier"] == 0.5
