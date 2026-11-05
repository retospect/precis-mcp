"""Selected embed jobs use shared leases and certify the pinned revision."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.store import Store
from precis.workers.base import ClaimedChunk
from precis.workers.embed import EmbedHandler
from precis.workers.job_types import embed_batch
from precis.workers.materialize import _in_failed_cooldown
from precis.workers.memory_embed import (
    MAX_TARGET_CHUNKS,
    TargetedMemoryEmbedHandler,
    parse_memory_target,
)
from tests.workers._helpers import seed_chunk, seed_chunks, seed_ref
from tests.workers.test_embed_batch import _mk_ctx

pytestmark = pytest.mark.db


def _memory(store: Store) -> dict[str, Any]:
    ref_id = seed_ref(store, title="Selected memory", kind="memory")
    seed_chunk(store, ref_id=ref_id, text="Selected body", chunk_kind="memory_body")
    store.chunks.sync_header_card(ref_id)
    with store.pool.connection() as conn:
        model = conn.execute("SELECT name FROM embedders WHERE is_default").fetchone()
        assert model is not None
        rows = conn.execute(
            "SELECT chunk_id, content_sha, text FROM chunks WHERE ref_id = %s "
            "AND retired_at IS NULL ORDER BY chunk_id",
            (ref_id,),
        ).fetchall()
    return {
        "ref_id": ref_id,
        "embedder": model[0],
        "chunks": [
            {
                "chunk_id": chunk_id,
                "content_sha": content_sha,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            for chunk_id, content_sha, text in rows
        ],
    }


def _worker(store: Store, target: dict[str, Any]) -> TargetedMemoryEmbedHandler:
    return TargetedMemoryEmbedHandler(
        MockEmbedder(model=target["embedder"], dim=store.embedding_dim()), target
    )


def _dispatch(
    store: Store, target: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> Any:
    monkeypatch.setattr(
        embed_batch,
        "resolve_embedder",
        lambda **_kw: MockEmbedder(model=target["embedder"], dim=store.embedding_dim()),
    )
    ctx = _mk_ctx(store, params={"memory_target": target})
    embed_batch._dispatch(ctx, embed_batch.SPEC)
    return ctx


def _embedded(store: Store) -> set[int]:
    with store.pool.connection() as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT chunk_id FROM chunk_embeddings WHERE status = 'ok'"
            ).fetchall()
        }


def test_target_embeddings_precede_bulk_and_include_header(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bulk = seed_chunks(store, ["Earlier bulk row"] * 4)
    target = _memory(store)
    ctx = _dispatch(store, target, monkeypatch)

    assert ctx.failures == []
    assert _embedded(store) == {chunk["chunk_id"] for chunk in target["chunks"]}
    assert not _embedded(store).intersection(bulk)
    assert "2 verified" in ctx.summaries[0][1]


def test_repeat_target_skips_completed_output(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _memory(store)
    _dispatch(store, target, monkeypatch)

    def unexpected_compute(
        _self: EmbedHandler, _rows: list[ClaimedChunk]
    ) -> list[object]:
        raise AssertionError("completed target must not recompute")

    monkeypatch.setattr(EmbedHandler, "process_batch", unexpected_compute)
    ctx = _dispatch(store, target, monkeypatch)
    assert ctx.failures == []
    assert "0 written; 2 verified" in ctx.summaries[0][1]


def test_target_does_not_steal_existing_chunk_lease(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _memory(store)
    chunk_id = target["chunks"][0]["chunk_id"]
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO chunk_claims (chunk_id, artifact) VALUES (%s, %s)",
            (chunk_id, target["embedder"]),
        )
    ctx = _dispatch(store, target, monkeypatch)
    assert len(ctx.failures) == 1
    assert "remain pending" in ctx.failures[0][0]
    assert ctx.summaries == []
    assert chunk_id not in _embedded(store)
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM chunk_claims WHERE chunk_id = %s AND artifact = %s",
            (chunk_id, target["embedder"]),
        ).fetchone()


def test_edit_during_compute_discards_entire_old_revision(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _memory(store)
    compute = EmbedHandler.process_batch

    def edit_during_compute(
        self: EmbedHandler, rows: list[ClaimedChunk]
    ) -> list[object]:
        result = compute(self, rows)
        with store.tx() as conn:
            conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (target["ref_id"],)
            )
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'memory_body'",
                (target["ref_id"],),
            )
            conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
                "VALUES (%s, 'system', 0, 'memory_body', 'Revised body')",
                (target["ref_id"],),
            )
        return result

    monkeypatch.setattr(EmbedHandler, "process_batch", edit_during_compute)
    ctx = _dispatch(store, target, monkeypatch)
    assert ctx.failures == []
    assert ctx.statuses == ["cancelled"]
    assert "revision changed" in ctx.summaries[0][1]
    assert not _in_failed_cooldown(store, "embed_batch")
    assert _embedded(store) == set()


def test_changed_header_metadata_refuses_old_header(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _memory(store)
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET title = 'Changed title' WHERE ref_id = %s",
            (target["ref_id"],),
        )
    ctx = _dispatch(store, target, monkeypatch)
    assert ctx.failures == []
    assert ctx.statuses == ["cancelled"]
    assert "revision changed" in ctx.summaries[0][1]
    assert not _in_failed_cooldown(store, "embed_batch")
    assert _embedded(store) == set()


def test_target_refuses_disabled_chunks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _memory(store)
    with store.tx() as conn:
        conn.execute(
            "UPDATE chunks SET meta = meta || '{\"no_index\": true}'::jsonb "
            "WHERE chunk_id = %s",
            (target["chunks"][0]["chunk_id"],),
        )
    ctx = _dispatch(store, target, monkeypatch)
    assert len(ctx.failures) == 1
    assert "no_index" in ctx.failures[0][0]
    assert not _in_failed_cooldown(store, "embed_batch")
    assert _embedded(store) == set()


def test_changed_lease_cannot_be_written_or_released(store: Store) -> None:
    target = _memory(store)
    handler = _worker(store, target)
    with store.tx() as conn:
        rows = handler.claim_batch(conn, limit=1)
    chunk_id = rows[0].chunk_id
    with store.tx() as conn:
        conn.execute(
            "UPDATE chunk_claims SET claimed_at = claimed_at + interval '1 second' "
            "WHERE chunk_id = %s AND artifact = %s",
            (chunk_id, target["embedder"]),
        )
    with store.tx() as conn:
        handler.write_ok(conn, chunk_id, [1.0] * store.embedding_dim())
        handler.release_claims(conn, [chunk_id])
    assert _embedded(store) == set()
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM chunk_claims WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()


def test_target_does_not_override_configured_model(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _memory(store)
    configured = target["embedder"]
    target["embedder"] = "different-model"
    calls: list[Any] = []

    def resolve(**kwargs: Any) -> MockEmbedder:
        calls.append(kwargs["name"])
        return MockEmbedder(model=configured, dim=store.embedding_dim())

    monkeypatch.setattr(embed_batch, "resolve_embedder", resolve)
    ctx = _mk_ctx(store, params={"memory_target": target})
    embed_batch._dispatch(ctx, embed_batch.SPEC)
    assert calls == [None]
    assert len(ctx.failures) == 1
    assert "model differs" in ctx.failures[0][0]
    assert _embedded(store) == set()


@pytest.mark.parametrize("mutation", ["too_many", "duplicate", "bad_hash", "batch_ref"])
def test_target_params_are_bounded(store: Store, mutation: str) -> None:
    target = _memory(store)
    if mutation == "too_many":
        target["chunks"] = target["chunks"] * MAX_TARGET_CHUNKS
    elif mutation == "duplicate":
        target["chunks"] = [target["chunks"][0]] * 2
    elif mutation == "bad_hash":
        target["chunks"][0]["text_sha256"] = "not-a-hash"
    else:
        target["ref_id"] = [target["ref_id"]]
    with pytest.raises(ValueError):
        parse_memory_target(target)
