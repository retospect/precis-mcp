"""A freshly written memory is embedded by the worker within seconds.

gr563669: a split memory's children sat ~15-32 min behind the paper
backlog with no ``chunk_embeddings`` row at all, so natural-language
recall missed them — only the exact-title lexical query hit. Mirrors
the hub fix (gr489243): the ``memory`` claim tier in
``EmbedHandler._claim_fresh`` jumps the backlog, and the memory
handler's chunk-writing paths call ``nudge_fresh_embed`` so a job
exists to claim them. The write never calls the embedder itself.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from precis.workers.embed import NUDGE_LIMIT, NUDGE_PRIO, EmbedHandler
from precis.workers.job_types import embed_batch
from tests.conftest import id_of
from tests.workers._helpers import (
    make_mock_bge_m3,
    seed_chunk,
    seed_chunks,
    seed_ref,
)
from tests.workers.test_embed_batch import _mk_ctx, _patch_embedder
from tests.workers.test_embed_fresh_hub import _nudge_jobs

pytestmark = pytest.mark.db

BODY = (
    "TAZ thermal via-in-pad is permitted for heat with a plugged/capped "
    "fab check — a split child note that must recall on a paraphrase, "
    "not only on its own title."
)


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def test_memory_tier_claimed_before_corpus_backlog(store: Store) -> None:
    h = EmbedHandler(make_mock_bge_m3())
    _paper, paper_ids = seed_chunks(store, ["paper one", "paper two"])
    conv_id = seed_chunk(store, ref_id=seed_ref(store, kind="conv"), text="c")
    memory_id = seed_chunk(
        store,
        ref_id=seed_ref(store, kind="memory"),
        text="a note",
        chunk_kind="memory_body",
    )
    with store.pool.connection() as conn:
        claimed = [c.chunk_id for c in h.claim_batch(conn, limit=10)]
        conn.commit()
    assert claimed[:2] == [conv_id, memory_id]
    assert set(claimed) == {memory_id, conv_id, *paper_ids}


def test_put_memory_queues_a_nudge(handler: MemoryHandler, store: Store) -> None:
    handler.put(text=BODY, title="TAZ thermal via note")
    [(ref_id, prio, meta, status)] = _nudge_jobs(store)
    assert prio == NUDGE_PRIO
    assert status == "queued"
    assert meta["executor"] == "job_inproc"
    assert meta["params"] == {"limit": NUDGE_LIMIT}
    assert isinstance(ref_id, int)


def test_edit_memory_body_queues_a_nudge(handler: MemoryHandler, store: Store) -> None:
    ref_id = id_of(handler.put(text=BODY, title="TAZ thermal via note").body)
    [(put_job, *_)] = _nudge_jobs(store)
    store.retire_ref(put_job)  # coalescing only reads live queued jobs

    handler.edit(id=ref_id, mode="replace", text=BODY + " Addendum.")

    jobs = _nudge_jobs(store)
    assert len(jobs) == 2
    assert jobs[0][0] == put_job and jobs[1][0] != put_job
    assert jobs[1][3] == "queued"


def test_meta_edit_queues_a_nudge(handler: MemoryHandler, store: Store) -> None:
    """A hook/retitle edit re-emits the ``card_combined`` chunk — its new
    id needs a vector before the new title/hook is recallable."""
    ref_id = id_of(handler.put(text=BODY, title="TAZ thermal via note").body)
    [(put_job, *_)] = _nudge_jobs(store)
    store.retire_ref(put_job)

    handler.edit(id=ref_id, meta={"hook": "thermal vias in the exposed pad"})

    jobs = _nudge_jobs(store)
    assert len(jobs) == 2 and jobs[1][0] != put_job


def test_put_memory_embedded_by_the_nudged_batch(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: put a memory behind a paper backlog larger than the
    nudge's limit, run the job its put queued, and both searchable chunks
    (``memory_body`` + ``card_combined``) carry vectors — the tier, not
    luck, picked them."""
    _patch_embedder(monkeypatch, make_mock_bge_m3())
    seed_chunks(store, [f"backlog {i}" for i in range(NUDGE_LIMIT + 8)])

    ref_id = id_of(handler.put(text=BODY, title="TAZ thermal via note").body)
    [(_job, _prio, meta, status)] = _nudge_jobs(store)
    assert status == "queued"

    ctx = _mk_ctx(store, params=meta["params"])
    embed_batch._dispatch(ctx, embed_batch.SPEC)
    assert ctx.failures == []

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.chunk_kind, ce.status, ce.vector IS NOT NULL "
            "FROM chunks c "
            "JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id "
            "WHERE c.ref_id = %s AND ce.embedder = 'bge-m3' "
            "ORDER BY c.chunk_kind",
            (ref_id,),
        ).fetchall()
    assert rows == [("card_combined", "ok", True), ("memory_body", "ok", True)]
