"""LLM-spending scheduler cadences honour ``PRECIS_DAILY_COST_CEILING``.

The ceiling used to gate the *dispatcher* only. The scheduler's three opus
cadences (``dream_agent`` / ``structural`` / ``deep_review``) ran on their own
leases and spent straight through a tripped envelope — so the gate froze the
cheap user-facing planner lane while the expensive background lane kept
billing. Prod ran exactly that inversion for 18h from 2026-08-06 19:02 (542
"daily ceiling hit" warnings from the dispatcher, ``dream_agent`` billing
~$0.40/h throughout).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.workers import scheduler
from precis.workers.planner_guardrails import DailyBudget
from precis.workers.scheduler import Cadence, run_scheduler_pass


class _Store:
    """Wins every lease it is asked for."""

    def __init__(self, *, win: bool = True) -> None:
        self.win = win
        self.claimed: list[str] = []

    def claim_scheduler_lease(self, name: str, interval_s: int, host: str) -> bool:
        self.claimed.append(name)
        return self.win


@pytest.fixture
def ran() -> list[str]:
    return []


def _cadence(name: str, ran: list[str], *, spends: bool) -> Cadence:
    return Cadence(
        name=name,
        interval_s=60,
        run=lambda store, batch: ran.append(name),
        spends=spends,
    )


def _budget(monkeypatch: Any, spent: float, ceiling: float = 50.0) -> None:
    monkeypatch.setattr(
        "precis.workers.planner_guardrails.daily_budget",
        lambda store, **kw: DailyBudget(spent=spent, ceiling=ceiling),
        raising=True,
    )


# ── the gate ──────────────────────────────────────────────────────────────


def test_spending_cadence_skipped_over_ceiling(monkeypatch, ran):
    _budget(monkeypatch, spent=51.0)
    store = _Store()
    result = run_scheduler_pass(
        store, host="h", cadences=(_cadence("deep_review", ran, spends=True),)
    )

    assert ran == []
    # The lease WAS claimed — the skip is post-claim on purpose, so
    # ``next_fire_at`` keeps advancing and §D's cadence-staleness alarm doesn't
    # fire for all three cadences during a budget freeze.
    assert store.claimed == ["deep_review"]
    assert (result.claimed, result.ok, result.failed) == (1, 0, 0)


def test_spending_cadence_runs_under_ceiling(monkeypatch, ran):
    _budget(monkeypatch, spent=49.99)
    result = run_scheduler_pass(
        _Store(), host="h", cadences=(_cadence("deep_review", ran, spends=True),)
    )

    assert ran == ["deep_review"]
    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)


def test_non_spending_cadence_ignores_the_ceiling(monkeypatch, ran):
    """``health_digest`` is SQL and ``watch_poll`` is a network poll — a blown
    LLM budget must not stop the liveness net or external acquisition."""
    _budget(monkeypatch, spent=999.0)
    result = run_scheduler_pass(
        _Store(), host="h", cadences=(_cadence("health_digest", ran, spends=False),)
    )

    assert ran == ["health_digest"]
    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)


def test_budget_error_fails_closed(monkeypatch, ran):
    """A cost gate that errors must not wave the spend through."""

    def _boom(store: Any, **kw: Any) -> DailyBudget:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(
        "precis.workers.planner_guardrails.daily_budget", _boom, raising=True
    )
    result = run_scheduler_pass(
        _Store(), host="h", cadences=(_cadence("dream_agent", ran, spends=True),)
    )

    assert ran == []
    assert (result.claimed, result.ok, result.failed) == (1, 0, 0)


def test_budget_is_not_queried_for_non_spending_cadences(monkeypatch, ran):
    """The check costs an aggregate over 24h of ``llm_call_log`` — it must run
    once per *spending* fire, not once per cadence in the table."""
    calls: list[int] = []

    def _counted(store: Any, **kw: Any) -> DailyBudget:
        calls.append(1)
        return DailyBudget(spent=0.0, ceiling=50.0)

    monkeypatch.setattr(
        "precis.workers.planner_guardrails.daily_budget", _counted, raising=True
    )
    run_scheduler_pass(
        _Store(),
        host="h",
        cadences=(
            _cadence("cron_tick", ran, spends=False),
            _cadence("watch_poll", ran, spends=False),
            _cadence("structural", ran, spends=True),
        ),
    )

    assert len(calls) == 1


def test_undue_cadence_never_reaches_the_budget_check(monkeypatch, ran):
    """A lost lease short-circuits before the query — the common case is
    "nothing due", and that must stay free."""
    calls: list[int] = []

    def _counted(store: Any, **kw: Any) -> DailyBudget:
        calls.append(1)
        return DailyBudget(spent=0.0, ceiling=50.0)

    monkeypatch.setattr(
        "precis.workers.planner_guardrails.daily_budget", _counted, raising=True
    )
    run_scheduler_pass(
        _Store(win=False),
        host="h",
        cadences=(_cadence("dream_agent", ran, spends=True),),
    )

    assert calls == []
    assert ran == []


# ── the registry ──────────────────────────────────────────────────────────


def test_exactly_the_llm_cadences_are_marked_spending():
    """Pin the classification. A new cadence that bills an LLM and forgets
    ``spends=True`` is invisible to the ceiling — the bug this file exists for.
    """
    spending = {c.name for c in scheduler.CADENCES if c.spends}
    assert spending == {"dream_agent", "structural", "deep_review"}


def test_cron_tick_is_not_marked_spending():
    """It spawns child todos; those become dispatch candidates and are gated
    at mint by ``planner_guardrails.check_parent``. Marking it here would
    double-gate the same spend and stall the schedule pass wholesale."""
    cron = next(c for c in scheduler.CADENCES if c.name == "cron_tick")
    assert cron.spends is False
