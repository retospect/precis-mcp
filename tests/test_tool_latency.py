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
    _seed(store, "get", "todo", [500] * 35, age_s=86400)
    _seed(store, "get", "todo", [2000] * 12)  # whole hour shifted
    _seed(store, "get", "memory", [20] * 35, age_s=86400)
    _seed(store, "get", "memory", [80] * 12)  # doubled, under the 1 s floor
    c = _checks(store)["tool_latency_regressed"]
    assert c.status == "stale"
    assert "get/todo" in c.detail and "get/memory" not in c.detail


def test_sparse_baseline_cannot_flag_regression(store) -> None:
    """gr542156's flap: put/fleet's week held only 10 calls at ~0 ms, so
    a 6 s hour read as "more than doubled". Under 30 baseline calls the
    pair is not judged, however slow the hour."""
    _seed(store, "put", "fleet", [10] * 10, age_s=86400)
    _seed(store, "put", "fleet", [6000] * 10)
    assert _checks(store)["tool_latency_regressed"].status == "ok"


def test_one_slow_call_over_a_dense_zero_baseline_is_green(store) -> None:
    """A dense but near-zero baseline still cannot be carried by one
    slowish call: the second-slowest call must clear the line too."""
    _seed(store, "put", "fleet", [10] * 40, age_s=86400)
    _seed(store, "put", "fleet", [10] * 9 + [6000])
    assert _checks(store)["tool_latency_regressed"].status == "ok"


def test_straggler_tail_over_a_normal_median_is_green(store) -> None:
    """gr646231: get/job n=22, week p95 0.5 s, two 6 s stragglers. The p95
    and second-slowest both clear the line but the median is normal."""
    _seed(store, "get", "job", [100] * 38 + [500] * 2, age_s=86400)
    _seed(store, "get", "job", [100] * 20 + [6000] * 2)
    assert _checks(store)["tool_latency_regressed"].status == "ok"


def test_regression_needs_the_absolute_margin(store) -> None:
    """Doubling a dense ~0 ms baseline to under 1 s stays green: once
    the ratio term collapses, the line is baseline + 1 s."""
    _seed(store, "get", "gripe", [10] * 40, age_s=86400)
    _seed(store, "get", "gripe", [600] * 10)
    assert _checks(store)["tool_latency_regressed"].status == "ok"


def test_tiny_hour_sample_cannot_flag_regression(store) -> None:
    """gr798548: put/memory's hour held 7 calls — p95, second-slowest and
    median all cleared the bars against a 3.6 s week, but under 10 calls
    the hour p95 is the max in disguise. The pair is not judged."""
    _seed(store, "put", "memory", [3600] * 40, age_s=86400)
    _seed(store, "put", "memory", [8000] * 6 + [19400])
    assert _checks(store)["tool_latency_regressed"].status == "ok"


def test_fast_pair_tail_noise_under_the_median_floor_is_green(store) -> None:
    """gr798548's link/todo shape: week p95 0.2 s, hour p95 2.4 s on two
    slow calls with a 0.3 s median. The median cleared the bare week p95
    but not the 1 s floor — a regression must move the typical call by a
    felt amount, not just past a near-zero baseline."""
    _seed(store, "link", "todo", [200] * 40, age_s=86400)
    _seed(store, "link", "todo", [300] * 9 + [1500, 2000, 2900])
    assert _checks(store)["tool_latency_regressed"].status == "ok"


def test_whole_distribution_shift_on_a_fast_pair_still_flags(store) -> None:
    """The median floor must not neuter the check: a fast pair whose
    typical call genuinely slowed past it (week p95 0.2 s, the hour all
    1.5 s) is a real regression."""
    _seed(store, "link", "todo", [200] * 40, age_s=86400)
    _seed(store, "link", "todo", [1500] * 12)
    c = _checks(store)["tool_latency_regressed"]
    assert c.status == "stale" and "link/todo" in c.detail


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
