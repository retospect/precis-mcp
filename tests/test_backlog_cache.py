"""gr573440: the corpus-wide backlog counters are parked hourly in
``app_state`` (:mod:`precis.backlog_cache`) instead of full-scanning
``chunks`` on every materializer tick, ``embed_batch`` tail and panel load.

Pins: each cached counter returns what its uncached query returns on a
seeded corpus; the cached path does not re-run the scan within the hour;
the ``discovery/embed`` check and the materializer's high-water still fire
on a backlog served from the cache.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis import backlog_cache, health_checks
from precis.store import Store
from precis.workers import embed, health_digest
from precis.workers import materialize as m
from precis.workers.classify import unclassified_chunk_count
from precis.workers.llm_summarize import unsummarized_chunk_count
from precis.workers.summarize import RakeLemmaHandler
from tests.workers._helpers import seed_chunk, seed_ref

pytestmark = pytest.mark.db

_LONG = "graphene nanoribbons confine electrons in one dimension; " * 4


def _seed_corpus(store: Store) -> None:
    """Eligible, short, skip-kind, embedded, failed and stale-embedded
    chunks — enough that every counter splits non-trivially."""
    ref_id = seed_ref(store)
    seed_chunk(store, ref_id=ref_id, ord=0, text=_LONG)
    seed_chunk(store, ref_id=ref_id, ord=1, text="short body")
    seed_chunk(store, ref_id=ref_id, ord=2, chunk_kind="references", text=_LONG)
    seed_chunk(store, ref_id=ref_id, ord=3, chunk_kind="field", text="sdf grid")
    ok = seed_chunk(store, ref_id=ref_id, ord=4, text=_LONG + " ok")
    failed = seed_chunk(store, ref_id=ref_id, ord=5, text=_LONG + " failed")
    stale = seed_chunk(store, ref_id=ref_id, ord=6, text=_LONG + " stale")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, status, content_sha) "
            "VALUES (%s, 'bge-m3', 'ok', "
            "        (SELECT content_sha FROM chunks WHERE chunk_id = %s)), "
            "       (%s, 'bge-m3', 'failed', NULL), "
            "       (%s, 'bge-m3', 'ok', 'not-the-current-sha')",
            (ok, ok, failed, stale),
        )
        conn.execute(
            "UPDATE chunks SET created_at = now() - interval '3 hours' "
            "WHERE chunk_id = %s",
            (stale,),
        )
        conn.commit()


def _counts_only(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in ("counted_at", "last_ts")}


def test_cached_counters_match_the_uncached_queries(store: Store) -> None:
    _seed_corpus(store)

    with store.pool.connection() as conn:
        counts = health_checks.compute_backlog_counts(conn, store.pool)
        assert _counts_only(counts["embed"]) == health_checks._terminal_pass_counts(
            conn,
            "chunk_embeddings",
            "embedder",
            "bge-m3",
            embed.EmbedHandler.skip_chunk_kinds,
        )
        assert _counts_only(counts["summarize"]) == health_checks._terminal_pass_counts(
            conn,
            "chunk_summaries",
            "summarizer",
            "rake-lemma",
            RakeLemmaHandler.skip_chunk_kinds,
        )
        assert _counts_only(counts["chunk_keywords"]) == health_checks._keywords_counts(
            conn
        )
        # ords 0/1/6 pending (6's embedding is for older content), 4 done,
        # 5 failed; references/field are not eligible.
        assert _counts_only(counts["embed"]) == {"pending": 3, "done": 1, "failed": 1}
        assert counts["embed"]["counted_at"] is not None

        backlog = embed.cached_unembedded_backlog(store.pool)
        assert backlog.count == embed.unembedded_chunk_count(conn) == 3
        assert backlog.oldest == embed.oldest_unembedded_chunk_at(conn)
        assert backlog.oldest is not None

        expect_summarize = unsummarized_chunk_count(conn)
        expect_classify = unclassified_chunk_count(conn)
    assert expect_classify > 0
    assert m._summarize_backlog_count(store) == expect_summarize
    assert m._classify_backlog_count(store) == expect_classify
    assert m._embed_backlog_count(store) == 3
    assert m._embed_oldest_pending_at(store) == backlog.oldest


def test_cached_count_does_not_rescan_within_the_hour(store: Store) -> None:
    calls: list[int] = []

    def compute(_conn: Any) -> int:
        calls.append(1)
        return len(calls)

    with store.pool.connection() as conn:
        first = backlog_cache.cached_count(store.pool, "probe", compute, conn=conn)
        second = backlog_cache.cached_count(store.pool, "probe", compute, conn=conn)
        assert calls == [1]
        assert (second.value, second.counted_at) == (first.value, first.counted_at)

        conn.execute(
            "UPDATE app_state SET updated_at = now() - interval '61 minutes' "
            "WHERE key = 'backlog_count:probe'"
        )
        conn.commit()
        third = backlog_cache.cached_count(store.pool, "probe", compute, conn=conn)
    assert calls == [1, 1]
    assert third.value == 2
    assert third.counted_at > first.counted_at


def test_stale_reader_that_loses_the_lock_keeps_the_stale_value(
    store: Store,
) -> None:
    """Another session is recounting (holds the advisory lock): a stale
    reader serves the parked value instead of running a second scan."""
    backlog_cache.cached_count(store.pool, "probe", lambda _c: 7)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE app_state SET updated_at = now() - interval '2 hours' "
            "WHERE key = 'backlog_count:probe'"
        )
    with store.pool.connection() as holder:
        holder.execute("SELECT pg_advisory_xact_lock(hashtext('backlog_count:probe'))")
        hit = backlog_cache.cached_count(
            store.pool, "probe", lambda _c: pytest.fail("rescanned under a held lock")
        )
        holder.rollback()
    assert hit.value == 7


def test_cache_write_leaves_the_callers_transaction_alone(store: Store) -> None:
    """A caller with an uncommitted write calls the counter, then rolls
    back: its write is gone, the parked count persists, and its transaction
    was still open after the call (nothing committed it)."""
    from psycopg.pq import TransactionStatus

    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('probe:caller-write', 'x')"
        )
        hit = backlog_cache.cached_count(store.pool, "probe", lambda _c: 5, conn=conn)
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        conn.rollback()
    with store.pool.connection() as conn:
        rows: dict[str, str] = dict(
            conn.execute(
                "SELECT key, value FROM app_state "
                "WHERE key IN ('probe:caller-write', 'backlog_count:probe')"
            ).fetchall()
        )
    assert hit.value == 5
    assert rows == {"backlog_count:probe": "5"}


def test_backlog_counts_served_from_cache_until_invalidated(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel and digest read the parked counts: a second call within the
    hour neither rescans nor sees a chunk written in between."""
    _seed_corpus(store)
    with store.pool.connection() as conn:
        before = health_checks.compute_backlog_counts(conn, store.pool)

    def _no_rescan(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("rescanned chunks within the hour")

    monkeypatch.setattr(health_checks, "_terminal_pass_counts", _no_rescan)
    monkeypatch.setattr(health_checks, "_keywords_counts", _no_rescan)
    seed_chunk(store, ref_id=seed_ref(store), text=_LONG + " new")
    with store.pool.connection() as conn:
        after = health_checks.compute_backlog_counts(conn, store.pool)
    for label in ("embed", "summarize", "chunk_keywords"):
        assert _counts_only(after[label]) == _counts_only(before[label])
        assert after[label]["counted_at"] == before[label]["counted_at"]

    monkeypatch.undo()
    backlog_cache.invalidate(store.pool)
    with store.pool.connection() as conn:
        fresh = health_checks.compute_backlog_counts(conn, store.pool)
    assert fresh["embed"]["pending"] == before["embed"]["pending"] + 1


def test_embed_watchdog_fires_on_a_cached_backlog(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``discovery/embed``: pending above zero and no embedding ever written
    reads stale, also when the pending count comes from the cache."""
    _seed_corpus(store)
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunk_embeddings")
        conn.commit()
        health_checks.compute_backlog_counts(conn, store.pool)

    def _no_rescan(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("rescanned chunks within the hour")

    monkeypatch.setattr(health_checks, "_terminal_pass_counts", _no_rescan)
    monkeypatch.setattr(health_checks, "_keywords_counts", _no_rescan)
    with store.pool.connection() as conn:
        checks = {
            c.name: c
            for c in health_digest._idle_aware_backlog_checks(conn, store.pool)
        }
    assert checks["embed"].status == "stale"
    assert "NOT draining" in checks["embed"].detail


def test_materializer_mints_on_a_cached_backlog_above_high(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The high-water still compares against the unscoped backlog, read from
    the cache without a second scan."""
    monkeypatch.setenv("PRECIS_MATERIALIZE_EMBED", "1")
    monkeypatch.setenv("PRECIS_EMBED_BACKLOG_HIGH", "1")
    _seed_corpus(store)
    assert embed.cached_unembedded_backlog(store.pool).count == 3

    def _no_rescan(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("rescanned chunks within the hour")

    monkeypatch.setattr(embed, "_unembedded_where", _no_rescan)
    result = m.run_materialize_pass(store)

    assert result.failed == 0
    assert result.claimed >= 1
