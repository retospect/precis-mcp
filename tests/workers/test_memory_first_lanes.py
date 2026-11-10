"""gr563702 — memory-first enrichment lanes and the bulk fairness reserve.

Every derived-work stage claims memory chunks in an interactive lane ahead
of the corpus backlog, and each claim reserves ``bulk_reserve(limit)``
slots for bulk work so an interactive burst can reorder but never starve
paper progress. Covers the embed tier, the shared ``priority_ref_kinds``
lane (rake-lemma), the keyword tier-1 lane, and the llm_summarize
interactive cap.
"""

from __future__ import annotations

import pytest

from precis.store import Store
from precis.workers._claim import bulk_reserve
from precis.workers.chunk_keywords import claim_chunks_without_keywords
from precis.workers.embed import EmbedHandler
from precis.workers.llm_summarize import (
    HOT_WINDOW_MIN,
    claim_chunks_without_summary,
)
from precis.workers.summarize import RakeLemmaHandler
from tests.workers._helpers import (
    make_mock_bge_m3,
    seed_chunk,
    seed_chunks,
    seed_ref,
)

_PROSE = (
    "Memory enrichment should be scheduled ahead of the bulk paper "
    "backlog because an agent or person needs the just-written note "
    "retrievable promptly. This body is long enough for every stage's "
    "minimum text window and reads like ordinary prose for keyword "
    "extraction."
)


def _attach_embedding(store: Store, chunk_id: int) -> None:
    """Stamp an ``ok`` bge-m3 row — the keyword claim's embedding gate."""
    vec = make_mock_bge_m3().embed_one(f"chunk:{chunk_id}")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, 'bge-m3', %s, 'ok')",
            (chunk_id, vec),
        )
        conn.commit()


def _seed_memory(store: Store, *, text: str = _PROSE) -> tuple[int, int]:
    """A memory ref + its body chunk; returns ``(ref_id, chunk_id)``."""
    ref_id = seed_ref(store, kind="memory")
    chunk_id = seed_chunk(
        store, ref_id=ref_id, text=text, ord=0, chunk_kind="memory_body"
    )
    return ref_id, chunk_id


class TestEmbedMemoryTier:
    def test_memory_chunk_claims_before_paper_backlog(self, store: Store) -> None:
        h = EmbedHandler(make_mock_bge_m3())
        _paper_ref, paper_ids = seed_chunks(store, ["paper one", "paper two"])
        _mem_ref, mem_id = _seed_memory(store)

        with store.pool.connection() as conn:
            claimed = h.claim_batch(conn, limit=1)
            conn.commit()

        assert [row.chunk_id for row in claimed] == [mem_id]
        assert not (set(paper_ids) & {row.chunk_id for row in claimed})

    def test_memory_orders_before_other_interactive_lanes(self, store: Store) -> None:
        h = EmbedHandler(make_mock_bge_m3())
        conv_ref = seed_ref(store, kind="conv")
        conv_id = seed_chunk(store, ref_id=conv_ref, text="conv text", ord=0)
        draft_ref = seed_ref(store, kind="draft")
        draft_id = seed_chunk(store, ref_id=draft_ref, text="draft text", ord=0)
        _mem_ref, mem_id = _seed_memory(store)

        with store.pool.connection() as conn:
            claimed = h.claim_batch(conn, limit=10)
            conn.commit()

        ids = [row.chunk_id for row in claimed]
        assert set(ids) == {mem_id, draft_id, conv_id}
        assert ids[0] == mem_id

    def test_interactive_burst_keeps_the_bulk_reserve(self, store: Store) -> None:
        """Fairness: a memory flood can't consume a whole claim batch —
        ``bulk_reserve(limit)`` slots always go to the corpus backlog."""
        h = EmbedHandler(make_mock_bge_m3())
        _paper_ref, paper_ids = seed_chunks(store, [f"paper {i}" for i in range(3)])
        # An interactive burst bigger than the interactive share.
        mem_ids = [_seed_memory(store, text=f"memory {i}")[1] for i in range(6)]

        limit = 8  # interactive cap = 8 - bulk_reserve(8) = 7
        with store.pool.connection() as conn:
            claimed = h.claim_batch(conn, limit=limit)
            conn.commit()

        ids = [row.chunk_id for row in claimed]
        assert len(ids) == limit
        interactive = [i for i in ids if i in mem_ids]
        bulk = [i for i in ids if i in paper_ids]
        assert len(interactive) <= limit - bulk_reserve(limit)
        assert len(bulk) >= bulk_reserve(limit)


class TestRakeLemmaMemoryLane:
    def test_memory_claims_before_corpus_fifo(self, store: Store) -> None:
        h = RakeLemmaHandler()
        _paper_ref, paper_ids = seed_chunks(store, [_PROSE, _PROSE])
        _mem_ref, mem_id = _seed_memory(store)

        with store.pool.connection() as conn:
            claimed = h.claim_batch(conn, limit=1)
            conn.commit()

        assert [row.chunk_id for row in claimed] == [mem_id]
        assert not (set(paper_ids) & {row.chunk_id for row in claimed})

    def test_bulk_keeps_reserve_under_memory_burst(self, store: Store) -> None:
        h = RakeLemmaHandler()
        _paper_ref, paper_ids = seed_chunks(store, [_PROSE] * 3)
        mem_ids = [_seed_memory(store, text=f"memory {i}")[1] for i in range(6)]

        with store.pool.connection() as conn:
            claimed = h.claim_batch(conn, limit=8)
            conn.commit()

        ids = [row.chunk_id for row in claimed]
        assert len(ids) == 8
        assert len([i for i in ids if i in mem_ids]) <= 8 - bulk_reserve(8)
        assert len([i for i in ids if i in paper_ids]) >= bulk_reserve(8)


