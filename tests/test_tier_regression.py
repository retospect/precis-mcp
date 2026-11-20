"""The weekly tier-ladder regression (gr595546).

``run_tier_regression`` enumerates every rung of every tier's resolved chain
and scores each offering over the packaged gold set; ``run_tier_regression_pass``
records the doc in ``app_settings``; the ``llm-tier-regression`` condition probe
fires a per-offering finding when a pass rate drops week-over-week. Tests inject
a scripted ``ladder_rungs`` + ``dispatch_for`` — no real model ever runs.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from precis.budget import settings as app_settings
from precis.llm_eval import regression
from precis.llm_eval.tasks import GoldTask, load_gold_set
from precis.utils.llm.router import Rung, Tier, Transport
from precis.workers import conditions, tier_regression
from precis.workers.scheduler import CADENCES

# ── packaged set ─────────────────────────────────────────────────


def test_packaged_set_loads_and_every_scorer_is_wired() -> None:
    from precis.llm_eval.scorers import SCORERS

    tasks = load_gold_set(regression.regression_set_path())
    assert 25 <= len(tasks) <= 45, "the weekly set should stay ~30 tasks"
    assert all(t.scorer in SCORERS for t in tasks)
    assert len({t.axis for t in tasks}) >= 3
    # td663780: no scorer that executes model-generated code — score_code
    # runs the reply's Python in a bare subprocess (no OS sandbox), fine
    # for attended `precis llm eval`, never for an unattended cadence.
    assert not {t.scorer for t in tasks} & regression._BLOCKED_UNATTENDED


# ── scripted ladder + dispatch ────────────────────────────────────


def _task(task_id: str, needle: str) -> GoldTask:
    return GoldTask(
        task_id=task_id,
        axis="long-context-recall",
        scorer="needle",
        prompt=f"say {needle}",
        expect={"needle": needle},
    )


_TASKS = [
    _task("t1", "AB-1"),
    _task("t2", "CD-2"),
    _task("t3", "EF-3"),
    _task("t4", "GH-4"),
]


def _ladder() -> list[tuple[Tier, Rung]]:
    return [
        (Tier.SMALL, Rung(transport=Transport.LOCAL, model="m-local", label="local")),
        (
            Tier.FRONTIER,
            Rung(transport=Transport.CLAUDE_P, model="m-cloud", label="cloud"),
        ),
    ]


def _scripted_dispatch(answers: dict[tuple[str, str], Any]) -> Any:
    """``dispatch_for``: per-rung dispatch_fn keyed by (tier.value, rung.label)."""

    def dispatch_for(tier: Tier, rung: Rung) -> Any:
        outcome = answers[(tier.value, rung.label or rung.transport.value)]

        def dispatch(req: Any) -> Any:
            out = outcome(req) if callable(outcome) else outcome
            if isinstance(out, Exception) or out is None:
                return SimpleNamespace(text="", data=None, error="provider 500")
            return SimpleNamespace(text=out, data=None, error=None)

        return dispatch

    return dispatch_for


def _use_ladder(
    monkeypatch: Any, ladder: list[tuple[Tier, Rung]] | None = None
) -> None:
    monkeypatch.setattr(
        regression, "ladder_rungs", lambda *a, **kw: ladder or _ladder()
    )


# ── run_tier_regression ───────────────────────────────────────────


def test_run_scores_every_offering_and_rates(store: Any, monkeypatch: Any) -> None:
    _use_ladder(monkeypatch)
    dispatch_for = _scripted_dispatch(
        {
            ("small", "local"): "AB-1",  # 1/4 pass → 25%
            ("frontier", "cloud"): "AB-1 CD-2 EF-3 GH-4",  # 4/4 → 100%
        }
    )
    doc = regression.run_tier_regression(
        store, tasks=_TASKS, dispatch_for=dispatch_for, pass_min=0.5
    )
    offerings = doc["offerings"]
    assert set(offerings) == {"small/local", "frontier/cloud"}
    local, cloud = offerings["small/local"], offerings["frontier/cloud"]
    assert (local["scored"], local["passed"], local["rate"]) == (4, 1, 0.25)
    assert local["placement"] == "local" and local["model"] == "m-local"
    assert (cloud["scored"], cloud["passed"], cloud["rate"]) == (4, 4, 1.0)
    assert cloud["placement"] == "cloud" and cloud["transport"] == "claude_p"
    assert doc["n_tasks"] == 4


def test_all_errors_run_is_void_not_zero(store: Any, monkeypatch: Any) -> None:
    """A run where every task errored records ``rate=None`` — a dark
    offering is a void, not a quality score of 0."""
    _use_ladder(monkeypatch)
    dispatch_for = _scripted_dispatch(
        {("small", "local"): None, ("frontier", "cloud"): "AB-1"}
    )
    doc = regression.run_tier_regression(store, tasks=_TASKS, dispatch_for=dispatch_for)
    local = doc["offerings"]["small/local"]
    assert local["rate"] is None and local["errors"] == 4 and local["scored"] == 0
    assert doc["offerings"]["frontier/cloud"]["rate"] == 0.25


def test_partial_errors_count_against_the_rate(store: Any, monkeypatch: Any) -> None:
    """td663780: errored tasks stay in the denominator — 1 pass + 3 errors
    is a 25% run, not a 100% run over a shrunken denominator."""
    _use_ladder(monkeypatch)
    dispatch_for = _scripted_dispatch(
        {
            ("small", "local"): lambda req: "AB-1" if "AB-1" in req.prompt else None,
            ("frontier", "cloud"): "AB-1 CD-2",
        }
    )
    doc = regression.run_tier_regression(store, tasks=_TASKS, dispatch_for=dispatch_for)
    local = doc["offerings"]["small/local"]
    assert (local["attempted"], local["scored"], local["errors"]) == (4, 1, 3)
    assert local["passed"] == 1 and local["rate"] == 0.25
    assert doc["offerings"]["frontier/cloud"]["rate"] == 0.5


def test_blocked_scorer_tasks_are_skipped_not_executed(
    store: Any, monkeypatch: Any
) -> None:
    """td663780: a ``code``-scorer task never reaches dispatch on the
    unattended path — it lands in ``skipped`` with a reason instead."""
    _use_ladder(monkeypatch)
    code_task = GoldTask(
        task_id="c1",
        axis="code",
        scorer="code",
        prompt="write f",
        expect={"test": "assert f() == 1"},
    )
    calls: list[Any] = []
    inner = _scripted_dispatch(
        {("small", "local"): "AB-1", ("frontier", "cloud"): "AB-1"}
    )

    def dispatch_for(tier: Tier, rung: Rung) -> Any:
        disp = inner(tier, rung)

        def dispatch(req: Any) -> Any:
            calls.append(req.prompt)
            return disp(req)

        return dispatch

    doc = regression.run_tier_regression(
        store, tasks=[*_TASKS, code_task], dispatch_for=dispatch_for
    )
    assert doc["n_tasks"] == 4
    assert any("c1" in s and "blocked" in s for s in doc["skipped"])
    assert "write f" not in calls


def test_duplicate_rung_labels_get_distinct_keys(store: Any, monkeypatch: Any) -> None:
    """Two same-labelled rungs in one chain both get recorded."""
    _use_ladder(
        monkeypatch,
        [
            (Tier.BIG, Rung(transport=Transport.OPENAI_COMPAT, model="a", label="oss")),
            (Tier.BIG, Rung(transport=Transport.OPENAI_COMPAT, model="b", label="oss")),
        ],
    )
    dispatch_for = _scripted_dispatch({("big", "oss"): "AB-1"})
    doc = regression.run_tier_regression(store, tasks=_TASKS, dispatch_for=dispatch_for)
    assert set(doc["offerings"]) == {"big/oss", "big/oss#2"}


# ── worker pass: record + history ────────────────────────────────


def _recorded(store: Any) -> dict[str, Any]:
    raw = app_settings.get_setting(store, tier_regression.LAST_SCORE_KEY)
    assert raw is not None
    return json.loads(raw)


def test_pass_records_doc_and_appends_history(store: Any, monkeypatch: Any) -> None:
    _use_ladder(monkeypatch)
    good = _scripted_dispatch(
        {("small", "local"): "AB-1 CD-2", ("frontier", "cloud"): "AB-1"}
    )
    res = tier_regression.run_tier_regression_pass(
        store, tasks=_TASKS, dispatch_for=good
    )
    assert res.handler == "llm_tier_regression"
    assert res.claimed == 8 and res.ok == 2 + 1 and res.failed == 5
    doc = _recorded(store)
    assert doc["history"] == []  # first run: no prior doc to carry forward

    worse = _scripted_dispatch(
        {("small", "local"): "nope", ("frontier", "cloud"): "AB-1"}
    )
    tier_regression.run_tier_regression_pass(store, tasks=_TASKS, dispatch_for=worse)
    doc = _recorded(store)
    assert len(doc["history"]) == 1
    assert doc["history"][0]["offerings"]["small/local"]["rate"] == 0.5
    assert doc["offerings"]["small/local"]["rate"] == 0.0


def test_pass_tolerates_corrupt_prior_doc(store: Any, monkeypatch: Any) -> None:
    _use_ladder(monkeypatch)
    app_settings.set_setting(store, tier_regression.LAST_SCORE_KEY, "{not json")
    res = tier_regression.run_tier_regression_pass(
        store,
        tasks=_TASKS,
        dispatch_for=_scripted_dispatch(
            {("small", "local"): "AB-1", ("frontier", "cloud"): "AB-1"}
        ),
    )
    assert res.ok == 2
    assert _recorded(store)["history"] == []


# ── condition probe ───────────────────────────────────────────────


def _seed_doc(store: Any, *, prev: dict[str, Any], cur: dict[str, Any]) -> None:
    doc = {
        "computed_at": "2026-11-13T00:00:00+00:00",
        "offerings": cur,
        "history": [{"computed_at": "2026-11-06T00:00:00+00:00", "offerings": prev}],
    }
    app_settings.set_setting(store, tier_regression.LAST_SCORE_KEY, json.dumps(doc))


def _offering(rate: float | None, **kw: Any) -> dict[str, Any]:
    entry = {
        "tier": "small",
        "rung": "local",
        "model": "m",
        "transport": "local",
        "placement": "local",
        "scored": 4,
        "errors": 0,
        "passed": int((rate or 0) * 4),
        "rate": rate,
    }
    entry.update(kw)
    return entry


def test_probe_green_without_baseline_or_drop(store: Any) -> None:
    probe = conditions._probe_llm_tier_regression
    assert probe(store) == []  # no row at all
    _seed_doc(
        store,
        prev={"small/local": _offering(0.9)},
        cur={"small/local": _offering(0.87)},
    )
    assert probe(store) == []  # 3-point drop is under the 5-point floor


def test_probe_fires_on_drop_past_floor(store: Any) -> None:
    _seed_doc(
        store,
        prev={"small/local": _offering(0.9)},
        cur={"small/local": _offering(0.7)},
    )
    findings = conditions._probe_llm_tier_regression(store)
    assert [f.key for f in findings] == ["llm-tier-regression:small/local"]
    assert "90%" in findings[0].detail and "70%" in findings[0].detail


def test_probe_fires_on_partial_error_drop(store: Any) -> None:
    """td663780: the review's case — 36/36 passes becoming 1 pass plus
    35 errors is a 2.8% run, a finding, not a silent 100%."""
    _seed_doc(
        store,
        prev={"small/local": _offering(1.0)},
        cur={
            "small/local": _offering(
                1 / 36, scored=1, errors=35, attempted=36, passed=1
            )
        },
    )
    findings = conditions._probe_llm_tier_regression(store)
    assert [f.key for f in findings] == ["llm-tier-regression:small/local"]
    assert "35 errored" in findings[0].detail


def test_probe_went_dark_and_quiet_cases(store: Any) -> None:
    _seed_doc(
        store,
        prev={
            "small/local": _offering(0.9),
            "frontier/cloud": _offering(None, errors=4, scored=0),
        },
        cur={
            "small/local": _offering(None, errors=4, scored=0),
            "frontier/cloud": _offering(None, errors=4, scored=0),
        },
    )
    findings = conditions._probe_llm_tier_regression(store)
    assert [f.key for f in findings] == ["llm-tier-regression:small/local"]
    assert "went dark" in findings[0].detail


def test_probe_recovery_and_new_rung_stay_quiet(store: Any) -> None:
    _seed_doc(
        store,
        prev={"small/local": _offering(None, errors=4, scored=0)},
        cur={
            "small/local": _offering(0.9),
            "frontier/cloud": _offering(0.5, tier="frontier", rung="cloud"),
        },
    )
    assert conditions._probe_llm_tier_regression(store) == []


def test_probe_end_to_end_on_pass_output(store: Any, monkeypatch: Any) -> None:
    """Two real recorded passes: the second's drop is a finding."""
    _use_ladder(monkeypatch)
    tier_regression.run_tier_regression_pass(
        store,
        tasks=_TASKS,
        dispatch_for=_scripted_dispatch(
            {("small", "local"): "AB-1 CD-2 EF-3 GH-4", ("frontier", "cloud"): "AB-1"}
        ),
    )
    assert conditions._probe_llm_tier_regression(store) == []  # no baseline yet
    tier_regression.run_tier_regression_pass(
        store,
        tasks=_TASKS,
        dispatch_for=_scripted_dispatch(
            {("small", "local"): "nope", ("frontier", "cloud"): "AB-1"}
        ),
    )
    findings = conditions._probe_llm_tier_regression(store)
    assert [f.key for f in findings] == ["llm-tier-regression:small/local"]
    tier_regression.run_tier_regression_pass(
        store,
        tasks=_TASKS,
        dispatch_for=_scripted_dispatch(
            {("small", "local"): "AB-1 CD-2 EF-3 GH-4", ("frontier", "cloud"): "AB-1"}
        ),
    )
    assert conditions._probe_llm_tier_regression(store) == []  # recovered


