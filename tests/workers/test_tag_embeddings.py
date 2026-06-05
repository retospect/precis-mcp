"""Tests for ``precis.workers.tag_embeddings``.

The tag_embeddings worker mirrors chunk_keywords' lazy-update
shape: claim → embed → write, with re-claim on version mismatch
so a model swap re-embeds the corpus.
"""

from __future__ import annotations

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.tag_embeddings import (
    TAG_EMBEDDINGS_VERSION,
    _slug_for,
    run_tag_embeddings_pass,
)
from tests.workers._helpers import make_mock_bge_m3, seed_ref

# ---------------------------------------------------------------------------
# _slug_for — canonical agent-facing string
# ---------------------------------------------------------------------------


class TestSlugFor:
    def test_closed_axis(self) -> None:
        assert _slug_for("STATUS", "done") == "STATUS:done"
        assert _slug_for("CACHE", "fresh") == "CACHE:fresh"

    def test_open_namespace(self) -> None:
        # OPEN value already carries the lowercase prefix.
        assert _slug_for("OPEN", "topic:co2-capture") == "topic:co2-capture"

    def test_flag_namespace(self) -> None:
        assert _slug_for("FLAG", "pinned") == "pinned"


# ---------------------------------------------------------------------------
# run_tag_embeddings_pass — claim + embed + write loop
# ---------------------------------------------------------------------------


class TestRunPass:
    def test_empty_queue_returns_zero_counts(self, store: Store) -> None:
        result = run_tag_embeddings_pass(store, make_mock_bge_m3(), batch_size=10)
        assert result == {"claimed": 0, "ok": 0, "failed": 0}

    def test_writes_embeddings_for_seeded_tags(self, store: Store) -> None:
        ref_id = seed_ref(store)
        store.add_tag(ref_id, Tag.open("topic:co2-capture"))
        store.add_tag(ref_id, Tag.closed("STATUS", "done"))
        store.add_tag(ref_id, Tag.open("topic:carbon"))

        result = run_tag_embeddings_pass(store, make_mock_bge_m3(), batch_size=10)
        assert result["claimed"] == 3
        assert result["ok"] == 3
        assert result["failed"] == 0

        # Each tag now has a row in tag_embeddings at the current
        # version.
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT namespace, value, version FROM tag_embeddings "
                "ORDER BY namespace, value"
            ).fetchall()
        assert len(rows) == 3
        assert all(r[2] == TAG_EMBEDDINGS_VERSION for r in rows)

    def test_idempotent_within_same_version(self, store: Store) -> None:
        ref_id = seed_ref(store)
        store.add_tag(ref_id, Tag.open("topic:idempotent"))
        emb = make_mock_bge_m3()
        first = run_tag_embeddings_pass(store, emb, batch_size=10)
        assert first["claimed"] == 1
        assert first["ok"] == 1

        # Second pass — version matches; nothing left to claim.
        second = run_tag_embeddings_pass(store, emb, batch_size=10)
        assert second == {"claimed": 0, "ok": 0, "failed": 0}

    def test_stale_version_gets_reclaimed(self, store: Store) -> None:
        ref_id = seed_ref(store)
        store.add_tag(ref_id, Tag.open("topic:stale"))
        emb = make_mock_bge_m3()

        # Initial embed.
        run_tag_embeddings_pass(store, emb, batch_size=10)

        # Manually downgrade the version stamp — simulates a
        # ``TAG_EMBEDDINGS_VERSION`` bump.
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE tag_embeddings SET version = 0 "
                "WHERE namespace = 'OPEN' AND value = 'topic:stale'"
            )
            conn.commit()

        # Re-run — the stale row gets re-claimed and rewritten.
        result = run_tag_embeddings_pass(store, emb, batch_size=10)
        assert result["claimed"] == 1
        assert result["ok"] == 1

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT version FROM tag_embeddings "
                "WHERE namespace = 'OPEN' AND value = 'topic:stale'"
            ).fetchone()
        assert row is not None
        assert row[0] == TAG_EMBEDDINGS_VERSION

    def test_batch_size_limits_claim(self, store: Store) -> None:
        ref_id = seed_ref(store)
        for i in range(5):
            store.add_tag(ref_id, Tag.open(f"topic:batch-{i}"))

        # Claim only 2 per batch.
        result = run_tag_embeddings_pass(store, make_mock_bge_m3(), batch_size=2)
        assert result["claimed"] == 2
        # Second pass picks up the next batch.
        result2 = run_tag_embeddings_pass(store, make_mock_bge_m3(), batch_size=2)
        assert result2["claimed"] == 2
        # Third pass drains the last one.
        result3 = run_tag_embeddings_pass(store, make_mock_bge_m3(), batch_size=2)
        assert result3["claimed"] == 1


