"""Grounding-auditor eval harness (gr634496): no DB, no model."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from precis.cli import mesh as mesh_cli
from precis.errors import BadInput
from precis.mesh_eval import grounding as ge

_CASE: dict[str, Any] = {
    "id": "x-1",
    "category": "missing_citation",
    "body": "Compound Q boils at 87 degrees Celsius.",
    "gold": {"claims": [{"quote": "Compound Q boils", "status": "abstain"}]},
}


def _case(**over: Any) -> dict[str, Any]:
    return {**_CASE, **over}


def _by_id() -> dict[str, ge.GroundingCase]:
    return {c.case_id: c for c in ge.load_cases()}


def _outcomes(case_id: str) -> list[ge.ClaimOutcome]:
    return ge.score_run(ge.run_case(_by_id()[case_id]))


def test_loader_rejects_case_without_category_or_gold_status() -> None:
    no_category = {k: v for k, v in _CASE.items() if k != "category"}
    with pytest.raises(BadInput, match="category"):
        ge.parse_case(no_category)
    with pytest.raises(BadInput, match="category"):
        ge.parse_case(_case(category="not-a-category"))
    with pytest.raises(BadInput, match="gold"):
        ge.parse_case({k: v for k, v in _CASE.items() if k != "gold"})
    with pytest.raises(BadInput, match="status"):
        ge.parse_case(_case(gold={"claims": [{"quote": "Compound Q boils"}]}))
    with pytest.raises(BadInput, match="status"):
        ge.parse_case(
            _case(gold={"claims": [{"quote": "Compound Q boils", "status": "maybe"}]})
        )
    assert ge.parse_case(_CASE).case_id == "x-1"


def test_loader_rejects_duplicate_ids_and_bad_json(tmp_path: Path) -> None:
    dup = tmp_path / "dup.jsonl"
    dup.write_text(
        json.dumps(_CASE) + "\n" + json.dumps(_CASE) + "\n", encoding="utf-8"
    )
    with pytest.raises(BadInput, match="duplicate"):
        ge.load_cases(dup)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{nope\n", encoding="utf-8")
    with pytest.raises(BadInput, match="invalid JSON"):
        ge.load_cases(bad)


def test_every_listed_category_has_two_cases() -> None:
    counts = Counter(c.category for c in ge.load_cases())
    assert set(counts) == set(ge.CATEGORIES)
    assert all(counts[cat] >= 2 for cat in ge.CATEGORIES), counts


def test_false_approval_counted_and_attributed_to_verification_stage() -> None:
    # wc-02 plants a verifier that approves a passage on a different topic.
    (outcome,) = _outcomes("wc-02")
    assert outcome.outcome == "false_approval"
    assert outcome.stage == "verification"
    report = ge.build_report([ge.run_case(_by_id()["wc-02"])])
    assert report.metrics["false_approvals"].k == 1
    assert report.stages["verification"].k == 1
    assert report.stages["policy"].k == 0
    assert report.stages["extraction"].k == 0


def test_fluent_judgment_with_unresolved_doi_cannot_pass() -> None:
    doi = "10.1000/xyz123"
    quote = "The catalyst doubles the yield"
    raw = _case(
        category="wrong_claim_citation",
        body=f"{quote} (doi:{doi}).",
        gold={
            "claims": [
                {
                    "quote": quote,
                    "kind": "empirical",
                    "status": "abstain",
                    "evidence": [doi],
                }
            ]
        },
        resolvable=[doi],  # the DOI resolves, but no passage comes back
        recorded={
            "classifier": {
                "claims": [
                    {
                        "quote": quote,
                        "kind": "empirical",
                        "status": "supported",
                        "evidence": [doi],
                    }
                ]
            },
            # A fluent verifier would approve if it were ever asked.
            "support": {
                quote: {
                    "supported": True,
                    "source_fidelity": "faithful",
                    "evidential_strength": "established",
                    "study_type": "experiment",
                    "quantities_match": True,
                    "conditions_match": True,
                }
            },
        },
    )
    run = ge.run_case(ge.parse_case(raw))
    (claim,) = [c for c in run.record.claims if c.kind == "empirical"]
    assert claim.status == "provisional"  # the classifier's fluent suggestion
    assert claim.rollup != "supported"
    assert claim.support is not None and claim.support["verdict"] == "unresolved"
    assert run.cost.verifier_calls == 0  # nothing to verify against
    (outcome,) = ge.score_run(run)
    assert outcome.outcome != "false_approval"
    # The identical gold with a retrieved passage can pass: retrieval gates it.
    assert ge.system_status(claim) == "unresolved"


def test_retrieval_failure_is_attributed_to_retrieval_stage() -> None:
    case = _by_id()["wc-01"]
    broken = ge.parse_case(
        {
            "id": "rf-1",
            "category": "wrong_claim_citation",
            "body": case.body,
            "gold": {
                "claims": [
                    {
                        "quote": "Enzyme E1 degrades polyester at 70 C",
                        "kind": "empirical",
                        "status": "supported",
                        "evidence": ["pc201"],
                    }
                ]
            },
            "sources": {"pc201": {"text": "x", "paper": "pa21", "retrievable": False}},
            "recorded": {
                "classifier": {
                    "claims": [
                        {
                            "quote": "Enzyme E1 degrades polyester at 70 C",
                            "kind": "empirical",
                            "status": "supported",
                            "evidence": ["pc201"],
                        }
                    ]
                }
            },
        }
    )
    run = ge.run_case(broken)
    (outcome,) = ge.score_run(run)
    assert outcome.stage == "retrieval"
    report = ge.build_report([run])
    assert report.metrics["evidence_retrieval_failures"].k == 1


def test_missed_claim_is_extraction_and_disclosed_when_truncated() -> None:
    outcomes = _outcomes("ts-01")
    tail = [o for o in outcomes if o.outcome == "missed"]
    assert len(tail) == 1
    assert tail[0].stage == "extraction"
    assert tail[0].disclosed is True
    report = ge.build_report([ge.run_case(_by_id()["ts-01"])])
    assert report.coverage["misses_silent"] == 0
    assert report.coverage["truncation_flag_agrees"].k == 1


def test_corrupt_classifier_reply_is_extraction_error() -> None:
    (outcome,) = _outcomes("ce-01")
    assert (outcome.outcome, outcome.stage) == ("missed", "extraction")
    assert outcome.disclosed is True


def test_cycle_members_are_unsupported_even_with_fluent_warrants() -> None:
    run = ge.run_case(_by_id()["cy-01"])
    verdicts = [c.rollup for c in run.record.claims if c.kind == "inference"]
    assert verdicts == ["unsupported", "unsupported"]
    assert all(o.correct for o in ge.score_run(run))


def test_source_edit_stales_only_the_claim_citing_it() -> None:
    run = ge.run_case(_by_id()["sx-02"])
    rollups = {c.text: c.rollup for c in run.record.claims if c.rollup}
    assert rollups["The pump delivers 5 litres per minute"] == "partial"
    assert rollups["The valve closes within 2 seconds"] == "supported"


def test_rates_report_denominators_and_wilson_interval() -> None:
    report = ge.run_corpus(ge.load_cases())
    far = report.metrics["false_approvals"]
    assert far.n > 0 and far.k <= far.n
    assert far.lo <= (far.value or 0.0) <= far.hi
    assert report.cases == len(ge.load_cases())
    assert report.cost["model_calls"] > 0
    assert set(report.stages) == set(ge.STAGES)
    # No independent adjudication exists yet; the report must say so.
    assert report.adjudication.get("independent", 0) == 0


def test_criteria_judge_only_set_values(tmp_path: Path) -> None:
    report = ge.run_corpus(ge.load_cases())
    blank = tmp_path / "blank.json"
    blank.write_text(json.dumps({"false_approval_upper_max": None}), encoding="utf-8")
    assert ge.evaluate_criteria(report, ge.load_criteria(blank)) == []
    strict = tmp_path / "strict.json"
    strict.write_text(json.dumps({"false_approval_upper_max": 0.0}), encoding="utf-8")
    (row,) = ge.evaluate_criteria(report, ge.load_criteria(strict))
    assert row["passed"] is False
    typo = tmp_path / "typo.json"
    typo.write_text(json.dumps({"false_approval_rate": 0.1}), encoding="utf-8")
    with pytest.raises(BadInput, match="unknown criteria"):
        ge.load_criteria(typo)


def _args(**over: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "criteria": None,
        "cases": None,
        "repairs": False,
        "json": False,
    }
    return argparse.Namespace(**{**base, **over})


def test_cli_report_only_without_criteria(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    mesh_cli._cmd_grounding(_args())  # no SystemExit: nothing to fail
    out = capsys.readouterr().out
    assert "report only, no pass/fail" in out
    assert "PASS" not in out and "FAIL" not in out
    assert "faulty stage" in out and "Wilson" in out

    mesh_cli._cmd_grounding(_args(json=True))
    data = json.loads(capsys.readouterr().out)
    assert "criteria" not in data
    assert data["metrics"]["false_approvals"]["n"] > 0

    blank = tmp_path / "blank.json"
    blank.write_text(json.dumps({"false_approval_upper_max": None}), encoding="utf-8")
    mesh_cli._cmd_grounding(_args(criteria=str(blank)))
    assert "sets no values" in capsys.readouterr().out

    strict = tmp_path / "strict.json"
    strict.write_text(json.dumps({"false_approval_upper_max": 0.0}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        mesh_cli._cmd_grounding(_args(criteria=str(strict)))
    assert exc.value.code == 1
    assert "FAIL false_approval_upper_max" in capsys.readouterr().out
