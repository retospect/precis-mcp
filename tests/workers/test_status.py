"""Tests for the per-handler status query (``WorkerHandler.status``).

The query feeds ``precis worker --status`` and ``precis health``; we
need it to be exact at zero / partial / full / mixed-failure states.
"""

from __future__ import annotations

from precis.embedder import MockEmbedder
from precis.workers.base import ArtifactStatus, ChunkRow
from precis.workers.embed import EmbedHandler
from precis.workers.runner import run_handler_once
from precis.workers.summarize import RakeLemmaHandler
from tests.workers._helpers import make_mock_bge_m3, seed_chunks


class TestStatus:
    def test_empty_db_zero_everywhere(self, store):
        h = EmbedHandler(make_mock_bge_m3())
        with store.pool.connection() as conn:
            s = h.status(conn)
        assert s == ArtifactStatus(
            name="embed:bge-m3", total=0, ok=0, failed=0, pending=0
        )

    def test_chunks_but_no_artifacts_all_pending(self, store):
        seed_chunks(store, ["a", "b", "c"])
        h = EmbedHandler(make_mock_bge_m3())
        with store.pool.connection() as conn:
            s = h.status(conn)
        assert s == ArtifactStatus(
            name="embed:bge-m3", total=3, ok=0, failed=0, pending=3
        )

    def test_after_full_drain_zero_pending(self, store):
        seed_chunks(store, ["a", "b", "c"])
        h = EmbedHandler(make_mock_bge_m3())
        run_handler_once(h, store, batch_size=10)
        with store.pool.connection() as conn:
            s = h.status(conn)
        assert s == ArtifactStatus(
            name="embed:bge-m3", total=3, ok=3, failed=0, pending=0
        )

    def test_failures_count_against_failed_not_pending(self, store):
        _ref_id, chunk_ids = seed_chunks(store, ["a", "b"])
        h = EmbedHandler(make_mock_bge_m3())
        with store.pool.connection() as conn:
            h.write_failed(conn, chunk_ids[0], "boom")
            conn.commit()
        with store.pool.connection() as conn:
            s = h.status(conn)
        assert s.total == 2
        assert s.ok == 0
        assert s.failed == 1
        assert s.pending == 1

    def test_two_handlers_independent_per_model(self, store):
        # Same chunks, two derived artifacts. Embedding all but
        # only summarising half — the two status snapshots must
        # report independently.
        _ref_id, chunk_ids = seed_chunks(
            store,
            ["alpha beta", "gamma delta", "epsilon zeta", "eta theta"],
        )
        embed = EmbedHandler(make_mock_bge_m3())
        summ = RakeLemmaHandler(max_keywords=3)

        # Embed everything.
        run_handler_once(embed, store, batch_size=10)
        # Summarize only half.
        run_handler_once(summ, store, batch_size=2)

        with store.pool.connection() as conn:
            s_emb = embed.status(conn)
            s_sum = summ.status(conn)

        assert s_emb == ArtifactStatus(
            name="embed:bge-m3", total=4, ok=4, failed=0, pending=0
        )
        assert s_sum == ArtifactStatus(
            name="summarize:rake-lemma",
            total=4,
            ok=2,
            failed=0,
            pending=2,
        )
        # Defensive — chunk_ids referenced
        assert len(chunk_ids) == 4

    def test_status_ignores_other_models_in_same_table(self, store):
        # Two embedders writing to chunk_embeddings: one set should
        # not poison the other's status.
        _ref_id, [chunk_id] = seed_chunks(store, ["alpha"])

        # Register a second embedder so the FK on
        # chunk_embeddings.embedder is satisfied.
        with store.pool.connection() as conn:
            conn.execute("INSERT INTO embedders (name, dim) VALUES ('alt-emb', 1024)")
            conn.commit()

        h_main = EmbedHandler(make_mock_bge_m3())
        h_alt = EmbedHandler(MockEmbedder(dim=1024, model="alt-emb"))

        # Embed the chunk with main only.
        vec = h_main.process(ChunkRow(chunk_id=chunk_id, text="alpha"))
        with store.pool.connection() as conn:
            h_main.write_ok(conn, chunk_id, vec)
            conn.commit()

        with store.pool.connection() as conn:
            s_main = h_main.status(conn)
            s_alt = h_alt.status(conn)

        # Main: 1 / 1 / 0 / 0; alt: 1 / 0 / 0 / 1. The other model's
        # row must not bleed into either count.
        assert s_main.ok == 1 and s_main.pending == 0
        assert s_alt.ok == 0 and s_alt.pending == 1