# ---------------------------------------------------------------------------
# Store-side: list_all_tags / search_tags_lexical / tag_metadata
# ---------------------------------------------------------------------------


class TestStoreDiscoveryOps:
    def test_list_all_tags_orders_by_count_desc(self, store: Store) -> None:
        ref1 = seed_ref(store)
        ref2 = seed_ref(store)
        store.add_tag(ref1, Tag.open("topic:popular"))
        store.add_tag(ref2, Tag.open("topic:popular"))
        store.add_tag(ref1, Tag.open("topic:singleton"))

        rows = store.list_all_tags()
        assert rows
        # popular (count 2) before singleton (count 1).
        names = [v for (_ns, v, _c) in rows]
        assert names.index("topic:popular") < names.index("topic:singleton")

    def test_list_all_tags_scope_filters_by_kind(self, store: Store) -> None:
        # Memory ref + tag.
        m = store.insert_ref(kind="memory", slug=None, title="m").id
        store.add_tag(m, Tag.open("topic:memory-only"))
        # Paper ref + tag.
        p = store.insert_ref(kind="paper", slug="p", title="p").id
        store.add_tag(p, Tag.open("topic:paper-only"))

        paper_rows = store.list_all_tags(kind="paper")
        names = [v for (_ns, v, _c) in paper_rows]
        assert "topic:paper-only" in names
        assert "topic:memory-only" not in names

    def test_search_tags_lexical(self, store: Store) -> None:
        ref = seed_ref(store)
        store.add_tag(ref, Tag.open("topic:carbon-capture"))
        store.add_tag(ref, Tag.open("project:other"))

        hits = store.search_tags_lexical(q="carbon")
        names = [v for (_ns, v, _c) in hits]
        assert "topic:carbon-capture" in names
        assert "project:other" not in names

    def test_search_tags_lexical_empty_q(self, store: Store) -> None:
        assert store.search_tags_lexical(q="") == []
        assert store.search_tags_lexical(q="   ") == []

    def test_tag_metadata_returns_none_for_missing(self, store: Store) -> None:
        assert store.tag_metadata(namespace="OPEN", value="never-used") is None

    def test_tag_metadata_returns_sample_refs(self, store: Store) -> None:
        p1 = store.insert_ref(kind="paper", slug="aa", title="aa").id
        p2 = store.insert_ref(kind="paper", slug="bb", title="bb").id
        store.add_tag(p1, Tag.open("topic:sampled"))
        store.add_tag(p2, Tag.open("topic:sampled"))

        meta = store.tag_metadata(namespace="OPEN", value="topic:sampled")
        assert meta is not None
        assert meta["count"] == 2
        samples = meta["sample_refs"]
        assert len(samples) == 2
        slugs = {s for (_k, s, _i) in samples}
        assert slugs == {"aa", "bb"}


# ---------------------------------------------------------------------------
# search_tags_semantic — round-trip through write_tag_embedding
# ---------------------------------------------------------------------------


class TestSemanticSearch:
    def test_returns_known_tag_after_embedding(self, store: Store) -> None:
        ref = seed_ref(store)
        store.add_tag(ref, Tag.open("topic:semantic"))
        emb = make_mock_bge_m3()
        run_tag_embeddings_pass(store, emb, batch_size=10)

        # Query with the same tag string — distance should be ~0.
        qvec = emb.embed_one("topic:semantic")
        hits = store.search_tags_semantic(query_vector=qvec, page=1, page_size=5)
        assert hits, "expected at least one semantic hit"
        # The seeded tag should be in the top results.
        names = [v for (_ns, v, _d) in hits]
        assert "topic:semantic" in names

    def test_empty_table_returns_empty(self, store: Store) -> None:
        emb = make_mock_bge_m3()
        qvec = emb.embed_one("anything")
        hits = store.search_tags_semantic(query_vector=qvec, page=1, page_size=5)
        assert hits == []


# ---------------------------------------------------------------------------
# Failure isolation — write failure on one tag doesn't kill the pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("which", [0, 1])
def test_per_tag_write_failure_counts_failed(
    store: Store, which: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = seed_ref(store)
    store.add_tag(ref, Tag.open("topic:ok"))
    store.add_tag(ref, Tag.open("topic:break"))

    orig_write = Store.write_tag_embedding

    def fake_write(self: Store, **kw: object) -> None:
        if kw.get("value") == "topic:break":
            raise RuntimeError("simulated failure")
        return orig_write(self, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Store, "write_tag_embedding", fake_write)
    result = run_tag_embeddings_pass(store, make_mock_bge_m3(), batch_size=10)
    assert result["claimed"] == 2
    assert result["ok"] == 1
    assert result["failed"] == 1
