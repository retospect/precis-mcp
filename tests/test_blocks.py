"""Block CRUD tests against an ephemeral migrated postgres."""

from __future__ import annotations

import psycopg
import pytest

from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.store import BlockInsert, Store


def _paper_ref(store: Store, slug: str = "wang2020state") -> int:
    cid = store.ensure_corpus("default")
    ref = store.insert_ref(
        corpus_id=cid,
        kind="paper",
        slug=slug,
        title="Wang 2020 — State of the art",
    )
    return ref.id


# ---------------------------------------------------------------------------
# insert_blocks
# ---------------------------------------------------------------------------


class TestInsertBlocks:
    def test_basic_insert(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        embedder = MockEmbedder(dim=1024)
        blocks = [
            BlockInsert(
                pos=0,
                text="The abstract.",
                slug="ABCDE",
                embedding=embedder.embed_one("The abstract."),
            ),
            BlockInsert(
                pos=1,
                text="Introduction goes here.",
                slug="FGHIJ",
                embedding=embedder.embed_one("Introduction goes here."),
            ),
        ]
        result = store.insert_blocks(ref_id, blocks)
        assert len(result) == 2
        assert result[0].pos == 0
        assert result[0].slug == "ABCDE"
        assert result[0].text == "The abstract."
        # Embedding excluded from default fetches but RETURNING includes it.
        assert result[0].embedding is not None
        assert len(result[0].embedding) == 1024
        assert result[1].pos == 1

    def test_empty_list_no_op(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        result = store.insert_blocks(ref_id, [])
        assert result == []
        assert store.count_blocks(ref_id) == 0

    def test_with_meta_and_density(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        blocks = [
            BlockInsert(
                pos=0,
                text="dense methodology block",
                density="dense",
                meta={"section": "methods"},
            ),
        ]
        result = store.insert_blocks(ref_id, blocks)
        assert result[0].density == "dense"
        assert result[0].meta == {"section": "methods"}

    def test_replace_drops_old_blocks(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.insert_blocks(
            ref_id,
            [BlockInsert(pos=0, text="old"), BlockInsert(pos=1, text="older")],
        )
        assert store.count_blocks(ref_id) == 2

        store.insert_blocks(ref_id, [BlockInsert(pos=0, text="new")], replace=True)
        assert store.count_blocks(ref_id) == 1
        block = store.get_block(ref_id, pos=0)
        assert block is not None
        assert block.text == "new"

    def test_in_existing_transaction(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        with store.tx() as conn:
            store.insert_blocks(ref_id, [BlockInsert(pos=0, text="atomic")], conn=conn)
        assert store.count_blocks(ref_id) == 1

    def test_pos_uniqueness_enforced(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.insert_blocks(ref_id, [BlockInsert(pos=0, text="a")])
        with pytest.raises(psycopg.errors.UniqueViolation):
            store.insert_blocks(ref_id, [BlockInsert(pos=0, text="b")])


# ---------------------------------------------------------------------------
# get_block / list_blocks_for_ref
# ---------------------------------------------------------------------------


class TestGetBlock:
    def test_by_pos(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.insert_blocks(ref_id, [BlockInsert(pos=5, text="five")])
        block = store.get_block(ref_id, pos=5)
        assert block is not None
        assert block.text == "five"

    def test_by_slug(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.insert_blocks(ref_id, [BlockInsert(pos=0, text="hi", slug="HELLO")])
        block = store.get_block(ref_id, slug="HELLO")
        assert block is not None
        assert block.slug == "HELLO"

    def test_returns_none_when_missing(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        assert store.get_block(ref_id, pos=99) is None

    def test_requires_exactly_one_locator(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        with pytest.raises(BadInput):
            store.get_block(ref_id)
        with pytest.raises(BadInput):
            store.get_block(ref_id, pos=0, slug="x")

    def test_embedding_excluded_by_default(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        embedder = MockEmbedder(dim=1024)
        store.insert_blocks(
            ref_id,
            [BlockInsert(pos=0, text="x", embedding=embedder.embed_one("x"))],
        )
        block = store.get_block(ref_id, pos=0)
        assert block is not None
        assert block.embedding is None
        block_with = store.get_block(ref_id, pos=0, with_embedding=True)
        assert block_with is not None
        assert block_with.embedding is not None
        assert len(block_with.embedding) == 1024


class TestListBlocks:
    def test_orders_by_pos(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.insert_blocks(
            ref_id,
            [
                BlockInsert(pos=2, text="c"),
                BlockInsert(pos=0, text="a"),
                BlockInsert(pos=1, text="b"),
            ],
        )
        blocks = store.list_blocks_for_ref(ref_id)
        assert [b.text for b in blocks] == ["a", "b", "c"]

    def test_pos_range_inclusive(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.insert_blocks(
            ref_id, [BlockInsert(pos=i, text=f"b{i}") for i in range(10)]
        )
        blocks = store.list_blocks_for_ref(ref_id, pos_range=(3, 5))
        assert [b.pos for b in blocks] == [3, 4, 5]

    def test_count_blocks(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.insert_blocks(
            ref_id, [BlockInsert(pos=i, text=f"x{i}") for i in range(7)]
        )
        assert store.count_blocks(ref_id) == 7


class TestUpdateBlock:
    def test_update_density(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        result = store.insert_blocks(
            ref_id, [BlockInsert(pos=0, text="x", density="medium")]
        )
        store.update_block_density(result[0].id, "dense")
        block = store.get_block(ref_id, pos=0)
        assert block is not None
        assert block.density == "dense"

    def test_update_embedding(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        result = store.insert_blocks(ref_id, [BlockInsert(pos=0, text="x")])
        # Initially no embedding.
        block = store.get_block(ref_id, pos=0, with_embedding=True)
        assert block is not None
        assert block.embedding is None

        embedder = MockEmbedder(dim=1024)
        store.update_block_embedding(result[0].id, embedder.embed_one("x"))
        block = store.get_block(ref_id, pos=0, with_embedding=True)
        assert block is not None
        assert block.embedding is not None
        assert len(block.embedding) == 1024


class TestBlocksMissingEmbeddings:
    def test_filters_by_null(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        embedder = MockEmbedder(dim=1024)
        store.insert_blocks(
            ref_id,
            [
                BlockInsert(pos=0, text="has", embedding=embedder.embed_one("has")),
                BlockInsert(pos=1, text="missing"),
                BlockInsert(pos=2, text="missing2"),
            ],
        )
        missing = store.blocks_missing_embeddings(kind="paper")
        assert len(missing) == 2
        assert {b.text for b in missing} == {"missing", "missing2"}

    def test_skips_deleted_refs(self, store: Store) -> None:
        ref_id = _paper_ref(store, slug="alive")
        store.insert_blocks(ref_id, [BlockInsert(pos=0, text="x")])

        cid = store.ensure_corpus("default")
        dead = store.insert_ref(corpus_id=cid, kind="paper", slug="dead", title="Dead")
        store.insert_blocks(dead.id, [BlockInsert(pos=0, text="y")])
        store.soft_delete_ref(dead.id)

        missing = store.blocks_missing_embeddings(kind="paper")
        assert len(missing) == 1
        assert missing[0].text == "x"

    def test_kind_filter(self, store: Store) -> None:
        cid = store.ensure_corpus("default")
        paper = store.insert_ref(corpus_id=cid, kind="paper", slug="p1", title="P")
        memory = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title="M")
        store.insert_blocks(paper.id, [BlockInsert(pos=0, text="paper text")])
        store.insert_blocks(memory.id, [BlockInsert(pos=0, text="mem text")])

        paper_only = store.blocks_missing_embeddings(kind="paper")
        assert len(paper_only) == 1
        assert paper_only[0].text == "paper text"


class TestCascade:
    def test_hard_delete_ref_removes_blocks(self, store: Store) -> None:
        ref_id = _paper_ref(store)
        store.insert_blocks(ref_id, [BlockInsert(pos=0, text="x")])
        # Use raw SQL since hard_delete_ref isn't on Store yet; test the
        # FK ON DELETE CASCADE we declared in the migration.
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM refs WHERE id = %s", (ref_id,))
        assert store.count_blocks(ref_id) == 0
