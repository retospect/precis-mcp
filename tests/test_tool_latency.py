"""Durable tool-call latency from the ``tool_calls`` ledger (gr479849):
the shared :func:`precis.health_checks.tool_latency` rollup, its
precis-status table and the two ``health_digest`` checks."""

from __future__ import annotations

from typing import Any

from precis import health_checks
from precis.workers.health_digest import _tool_latency_checks


def _seed(
    store: Any, verb: str, kind: str | None, ms: list[int], *, age_s: int = 60
) -> None:
    with store.pool.connection() as conn:
        for m in ms:
            conn.execute(
                "INSERT INTO tool_calls (ts, verb, kind, outcome, latency_ms) "
                "VALUES (now() - make_interval(secs => %s), %s, %s, 'ok', %s)",
                (age_s, verb, kind, m),
            )
        conn.commit()


def _checks(store: Any) -> dict[str, Any]:
    with store.pool.connection() as conn:
        return {c.name: c for c in _tool_latency_checks(conn)}


def test_budget_override_beats_default() -> None:
    assert health_checks.tool_latency_budget_ms("get", "gripe") == (
        health_checks.TOOL_LATENCY_BUDGET_MS
    )
    assert (
        health_checks.tool_latency_budget_ms("get", "perplexity-research")
        == (health_checks.TOOL_LATENCY_BUDGET_OVERRIDES_MS["perplexity-research"])
    )


def test_draft_writes_have_a_pair_budget() -> None:
    assert health_checks.tool_latency_budget_ms("edit", "draft") == 400_000
    assert health_checks.tool_latency_budget_ms("put", "draft") == 400_000
    assert health_checks.tool_latency_budget_ms("get", "draft") == (
        health_checks.TOOL_LATENCY_BUDGET_MS
    )


def test_rollup_reads_window_and_baseline(store) -> None:
    _seed(store, "get", "paper", [10] * 19 + [1000])
    _seed(store, "search", None, [5])
    _seed(store, "get", "paper", [100] * 10, age_s=7200)  # baseline only
    with store.pool.connection() as conn:
        rows = health_checks.tool_latency(conn, baseline_s=86400)
        plain = health_checks.tool_latency(conn)
    by = {(t.verb, t.kind): t for t in rows}
    paper = by[("get", "paper")]
    assert (paper.calls, paper.p50_ms, paper.second_slowest_ms) == (20, 10, 10)
    assert paper.p95_ms > 10 and paper.baseline_calls == 10
    assert paper.baseline_p95_ms == 100
    assert by[("search", "-")].calls == 1
    assert rows[0].verb == "get"  # slowest p95 first
    assert {(t.verb, t.kind) for t in plain} == set(by)
    assert all(t.baseline_calls == 0 for t in plain)


def test_slow_verb_over_budget_turns_check_red(store) -> None:
    budget = health_checks.TOOL_LATENCY_BUDGET_MS
    _seed(store, "search", "memory", [budget * 2] * 12)
    c = _checks(store)["tool_latency"]
    assert c.status == "stale" and c.is_finding
    assert "search/memory" in c.detail and "budget" in c.detail


def test_one_cold_call_does_not_turn_check_red(store) -> None:
    """r37's first eval: 11 fast get/skill calls plus one 36 s precis-status
    just after a restart read p95 16.9 s. One outlier is not a slow MCP;
    a second slow call is."""
    _seed(store, "get", "skill", [50] * 11 + [36_000])
    assert _checks(store)["tool_latency"].status == "ok"
    _seed(store, "get", "skill", [36_000])
    assert _checks(store)["tool_latency"].status == "stale"


def test_under_budget_or_too_few_calls_is_green(store) -> None:
    budget = health_checks.TOOL_LATENCY_BUDGET_MS
    _seed(store, "search", "memory", [budget // 2] * 12)
    _seed(store, "get", "skill", [budget * 3] * 3)  # under the call floor
    checks = _checks(store)
    assert checks["tool_latency"].status == "ok"
    assert checks["tool_latency_regressed"].status == "ok"


def test_p95_doubled_against_the_week_flags_regression(store) -> None:
    _seed(store, "get", "todo", [500] * 12, age_s=86400)
    _seed(store, "get", "todo", [2000] * 12)
    _seed(store, "get", "memory", [20] * 12, age_s=86400)
    _seed(store, "get", "memory", [80] * 12)  # doubled, under the 1 s floor
    c = _checks(store)["tool_latency_regressed"]
    assert c.status == "stale"
    assert "get/todo" in c.detail and "get/memory" not in c.detail


def test_status_table_is_durable_across_renders(store) -> None:
    """The rows come from the ledger, so a second render (a restarted
    process) shows the same numbers."""
    from precis.handlers import skill as skill_mod

    _seed(store, "get", "paper", [10, 20, 30, 40])
    first = skill_mod._render_tool_latency(store)
    assert first == skill_mod._render_tool_latency(store)
    out = "\n".join(first)
    assert "4 calls, 0 errors, 1 verb/kind pair(s)" in out
    assert "get/paper" in out
    assert skill_mod._render_tool_latency(None) == [
        "_stateless build — no tool_calls ledger to read_"
    ]
