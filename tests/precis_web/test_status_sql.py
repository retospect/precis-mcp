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

from precis_web.routes.status import _backlog_counts, _recent_passes


def _log_runner_batch(
    store: Any, *, handler: str, ok: int, claimed: int, host: str = "melchior"
) -> None:
    """Insert one ``pass='runner'`` worker_logs row, as the runner emits."""
    with store.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO worker_logs (host, pass, level, message, payload)
            VALUES (%s, 'runner', 'INFO', 'worker: batch',
                    jsonb_build_object('handler', %s::text, 'claimed', %s::int,
                                       'ok', %s::int, 'failed', 0))
            """,
            (host, handler, claimed, ok),
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
