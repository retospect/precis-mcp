"""Tests for ``precis.workers.runner``.

Exercises the claim → process → write loop against real Postgres,
plus the round-robin :func:`run_loop` driver. A small in-process
"counter" handler captures iteration semantics without depending
on the real embedder.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from precis.workers.base import ChunkRow
from precis.workers.embed import EmbedHandler
from precis.workers.runner import (
    BatchResult,
    run_handler_once,
    run_loop,
)
from precis.workers.summarize import RakeLemmaHandler
from tests.workers._helpers import make_mock_bge_m3, seed_chunks

# ---------------------------------------------------------------------------
# A controllable test handler — counts process / write calls.
# ---------------------------------------------------------------------------


class _CountingEmbedHandler(EmbedHandler):
    """EmbedHandler with hooks: count calls, optionally fail Nth chunk.

    EmbedHandler's default ``process_batch`` runs one batched
    transformer pass that bypasses ``process()``. To exercise the
    per-row success/failure routing in the runner we override
    ``process_batch`` here to dispatch per-row through ``process``
    instead — the same fallback shape the base handler uses when
    the bulk path raises.
    """

    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        super().__init__(make_mock_bge_m3())
        self.processed: list[int] = []
        self.failures: list[int] = []
        self._fail_on = fail_on or set()

    def process(self, row: ChunkRow) -> list[float]:
        self.processed.append(row.chunk_id)
        if row.chunk_id in self._fail_on:
            self.failures.append(row.chunk_id)
            raise RuntimeError(f"forced failure on chunk {row.chunk_id}")
        return super().process(row)

    def process_batch(self, rows: list[ChunkRow]) -> list[object]:
        # Per-row dispatch so the test handler's `process` override
        # observes every claimed chunk (the bulk embed path would
        # never call it).
        out: list[object] = []
        for row in rows:
            try:
                out.append(self.process(row))
            except Exception as exc:
                out.append(exc)
        return out


# ---------------------------------------------------------------------------
# run_handler_once — happy path
# ---------------------------------------------------------------------------


class TestRunHandlerOnce:
    def test_empty_db_returns_zero_claimed(self, store):
        h = EmbedHandler(make_mock_bge_m3())
        result = run_handler_once(h, store, batch_size=10)
        assert result == BatchResult(handler="embed:bge-m3", claimed=0, ok=0, failed=0)

    def test_drains_one_batch(self, store):
        _ref_id, chunk_ids = seed_chunks(store, ["alpha", "beta", "gamma"])
        h = EmbedHandler(make_mock_bge_m3())

        result = run_handler_once(h, store, batch_size=10)
        assert result == BatchResult(handler="embed:bge-m3", claimed=3, ok=3, failed=0)
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM chunk_embeddings WHERE status = 'ok'"
            ).fetchone()
        assert n == (3,)

        # Re-running drains nothing — all chunks have rows already.
        result2 = run_handler_once(h, store, batch_size=10)
        assert result2.claimed == 0
        # Same chunks are still embedded; row count unchanged.
        with store.pool.connection() as conn:
            same = conn.execute("SELECT count(*) FROM chunk_embeddings").fetchone()
        assert same == (3,)
        # Defensive: chunk_ids actually used in the assertion above
        assert len(chunk_ids) == 3

    def test_batch_size_limits_claim(self, store):
        seed_chunks(store, ["a", "b", "c", "d", "e"])
        h = EmbedHandler(make_mock_bge_m3())
        result = run_handler_once(h, store, batch_size=2)
        assert result.claimed == 2
        assert result.ok == 2

    def test_failed_process_writes_marker_continues(self, store):
        _ref_id, chunk_ids = seed_chunks(store, ["a", "b", "c"])
        h = _CountingEmbedHandler(fail_on={chunk_ids[1]})
        result = run_handler_once(h, store, batch_size=10)
        assert result == BatchResult(handler="embed:bge-m3", claimed=3, ok=2, failed=1)
        # All three chunks were attempted; the middle one failed.
        assert h.processed == chunk_ids
        assert h.failures == [chunk_ids[1]]

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, status FROM chunk_embeddings ORDER BY chunk_id"
            ).fetchall()
        assert rows == [
            (chunk_ids[0], "ok"),
            (chunk_ids[1], "failed"),
            (chunk_ids[2], "ok"),
        ]

    def test_failed_chunk_not_re_claimed(self, store):
        _ref_id, [chunk_id] = seed_chunks(store, ["a"])
        h = _CountingEmbedHandler(fail_on={chunk_id})
        run_handler_once(h, store, batch_size=10)
        assert h.processed == [chunk_id]

        # Second pass: the failure marker keeps the chunk out of
        # the LEFT JOIN result. ``processed`` should not grow.
        run_handler_once(h, store, batch_size=10)
        assert h.processed == [chunk_id]

    def test_invalid_batch_size_raises(self, store):
        h = EmbedHandler(make_mock_bge_m3())
        with pytest.raises(ValueError):
            run_handler_once(h, store, batch_size=0)


# ---------------------------------------------------------------------------
# run_loop — multiple handlers, once-pass, stop signal
# ---------------------------------------------------------------------------


class TestRunLoop:
    def test_once_drains_then_returns(self, store):
        seed_chunks(store, ["alpha beta", "gamma delta"])

        embed = EmbedHandler(make_mock_bge_m3())
        summ = RakeLemmaHandler(max_keywords=3)

        run_loop([embed, summ], store, once=True, batch_size=10)

        with store.pool.connection() as conn:
            n_emb = conn.execute(
                "SELECT count(*) FROM chunk_embeddings WHERE status = 'ok'"
            ).fetchone()
            n_sum = conn.execute(
                "SELECT count(*) FROM chunk_summaries WHERE status = 'ok'"
            ).fetchone()
        assert n_emb == (2,)
        assert n_sum == (2,)

    def test_once_with_no_handlers_is_noop(self, store):
        # No-op smoke: must not crash, must not loop.
        run_loop([], store, once=True)

    def test_once_handles_more_than_one_batch_pass(self, store):
        # 5 chunks, batch_size=2, once=True -> only the first 2 are
        # processed (one pass per handler).
        _ref_id, chunk_ids = seed_chunks(store, [f"phrase {i}" for i in range(5)])
        h = EmbedHandler(make_mock_bge_m3())
        run_loop([h], store, once=True, batch_size=2)

        with store.pool.connection() as conn:
            n = conn.execute("SELECT count(*) FROM chunk_embeddings").fetchone()
        assert n == (2,)
        # Defensive — chunk_ids referenced
        assert len(chunk_ids) == 5

    def test_should_stop_short_circuits(self, store):
        seed_chunks(store, ["a", "b", "c"])
        h = EmbedHandler(make_mock_bge_m3())
        # Stop is requested *immediately*; loop must return without
        # processing anything.
        run_loop(
            [h],
            store,
            once=False,
            batch_size=2,
            should_stop=lambda: True,
        )
        with store.pool.connection() as conn:
            n = conn.execute("SELECT count(*) FROM chunk_embeddings").fetchone()
        assert n == (0,)

    def test_continuous_mode_drains_then_stops_on_signal(self, store):
        # 4 chunks, batch_size=2 -> needs two passes to drain.
        # We let the loop run continuously and trip the stop flag
        # once 4 chunks have been embedded.
        seed_chunks(store, ["a", "b", "c", "d"])

        @dataclass
        class _Flag:
            stop: bool = False

        flag = _Flag()
        h = EmbedHandler(make_mock_bge_m3())

        def stop_after_drain() -> bool:
            with store.pool.connection() as conn:
                n = conn.execute(
                    "SELECT count(*) FROM chunk_embeddings WHERE status = 'ok'"
                ).fetchone()
            if n is not None and n[0] >= 4:
                flag.stop = True
            return flag.stop

        # idle_seconds=0 so once the queue drains we don't sleep.
        # The loop should return immediately when stop_after_drain
        # flips on to True.
        thread = threading.Thread(
            target=run_loop,
            args=([h], store),
            kwargs=dict(
                once=False,
                batch_size=2,
                idle_seconds=0.05,
                should_stop=stop_after_drain,
            ),
        )
        thread.start()
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "run_loop did not stop within 10s"

        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM chunk_embeddings WHERE status = 'ok'"
            ).fetchone()
        assert n == (4,)
