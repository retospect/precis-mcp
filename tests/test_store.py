"""Store CRUD tests against an ephemeral migrated postgres."""

from __future__ import annotations

import pytest

from precis.errors import BadInput, NotFound
from precis.store import Store, Tag

# ---------------------------------------------------------------------------
# system / corpus
# ---------------------------------------------------------------------------
#
# v2 dropped both the ``system`` key-value table and the ``corpuses``
# table. ``get_setting``/``set_setting`` are stubbed to no-op
# (return None / pass) in v2; tests that exercised the v1 system table
# have been removed. ``ensure_corpus``/``get_corpus`` were deleted
# entirely. embedding_dim now reads ``embedders.dim`` directly from
# the registry table; covered by test_embedding_dim_reads_default
# below.


# ---------------------------------------------------------------------------
# refs CRUD
# ---------------------------------------------------------------------------


def test_insert_numeric_kind(store: Store) -> None:
    ref = store.insert_ref(
        kind="memory",
        slug=None,
        title="hello world",
    )
    assert ref.id > 0
    assert ref.kind == "memory"
    assert ref.slug is None
    assert ref.title == "hello world"
    assert ref.public_id == str(ref.id)


def test_insert_numeric_kind_rejects_slug(store: Store) -> None:
    with pytest.raises(BadInput, match="numeric"):
        store.insert_ref(
            kind="memory",
            slug="not-allowed",
            title="x",
        )


def test_insert_slug_kind_requires_slug(store: Store) -> None:
    with pytest.raises(BadInput, match="slug-addressed"):
        store.insert_ref(
            kind="paper",
            slug=None,
            title="x",
        )


def test_insert_slug_kind(store: Store) -> None:
    ref = store.insert_ref(
        kind="paper",
        slug="wang2020state",
        title="Wang 2020",
        meta={"doi": "10.1/x"},
    )
    assert ref.slug == "wang2020state"
    assert ref.public_id == "wang2020state"
    assert ref.meta == {"doi": "10.1/x"}


def test_get_ref_numeric(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="findme")
    fetched = store.get_ref(kind="memory", id=ref.id)
    assert fetched is not None
    assert fetched.id == ref.id
    assert fetched.title == "findme"


def test_get_ref_slug(store: Store) -> None:
    store.insert_ref(kind="paper", slug="abc", title="Paper A")
    fetched = store.get_ref(kind="paper", id="abc")
    assert fetched is not None
    assert fetched.slug == "abc"


def test_get_ref_returns_none_when_missing(store: Store) -> None:
    assert store.get_ref(kind="memory", id=99999) is None


def test_update_ref_title_and_meta(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="v1", meta={"a": 1})
    updated = store.update_ref(ref.id, title="v2", meta_patch={"b": 2})
    assert updated.title == "v2"
    assert updated.meta == {"a": 1, "b": 2}


def test_update_ref_missing_raises(store: Store) -> None:
    with pytest.raises(NotFound):
        store.update_ref(99999, title="nope")


def test_soft_delete(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="bye")
    store.soft_delete_ref(ref.id)

    assert store.get_ref(kind="memory", id=ref.id) is None
    found = store.get_ref(kind="memory", id=ref.id, include_deleted=True)
    assert found is not None
    assert found.deleted_at is not None


def test_list_refs_filters(store: Store) -> None:
    store.insert_ref(kind="memory", slug=None, title="m1")
    store.insert_ref(kind="memory", slug=None, title="m2")
    store.insert_ref(kind="todo", slug=None, title="t1")

    memories = store.list_refs(kind="memory")
    assert len(memories) == 2
    todos = store.list_refs(kind="todo")
    assert len(todos) == 1
    everything = store.list_refs()
    assert len(everything) == 3


def test_search_refs_lexical(store: Store) -> None:
    store.insert_ref(
        kind="memory",
        slug=None,
        title="nitrate reduction on copper electrodes",
    )
    store.insert_ref(
        kind="memory",
        slug=None,
        title="something else entirely",
    )
    hits = store.search_refs_lexical(q="nitrate copper", kind="memory")
    assert len(hits) == 1
    assert "nitrate" in hits[0][0].title


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------


def test_tags_add_and_list(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="x")

    store.add_tag(ref.id, Tag.closed("STATUS", "doing"))
    store.add_tag(ref.id, Tag.flag("pinned"))
    store.add_tag(ref.id, Tag.open("topic-x"))

    tags = store.tags_for(ref.id)
    by_ns = {t.namespace: t for t in tags}
    assert by_ns["closed"].prefix == "STATUS"
    assert by_ns["closed"].value == "doing"
    assert by_ns["flag"].value == "pinned"
    assert by_ns["open"].value == "topic-x"


def test_tags_replace_prefix(store: Store) -> None:
    """replace_prefix=True removes existing closed tag with same prefix."""
    ref = store.insert_ref(kind="memory", slug=None, title="x")

    store.add_tag(ref.id, Tag.closed("CONFIDENCE", "tentative"), replace_prefix=True)
    store.add_tag(ref.id, Tag.closed("CONFIDENCE", "certain"), replace_prefix=True)

    tags = store.tags_for(ref.id)
    confidences = [
        t for t in tags if t.namespace == "closed" and t.prefix == "CONFIDENCE"
    ]
    assert len(confidences) == 1
    assert confidences[0].value == "certain"


def test_tags_remove(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="x")

    store.add_tag(ref.id, Tag.flag("pinned"))
    assert store.has_flag(ref.id, "pinned") is True

    store.remove_tag(ref.id, Tag.flag("pinned"))
    assert store.has_flag(ref.id, "pinned") is False


def test_tags_idempotent_add(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="x")
    store.add_tag(ref.id, Tag.open("dup"))
    store.add_tag(ref.id, Tag.open("dup"))  # ON CONFLICT DO NOTHING
    tags = store.tags_for(ref.id)
    assert len([t for t in tags if t.value == "dup"]) == 1


def test_tag_parse() -> None:
    assert Tag.parse("STATUS:done") == Tag.closed("STATUS", "done")
    assert Tag.parse("topic-x") == Tag.open("topic-x")
    assert Tag.parse("XYZ", known_flags=frozenset({"XYZ"})) == Tag.flag("XYZ")
    # not all-uppercase prefix → open
    assert Tag.parse("kind:note").namespace == "open"


def test_tag_str() -> None:
    assert str(Tag.closed("STATUS", "done")) == "STATUS:done"
    assert str(Tag.flag("pinned")) == "pinned"
    assert str(Tag.open("topic-x")) == "topic-x"


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------


def test_tx_rolls_back(store: Store) -> None:
    try:
        with store.tx() as conn:
            store.insert_ref(
                kind="memory",
                slug=None,
                title="will roll back",
                conn=conn,
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    refs = store.list_refs(kind="memory")
    assert refs == []
