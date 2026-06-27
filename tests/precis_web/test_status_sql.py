"""Real-PG regression tests for the Status route's raw SQL.

The route-level tests in ``test_routes.py`` run against the web
``FakeStore``, which does *not* parse SQL — so they monkeypatch
``_backlog_counts`` / ``_recent_passes`` and never exercise the
queries themselves. These two passes log under ``pass='runner'``
(the runner's own logger; ``worker_logs.pass`` is the logger name,
not the handler), so the panels must recover the real pass name from
``payload->>'handler'``. That join only works against real postgres,
hence this file uses the live ``store`` fixture.

See ``docs`` / CLAUDE.md "psycopg % LIKE / fake-store gap".
"""

from __future__ import annotations

from typing import Any

from precis_web.routes.status import (
    _LIVENESS_SIGNALS,
    _background_anomalies,
    _backlog_counts,
    _liveness,
    _recent_passes,
)


def _log_runner_batch(
    store: Any,
    *,
    handler: str,
    ok: int,
    claimed: int,
    failed: int = 0,
    host: str = "melchior",
) -> None:
    """Insert one ``pass='runner'`` worker_logs row, as the runner emits."""
    with store.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO worker_logs (host, pass, level, message, payload)
            VALUES (%s, 'runner', 'INFO', 'worker: batch',
                    jsonb_build_object('handler', %s::text, 'claimed', %s::int,
                                       'ok', %s::int, 'failed', %s::int))
            """,
            (host, handler, claimed, ok, failed),
        )
        conn.commit()


def test_backlog_last_done_reads_handler_not_pass(store: Any) -> None:
    """``last_ts`` comes from ``payload->>'handler'``, not ``pass``.

    A productive ``embed:bge-m3`` batch logged under ``pass='runner'``
    must still stamp the ``embed`` backlog row's ``last_ts``. The
    ``summarize`` row has no productive batch → no ``last_ts``.
    """
    _log_runner_batch(store, handler="embed:bge-m3", ok=32, claimed=32)
    # An idle (claimed/ok = 0) embed batch must NOT count as productive.
    _log_runner_batch(store, handler="chunk_keywords", ok=0, claimed=0)

    backlog = _backlog_counts(store)

    assert backlog["embed"].get("last_ts") is not None
    # chunk_keywords only logged an idle batch → no productive timestamp.
    assert backlog["chunk_keywords"].get("last_ts") is None
    # summarize never logged at all → no timestamp.
    assert backlog["summarize"].get("last_ts") is None


def test_recent_passes_surfaces_real_handler_name(store: Any) -> None:
    """The panel shows ``embed`` / ``summarize`` — never the raw ``runner``."""
    _log_runner_batch(store, handler="embed:bge-m3", ok=32, claimed=32)
    _log_runner_batch(store, handler="summarize:rake-lemma", ok=10, claimed=10)
    # Idle batch is filtered out (claimed = 0).
    _log_runner_batch(store, handler="chunk_keywords", ok=0, claimed=0)

    passes = _recent_passes(store)

    names = {p["pass"] for p in passes}
    assert "embed" in names
    assert "summarize" in names
    assert "runner" not in names  # the logger name must never leak through
    assert "chunk_keywords" not in names  # idle batch excluded


def test_failed_passes_groups_by_handler_and_drops_schedule(store: Any) -> None:
    """The failed-passes panel reports per-handler and excludes ``schedule``.

    ``schedule`` overloads ``BatchResult.failed`` to count *skipped*
    ticks (collision-skip), not errors, so a single wedged recurring can
    log tens of thousands of "failures" that are pure noise. Real handler
    errors (``embed:bge-m3`` poison chunks) must still surface — keyed by
    the real handler name recovered from ``payload->>'handler'``, never
    the raw ``runner`` logger name.
    """
    _log_runner_batch(store, handler="schedule", ok=0, claimed=2, failed=999)
    _log_runner_batch(store, handler="embed:bge-m3", ok=0, claimed=4, failed=3)
    # A clean batch (failed=0) must not appear at all.
    _log_runner_batch(store, handler="chunk_keywords", ok=32, claimed=32, failed=0)

    fails = _background_anomalies(store)["failed_passes"]
    by_handler = {f["handler"]: f for f in fails}

    assert "schedule" not in by_handler  # skip-as-failed noise excluded
    assert "runner" not in by_handler  # logger name must never leak through
    assert "chunk_keywords" not in by_handler  # failed=0 → not a failure
    assert by_handler["embed:bge-m3"]["failed"] == 3


def test_liveness_runs_every_signal_against_real_pg(store: Any) -> None:
    """Each liveness query is valid SQL against real PG (the panel's
    point): one row per signal, in registry order, none degraded to the
    ``unknown`` sentinel (which only happens on a query exception). Only
    the scheduled-cadence signals carry the ``scheduled`` flag.
    """
    rows = _liveness(store)

    assert [r["label"] for r in rows] == [label for label, _, _ in _LIVENESS_SIGNALS]
    assert not any(r["unknown"] for r in rows)  # every query executed cleanly

    by_label = {r["label"]: r for r in rows}
    assert by_label["News ingested"]["scheduled"] is True
    assert by_label["Morning briefing"]["scheduled"] is True
    # Pipeline stages are informational — never flagged on cadence.
    assert by_label["Chunk extracted"]["scheduled"] is False


def test_liveness_scheduled_signal_clears_when_fresh(store: Any) -> None:
    """A recent ``briefing`` pass log clears the stale flag; the signal
    reads ``worker_logs.pass``, so a fresh row means "alive"."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs (host, pass, level, message) "
            "VALUES ('melchior', 'briefing', 'INFO', 'delivered')"
        )
        conn.commit()

    by_label = {r["label"]: r for r in _liveness(store)}
    assert by_label["Morning briefing"]["stale"] is False
