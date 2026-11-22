"""Tier-1 statistical keywords: a chunk gets keywords before its embedding.

gr739436 (ruling 2026-11-20): keyword extraction must not need the
embedding. A chunk with no current ``bge-m3`` vector gets RAKE-ranked
keywords from a pass that never calls the embedder; ``keywords_meta``
records the extractor, and the KeyBERT pass upgrades the row once the
chunk is embedded.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import Mock

import precis.workers.chunk_keywords as ck
from precis.store import Store
from precis.workers.chunk_keywords import (
    KEYWORDS_VERSION,
    TIER1_MIN_AGE,
    run_chunk_keywords_pass,
)
from tests.workers._helpers import make_mock_bge_m3, seed_chunk, seed_ref

_BODY = (
    "Metal-organic frameworks (MOFs) are porous crystalline materials "
    "built from metal nodes and organic linkers. Their tunable pore "
    "geometry and high surface area make them attractive for gas "
    "storage, catalysis, and separation applications."
)


def _attach_embedding(store: Store, chunk_id: int) -> None:
    vec = make_mock_bge_m3().embed_one(f"chunk:{chunk_id}")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, 'bge-m3', %s, 'ok')",
            (chunk_id, vec),
        )
        conn.commit()


def _seed_old_chunk(store: Store, **kwargs: Any) -> int:
    """A chunk past :data:`TIER1_MIN_AGE`, so tier 1 may claim it."""
    chunk_id = seed_chunk(store, **kwargs)
    _set_age(store, chunk_id, TIER1_MIN_AGE * 2)
    return chunk_id


def _set_age(store: Store, chunk_id: int, age: Any) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET created_at = now() - %s WHERE chunk_id = %s",
            (age, chunk_id),
        )
        conn.commit()


def _keywords_row(store: Store, chunk_id: int) -> tuple[Any, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT keywords, keywords_meta FROM chunks WHERE chunk_id = %s",
            (chunk_id,),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _spy_embedder(monkeypatch: Any) -> tuple[Any, Mock]:
    embedder = make_mock_bge_m3()
    embed = Mock(wraps=embedder.embed)
    monkeypatch.setattr(embedder, "embed", embed)
    return embedder, embed


class TestStatisticalTier:
    def test_unembedded_chunk_gets_keywords_without_the_embedder(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """The gripe's ask: no vector, no model call, keywords anyway —
        and the envelope names the extractor that produced them."""
        chunk_id = _seed_old_chunk(store, ref_id=seed_ref(store), text=_BODY)
        embedder, embed = _spy_embedder(monkeypatch)

        result = run_chunk_keywords_pass(store, embedder, batch_size=10)

        assert result == {
            "claimed": 0,
            "ok": 0,
            "failed": 0,
            "tier1_claimed": 1,
            "tier1_ok": 1,
            "tier1_failed": 0,
        }
        embed.assert_not_called()
        keywords, meta = _keywords_row(store, chunk_id)
        assert keywords
        assert "tunable pore geometry" in keywords
        assert meta["extractor"] == "rake"
        assert meta["version"] != KEYWORDS_VERSION
        assert "embedder" not in meta
        assert [(k["short"] or k["long"]).lower() for k in meta["keywords"]] == keywords
        assert {k["score"] for k in meta["keywords"]} == {None}

    def test_failed_embedding_does_not_block_keywords(
        self, store: Store, monkeypatch: Any
    ) -> None:
        chunk_id = _seed_old_chunk(store, ref_id=seed_ref(store), text=_BODY)
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedder, status, "
                "last_error) VALUES (%s, 'bge-m3', 'failed', 'simulated')",
                (chunk_id,),
            )
            conn.commit()
        embedder, _ = _spy_embedder(monkeypatch)

        run_chunk_keywords_pass(store, embedder, batch_size=10)

        keywords, meta = _keywords_row(store, chunk_id)
        assert keywords
        assert meta["extractor"] == "rake"

    def test_tier1_row_is_not_reclaimed_while_unembedded(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """No spin: a tier-1 row waits for its embedding, on every tier
        (the stale claim is forced on so tier 3 gets its say)."""
        monkeypatch.setattr(ck, "_STALE_CLAIM_EVERY", 1)
        _seed_old_chunk(store, ref_id=seed_ref(store), text=_BODY)
        embedder, _ = _spy_embedder(monkeypatch)
        run_chunk_keywords_pass(store, embedder, batch_size=10)

        again = run_chunk_keywords_pass(store, embedder, batch_size=10)

        assert again == {"claimed": 0, "ok": 0, "failed": 0}

    def test_keybert_upgrades_a_tier1_row_once_embedded(
        self, store: Store, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(ck, "_STALE_CLAIM_EVERY", 1)
        chunk_id = _seed_old_chunk(store, ref_id=seed_ref(store), text=_BODY)
        embedder, embed = _spy_embedder(monkeypatch)
        run_chunk_keywords_pass(store, embedder, batch_size=10)
        _attach_embedding(store, chunk_id)

        result = run_chunk_keywords_pass(store, embedder, batch_size=10)

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        embed.assert_called()
        keywords, meta = _keywords_row(store, chunk_id)
        assert keywords
        assert meta["extractor"] == "keybert"
        assert meta["version"] == KEYWORDS_VERSION
        assert meta["embedder"] == "bge-m3"

    def test_embedded_chunk_goes_straight_to_keybert(
        self, store: Store, monkeypatch: Any
    ) -> None:
        chunk_id = _seed_old_chunk(store, ref_id=seed_ref(store), text=_BODY)
        _attach_embedding(store, chunk_id)
        embedder, _ = _spy_embedder(monkeypatch)

        run_chunk_keywords_pass(store, embedder, batch_size=10)

        _, meta = _keywords_row(store, chunk_id)
        assert meta["extractor"] == "keybert"
        assert meta["version"] == KEYWORDS_VERSION

    def test_embedder_outage_does_not_stop_tier1(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """Tier 1 lands even when the pass's KeyBERT half defers."""
        from precis.embedder import EmbedderUnavailable

        ref_id = seed_ref(store)
        bare_id = _seed_old_chunk(store, ref_id=ref_id, ord=0, text=_BODY)
        embedded_id = _seed_old_chunk(store, ref_id=ref_id, ord=1, text=_BODY)
        _attach_embedding(store, embedded_id)
        embedder = make_mock_bge_m3()
        monkeypatch.setattr(
            embedder, "embed", Mock(side_effect=EmbedderUnavailable("down"))
        )

        run_chunk_keywords_pass(store, embedder, batch_size=10)

        assert _keywords_row(store, bare_id)[1]["extractor"] == "rake"
        assert _keywords_row(store, embedded_id) == (None, None)