class TestKeywordMemoryTier:
    def test_memory_chunk_claims_in_tier_1(self, store: Store) -> None:
        """A memory's embedded body chunk claims via the interactive tier —
        ahead of a NEWER paper chunk the tier-2 ``chunk_id DESC`` walk
        would otherwise take first."""
        _mem_ref, mem_id = _seed_memory(store)
        _attach_embedding(store, mem_id)
        _paper_ref, paper_ids = seed_chunks(store, [_PROSE] * 2)
        for cid in paper_ids:
            _attach_embedding(store, cid)

        with store.pool.connection() as conn:
            rows = claim_chunks_without_keywords(conn, limit=1)
            conn.commit()

        assert [r[0] for r in rows] == [mem_id]


class TestLlmSummarizeMemoryTier:
    def test_memory_claims_before_draft_conv_and_rest(self, store: Store) -> None:
        draft_ref = seed_ref(store, kind="draft")
        draft_id = seed_chunk(
            store, ref_id=draft_ref, text=_PROSE, ord=0, chunk_kind="paragraph"
        )
        _paper_ref, paper_ids = seed_chunks(store, [_PROSE] * 2)
        _mem_ref, mem_id = _seed_memory(store)
        aged = f"now() - interval '{HOT_WINDOW_MIN + 60} minutes'"
        with store.pool.connection() as conn:
            # Age everything out of the hot window so the claim order is
            # memory > draft > rest, not salience.
            conn.execute(
                f"UPDATE chunks SET last_seen = {aged} WHERE ref_id = %s",
                (_paper_ref,),
            )
            conn.commit()
            claimed = claim_chunks_without_summary(conn, summarizer="llm-v1", limit=10)

        ids = [c.chunk_id for c in claimed]
        assert set(ids) == {mem_id, draft_id, *paper_ids}
        assert ids[0] == mem_id
        assert ids[1] == draft_id

    def test_interactive_tiers_capped_for_bulk(self, store: Store) -> None:
        """The memory/draft/conv lanes share ``limit - bulk_reserve`` —
        papers keep a floor of every claim batch."""
        mem_ids = [_seed_memory(store)[1] for _ in range(10)]
        _paper_ref, paper_ids = seed_chunks(store, [_PROSE] * 6)

        limit = 8
        with store.pool.connection() as conn:
            claimed = claim_chunks_without_summary(
                conn, summarizer="llm-v1", limit=limit
            )

        ids = [c.chunk_id for c in claimed]
        assert len(ids) == limit
        interactive = [i for i in ids if i in mem_ids]
        bulk = [i for i in ids if i in paper_ids]
        assert len(interactive) <= limit - bulk_reserve(limit)
        assert len(bulk) >= bulk_reserve(limit)

    def test_reclaim_slice_preserves_bulk_reserve(self, store: Store) -> None:
        """The reserved reclaim slice cannot spend the bulk floor: at
        limit=16 two expired memory claims plus a 14-chunk memory burst
        still leave ``bulk_reserve(16)`` slots for papers — before the
        fix, reclaim + interactive filled the whole batch."""
        stale_ids = [_seed_memory(store)[1] for _ in range(2)]
        mem_ids = [_seed_memory(store)[1] for _ in range(14)]
        _paper_ref, paper_ids = seed_chunks(store, [_PROSE] * 4)

        limit = 16  # reclaim slice = min(2, 16//8) = 2; bulk_reserve(16) = 2
        with store.pool.connection() as conn:
            # Past the 20-minute lease cooldown, inside the 8 h GC window —
            # the reserved reclaim slice takes both.
            conn.execute(
                "INSERT INTO chunk_claims (chunk_id, artifact, claimed_at) "
                "SELECT cid, 'llm-v1', now() - interval '1 hour' "
                "FROM unnest(%s::bigint[]) AS cid",
                (stale_ids,),
            )
            claimed = claim_chunks_without_summary(
                conn, summarizer="llm-v1", limit=limit
            )
            conn.commit()

        ids = [c.chunk_id for c in claimed]
        assert len(ids) == limit
        assert len([i for i in ids if i in stale_ids]) == 2
        fresh_mem = [i for i in ids if i in mem_ids]
        bulk = [i for i in ids if i in paper_ids]
        assert len(fresh_mem) <= limit - bulk_reserve(limit) - len(stale_ids)
        assert len(bulk) >= bulk_reserve(limit)


class TestBulkReserveShape:
    @pytest.mark.parametrize(
        ("limit", "expected"),
        [(1, 0), (7, 0), (8, 1), (16, 2), (32, 4), (500, 4)],
    )
    def test_reserve_scales_then_caps(self, limit: int, expected: int) -> None:
        assert bulk_reserve(limit) == expected
