"""Chunk CRUD tests against an ephemeral migrated postgres."""

from __future__ import annotations

import psycopg
import pytest

from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.store import ChunkInsert, Store


def _paper_ref(store: Store, slug: str = "wang2020state") -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title="Wang 2020 — State of the art",
    )
    return ref.id


# ---------------------------------------------------------------------------
# insert_chunks
# ---------------------------------------------------------------------------


class TestInsertBlocks:
    def test_basic_insert(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        embedder = MockEmbedder(dim=1024)
        blocks = [
            ChunkInsert(
                ord=0,
                text="The abstract.",
                slug="ABCDE",
                embedding=embedder.embed_one("The abstract."),
            ),
            ChunkInsert(
                ord=1,
                text="Introduction goes here.",
                slug="FGHIJ",
                embedding=embedder.embed_one("Introduction goes here."),
            ),
        ]
        result = store.chunks.insert_chunks(ref_id, blocks)
        assert len(result) == 2
        assert result[0].ord == 0
        assert result[0].slug == "ABCDE"
        assert result[0].text == "The abstract."
        # Embedding excluded from default fetches but RETURNING includes it.
        assert result[0].embedding is not None
        assert len(result[0].embedding) == 1024
        assert result[1].ord == 1

    def test_empty_list_no_op(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        result = store.chunks.insert_chunks(ref_id, [])
        assert result == []
        assert store.chunks.count_chunks(ref_id) == 0

    def test_with_meta_and_density(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        blocks = [
            ChunkInsert(
                ord=0,
                text="dense methodology block",
                density="dense",
                meta={"section": "methods"},
            ),
        ]
        result = store.chunks.insert_chunks(ref_id, blocks)
        assert result[0].density == "dense"
        assert result[0].meta == {"section": "methods"}

    def test_replace_drops_old_blocks(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.chunks.insert_chunks(
            ref_id,
            [ChunkInsert(ord=0, text="old"), ChunkInsert(ord=1, text="older")],
        )
        assert store.chunks.count_chunks(ref_id) == 2

        store.chunks.insert_chunks(
            ref_id, [ChunkInsert(ord=0, text="new")], replace=True
        )
        assert store.chunks.count_chunks(ref_id) == 1
        block = store.chunks.get_chunk(ref_id, pos=0)
        assert block is not None
        assert block.text == "new"

    def test_in_existing_transaction(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        with store.tx() as conn:
            store.chunks.insert_chunks(
                ref_id, [ChunkInsert(ord=0, text="atomic")], conn=conn
            )
        assert store.chunks.count_chunks(ref_id) == 1

    def test_pos_uniqueness_enforced(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.chunks.insert_chunks(ref_id, [ChunkInsert(ord=0, text="a")])
        with pytest.raises(psycopg.errors.UniqueViolation):
            store.chunks.insert_chunks(ref_id, [ChunkInsert(ord=0, text="b")])


# ---------------------------------------------------------------------------
# get_chunk / list_chunks_for_ref
# ---------------------------------------------------------------------------


class TestGetBlock:
    def test_by_pos(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.chunks.insert_chunks(ref_id, [ChunkInsert(ord=5, text="five")])
        block = store.chunks.get_chunk(ref_id, pos=5)
        assert block is not None
        assert block.text == "five"

    def test_by_slug(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.chunks.insert_chunks(
            ref_id, [ChunkInsert(ord=0, text="hi", slug="HELLO")]
        )
        block = store.chunks.get_chunk(ref_id, slug="HELLO")
        assert block is not None
        assert block.slug == "HELLO"

    def test_returns_none_when_missing(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        assert store.chunks.get_chunk(ref_id, pos=99) is None

    def test_requires_exactly_one_locator(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        with pytest.raises(BadInput):
            store.chunks.get_chunk(ref_id)
        with pytest.raises(BadInput):
            store.chunks.get_chunk(ref_id, pos=0, slug="x")

    def test_embedding_excluded_by_default(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        embedder = MockEmbedder(dim=1024)
        store.chunks.insert_chunks(
            ref_id,
            [ChunkInsert(ord=0, text="x", embedding=embedder.embed_one("x"))],
        )
        block = store.chunks.get_chunk(ref_id, pos=0)
        assert block is not None
        assert block.embedding is None
        block_with = store.chunks.get_chunk(ref_id, pos=0, with_embedding=True)
        assert block_with is not None
        assert block_with.embedding is not None
        assert len(block_with.embedding) == 1024


class TestListBlocks:
    def test_orders_by_pos(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.chunks.insert_chunks(
            ref_id,
            [
                ChunkInsert(ord=2, text="c"),
                ChunkInsert(ord=0, text="a"),
                ChunkInsert(ord=1, text="b"),
            ],
        )
        blocks = store.chunks.list_chunks_for_ref(ref_id)
        assert [b.text for b in blocks] == ["a", "b", "c"]

    def test_pos_range_inclusive(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.chunks.insert_chunks(
            ref_id, [ChunkInsert(ord=i, text=f"b{i}") for i in range(10)]
        )
        blocks = store.chunks.list_chunks_for_ref(ref_id, pos_range=(3, 5))
        assert [b.ord for b in blocks] == [3, 4, 5]

    def test_count_blocks(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.chunks.insert_chunks(
            ref_id, [ChunkInsert(ord=i, text=f"x{i}") for i in range(7)]
        )
        assert store.chunks.count_chunks(ref_id) == 7


class TestUpdateBlock:
    def test_update_density(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        result = store.chunks.insert_chunks(
            ref_id, [ChunkInsert(ord=0, text="x", density="medium")]
        )
        store.chunks.update_chunk_density(result[0].id, "dense")
        block = store.chunks.get_chunk(ref_id, pos=0)
        assert block is not None
        assert block.density == "dense"

    def test_update_embedding(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        result = store.chunks.insert_chunks(ref_id, [ChunkInsert(ord=0, text="x")])
        # Initially no embedding.
        block = store.chunks.get_chunk(ref_id, pos=0, with_embedding=True)
        assert block is not None
        assert block.embedding is None

        embedder = MockEmbedder(dim=1024)
        store.chunks.update_chunk_embedding(result[0].id, embedder.embed_one("x"))
        block = store.chunks.get_chunk(ref_id, pos=0, with_embedding=True)
        assert block is not None
        assert block.embedding is not None
        assert len(block.embedding) == 1024


class TestBlocksMissingEmbeddings:
    def test_filters_by_null(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        embedder = MockEmbedder(dim=1024)
        store.chunks.insert_chunks(
            ref_id,
            [
                ChunkInsert(ord=0, text="has", embedding=embedder.embed_one("has")),
                ChunkInsert(ord=1, text="missing"),
                ChunkInsert(ord=2, text="missing2"),
            ],
        )
        missing = store.chunks.chunks_missing_embeddings(kind="paper")
        assert len(missing) == 2
        assert {b.text for b in missing} == {"missing", "missing2"}

    def test_skips_deleted_refs(self, store: Store) -> None:
        ref_id = _paper_ref(store, slug="alive")
        store.chunks.insert_chunks(ref_id, [ChunkInsert(ord=0, text="x")])
        dead = store.insert_ref(kind="paper", slug="dead", title="Dead")
        store.chunks.insert_chunks(dead.id, [ChunkInsert(ord=0, text="y")])
        store.soft_delete_ref(dead.id)

        missing = store.chunks.chunks_missing_embeddings(kind="paper")
        assert len(missing) == 1
        assert missing[0].text == "x"

    def test_kind_filter(self, store: Store) -> None:
        paper = store.insert_ref(kind="paper", slug="p1", title="P")
        memory = store.insert_ref(kind="memory", slug=None, title="M")
        store.chunks.insert_chunks(paper.id, [ChunkInsert(ord=0, text="paper text")])
        store.chunks.insert_chunks(memory.id, [ChunkInsert(ord=0, text="mem text")])

        paper_only = store.chunks.chunks_missing_embeddings(kind="paper")
        assert len(paper_only) == 1
        assert paper_only[0].text == "paper text"


class TestCountRefsMatchingLexical:
    """``count_chunks_lexical(kinds=…, distinct_refs=True)`` — the
    ``/drive`` "showing N of ~K" denominator (formerly the standalone
    ``count_refs_matching_lexical``, folded in to keep the WHERE-clause
    guards in one place)."""

    def test_counts_distinct_matching_refs_across_kinds(self, store: Store) -> None:
        ref_a = _paper_ref(store, slug="lex-a")
        ref_b = _paper_ref(store, slug="lex-b")
        # Two body chunks in the same ref shouldn't double-count the ref.
        store.chunks.insert_chunks(
            ref_a,
            [
                ChunkInsert(ord=0, text="a study of xenocryst formation"),
                ChunkInsert(ord=1, text="more on xenocryst growth rates"),
            ],
        )
        store.chunks.insert_chunks(
            ref_b, [ChunkInsert(ord=0, text="xenocryst inclusions in basalt")]
        )
        total = store.chunks.count_chunks_lexical(
            kinds=["paper"], q="xenocryst", distinct_refs=True
        )
        assert total == 2

    def test_no_match_returns_zero(self, store: Store) -> None:
        ref_id = _paper_ref(store, slug="lex-nomatch")
        store.chunks.insert_chunks(
            ref_id, [ChunkInsert(ord=0, text="unrelated content")]
        )
        total = store.chunks.count_chunks_lexical(
            kinds=["paper"], q="xenocryst", distinct_refs=True
        )
        assert total == 0

    def test_empty_kinds_or_blank_q_returns_zero(self, store: Store) -> None:
        # No Python-level early return on the store method (that guard now
        # lives at the /drive call site) — an empty ``kinds`` list or blank
        # ``q`` still resolves to zero matches via plain SQL semantics
        # (``r.kind = ANY('{}')`` and an empty tsquery both match nothing).
        ref_id = _paper_ref(store, slug="lex-empty")
        store.chunks.insert_chunks(ref_id, [ChunkInsert(ord=0, text="xenocryst text")])
        assert (
            store.chunks.count_chunks_lexical(
                kinds=[], q="xenocryst", distinct_refs=True
            )
            == 0
        )
        assert (
            store.chunks.count_chunks_lexical(kinds=["paper"], q="", distinct_refs=True)
            == 0
        )


class TestCascade:
    def test_hard_delete_ref_removes_blocks(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.chunks.insert_chunks(ref_id, [ChunkInsert(ord=0, text="x")])
        # Use raw SQL since hard_delete_ref isn't on Store yet; test the
        # FK ON DELETE CASCADE we declared in the migration. v2 column
        # is ``ref_id`` (formerly ``id``).
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM refs WHERE ref_id = %s", (ref_id,))
        assert store.chunks.count_chunks(ref_id) == 0


class TestChunkSummariesBulk:
    def test_chunk_summaries_bulk_maps_ref_ord_pairs(self, store: Store) -> None:
        ref_a = _paper_ref(store, slug="bulk-a")
        ref_b = _paper_ref(store, slug="bulk-b")
        blocks_a = store.chunks.insert_chunks(
            ref_a,
            [ChunkInsert(ord=0, text="a0"), ChunkInsert(ord=1, text="a1")],
        )
        blocks_b = store.chunks.insert_chunks(ref_b, [ChunkInsert(ord=1, text="b1")])
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
                "VALUES (%s, 'llm-v1', %s, 'ok')",
                (blocks_a[0].id, "gloss for a0"),
            )
            conn.execute(
                "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
                "VALUES (%s, 'llm-v1', %s, 'ok')",
                (blocks_b[0].id, "gloss for b1"),
            )

        out = store.chunks.chunk_summaries_bulk([(ref_a, 0), (ref_b, 1), (ref_a, 99)])
        assert out == {
            (ref_a, 0): "gloss for a0",
            (ref_b, 1): "gloss for b1",
        }

    def test_chunk_summaries_bulk_empty_pairs(self, store: Store) -> None:
        assert store.chunks.chunk_summaries_bulk([]) == {}
