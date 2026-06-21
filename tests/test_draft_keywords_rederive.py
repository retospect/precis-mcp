"""Edited draft chunks re-derive keywords too — gated on embed freshness
(ADR 0033 §4). KeyBERT uses the chunk's embedding, so keywords must
wait for embed to refresh after an edit before re-deriving."""

from __future__ import annotations

from precis.store.store import Store
from precis.workers.chunk_keywords import (
    claim_chunks_without_keywords,
    write_chunk_keywords,
)

_LONG = "nanoscale transistor leakage current density characterization " * 4
_LONG2 = "graphene channel carrier mobility enhancement factor analysis " * 4


def _claimed(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        ids = [r[0] for r in claim_chunks_without_keywords(conn, limit=50)]
        conn.rollback()  # claim only; release FOR UPDATE
    return ids


def _set_embed_sha_to_current(store: Store, chunk_id: int) -> None:
    """Simulate the embed worker catching up: stamp the embedding row's
    content_sha to the chunk's current value."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunk_embeddings SET content_sha = "
            "(SELECT content_sha FROM chunks WHERE chunk_id = %s) "
            "WHERE chunk_id = %s AND embedder = 'bge-m3'",
            (chunk_id, chunk_id),
        )
        conn.commit()


def test_keywords_rederive_gated_on_embed_then_reclaims(store: Store) -> None:
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    ref, title = store.create_draft(name="nt", title="T", project_ref_id=proj)
    p = store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text=_LONG, at={"after": title.handle}
    )[0]

    # Stand in for a completed embed at the current content_sha.
    dim = store.embedding_dim()
    with store.pool.connection() as conn:
        sha = conn.execute(
            "SELECT content_sha FROM chunks WHERE chunk_id = %s", (p.chunk_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, content_sha) "
            "VALUES (%s, 'bge-m3', %s, 'ok', %s)",
            (p.chunk_id, [0.0] * dim, sha),
        )
        conn.commit()

    # 1) no keywords yet → claimed
    assert p.chunk_id in _claimed(store)
    with store.pool.connection() as conn:
        write_chunk_keywords(
            conn,
            p.chunk_id,
            keywords=[{"short": "x", "long": "x", "score": 1.0}],
            embedder_name="bge-m3",
        )
        conn.commit()

    # 2) fresh + unchanged → not re-claimed
    assert p.chunk_id not in _claimed(store)

    # 3) edit the text → content_sha changes, but the embedding is now
    #    stale → keywords WAIT (don't re-derive against a stale vector)
    store.edit_text(p.handle, _LONG2)
    assert p.chunk_id not in _claimed(store)

    # 4) embed catches up → keywords re-claim for re-derivation
    _set_embed_sha_to_current(store, p.chunk_id)
    assert p.chunk_id in _claimed(store)
