"""No-compute fixtures for the unwired step-retry eligibility prerequisite."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from precis_pathway.step_retry import retryable_convergence_blockers


def _record(**changes: Any) -> dict[str, Any]:
    return {
        "id": "R->M#s0#neb_convergence",
        "step": "R->M",
        "seed": 0,
        "check": "neb_convergence",
        "verdict": "fail",
        "severity": "fatal",
        "evidence": {"fmax": 0.2},
        **changes,
    }


def _results(record: dict[str, Any], *, quantity: str = "barrier") -> dict[str, Any]:
    blockers: list[Any] = [record["id"]]
    if quantity == "selectivity":
        blockers = [{"fork": "M", "competitor": "S", "reasons": blockers}]
    return {
        "trust_schema": 2,
        "trust": [record],
        "trust_summary": {quantity: {"available": False, "blocked_by": blockers}},
    }


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("quantity", ["barrier", "selectivity"])
def test_selects_only_cited_failures_without_mutating_results(
    schema: int, quantity: str
):
    record = _record()
    results = _results(record, quantity=quantity)
    results["trust_schema"] = schema
    # Spectator is a real fatal failure but blocks neither required quantity.
    results["trust"].append(_record(id="M->S#s0#neb_convergence", step="M->S"))
    before = deepcopy(results)
    assert retryable_convergence_blockers(results) == [record]
    assert results == before


def test_retains_state_attempt_and_model_identity_once_per_record():
    record = _record(
        id="M@R->M#s0#relax_convergence#a2",
        state="M",
        check="relax_convergence",
        attempt=2,
        model="mace:model-a",
    )
    other_model = {**record, "model": "mace:model-b"}
    results = _results(record)
    results["trust"].append(other_model)
    results["trust_summary"]["selectivity"] = {
        "available": False,
        "blocked_by": [{"reasons": [record["id"], record["id"]]}],
    }
    assert retryable_convergence_blockers(results) == [record, other_model]


@pytest.mark.parametrize(
    "changes",
    [
        {"verdict": "pass"},
        {"verdict": "marginal"},
        {"severity": "warn"},
        {"step": None, "state": "M"},
        {"step": " "},
        {"seed": None},
        {"seed": True},
        {"seed": -1},
        {"seed": "0"},
        {"check": "future_unknown_check"},
        {"check": "detachment"},
        {"id": None},
    ],
)
def test_does_not_infer_a_retry_target(changes: dict[str, Any]):
    assert retryable_convergence_blockers(_results(_record(**changes))) == []


@pytest.mark.parametrize("schema", [None, True, "2", 0, 3])
def test_unknown_or_missing_schema_is_not_retryable(schema: Any):
    results = _results(_record())
    results["trust_schema"] = schema
    assert retryable_convergence_blockers(results) == []


@pytest.mark.parametrize("available", [True, None, 0])
def test_requires_explicit_unavailable_quantity(available: Any):
    results = _results(_record())
    results["trust_summary"]["barrier"]["available"] = available
    assert retryable_convergence_blockers(results) == []


@pytest.mark.parametrize(
    "reason", ["cost-cap", "spend-limit", "no_route", "unmeasured"]
)
def test_budget_and_nonrecord_blockers_never_select_a_step(reason: str):
    results = _results(_record())
    results["trust_summary"]["barrier"]["blocked_by"] = [reason]
    assert retryable_convergence_blockers(results) == []
    # A job's error is not a trust record, even if it names a known step.
    assert retryable_convergence_blockers({"error": reason, "step": "R->M"}) == []
    # Nor does promoting that string to a fatal check make it convergence.
    assert retryable_convergence_blockers(_results(_record(check=reason))) == []


@pytest.mark.parametrize(
    "endpoint",
    [
        _record(check="endpoint_identity"),
        _record(check="endpoint_agreement", step=None, state="M"),
        _record(evidence={"reason": "endpoint-mismatch"}),
    ],
)
def test_endpoint_failure_vetoes_even_another_cited_convergence_failure(endpoint):
    results = _results(_record())
    results["trust"].append(endpoint)
    assert retryable_convergence_blockers(results) == []


@pytest.mark.parametrize(
    "patch",
    [
        {"trust": None},
        {"trust": [None, "error"]},
        {"trust_summary": None},
        {"trust_summary": {"barrier": None}},
        {"trust_summary": {"barrier": {"available": False, "blocked_by": "bad"}}},
        {"trust_summary": {"barrier": {"available": False, "blocked_by": [{}]}}},
        {"trust_summary": {"barrier": {"available": False, "blocked_by": [None]}}},
    ],
)
def test_missing_or_malformed_provenance_is_not_a_retry(patch: dict[str, Any]):
    assert retryable_convergence_blockers({**_results(_record()), **patch}) == []
