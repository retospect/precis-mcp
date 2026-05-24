"""Tests for ``precis.workers.embed``.

Pure unit tests cover the metadata setup; integration tests exercise
``write_ok`` / ``write_failed`` against an ephemeral Postgres so the
INSERT shapes line up with ``chunk_embeddings``.
"""

from __future__ import annotations

from precis.embedder import MockEmbedder
from precis.workers.base import ChunkRow
from precis.workers.embed import EmbedHandler
from tests.workers._helpers import make_mock_bge_m3, seed_chunks

# ---------------------------------------------------------------------------
# Pure unit tests — no DB
# ---------------------------------------------------------------------------


class TestEmbedHandlerPure:
    def test_metadata_from_embedder(self):
        h = EmbedHandler(make_mock_bge_m3())
        assert h.output_table == "chunk_embeddings"
        assert h.model_column == "embedder"
        assert h.model_name == "bge-m3"
        assert h.name == "embed:bge-m3"

    def test_custom_model_name_propagates(self):
        h = EmbedHandler(MockEmbedder(model="custom-emb"))
        assert h.model_name == "custom-emb"
        assert h.name == "embed:custom-emb"

    def test_process_returns_vector(self):
        emb = make_mock_bge_m3()
        h = EmbedHandler(emb)
        row = ChunkRow(chunk_id=1, text="surface code")
        vec = h.process(row)
        assert isinstance(vec, list)
        assert len(vec) == 1024
        # Mock embedder is deterministic.
        assert vec == emb.embed_one("surface code")

    def test_process_empty_text_still_returns_vector(self):
        # Empty text is *not* a special case — caller policy.
        h = EmbedHandler(make_mock_bge_m3())
        vec = h.process(ChunkRow(chunk_id=1, text=""))
        assert len(vec) == 1024


# ---------------------------------------------------------------------------
# Integration — write_ok / write_failed against real Postgres
# ---------------------------------------------------------------------------


class TestEmbedHandlerWrites:
    def test_write_ok_inserts_vector(self, store):
        _ref_id, [chunk_id] = seed_chunks(store, ["one chunk of text"])

        h = EmbedHandler(make_mock_bge_m3())
        vec = h.process(ChunkRow(chunk_id=chunk_id, text="one chunk of text"))
        with store.pool.connection() as conn:
            h.write_ok(conn, chunk_id, vec)
            conn.commit()

            row = conn.execute(
                "SELECT embedder, status, attempts, last_error "
                "FROM chunk_embeddings WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
        assert row == ("bge-m3", "ok", 1, None)

    def test_write_ok_idempotent_on_conflict_updates(self, store):
        _ref_id, [chunk_id] = seed_chunks(store, ["text"])
        h = EmbedHandler(make_mock_bge_m3())
        vec = h.process(ChunkRow(chunk_id=chunk_id, text="text"))
        with store.pool.connection() as conn:
            h.write_ok(conn, chunk_id, vec)
            h.write_ok(conn, chunk_id, vec)
            conn.commit()

            count = conn.execute(
                "SELECT count(*), max(attempts) FROM chunk_embeddings "
                "WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
        # Single row, attempts incremented to 2.
        assert count == (1, 2)

    def test_write_failed_inserts_marker(self, store):
        _ref_id, [chunk_id] = seed_chunks(store, ["text"])
        h = EmbedHandler(make_mock_bge_m3())
        with store.pool.connection() as conn:
            h.write_failed(conn, chunk_id, "boom: something broke")
            conn.commit()

            row = conn.execute(
                "SELECT status, last_error, vector FROM chunk_embeddings "
                "WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
        assert row is not None
        status, last_error, vector = row
        assert status == "failed"
        assert last_error == "boom: something broke"
        assert vector is None

    def test_write_failed_truncates_long_error(self, store):
        _ref_id, [chunk_id] = seed_chunks(store, ["text"])
        h = EmbedHandler(make_mock_bge_m3())
        very_long = "x" * 5_000
        with store.pool.connection() as conn:
            h.write_failed(conn, chunk_id, very_long)
            conn.commit()

            (last_error,) = conn.execute(
                "SELECT last_error FROM chunk_embeddings WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
        # Truncated to ≤1000 chars (with ellipsis).
        assert last_error is not None
        assert len(last_error) <= 1000

    def test_write_failed_then_ok_marks_ok(self, store):
        # A failure marker followed by a successful retry must end
        # with status='ok' and the vector populated.
        _ref_id, [chunk_id] = seed_chunks(store, ["text"])
        h = EmbedHandler(make_mock_bge_m3())
        with store.pool.connection() as conn:
            h.write_failed(conn, chunk_id, "transient")
            vec = h.process(ChunkRow(chunk_id=chunk_id, text="text"))
            h.write_ok(conn, chunk_id, vec)
            conn.commit()

            row = conn.execute(
                "SELECT status, last_error, vector IS NOT NULL "
                "FROM chunk_embeddings WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
        assert row == ("ok", None, True)