class TestUnembeddedClaim:
    def test_claims_only_eligible_unembedded_fresh_rows(self, store: Store) -> None:
        ref_id = seed_ref(store)
        bare_id = _seed_old_chunk(store, ref_id=ref_id, ord=0, text=_BODY)
        embedded_id = _seed_old_chunk(store, ref_id=ref_id, ord=1, text=_BODY)
        _attach_embedding(store, embedded_id)
        _seed_old_chunk(store, ref_id=ref_id, ord=2, text="too short")
        _seed_old_chunk(
            store, ref_id=ref_id, ord=3, text=_BODY, chunk_kind="references"
        )

        with store.pool.connection() as conn:
            rows = ck.claim_unembedded_chunks(conn, limit=10)
            conn.rollback()

        assert [(r[0], r[1]) for r in rows] == [(bare_id, ref_id)]

    def test_newest_first_and_sharded(self, store: Store) -> None:
        ref_id = seed_ref(store)
        ids = [
            _seed_old_chunk(store, ref_id=ref_id, ord=i, text=_BODY) for i in range(6)
        ]

        with store.pool.connection() as conn:
            whole = [r[0] for r in ck.claim_unembedded_chunks(conn, limit=4)]
            conn.rollback()
            shards = [
                {
                    r[0]
                    for r in ck.claim_unembedded_chunks(
                        conn, limit=10, shard_count=2, shard_index=i
                    )
                }
                for i in range(2)
            ]
            conn.rollback()

        assert whole == sorted(ids, reverse=True)[:4]
        assert shards[0] | shards[1] == set(ids)
        assert shards[0].isdisjoint(shards[1])
        assert all(c % 2 == 0 for c in shards[0])

    def test_age_gate_skips_young_chunks(self, store: Store) -> None:
        """A chunk younger than :data:`TIER1_MIN_AGE` stays on the
        indexed KeyBERT path; one past the gate is tier 1's."""
        ref_id = seed_ref(store)
        young_id = seed_chunk(store, ref_id=ref_id, ord=0, text=_BODY)
        _set_age(store, young_id, TIER1_MIN_AGE / 2)
        old_id = seed_chunk(store, ref_id=ref_id, ord=1, text=_BODY)
        _set_age(store, old_id, TIER1_MIN_AGE * 2)

        with store.pool.connection() as conn:
            rows = ck.claim_unembedded_chunks(conn, limit=10)
            conn.rollback()

        assert [r[0] for r in rows] == [old_id]

    def test_claim_head_walks_the_pending_index(self, store: Store) -> None:
        """The age predicate sits outside the MATERIALIZED head, so the
        head still proves ``chunks_keywords_pending_idx``'s predicate."""
        with store.pool.connection() as conn:
            conn.execute("SET LOCAL enable_seqscan = off")
            plan = "\n".join(
                str(r[0])
                for r in conn.execute(
                    "EXPLAIN " + ck._unembedded_claim_sql(1),
                    (40, TIER1_MIN_AGE, "bge-m3", 10),
                ).fetchall()
            )
            conn.rollback()

        assert "chunks_keywords_pending_idx" in plan


