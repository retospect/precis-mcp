"""Tests for :class:`precis.handlers.conversation.ConversationHandler`.

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
from precis.handlers.conversation import ConversationHandler
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


def _conv_ref(
    slug="conv:2026-04-21-asa",
    title="Asa session",
    first_seen_at="2026-04-21T10:00:00",
    turn_count=3,
    ref_id=10,
    deleted=False,
):
    meta = {"created_at": first_seen_at, "updated_at": first_seen_at}
    if deleted:
        meta["deleted"] = True
    return {
        "slug": slug,
        "title": title,
        "ref_id": ref_id,
        "id": ref_id,
        "corpus_id": "conversations",
        "tags": [],
        "first_seen_at": first_seen_at,
        "_turn_count": turn_count,
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# Read surface
# ---------------------------------------------------------------------------


class TestConversationRead:
    def test_list_overview_empty(self):
        store = _make_store(refs=[])
        h = ConversationHandler()
        h._query_corpus_refs = lambda _s: []
        out = h._list_overview(store)
        assert "No conversations yet" in out

    def test_recent_renders_turn_counts(self):
        refs = [_conv_ref(slug=f"conv:s-{i}", turn_count=i + 1) for i in range(3)]
        store = _make_store(refs=refs)
        h = ConversationHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._read_recent(store)
        assert "conv:s-0" in out
        assert "1 turn" in out
        assert "3 turns" in out

    def test_session_view_renders_full_transcript(self):
        ref = _conv_ref()
        blocks = [
            {
                "slug": ref["slug"],
                "block_index": 0,
                "text": "Hello",
                "section_path": '["user", "2026-04-21T10:00:00"]',
            },
            {
                "slug": ref["slug"],
                "block_index": 1,
                "text": "Hi there",
                "section_path": '["asa", "2026-04-21T10:00:05"]',
            },
        ]
        store = _make_store(refs=[ref], blocks=blocks)
        h = ConversationHandler()
        out = h._read_session(store, ref)
        assert "Hello" in out
        assert "Hi there" in out
        assert "user" in out
        assert "asa" in out

    def test_session_view_empty(self):
        ref = _conv_ref()
        store = _make_store(refs=[ref], blocks=[])
        h = ConversationHandler()
        out = h._read_session(store, ref)
        assert "no turns yet" in out


# ---------------------------------------------------------------------------
# Write surface
# ---------------------------------------------------------------------------


class TestConversationWrite:
    _PATCH_STORE = "precis.handlers.conversation._get_store"

    def test_append_requires_text_and_id(self):
        store = _make_store()
        with patch(self._PATCH_STORE, return_value=store):
            h = ConversationHandler()
            with pytest.raises(PrecisError) as exc:
                h.put(path="conv:x", selector=None, text="", mode="append")
            assert exc.value.code == ErrorCode.PARAM_INVALID

            with pytest.raises(PrecisError) as exc:
                h.put(path="", selector=None, text="hi", mode="append")
            assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_first_append_creates_ref(self):
        store = _make_store(refs=[])
        with patch(self._PATCH_STORE, return_value=store):
            h = ConversationHandler()
            out = h.put(
                path="conv:2026-04-21-asa",
                selector=None,
                text="Hello world",
                mode="append",
                speaker="user",
            )
        assert "Conversation started" in out
        store.create_ref.assert_called_once()
        kwargs = store.create_ref.call_args.kwargs
        assert kwargs["slug"] == "conv:2026-04-21-asa"
        assert kwargs["corpus_id"] == "conversations"
        # section_path carries speaker + timestamp
        sp = kwargs["blocks"][0]["section_path"]
        assert "user" in sp

    def test_append_normalises_bare_slug(self):
        store = _make_store(refs=[])
        with patch(self._PATCH_STORE, return_value=store):
            h = ConversationHandler()
            h.put(
                path="2026-04-21-asa",
                selector=None,
                text="x",
                mode="append",
            )
        kwargs = store.create_ref.call_args.kwargs
        # Bare slugs are normalised with the canonical ``conversation:``
        # prefix (the short ``conv:`` form was retired — see registry
        # cleanup, Apr 2026)
        assert kwargs["slug"] == "conversation:2026-04-21-asa"

    def test_delete_marks_meta(self):
        ref = _conv_ref()
        store = _make_store(refs=[ref])
        with patch(self._PATCH_STORE, return_value=store):
            h = ConversationHandler()
            out = h.put(
                path="conv:2026-04-21-asa",
                selector=None,
                text="",
                mode="delete",
            )
        assert "soft-deleted" in out


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestConversationRegistration:
    @classmethod
    def setup_class(cls):
        import precis.registry as reg

        reg._discover()

    def test_conversation_kind_registered(self):
        assert "conversation" in KINDS
        assert "conversation" in SCHEMES

    def test_no_conv_alias_registered(self):
        # The short ``conv`` alias was removed so agents get a single
        # canonical kind name.  Confirm it does not resolve to any kind.
        from precis.registry import ALIASES

        assert "conv" not in ALIASES


# ---------------------------------------------------------------------------
# Server URI dispatch — type='conversation'
# ---------------------------------------------------------------------------


class TestConversationURIDispatch:
    def test_type_conversation_builds_conversation_uri(self):
        out = server._to_uri("2026-04-21-x", kind="conversation")
        assert out == "conversation:2026-04-21-x"

    def test_bare_conv_slug_needs_prefix(self):
        # Without a ``conversation:`` prefix the classifier falls back
        # to paper.  This is the same rule that applies to every
        # slug-based kind.
        out = server._to_uri("2026-04-21-asa", kind="")
        assert out.startswith("paper:")
