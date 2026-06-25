"""Store CRUD tests against an ephemeral migrated postgres."""

from __future__ import annotations

import pytest

from precis.errors import BadInput, NotFound
from precis.store import Store, Tag

# ---------------------------------------------------------------------------
# app_state (settings)
# ---------------------------------------------------------------------------
#
# v2 dropped the v1 ``system`` table and replaced it with ``app_state``
# (now part of the sealed ``0001_initial.sql`` per ADR 0019).
# ``get_setting``/``set_setting`` are now
# backed by a real SELECT and an ``ON CONFLICT … DO UPDATE`` upsert.
# The tests below exercise the round-trip against an ephemeral migrated
# DB — the ``store`` fixture in conftest applies all migrations before
# yielding the Store, so this also asserts the migration shipped a
# usable table. ``ensure_corpus``/``get_corpus`` were deleted entirely
# in v2. embedding_dim now reads ``embedders.dim`` directly from the
# registry table; covered by test_embedding_dim_reads_default below.


def test_get_setting_missing_returns_none(store: Store) -> None:
    assert store.get_setting("nope.never.set") is None


def test_set_setting_roundtrip(store: Store) -> None:
    store.set_setting("corpus.oracle.version", "6000000000000")
    assert store.get_setting("corpus.oracle.version") == "6000000000000"


def test_set_setting_upserts(store: Store) -> None:
    store.set_setting("corpus.oracle.sha256", "old-sha")
    store.set_setting("corpus.oracle.sha256", "new-sha")
    assert store.get_setting("corpus.oracle.sha256") == "new-sha"


def test_set_setting_empty_string_preserved(store: Store) -> None:
    # Empty string is a distinct value from NULL/missing — important for
    # the "row exists with empty value" case (e.g. an oracle dir whose
    # sha256 didn't compute). value is NOT NULL so this is the floor.
    store.set_setting("k", "")
    assert store.get_setting("k") == ""


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


def test_list_refs_order_by_whitelist(store: Store) -> None:
    store.insert_ref(kind="memory", slug=None, title="banana")
    store.insert_ref(kind="memory", slug=None, title="apple")
    store.insert_ref(kind="memory", slug=None, title="cherry")

    by_title = store.list_refs(kind="memory", order_by="title_asc")
    assert [r.title for r in by_title] == ["apple", "banana", "cherry"]

    by_title_desc = store.list_refs(kind="memory", order_by="title_desc")
    assert [r.title for r in by_title_desc] == ["cherry", "banana", "apple"]

    by_id = store.list_refs(kind="memory", order_by="id_asc")
    assert [r.id for r in by_id] == sorted(r.id for r in by_id)


def test_touch_viewed_orders_most_recent_first(store: Store) -> None:
    # last_viewed_at drives the drafts list's most-recently-opened order:
    # never-opened refs fall to the bottom, the most recent open floats up.
    a = store.insert_ref(kind="memory", slug=None, title="a")
    b = store.insert_ref(kind="memory", slug=None, title="b")
    store.insert_ref(kind="memory", slug=None, title="c")  # never opened

    store.touch_viewed(a.id)
    store.touch_viewed(b.id)  # b opened last → first

    viewed = store.list_refs(kind="memory", order_by="viewed_desc")
    # b (last opened), a (opened), then c (never opened, NULLS LAST).
    assert [r.title for r in viewed[:2]] == ["b", "a"]
    assert viewed[-1].title == "c"


def test_list_refs_unknown_order_by_falls_back(store: Store) -> None:
    # A stale/garbage order_by must not 500 — it falls back to the
    # default (updated_desc) rather than interpolating into the SQL.
    store.insert_ref(kind="memory", slug=None, title="x")
    refs = store.list_refs(kind="memory", order_by="; DROP TABLE refs --")
    assert len(refs) == 1


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
#
# Tag CRUD tests live in tests/test_phase_boundary.py for now —
# Phase 3 of the storage-v2 rewrite has not yet ported add_tag /
# remove_tag / tags_for / has_tag / find_first_meta_for_open_tag
# from the v1 three-table model to the v2 unified tags+ref_tags/
# chunk_tags model. The boundary test pins which methods raise
# NotImplementedError so we notice when Phase 3 finishes; the
# real CRUD tests get rewritten alongside that work (using
# has_tag, the v2 unified probe, in place of v1 has_flag).


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