class TestStatisticalWrite:
    def test_bulk_write_leaves_keybert_keywords_alone(self, store: Store) -> None:
        """KeyBERT keywords that land between the tier-1 claim and its
        write survive: the guard is ``keywords IS NULL``."""
        chunk_id = _seed_old_chunk(store, ref_id=seed_ref(store), text=_BODY)
        kw = [{"short": None, "long": "metal nodes", "score": 0.9}]
        with store.pool.connection() as conn:
            ck.write_chunk_keywords(conn, chunk_id, keywords=kw, embedder_name="bge-m3")
            conn.commit()
        before = _keywords_row(store, chunk_id)

        with store.pool.connection() as conn:
            written = ck.write_statistical_keywords_bulk(
                conn, [(chunk_id, [{"short": None, "long": "x y", "score": None}])]
            )
            conn.commit()

        assert written == 0
        assert _keywords_row(store, chunk_id) == before
        assert before[1]["extractor"] == "keybert"

    def test_no_candidates_writes_empty_row_and_does_not_loop(
        self, store: Store, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(ck, "_rake_phrases", lambda text, *, max_keywords: [])
        chunk_id = _seed_old_chunk(store, ref_id=seed_ref(store), text=_BODY)
        embedder, _ = _spy_embedder(monkeypatch)

        first = run_chunk_keywords_pass(store, embedder, batch_size=10)
        keywords, meta = _keywords_row(store, chunk_id)
        with store.pool.connection() as conn:
            reclaim = ck.claim_unembedded_chunks(conn, limit=10)
            conn.rollback()
        again = run_chunk_keywords_pass(store, embedder, batch_size=10)

        assert first["tier1_ok"] == 1
        assert keywords == []
        assert meta["extractor"] == "rake"
        assert meta["keywords"] == []
        assert reclaim == []
        assert again == {"claimed": 0, "ok": 0, "failed": 0}


class TestPassBudget:
    def test_stop_flag_skips_tier1(self, store: Store, monkeypatch: Any) -> None:
        chunk_id = _seed_old_chunk(store, ref_id=seed_ref(store), text=_BODY)
        embedder, _ = _spy_embedder(monkeypatch)

        result = run_chunk_keywords_pass(
            store, embedder, batch_size=10, should_stop=lambda: True
        )

        assert result == {"claimed": 0, "ok": 0, "failed": 0}
        assert _keywords_row(store, chunk_id) == (None, None)

    def test_deadline_stops_tier1_mid_claim(self, store: Store) -> None:
        """Each clock read advances one second; the 2.5 s deadline lets
        the claim and the first row through, then the rest wait."""
        ref_id = seed_ref(store)
        ids = [
            _seed_old_chunk(store, ref_id=ref_id, ord=i, text=_BODY) for i in range(3)
        ]
        ticks = iter(range(1, 100))

        result = ck._run_statistical_tier(
            store, batch_size=10, deadline=2.5, clock=lambda: float(next(ticks))
        )

        assert result == {"claimed": 3, "ok": 1, "failed": 0}
        written = [cid for cid in ids if _keywords_row(store, cid)[0] is not None]
        assert written == [max(ids)]

    def test_spent_deadline_claims_nothing(self, store: Store) -> None:
        _seed_old_chunk(store, ref_id=seed_ref(store), text=_BODY)

        result = ck._run_statistical_tier(
            store, batch_size=10, deadline=1.0, clock=lambda: 1.0
        )

        assert result == {"claimed": 0, "ok": 0, "failed": 0}


class TestDrainLedger:
    def test_tier1_writes_stay_off_the_keybert_ledger(
        self, store: Store, caplog: Any, monkeypatch: Any
    ) -> None:
        """Embedder down: tier 1 writes, KeyBERT defers. The lane's
        payload keeps ``ok`` at 0 (tier 1 under its own keys), the lane
        backs off, and ``keywords_backlog_rates`` reads no drain."""
        from psycopg.types.json import Jsonb

        from precis.embedder import EmbedderUnavailable
        from precis.health_checks import keywords_backlog_rates

        ref_id = seed_ref(store)
        bare_id = _seed_old_chunk(store, ref_id=ref_id, ord=0, text=_BODY)
        embedded_id = _seed_old_chunk(store, ref_id=ref_id, ord=1, text=_BODY)
        _attach_embedding(store, embedded_id)
        embedder = make_mock_bge_m3()
        monkeypatch.setattr(
            embedder, "embed", Mock(side_effect=EmbedderUnavailable("down"))
        )
        sleeps: list[float] = []

        with caplog.at_level(logging.INFO, logger="precis.workers.runner"):
            ck.run_dedicated_loop(
                store,
                embedder,
                batch_size=10,
                idle_seconds=1.0,
                max_seconds=60.0,
                should_stop=lambda: bool(sleeps),
                sleep=sleeps.append,
            )
        payloads = [
            r.payload
            for r in caplog.records
            if r.name == "precis.workers.runner" and hasattr(r, "payload")
        ]
        with store.pool.connection() as conn:
            for payload in payloads:
                conn.execute(
                    "INSERT INTO worker_logs (host, pass, level, message, payload) "
                    "VALUES ('maple', 'runner', 'INFO', 'worker: batch', %s)",
                    (Jsonb(payload),),
                )
            conn.commit()
            rates = keywords_backlog_rates(conn, window_hours=1.0)

        assert _keywords_row(store, bare_id)[1]["extractor"] == "rake"
        assert len(payloads) == 1
        assert payloads[0]["claimed"] == 0
        assert payloads[0]["ok"] == 0
        assert payloads[0]["tier1_claimed"] == 1
        assert payloads[0]["tier1_ok"] == 1
        assert sleeps, "the lane must back off while KeyBERT is stalled"
        assert rates is not None
        assert rates["drained"] == 0
