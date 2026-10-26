"""Evidence and spend gates for the offline shadows; dispatch is always stubbed."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(scope="module")
def shadow() -> Any:
    path = (
        Path(__file__).resolve().parents[1] / "scripts/llm_eval/local_first_shadow.py"
    )
    spec = importlib.util.spec_from_file_location("local_first_shadow_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def claim(
    index: int = 0, *, file: str = "join.yaml", confidence: str = "handbook"
) -> dict[str, Any]:
    return {
        "version": 1,
        "op": "dtree",
        "id": f"rule-{index}",
        "provenance": {"source": "fixture:design-source", "captured_at": "2026-10-25"},
        "file": file,
        "confidence": confidence,
        "rule": {
            "text": "Allow 10 mm clearance.",
            "value": "10",
            "unit": "mm",
            "source_url": "https://example.org/design-source",
        },
        "windows": [{"text": "Allow 10 mm clearance.", "locator": "page 2"}],
    }


def triage_sample() -> dict[str, Any]:
    return {
        "version": 1,
        "op": "triage",
        "id": "gr1",
        "provenance": {"source": "fixture:triage-export", "captured_at": "2026-10-25"},
        "prompt": "Production single-gripe prompt for gr1: git status",
        "baseline_verdict": "noise",
        "fix_evidence": [],
        "gripe": {
            "ref_id": 1,
            "title": "git status",
            "body": "",
            "created_at": "2026-10-25T00:00:00+00:00",
            "tags": [],
            "reopened": False,
            "filed_by_owner": False,
        },
    }


def options(**overrides: Any) -> argparse.Namespace:
    values = {
        "op": "dtree",
        "seed": "1",
        "cap_usd": 15.0,
        "judge": 20,
        "local_url": "http://localhost:12345",
        "n": 2,
        "controls": 0,
        "run": False,
        "dry_run": False,
    }
    return argparse.Namespace(**(values | overrides))


def answer(verdict: str = "supported", *, cost: Any = None) -> dict[str, Any]:
    return {
        "text": "",
        "error": False,
        "paused": False,
        "timed_out": False,
        "cost_usd": cost,
        "duration_s": 1.0,
        "data": {
            "verdict": verdict,
            "confidence": "high",
            "quote": "Allow 10 mm clearance.",
            "reason": "Matches the source.",
        },
    }


@pytest.mark.parametrize(
    ("op", "n", "local_tokens", "controls"),
    [
        ("triage", 150, 4096, 0),
        ("table_recipe", 100, 4096, 0),
        ("dtree", 150, 3000, 40),
    ],
)
def test_dry_run_needs_no_evidence_or_runtime(
    shadow: Any,
    monkeypatch: Any,
    capsys: Any,
    op: str,
    n: int,
    local_tokens: int,
    controls: int,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run reached runtime/evidence code")

    monkeypatch.setattr(shadow, "execute", forbidden)
    monkeypatch.setattr(shadow, "preflight", forbidden)
    monkeypatch.setattr(shadow, "load_samples", forbidden)
    assert shadow.main(["--op", op, "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["base_n"] == n
    assert plan["controls"] == controls
    assert plan["arms"][0]["tokens"] == local_tokens
    assert plan["estimate_total_usd"] > 0
    assert plan["cap_usd"] == 15
    assert plan["db_access"] is False and plan["execution"] is False
    if op == "table_recipe":
        assert [(a["label"], a["tokens"]) for a in plan["arms"]] == [
            ("A", 4096),
            ("B", 1024),
            ("C", 4096),
        ]


def test_default_is_plan_and_dry_run_overrides_run(
    shadow: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(shadow, "execute", lambda *a, **k: pytest.fail("dispatch"))
    assert shadow.main(["--op", "triage"]) == 0
    assert shadow.main(["--op", "triage", "--run", "--dry-run"]) == 0


def test_run_without_samples_refuses_before_dispatch(
    shadow: Any, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(shadow, "execute", lambda *a, **k: pytest.fail("dispatch"))
    assert shadow.main(["--op", "dtree", "--run"]) == 2
    assert "requires valid --samples" in capsys.readouterr().err


@pytest.mark.parametrize("cap", ["nan", "inf", "-1", "0"])
def test_invalid_cap_refuses(shadow: Any, cap: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        shadow.main(["--op", "dtree", "--cap-usd", cap])


def test_refuses_missing_source_evidence_and_duplicate_ids(
    shadow: Any, tmp_path: Path
) -> None:
    path = tmp_path / "samples.jsonl"
    row = claim()
    row["windows"] = []
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1.*windows"):
        shadow.load_samples(path, "dtree")
    row = claim()
    path.write_text((json.dumps(row) + "\n") * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="line 2.*duplicate"):
        shadow.load_samples(path, "dtree")
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        shadow.load_samples(path, "dtree")


def test_refuses_supplied_control_labels(shadow: Any) -> None:
    row = claim()
    row["control"] = {"expected": "supported"}
    with pytest.raises(ValueError, match="reserved"):
        shadow.validate_sample(row, "dtree")


def test_seeded_sampling_preserves_small_strata(shadow: Any) -> None:
    rows = [claim(i) for i in range(12)] + [
        claim(20, file="rotate.yaml", confidence="secondary")
    ]
    sampled = shadow.sample_rows(rows, "dtree", 6, "reproducible")
    assert len(sampled) == 6
    assert any(r["file"] == "rotate.yaml" for r in sampled)
    assert sampled == shadow.sample_rows(
        list(reversed(rows)), "dtree", 6, "reproducible"
    )
    with pytest.raises(ValueError, match="need 150 distinct"):
        shadow.sample_rows(rows, "dtree", 150, "1")


def test_controls_mutate_claim_but_preserve_source_windows(shadow: Any) -> None:
    rows = [claim(i) for i in range(150)]
    controls = shadow.build_controls(rows, 40, "seed")
    assert len(controls) == 40
    assert len({r["control"]["base_id"] for r in controls}) == 40
    assert controls == shadow.build_controls(rows, 40, "seed")
    assert {r["control"]["mutation"] for r in controls} == {
        "value_x2",
        "value_half",
        "swapped_unit",
    }
    for row in controls:
        assert row["rule"]["text"] != rows[0]["rule"]["text"]
        assert row["rule"]["value"] != "10" or row["rule"]["unit"] != "mm"
        assert row["windows"] == rows[0]["windows"]
        prompt = shadow.prompt_for(row, "dtree")
        assert "control" not in prompt
        assert row["rule"]["text"] in prompt
    unmutable = claim()
    unmutable["rule"].update(
        text="Check adequate clearance", value="unknown", unit="unknown"
    )
    with pytest.raises(ValueError, match="need 40 distinct mutable"):
        shadow.build_controls([unmutable], 40, "seed")


def test_budget_reserves_before_spend_and_reconciles(shadow: Any) -> None:
    budget = shadow.Budget(0.5)
    assert budget.reserve(0.4)
    assert not budget.reserve(0.2)
    budget.reconcile(0.4, 0.1)
    assert math.isclose(budget.spent, 0.1)
    assert math.isclose(budget.held, 0)
    assert budget.reserve(0.4)


@pytest.mark.parametrize("cost", [None, float("nan"), float("inf"), -0.1, True, "0.01"])
def test_unknown_or_invalid_cost_blocks_further_paid_dispatch(
    shadow: Any, cost: Any
) -> None:
    calls = []
    records: list[dict[str, Any]] = []

    def fake(prompt: str, arm: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(arm.label)
        return answer(cost=None if arm.local else cost)

    summary = shadow.execute(
        [claim(1), claim(2)], options(), records.append, dispatch_fn=fake
    )
    assert calls == ["A", "C"]
    assert summary["paid_stop"] == "unknown_cost"
    assert summary["unresolved_reservation_usd"] > 0
    assert summary["n"] == 1
    # NaN never reaches the durable JSONL encoder.
    json.dumps(records, allow_nan=False)


def test_cap_preflight_makes_zero_paid_calls_and_reports_stop(shadow: Any) -> None:
    calls = []

    def fake(prompt: str, arm: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(arm.label)
        assert arm.local
        return answer()

    summary = shadow.execute(
        [claim(1), claim(2)], options(cap_usd=0.001), lambda r: None, dispatch_fn=fake
    )
    assert calls == ["A"]
    assert summary["paid_stop"] == "cap_preflight"
    assert summary["agreement"] == 0 and summary["n"] == 1


def test_paid_overrun_and_unexpected_paid_local_stop(shadow: Any) -> None:
    calls = []

    def fake(prompt: str, arm: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(arm.label)
        return answer(cost=10 if not arm.local else None)

    summary = shadow.execute(
        [claim(1), claim(2)], options(), lambda r: None, dispatch_fn=fake
    )
    assert summary["paid_stop"] == "reservation_exceeded"
    assert calls == ["A", "C"]
    calls.clear()

    def accidental_paid(prompt: str, arm: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(arm.label)
        return answer(cost=0.1) | {"placement": "cloud"}

    summary = shadow.execute(
        [claim(1), claim(2)], options(), lambda r: None, dispatch_fn=accidental_paid
    )
    assert summary["paid_stop"] == "unexpected_paid_local"
    assert calls == ["A"]
    assert math.isclose(summary["spent_usd"], 0.1)


def test_blind_judge_only_disagreements_max_twenty(shadow: Any) -> None:
    calls = []
    judged_prompts = []

    def fake(prompt: str, arm: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(arm.label)
        if arm.label == "judge":
            judged_prompts.append(prompt)
            return {"data": {"winner": "A", "reason": "Faithful"}, "cost_usd": 0.01}
        return answer(
            "supported" if arm.local else "contradicted",
            cost=None if arm.local else 0.01,
        )

    records: list[dict[str, Any]] = []
    rows = [claim(i) for i in range(25)]
    summary = shadow.execute(rows, options(), records.append, dispatch_fn=fake)
    assert calls.count("judge") == 20
    assert summary["judge_n"] == 20 and summary["agreement"] == 0
    for prompt in judged_prompts:
        assert "castor" not in prompt and "Sonnet" not in prompt
        assert "local-first-shadow" not in prompt
    for record in records:
        if record["kind"] == "judge":
            assert record["winner"] == ("paid" if record["flipped"] else "local")

    def agreeing(prompt: str, arm: Any, **kwargs: Any) -> dict[str, Any]:
        assert arm.label != "judge"
        return answer(cost=None if arm.local else 0.01)

    summary = shadow.execute([claim()], options(), lambda r: None, dispatch_fn=agreeing)
    assert summary["judge_n"] == 0 and summary["agreement"] == 1


def test_dtree_requires_verbatim_quote_and_valid_contract(shadow: Any) -> None:
    assert shadow.decision(claim(), "dtree", answer())["valid"]
    bad = answer()
    bad["data"]["quote"] = "Made up source text"
    assert not shadow.decision(claim(), "dtree", bad)["valid"]
    bad = answer()
    bad["stop_reason"] = "length"
    assert not shadow.decision(claim(), "dtree", bad)["valid"]


def test_triage_gates_compare_proposal_before_downgrade(shadow: Any) -> None:
    row = triage_sample()
    payload = {
        "id": "gr1",
        "verdict": "noise",
        "fix": "code",
        "size": "S",
        "severity": 1,
        "cluster": "noise",
        "reason": "Command dump",
        "confidence": "high",
    }
    result = {"data": {"verdicts": [payload]}}
    score = shadow.decision(row, "triage", result)
    assert score["valid"] and score["screen"] and score["decision"] == "noise"
    row["gripe"]["filed_by_owner"] = True
    assert not shadow.decision(row, "triage", result)["screen"]
    row["gripe"]["filed_by_owner"] = False
    row["gripe"]["title"] = "Search returns wrong results"
    score = shadow.decision(row, "triage", result)
    assert score["decision"] == "noise" and not score["screen"]
    payload.update(verdict="fixed", sha="abc1234")
    assert not shadow.decision(row, "triage", result)["screen"]


def test_dispatch_pins_one_rung_and_restores_environment(
    shadow: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr("precis.budget.meter.active_store", lambda: None)
    monkeypatch.setenv("PRECIS_LLM_CHAIN_BIG", "original")
    calls = []

    def fake(req: Any) -> Any:
        calls.append(req)
        chain = json.loads(shadow.os.environ["PRECIS_LLM_CHAIN_BIG"])
        assert len(chain) == 1 and chain[0]["placement"] == "local"
        assert chain[0]["transport"] == "local"
        assert chain[0]["max_tokens"] == 4096
        return SimpleNamespace(
            text="{}",
            data={},
            cost_usd=None,
            error=None,
            paused=False,
            timed_out=False,
            model=req.model,
            placement="local",
        )

    result = shadow.dispatch(
        "fixture",
        shadow.arms_for("triage")[0],
        op="triage",
        local_url="http://localhost:12345",
        allowance=None,
        route_fn=fake,
    )
    req = calls[0]
    assert req.placement == "local" and req.model == shadow.LOCAL_MODEL
    assert (
        req.log_call is False and req.log_blobs is False and req.tools_needed is False
    )
    assert "local_url" not in result and "http" not in json.dumps(result)
    assert shadow.os.environ["PRECIS_LLM_CHAIN_BIG"] == "original"

    def fail(req: Any) -> Any:
        raise RuntimeError("fixture")

    with pytest.raises(RuntimeError):
        shadow.dispatch(
            "fixture",
            shadow.arms_for("triage")[0],
            op="triage",
            local_url="http://localhost:12345",
            allowance=None,
            route_fn=fail,
        )
    assert shadow.os.environ["PRECIS_LLM_CHAIN_BIG"] == "original"


def test_dispatch_refuses_bound_store(shadow: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr("precis.budget.meter.active_store", lambda: object())
    with pytest.raises(ValueError, match="bound store"):
        shadow.dispatch(
            "fixture",
            shadow.arms_for("triage")[0],
            op="triage",
            local_url="http://localhost:12345",
            allowance=None,
            route_fn=lambda req: pytest.fail("dispatch"),
        )


def test_table_requires_real_expander_before_any_dispatch(
    shadow: Any, monkeypatch: Any
) -> None:
    monkeypatch.setitem(sys.modules, "precis.extract.recipes", None)
    with pytest.raises(ValueError, match="real extract.recipes.expand_recipe"):
        shadow.preflight([], "table_recipe")


def test_wilson_uses_real_denominator_and_no_data_is_unknown(shadow: Any) -> None:
    assert shadow.wilson(0, 0) is None
    low, high = shadow.wilson(36, 40)
    assert 0.7 < low < 0.9 < high < 1


def test_two_invalid_triage_answers_never_agree_on_missing_fields(shadow: Any) -> None:
    invalid = {"valid": False, "decision": None}
    comparison = shadow.compare(invalid, invalid, "triage")
    assert not comparison["agree"]
    assert not any(comparison["fields"].values())


@pytest.mark.parametrize(("field", "value"), [("verdict", []), ("confidence", {})])
def test_unhashable_dtree_fields_record_invalid_pair(
    shadow: Any, field: str, value: Any
) -> None:
    records: list[dict[str, Any]] = []

    def fake(prompt: str, arm: Any, **kwargs: Any) -> dict[str, Any]:
        result = answer(cost=None if arm.local else 0.01)
        result["data"][field] = value
        return result

    summary = shadow.execute(
        [claim()], options(judge=0), records.append, dispatch_fn=fake
    )
    assert summary["local_valid"] == 0 and summary["agreement"] == 0
    assert [r["kind"] for r in records] == ["pair", "summary"]


def test_scoring_exception_preserves_raw_pair_and_summary(
    shadow: Any, monkeypatch: Any
) -> None:
    records: list[dict[str, Any]] = []

    def broken(*args: Any) -> Any:
        raise TypeError("malformed model payload")

    monkeypatch.setattr(shadow, "decision", broken)

    def fake(prompt: str, arm: Any, **kwargs: Any) -> dict[str, Any]:
        return answer(cost=None if arm.local else 0.01)

    shadow.execute([claim()], options(judge=0), records.append, dispatch_fn=fake)
    assert records[0]["arms"]["C"]["data"]["verdict"] == "supported"
    assert records[0]["scores"]["C"]["reason"] == "scorer:TypeError"
    assert records[-1]["kind"] == "summary"


@pytest.mark.parametrize(
    ("winner", "reason"),
    [([], "reason"), ({}, "reason"), ("A", ""), ("unknown", "reason")],
)
def test_malformed_judge_is_excluded_from_valid_denominator(
    shadow: Any, winner: Any, reason: Any
) -> None:
    records: list[dict[str, Any]] = []

    def fake(prompt: str, arm: Any, **kwargs: Any) -> dict[str, Any]:
        if arm.label == "judge":
            return {"cost_usd": 0.01, "data": {"winner": winner, "reason": reason}}
        return answer(
            "supported" if arm.local else "contradicted",
            cost=None if arm.local else 0.01,
        )

    summary = shadow.execute([claim()], options(), records.append, dispatch_fn=fake)
    assert summary["judge_attempts"] == 1
    assert summary["judge_n"] == 0 and summary["judge_local_wrong"] == 0
    assert records[-2]["kind"] == "judge" and records[-2]["valid"] is False


def test_duplicate_triage_verdicts_cannot_create_false_screen_agreement(
    shadow: Any,
) -> None:
    base = {
        "id": "gr1",
        "fix": "code",
        "size": "S",
        "severity": 1,
        "cluster": "noise",
        "reason": "Command dump",
        "confidence": "high",
    }
    data = {
        "verdicts": [base | {"verdict": "noise"}, base | {"id": 1, "verdict": "bug"}]
    }
    score = shadow.decision(triage_sample(), "triage", {"data": data})
    assert score["valid"] is False
    assert score["decision"] is None and "screen" not in score
