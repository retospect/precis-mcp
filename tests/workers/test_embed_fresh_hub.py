"""A fresh hub is embedded by the worker within seconds (embed-freshness §B).

gr489243, Reto 2026-10-15: the embed worker picks up a new hub's
``finding_body`` promptly. Two parts: the ``finding`` claim tier in
``EmbedHandler._claim_fresh`` (a hub jumps the paper backlog) and
``nudge_fresh_embed`` (a hub write queues a small high-priority
``embed_batch``, so there is a job to claim it). The hub write never calls
the embedder itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.taproot.canon import CanonicalClaim, block
from precis.taproot.hub import mint_hub
from precis.workers.embed import (
    NUDGE_LIMIT,
    NUDGE_PRIO,
    EmbedHandler,
    nudge_fresh_embed,
)
from precis.workers.job_types import embed_batch
from tests.workers._helpers import make_mock_bge_m3, seed_chunk, seed_chunks, seed_ref
from tests.workers.test_embed_batch import _mk_ctx, _patch_embedder

pytestmark = pytest.mark.db

SENTENCE = (
    "Lengthening the neck of a carbon nanobud lowers the conductance plateau "
    "of the junction."
)


def _nudge_jobs(store: Store) -> list[tuple[int, int, dict[str, Any], str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.prio, r.meta, t.value
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
             WHERE r.kind = 'job' AND r.meta->>'job_type' = 'embed_batch'
               AND r.meta->>'idem_key' LIKE 'embed-nudge:%%'
             ORDER BY r.ref_id
            """
        ).fetchall()
    return [(int(r[0]), int(r[1]), dict(r[2]), str(r[3])) for r in rows]


def test_finding_tier_claimed_before_draft_and_paper_backlog(store: Store) -> None:
    h = EmbedHandler(make_mock_bge_m3())
    _paper, paper_ids = seed_chunks(store, ["paper one", "paper two"])
    draft_id = seed_chunk(store, ref_id=seed_ref(store, kind="draft"), text="d", ord=0)
    hub_id = seed_chunk(
        store,
        ref_id=seed_ref(store, kind="finding"),
        text="a claim",
        ord=0,
        chunk_kind="finding_body",
    )
    with store.pool.connection() as conn:
        claimed = [c.chunk_id for c in h.claim_batch(conn, limit=10)]
        conn.commit()
    assert claimed[:2] == [hub_id, draft_id]
    assert set(claimed) == {hub_id, draft_id, *paper_ids}


def test_nudge_queues_one_small_urgent_job_per_burst(store: Store) -> None:
    with store.tx() as conn:
        first = nudge_fresh_embed(store, conn=conn, reason="test")
        second = nudge_fresh_embed(store, conn=conn, reason="test")
    assert first is not None
    assert second is None  # the queued one claims the finding tier first
    [(ref_id, prio, meta, status)] = _nudge_jobs(store)
    assert ref_id == first
    assert prio == NUDGE_PRIO
    assert status == "queued"
    assert meta["executor"] == "job_inproc"
    assert meta["params"] == {"limit": NUDGE_LIMIT}


def test_nudge_failure_never_breaks_the_write(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("insert failed")

    monkeypatch.setattr(store, "insert_ref", boom)
    with store.tx() as conn:
        assert nudge_fresh_embed(store, conn=conn, reason="test") is None
        # The savepoint rolled back; the caller's transaction still works.
        assert conn.execute("SELECT 1").fetchone() == (1,)
    assert _nudge_jobs(store) == []


def test_minted_hub_finds_itself_after_the_nudged_batch(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: mint a hub, run the job its mint queued, search the
    hub's own sentence, and it comes back. A paper backlog larger than the
    nudge's limit proves the finding tier, not luck, picks the hub."""
    embedder = make_mock_bge_m3()
    _patch_embedder(monkeypatch, embedder)
    seed_chunks(store, [f"backlog {i}" for i in range(NUDGE_LIMIT + 8)])
    claim = CanonicalClaim(sentence=SENTENCE, scope={"material": "carbon nanobuds"})

    hub = mint_hub(store, claim)
    assert block(claim, store, embedder) == []  # invisible until embedded

    [(_job, _prio, meta, status)] = _nudge_jobs(store)
    assert status == "queued"
    ctx = _mk_ctx(store, params=meta["params"])
    embed_batch._dispatch(ctx, embed_batch.SPEC)
    assert ctx.failures == []

    hits = block(claim, store, embedder)
    assert hits and hits[0].hub_ref_id == hub
