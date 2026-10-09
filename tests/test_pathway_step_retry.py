"""No-compute fixtures for the step-retry policy: eligibility selection,
retry planning (fresh seed, per-step cap), partial narrowing and the
aggregate-side replacement — all pure, no store, no engine."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from precis_pathway.step_retry import (
    DEFAULT_MAX_STEP_RETRIES,
    MAX_STEP_RETRIES_ENV,
    StepRetry,
    apply_step_replacements,
    exhausted_blockers,
    max_step_retries,
    narrow_partial,
    narrow_structures,
    plan_step_retries,
    retry_content_key,
    retryable_convergence_blockers,
)


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


# ───────────────────────── planning: fresh seed, per-step cap ─────────────────


def _plan(results: dict[str, Any], **kw: Any) -> list[StepRetry]:
    kw.setdefault("events", [])
    kw.setdefault("model_index_of", {"mace:model-a": 0, None: 0})
    kw.setdefault("seeds_in_use", [0, 1, 2])
    kw.setdefault("max_retries", 2)
    return plan_step_retries(results, **kw)


def test_plan_retries_the_blocked_step_once_at_a_fresh_seed():
    record = _record(model="mace:model-a")
    results = _results(record)
    results["trust"].append(_record(id="M->S#s0#neb_convergence", step="M->S"))
    plans = _plan(results)
    assert plans == [
        StepRetry(
            step="R->M",
            model_index=0,
            model="mace:model-a",
            from_seed=0,
            to_seed=3,
            check="neb_convergence",
            record_id="R->M#s0#neb_convergence",
            attempt=1,
        )
    ]
    # the fresh seed is above every seed the pathway has used, including
    # retries already in the log
    plans = _plan(results, events=[{"step": "X", "model_index": 0, "to_seed": 7}])
    assert plans[0].to_seed == 8 and plans[0].attempt == 1


def test_plan_respects_the_per_step_cap_and_names_the_exhausted_blocker():
    results = _results(_record(model="mace:model-a"))
    spent = [
        {"step": "R->M", "model_index": 0, "to_seed": 3},
        {"step": "R->M", "model_index": 0, "to_seed": 4},
    ]
    assert _plan(results, events=spent) == []
    exhausted = exhausted_blockers(
        results, events=spent, model_index_of={"mace:model-a": 0}, max_retries=2
    )
    assert [r["id"] for r in exhausted] == ["R->M#s0#neb_convergence"]
    # one retry left -> attempt 2; a different step is unaffected by the cap
    assert _plan(results, events=spent[:1])[0].attempt == 2
    other = _results(_record(id="M->S#s1#neb_convergence", step="M->S", seed=1))
    assert _plan(other, events=spent)[0].step == "M->S"
    # cap 0 disables the policy outright
    assert _plan(results, max_retries=0) == []


def test_plan_never_retries_the_same_measurement_twice():
    """A re-run of the same aggregate sees the same record; its replacement
    is already in flight. Only a failure at the retry's OWN seed re-plans."""
    results = _results(_record(model="mace:model-a"))
    logged = [{"step": "R->M", "model_index": 0, "from_seed": 0, "to_seed": 3}]
    assert _plan(results, events=logged) == []
    assert (
        exhausted_blockers(
            results, events=logged, model_index_of={"mace:model-a": 0}, max_retries=1
        )
        == []
    )
    again = _results(
        _record(id="R->M#s3#neb_convergence", seed=3, model="mace:model-a")
    )
    (plan,) = _plan(again, events=logged)
    assert (plan.from_seed, plan.to_seed, plan.attempt) == (3, 4, 2)


def test_plan_collapses_two_failed_checks_on_one_measurement_into_one_retry():
    neb = _record(model="mace:model-a")
    relax = _record(
        id="M@R->M#s0#relax_convergence",
        state="M",
        check="relax_convergence",
        model="mace:model-a",
    )
    results = _results(neb)
    results["trust"].append(relax)
    results["trust_summary"]["barrier"]["blocked_by"].append(relax["id"])
    plans = _plan(results)
    assert len(plans) == 1 and plans[0].from_seed == 0
    # two failed seeds of the same step each get their own fresh seed,
    # spending the cap together
    two = _results(neb)
    two["trust"].append(
        _record(id="R->M#s2#neb_convergence", seed=2, model="mace:model-a")
    )
    two["trust_summary"]["barrier"]["blocked_by"].append("R->M#s2#neb_convergence")
    plans = _plan(two)
    assert [(p.from_seed, p.to_seed, p.attempt) for p in plans] == [
        (0, 3, 1),
        (2, 4, 2),
    ]
    assert _plan(two, max_retries=1)[0].from_seed == 0