def test_probe_is_registered() -> None:
    names = {c.name for c in conditions.CONDITIONS}
    assert "llm-tier-regression" in names


# ── scheduler wiring ──────────────────────────────────────────────


def test_cadence_registered_weekly_and_host_agnostic() -> None:
    cad = {c.name: c for c in CADENCES}["llm_tier_regression"]
    assert 7 * 24 * 3600 < cad.interval_s < 8 * 24 * 3600
    assert cad.host_affinity is None and cad.service is None
    assert cad.eligible is not None and cad.spends is True


def test_cadence_eligible_gates_on_the_required_transports(
    monkeypatch: Any,
) -> None:
    """td663780: the gate is per-rung dispatchability, not the agent lane.

    A container-only host passes ``_claude_agent_lane`` but cannot run a
    ``claude_p`` completion rung (the container rescues ``claude_agent``
    only) — it must not consume the weekly fire. A ``claude_agent`` rung
    on the same host IS eligible (the rescue applies), as is a claude-
    binary host without a reachable ``served_by`` slot for its local
    rung.
    """
    from precis.utils.llm import local_serving
    from precis.workers import capability_probe, scheduler
    from precis.workers.executors import agent_container

    # The verdict is memoized for the 5s tick; this test flips inputs
    # between calls, so recompute every time.
    monkeypatch.setattr(scheduler, "_LLM_TIER_REGRESSION_GATE_TTL_S", 0.0)
    monkeypatch.setattr(local_serving, "served_locally", lambda model: True)
    monkeypatch.setattr(agent_container, "container_agent_enabled", lambda: True)
    monkeypatch.setattr(
        agent_container, "container_capability_ok", lambda *a, **k: True
    )
    monkeypatch.setattr(capability_probe, "claude_auth_present", lambda: True)

    # Container-only host, ladder carries a claude_p rung → ineligible.
    monkeypatch.setattr(capability_probe, "claude_bin_present", lambda: False)
    _use_ladder(monkeypatch)
    assert scheduler._llm_tier_regression_eligible() is False

    # Same host, but the cloud rung is claude_agent → container rescue
    # carries it → eligible.
    _use_ladder(
        monkeypatch,
        [
            (
                Tier.SMALL,
                Rung(transport=Transport.LOCAL, model="m-local", label="local"),
            ),
            (
                Tier.FRONTIER,
                Rung(transport=Transport.CLAUDE_AGENT, model="m-cloud", label="cloud"),
            ),
        ],
    )
    assert scheduler._llm_tier_regression_eligible() is True

    # claude binary + credential on the host → the claude_p rung is fine
    # too → eligible.
    _use_ladder(monkeypatch)
    monkeypatch.setattr(capability_probe, "claude_bin_present", lambda: True)
    assert scheduler._llm_tier_regression_eligible() is True

    # …unless the local rung's model has no reachable served_by slot.
    monkeypatch.setattr(local_serving, "served_locally", lambda model: False)
    assert scheduler._llm_tier_regression_eligible() is False


def test_cadence_wrapper_runs_the_pass(store: Any, monkeypatch: Any) -> None:
    _use_ladder(monkeypatch)
    monkeypatch.setattr(
        tier_regression,
        "run_tier_regression_pass",
        lambda store, **kw: tier_regression.BatchResult(
            handler="llm_tier_regression", claimed=1, ok=1, failed=0
        ),
    )
    cad = {c.name: c for c in CADENCES}["llm_tier_regression"]
    cad.run(store, 7)  # must not raise


# ── alert registry ────────────────────────────────────────────────


def test_finding_keys_match_a_registered_rule() -> None:
    from precis.alert_ids import RULES_BY_ID

    assert "watchdog:condition/llm-tier-regression:{tier}/{rung}" in RULES_BY_ID
