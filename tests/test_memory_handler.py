"""Tests for :class:`precis.handlers.memory.MemoryHandler`.

Originally Phase 6 (journal kinds, see CHANGELOG).  Split out of
``test_phase6_journal.py`` so memory and conversation each have their
own test file.

Mocks acatome-store at the ``precis.handlers._ref_base._get_store``
boundary.  No PG / SQLAlchemy involvement.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from precis import server
from precis.handlers.memory import MemoryHandler, _slugify
from precis.protocol import ErrorCode, PrecisError
from precis.registry import KINDS, SCHEMES

# ---------------------------------------------------------------------------
# Shared store stubs
# ---------------------------------------------------------------------------


def _make_store(refs=None, blocks=None):
    """Build a MagicMock store that behaves like acatome-store.

    Only the surface the journal handlers touch is stubbed; anything
    else returns the default MagicMock, which will raise on use — by
    design, so tests fail loudly if a handler reaches outside the
    documented surface.
    """
    store = MagicMock()
    _refs = {r["slug"]: r for r in (refs or [])}
    _blocks = blocks or []

    def _get(ident):
        if ident in _refs:
            return _refs[ident]
        for r in _refs.values():
            if r.get("ref_id") == ident or r.get("id") == ident:
                return r
        return None

    def _get_blocks(slug, **_kwargs):
        return [b for b in _blocks if b.get("slug") == slug]

    store.get.side_effect = _get
    store.get_blocks.side_effect = _get_blocks
    store.get_toc.return_value = []
    store.get_links.return_value = []
    store.get_link_count.return_value = {}
    store.create_ref.return_value = 42
    store.update_ref_metadata.return_value = None
    return store


def _memory_ref(
    slug="memory:cluster-db-user",
    title="cluster db user",
    tags=None,
    first_seen_at="2026-04-21T10:00:00",
    ref_id=1,
    deleted=False,
):
    meta = {"created_at": first_seen_at}
    if deleted:
        meta["deleted"] = True
    return {
        "slug": slug,
        "title": title,
        "ref_id": ref_id,
        "id": ref_id,
        "corpus_id": "memories",
        "tags": tags or [],
        "first_seen_at": first_seen_at,
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self):
        assert _slugify("Cluster DB user") == "memory:cluster-db-user"

    def test_special_chars_stripped(self):
        assert _slugify("what is Π?") == "memory:what-is"

    def test_long_title_truncated(self):
        out = _slugify("a" * 200)
        assert out.startswith("memory:")
        assert len(out) <= len("memory:") + 60

    def test_empty_returns_empty(self):
        assert _slugify("") == ""
        assert _slugify("!!!") == ""


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestMemoryRegistration:
    @classmethod
    def setup_class(cls):
        import precis.registry as reg

        reg._discover()

    def test_memory_kind_registered(self):
        assert "memory" in KINDS
        assert "memory" in SCHEMES

    def test_memory_is_free(self):
        assert KINDS["memory"].spec.cost_hint == "free"

    def test_memory_has_no_env_requirement(self):
        # Journal kinds are state-backed via acatome-store, not
        # env-gated.  visibility is driven purely by whether the
        # store imports successfully at registration time.
        assert KINDS["memory"].spec.requires == []


# ---------------------------------------------------------------------------
# Read surface
# ---------------------------------------------------------------------------


class TestMemoryRead:
    _PATCH_STORE = "precis.handlers._ref_base._get_store"

    def test_bare_scheme_lists_overview(self):
        store = _make_store(refs=[_memory_ref()])
        h = MemoryHandler()
        h._query_corpus_refs = lambda _s: [_memory_ref()]
        out = h._list_overview(store)
        assert "1 memories" in out

    def test_bare_scheme_no_memories(self):
        store = _make_store(refs=[])
        h = MemoryHandler()
        h._query_corpus_refs = lambda _s: []
        out = h._list_overview(store)
        assert "No memories yet" in out
        assert "put(type='memory'" in out

    def test_recent_view(self):
        refs = [
            _memory_ref(slug=f"memory:item-{i}", title=f"Item {i}") for i in range(5)
        ]
        store = _make_store(refs=refs)
        h = MemoryHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._read_recent(store, limit=3)
        assert "3 recent memories (of 5 total)" in out
        assert "memory:item-0" in out
        assert "memory:item-2" in out

    def test_tags_view(self):
        refs = [
            _memory_ref(slug="memory:a", tags=["python", "db"]),
            _memory_ref(slug="memory:b", tags=["python"]),
            _memory_ref(slug="memory:c", tags=["db"]),
        ]
        store = _make_store(refs=refs)
        h = MemoryHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._read_tags(store)
        assert "tags" in out
        assert "python" in out
        # Python appears twice, db appears twice — both should be counted.
        assert "2" in out

    def test_tags_empty(self):
        store = _make_store(refs=[])
        h = MemoryHandler()
        h._query_corpus_refs = lambda _s: []
        out = h._read_tags(store)
        assert "No tagged memories" in out

    def test_deleted_memories_excluded_from_recent(self):
        refs = [
            _memory_ref(slug="memory:alive"),
            _memory_ref(slug="memory:deleted", deleted=True),
        ]
        store = _make_store(refs=refs)
        h = MemoryHandler()
        # Simulate the filter that _query_corpus_refs does.
        h._query_corpus_refs = lambda _s: [
            r for r in refs if not r["meta"].get("deleted")
        ]
        out = h._read_recent(store)
        assert "memory:alive" in out
        assert "memory:deleted" not in out


class TestTagHydration:
    """Regression: ``Ref.tags`` is a JSON-string column, not an ORM
    relationship.  A previous implementation iterated ``r.tags``
    directly and produced character-by-character output
    (``tags: [, ", s, m, o, k, e, ...]``).  These tests pin the
    JSON-string decode path that the live store actually returns.
    Fixture tags that look like real ``Ref.to_dict()`` output drive
    the regression.
    """

    def test_overview_renders_json_string_tags_as_names(self):
        # Mimic the raw shape ``Ref.to_dict()`` emits: tags as a JSON
        # string, not a Python list.  Before the fix the join below
        # would hit characters and render ``tags: [, ", u, r, g, ...]``.
        ref = _memory_ref(slug="memory:demo", tags=[])
        ref["tags"] = '["urgent", "smoke-test"]'
        h = MemoryHandler()
        store = _make_store(refs=[ref])
        out = h._read_overview(store, ref)
        assert "tags: urgent, smoke-test" in out
        # Guard: the bug symptom must not be present.
        assert "tags: [" not in out
        assert "tags: , " not in out

    def test_overview_handles_unparseable_tags_silently(self):
        # A malformed tags column must not explode the overview — the
        # defensive parse in ``_parse_tags`` returns ``[]`` so the
        # ``if tags`` guard skips the line entirely.
        ref = _memory_ref(slug="memory:broken", tags=[])
        ref["tags"] = "not-json-at-all"
        h = MemoryHandler()
        store = _make_store(refs=[ref])
        out = h._read_overview(store, ref)
        assert "tags:" not in out  # no render, no crash

    def test_overview_handles_none_tags(self):
        # ``Ref.tags`` can be NULL when no tags have ever been set.
        ref = _memory_ref(slug="memory:bare")
        ref["tags"] = None
        h = MemoryHandler()
        store = _make_store(refs=[ref])
        out = h._read_overview(store, ref)
        assert "tags:" not in out

    def test_overview_accepts_already_parsed_list(self):
        # Test fixtures commonly pass tags as a plain list; both the
        # live-store and the unit-test paths must render identically.
        ref = _memory_ref(slug="memory:list", tags=["urgent", "smoke-test"])
        h = MemoryHandler()
        store = _make_store(refs=[ref])
        out = h._read_overview(store, ref)
        assert "tags: urgent, smoke-test" in out


# ---------------------------------------------------------------------------
# Write surface
# ---------------------------------------------------------------------------


class TestMemoryWrite:
    _PATCH_STORE = "precis.handlers.memory._get_store"

    def test_append_requires_text(self):
        store = _make_store()
        with patch(self._PATCH_STORE, return_value=store):
            h = MemoryHandler()
            with pytest.raises(PrecisError) as exc:
                h.put(path="", selector=None, text="", mode="append")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_append_derives_slug_from_title(self):
        store = _make_store()
        with patch(self._PATCH_STORE, return_value=store):
            h = MemoryHandler()
            out = h.put(
                path="",
                selector=None,
                text="The DB user is cluster_app.",
                mode="append",
                title="Cluster DB user",
            )
        assert "memory:cluster-db-user" in out
        store.create_ref.assert_called_once()
        kwargs = store.create_ref.call_args.kwargs
        assert kwargs["slug"] == "memory:cluster-db-user"
        assert kwargs["corpus_id"] == "memories"
        assert kwargs["blocks"][0]["text"] == "The DB user is cluster_app."

    def test_append_uses_explicit_slug(self):
        store = _make_store()
        with patch(self._PATCH_STORE, return_value=store):
            h = MemoryHandler()
            out = h.put(
                path="memory:my-slug",
                selector=None,
                text="content",
                mode="append",
            )
        assert "memory:my-slug" in out
        assert store.create_ref.call_args.kwargs["slug"] == "memory:my-slug"

    def test_append_missing_title_and_path_fails(self):
        store = _make_store()
        with patch(self._PATCH_STORE, return_value=store):
            h = MemoryHandler()
            # Text is emoji-only → slugify returns empty.
            with pytest.raises(PrecisError) as exc:
                h.put(path="", selector=None, text="🧠🧠🧠", mode="append")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_append_passes_tags(self):
        store = _make_store()
        with patch(self._PATCH_STORE, return_value=store):
            h = MemoryHandler()
            h.put(
                path="memory:foo",
                selector=None,
                text="x",
                mode="append",
                tags=["cluster", "db"],
            )
        assert store.create_ref.call_args.kwargs["tags"] == ["cluster", "db"]

    def test_append_string_tags_split_on_commas(self):
        store = _make_store()
        with patch(self._PATCH_STORE, return_value=store):
            h = MemoryHandler()
            h.put(
                path="memory:foo",
                selector=None,
                text="x",
                mode="append",
                tags="a, b,c",
            )
        assert store.create_ref.call_args.kwargs["tags"] == ["a", "b", "c"]

    def test_append_duplicate_slug_raises_id_ambiguous(self):
        store = _make_store()
        store.create_ref.side_effect = ValueError("Slug already exists: memory:foo")
        with patch(self._PATCH_STORE, return_value=store):
            h = MemoryHandler()
            with pytest.raises(PrecisError) as exc:
                h.put(
                    path="memory:foo",
                    selector=None,
                    text="x",
                    mode="append",
                )
        assert exc.value.code == ErrorCode.ID_AMBIGUOUS

    def test_delete_marks_meta(self):
        ref = _memory_ref()
        store = _make_store(refs=[ref])
        with patch(self._PATCH_STORE, return_value=store):
            h = MemoryHandler()
            out = h.put(
                path="memory:cluster-db-user",
                selector=None,
                text="",
                mode="delete",
            )
        assert "soft-deleted" in out
        # update_ref_metadata was called with deleted=True.
        call_args = store.update_ref_metadata.call_args
        meta_arg = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("metadata")
        )
        # The second positional (or metadata kwarg) is the meta dict.
        assert meta_arg is not None
        assert meta_arg.get("deleted") is True

    def test_delete_unknown_raises_id_not_found(self):
        store = _make_store()
        store.get.side_effect = lambda _i: None
        with patch(self._PATCH_STORE, return_value=store):
            h = MemoryHandler()
            with pytest.raises(PrecisError) as exc:
                h.put(
                    path="memory:ghost",
                    selector=None,
                    text="",
                    mode="delete",
                )
        assert exc.value.code == ErrorCode.ID_NOT_FOUND


# ---------------------------------------------------------------------------
# Server URI dispatch — type='memory'
# ---------------------------------------------------------------------------


class TestMemoryURIDispatch:
    def test_type_memory_builds_memory_uri(self):
        assert server._to_uri("my-slug", kind="memory") == "memory:my-slug"