def test_plan_keeps_models_apart_and_skips_an_unknown_model():
    a = _record(model="mace:model-a")
    b = {**a, "model": "mace:model-b"}
    results = _results(a)
    results["trust"].append(b)
    plans = _plan(results, model_index_of={"mace:model-a": 0, "mace:model-b": 1})
    assert [(p.model_index, p.to_seed) for p in plans] == [(0, 3), (1, 4)]
    # a record whose model never ran names no unit -> no retry, no crash
    assert _plan(results, model_index_of={"mace:model-a": 0}) == [plans[0]]


@pytest.mark.parametrize("reason", ["cost-cap", "spend-limit", "no_route"])
def test_plan_never_fires_on_budget_or_job_failures(reason: str):
    """The parked-forever regression: only trust-check verdicts retry."""
    results = _results(_record())
    results["trust_summary"]["barrier"]["blocked_by"] = [reason]
    assert _plan(results) == []
    assert _plan({"error": f"{reason}: seed job failed", "trust_schema": 2}) == []


def test_plan_withholds_everything_on_endpoint_mismatch():
    results = _results(_record(model="mace:model-a"))
    results["trust"].append(_record(evidence={"reason": "endpoint-mismatch"}))
    assert _plan(results) == []


def test_max_step_retries_env_dial(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MAX_STEP_RETRIES_ENV, raising=False)
    assert max_step_retries() == DEFAULT_MAX_STEP_RETRIES == 2
    monkeypatch.setenv(MAX_STEP_RETRIES_ENV, "0")
    assert max_step_retries() == 0
    monkeypatch.setenv(MAX_STEP_RETRIES_ENV, "-3")
    assert max_step_retries() == 0
    monkeypatch.setenv(MAX_STEP_RETRIES_ENV, "many")
    assert max_step_retries() == DEFAULT_MAX_STEP_RETRIES


def test_retry_content_key_is_distinct_per_unit():
    keys = {
        retry_content_key("base", step="R->M", seed=3, model_index=0),
        retry_content_key("base", step="R->M", seed=4, model_index=0),
        retry_content_key("base", step="M->S", seed=3, model_index=0),
        retry_content_key("base", step="R->M", seed=3, model_index=1),
        retry_content_key("other", step="R->M", seed=3, model_index=0),
    }
    assert len(keys) == 5


# ───────────────────── narrowing + aggregate-side replacement ─────────────────


def _partial(seed: int, *, fail_rm: bool = False) -> dict[str, Any]:
    def rec(step: str, check: str = "neb_convergence", **kw: Any) -> dict[str, Any]:
        return {
            "id": f"{step}#s{seed}#{check}",
            "step": step,
            "seed": seed,
            "check": check,
            "verdict": "pass",
            "severity": "fatal",
            "evidence": {},
            **kw,
        }

    return {
        "seed": seed,
        "model": "emt",
        "states": {"*": -1.0, "R": 0.0, "M": 0.5, "S": 0.9},
        "steps": {
            "R->M": {"reactant": "R", "product": "M", "barrier": 1.0, "delta_e": 0.5},
            "M->S": {"reactant": "M", "product": "S", "barrier": 0.7, "delta_e": 0.4},
        },
        "trust": [
            rec("R->M", verdict="fail" if fail_rm else "pass"),
            {
                **rec("R->M", "relax_convergence"),
                "state": "M",
                "id": f"M@R->M#s{seed}#relax_convergence",
            },
            rec("M->S"),
        ],
        "warnings": [
            f"[R->M] seed={seed} NEB not converged",
            f"S seed={seed} not converged",
        ],
        "vib": {"R": {"g": 0.1}, "S": {"g": 0.2}},
        "ads_barriers": {"S": 0.3},
        "refs": {"gas": {"H2": -6.0}},
        "shed": {},
    }


def test_narrow_partial_keeps_one_step_its_records_endpoints_and_root():
    full = _partial(3)
    before = deepcopy(full)
    narrowed = narrow_partial(full, "R->M", root="*")
    assert full == before
    assert set(narrowed["steps"]) == {"R->M"}
    assert set(narrowed["states"]) == {"*", "R", "M"}
    assert {r["id"] for r in narrowed["trust"]} == {
        "R->M#s3#neb_convergence",
        "M@R->M#s3#relax_convergence",
    }
    assert narrowed["warnings"] == ["[R->M] seed=3 NEB not converged"]
    assert narrowed["vib"] == {"R": {"g": 0.1}} and narrowed["ads_barriers"] == {}
    assert narrowed["refs"] == full["refs"] and narrowed["seed"] == 3
    # idempotent, and a step the run never produced yields no entry
    assert narrow_partial(narrowed, "R->M", root="*") == narrowed
    assert narrow_partial(full, "X->Y", root="*")["steps"] == {}
    structures = {
        "*": {"energy": -1.0, "extxyz": "a"},
        "S": {"energy": 0.9, "extxyz": "b"},
    }
    assert set(narrow_structures(structures, narrowed)) == {"*"}


def test_apply_step_replacements_swaps_the_failed_measurement_only():
    base0: dict[str, Any] = {
        "seed": 0,
        "model": "emt",
        "model_index": 0,
        "partial": _partial(0, fail_rm=True),
    }
    base1: dict[str, Any] = {
        "seed": 1,
        "model": "emt",
        "model_index": 0,
        "partial": _partial(1),
    }
    retry: dict[str, Any] = {
        "seed": 3,
        "model": "emt",
        "model_index": 0,
        "partial": _partial(3),
        "structures": {
            "*": {"energy": -1.0, "extxyz": "a"},
            "S": {"energy": 0.9, "extxyz": "b"},
        },
        "step_retry": {"step": "R->M", "from_seed": 0},
    }
    inputs = [base0, base1, retry]
    before = deepcopy(inputs)
    out = apply_step_replacements(inputs, root="*")
    assert inputs == before  # job meta is never mutated
    assert [r["seed"] for r in out] == [0, 1, 3]
    # seed 0 lost R->M (entry + its trust records), kept M->S and its energies
    assert set(out[0]["partial"]["steps"]) == {"M->S"}
    assert {r["step"] for r in out[0]["partial"]["trust"]} == {"M->S"}
    assert out[0]["partial"]["states"] == base0["partial"]["states"]
    # seed 1 untouched; the retry carries only R->M
    assert out[1]["partial"] == base1["partial"]
    assert set(out[2]["partial"]["steps"]) == {"R->M"}
    assert set(out[2]["structures"]) == {"*"}
    # no fatal R->M failure survives -> the engine's summary would unblock
    assert not any(
        r["step"] == "R->M" and r["verdict"] == "fail"
        for r in out[0]["partial"]["trust"] + out[2]["partial"]["trust"]
    )


def test_apply_step_replacements_chains_a_retry_of_a_retry():
    base: dict[str, Any] = {
        "seed": 0,
        "model": "emt",
        "model_index": 0,
        "partial": _partial(0, fail_rm=True),
    }
    first: dict[str, Any] = {
        "seed": 3,
        "model": "emt",
        "model_index": 0,
        "partial": _partial(3, fail_rm=True),
        "step_retry": {"step": "R->M", "from_seed": 0},
    }
    second: dict[str, Any] = {
        "seed": 4,
        "model": "emt",
        "model_index": 0,
        "partial": _partial(4),
        "step_retry": {"step": "R->M", "from_seed": 3},
    }
    out = apply_step_replacements([base, first, second], root="*")
    assert set(out[0]["partial"]["steps"]) == {"M->S"}
    assert out[1]["partial"]["steps"] == {} and out[1]["partial"]["trust"] == []
    assert set(out[1]["partial"]["states"]) == {"*", "R", "M"}  # root kept
    assert set(out[2]["partial"]["steps"]) == {"R->M"}
    # a retry marker on another model's seed never prunes this model
    other_model = {**first, "model_index": 1, "model": "mace"}
    out = apply_step_replacements([base, other_model], root="*")
    assert set(out[0]["partial"]["steps"]) == {"R->M", "M->S"}
