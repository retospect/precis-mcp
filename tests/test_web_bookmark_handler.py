"""Tests for :class:`precis.handlers.web.WebHandler`.

Phase 1 of :doc:`websites-plan`.  Uses a MagicMock store — no DB, no
network.  The :mod:`precis.web_archive` module is patched to return
deterministic ArchiveResult values per test.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from precis.handlers.web import (
    WebHandler,
    _HOST_KINDS,
    _infer_kind,
    _normalise_tags,
    _parse_meta,
    _VALID_KINDS,
)
from precis.protocol import ErrorCode, PrecisError
from precis.web_archive import ArchiveResult, SkipReason

_PATCH_STORE_REF = "precis.handlers._ref_base._get_store"
_PATCH_STORE_WEB = "precis.handlers.web._get_store"
_PATCH_ARCHIVE = "precis.handlers.web.archive_url"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_store(refs=None, blocks=None):
    """Mock acatome-store with web-bookmark-aware helpers."""
    store = MagicMock()
    _refs = {r["slug"]: r for r in (refs or [])}

    def _get(ident):
        if ident in _refs:
            return _refs[ident]
        return None

    store.get.side_effect = _get
    store.get_blocks.return_value = blocks or []
    store.create_ref.return_value = 42
    store.update_ref_metadata.return_value = None
    store.add_block.return_value = "web:example-com-b0001"
    store.update_block_text.return_value = None
    return store


def _bookmark_ref(
    slug: str = "web:example-com",
    url: str = "https://example.com/",
    canonical: str | None = None,
    kind: str = "other",
    tags: list[str] | None = None,
    title: str = "",
    deleted: bool = False,
    wayback_url: str | None = None,
):
    """Build a dict that quacks like ``Ref.to_dict()``."""
    meta = {
        "url": url,
        "canonical_url": canonical or url,
        "kind": kind,
        "status": "ok",
        "captured_at": "2026-04-24T12:00:00Z",
        "wayback_url": wayback_url,
    }
    if deleted:
        meta["deleted"] = True
    return {
        "slug": slug,
        "title": title,
        "ref_id": 1,
        "id": 1,
        "corpus_id": "websites",
        "first_seen_at": "2026-04-24T12:00:00Z",
        "tags": tags or [],
        "meta": meta,
    }


@pytest.fixture
def archived_ok():
    """Patch archive_url to return a successful snapshot."""
    with patch(
        _PATCH_ARCHIVE,
        return_value=ArchiveResult(
            wayback_url="https://web.archive.org/web/20260424/https://example.com/"
        ),
    ) as m:
        yield m


@pytest.fixture
def archive_skipped():
    """Patch archive_url to return a skipped result (e.g. private URL)."""
    with patch(
        _PATCH_ARCHIVE,
        return_value=ArchiveResult(
            skipped_reason=SkipReason.USER_OPTOUT
        ),
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestNormaliseTags:
    def test_list(self):
        assert _normalise_tags(["a", "b"]) == ["a", "b"]

    def test_string_comma_split(self):
        assert _normalise_tags("a, b,c") == ["a", "b", "c"]

    def test_strips_whitespace(self):
        assert _normalise_tags(["  spaced  ", "ok"]) == ["spaced", "ok"]

    def test_drops_empty(self):
        assert _normalise_tags(["", "  ", "real"]) == ["real"]

    def test_none(self):
        assert _normalise_tags(None) == []

    def test_empty_string(self):
        assert _normalise_tags("") == []


class TestInferKind:
    def test_youtube(self):
        assert _infer_kind("https://www.youtube.com/watch?v=abc") == "video"
        assert _infer_kind("https://youtu.be/abc") == "video"

    def test_arxiv(self):
        assert _infer_kind("https://arxiv.org/abs/2301.12345") == "paper"

    def test_github_repo(self):
        assert _infer_kind("https://github.com/org/repo") == "repo"

    def test_github_user_profile(self):
        # Single-segment path on github.com → user profile, not a repo.
        assert _infer_kind("https://github.com/someuser") == "other"

    def test_hackernews(self):
        assert _infer_kind("https://news.ycombinator.com/item?id=1") == "article"

    def test_blog_path(self):
        assert _infer_kind("https://example.com/blog/post-1") == "article"

    def test_default_other(self):
        assert _infer_kind("https://example.com/") == "other"

    def test_empty(self):
        assert _infer_kind("") == "other"

    def test_all_valid_kinds_appear_in_map(self):
        # Safety: every kind emitted by _infer_kind must be in _VALID_KINDS.
        for _suffix, kind in _HOST_KINDS:
            assert kind in _VALID_KINDS


# ---------------------------------------------------------------------------
# Create — from URL in id
# ---------------------------------------------------------------------------


class TestCreateFromId:
    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_create_with_url_in_id(
        self, mock_ref_store, mock_web_store, archived_ok
    ):
        store = _make_store()
        # No existing refs; idempotency lookup returns empty list.
        store.create_ref.return_value = 42
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        out = h.put(
            path="https://example.com/page",
            selector=None,
            text="Great page.",
            mode="append",
        )

        assert "Bookmarked" in out
        assert "web:example-com-page" in out
        store.create_ref.assert_called_once()
        kwargs = store.create_ref.call_args.kwargs
        assert kwargs["corpus_id"] == "websites"
        assert kwargs["slug"] == "web:example-com-page"
        # Summary recorded as the first block.
        assert kwargs["blocks"][0]["text"] == "Great page."
        # Meta carries canonical + url + kind + captured_at.
        meta = kwargs["metadata"]
        assert meta["url"] == "https://example.com/page"
        assert meta["canonical_url"] == "https://example.com/page"
        assert meta["kind"] == "other"
        assert "captured_at" in meta

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_create_with_url_in_text(
        self, mock_ref_store, mock_web_store, archived_ok
    ):
        # URL lives on the first line of ``text`` when ``id`` is empty.
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        out = h.put(
            path="",
            selector=None,
            text="https://github.com/foo/bar\n\nGreat tool.",
            mode="append",
        )

        assert "Bookmarked" in out
        kwargs = store.create_ref.call_args.kwargs
        assert kwargs["slug"] == "web:github-com-foo-bar"
        # The summary is whatever's after the URL line.
        assert kwargs["blocks"][0]["text"] == "Great tool."
        # Kind auto-inferred as ``repo`` for github.com/<user>/<repo>.
        assert kwargs["metadata"]["kind"] == "repo"

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_canonicalisation_strips_tracking(
        self, mock_ref_store, mock_web_store, archived_ok
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        h.put(
            path="https://EXAMPLE.com/page?utm_source=twitter&q=1#anchor",
            selector=None,
            text="x",
            mode="append",
        )

        meta = store.create_ref.call_args.kwargs["metadata"]
        assert meta["canonical_url"] == "https://example.com/page?q=1"
        # Original URL preserved too.
        assert meta["url"] == (
            "https://EXAMPLE.com/page?utm_source=twitter&q=1#anchor"
        )

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_invalid_url_raises(self, mock_ref_store, mock_web_store):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        with pytest.raises(PrecisError) as exc:
            # Not a URL, empty text → can't derive URL from anywhere.
            h.put(path="", selector=None, text="", mode="append")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_url_without_scheme_raises(
        self, mock_ref_store, mock_web_store
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(path="example.com/page", selector=None, text="", mode="append")
        # Treated as slug (no http scheme) → looked up as web:example.com...
        # but ref doesn't exist → ID_NOT_FOUND.  Either code is acceptable
        # for now; assert it doesn't succeed.
        assert exc.value.code in (
            ErrorCode.PARAM_INVALID,
            ErrorCode.ID_NOT_FOUND,
        )

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_explicit_kind_honoured(
        self, mock_ref_store, mock_web_store, archived_ok
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        h.put(
            path="https://example.com/some-page",
            selector=None,
            text="x",
            mode="append",
            kind="tool",
        )
        assert store.create_ref.call_args.kwargs["metadata"]["kind"] == "tool"

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_unknown_kind_raises(self, mock_ref_store, mock_web_store, archived_ok):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        with pytest.raises(PrecisError) as exc:
            h.put(
                path="https://example.com/",
                selector=None,
                text="x",
                mode="append",
                kind="not-a-real-kind",
            )
        assert exc.value.code == ErrorCode.PARAM_INVALID
        assert "not-a-real-kind" in str(exc.value)

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_tags_forwarded(self, mock_ref_store, mock_web_store, archived_ok):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        h.put(
            path="https://example.com/",
            selector=None,
            text="x",
            mode="append",
            tags=["tool", "dev"],
        )
        assert store.create_ref.call_args.kwargs["tags"] == ["tool", "dev"]

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_tags_string_split(self, mock_ref_store, mock_web_store, archived_ok):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        h.put(
            path="https://example.com/",
            selector=None,
            text="x",
            mode="append",
            tags="tool, dev ,db",
        )
        assert store.create_ref.call_args.kwargs["tags"] == ["tool", "dev", "db"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_second_create_returns_existing(
        self, mock_ref_store, mock_web_store, archived_ok
    ):
        existing = _bookmark_ref(
            slug="web:example-com-page",
            url="https://example.com/page",
            canonical="https://example.com/page",
        )
        store = _make_store(refs=[existing])
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: [existing]
        out = h.put(
            path="https://example.com/page?utm_source=social",
            selector=None,
            text="Different summary",
            mode="append",
        )

        # Must NOT create a new ref.
        store.create_ref.assert_not_called()
        # Must surface the existing slug.
        assert "Already bookmarked" in out
        assert "web:example-com-page" in out

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_slug_collision_disambiguated(
        self, mock_ref_store, mock_web_store, archived_ok
    ):
        # Simulate a slug collision on a URL whose canonical form is
        # genuinely different from an existing one (so idempotency
        # skips but slug collides).  First create_ref raises
        # ValueError "slug already exists", second succeeds.
        store = _make_store()
        store.create_ref.side_effect = [
            ValueError("slug already exists: web:example-com"),
            42,
        ]
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        out = h.put(
            path="https://example.com/",
            selector=None,
            text="x",
            mode="append",
        )
        # Second call should use -a suffix.
        assert store.create_ref.call_count == 2
        last_slug = store.create_ref.call_args.kwargs["slug"]
        assert last_slug == "web:example-com-a"
        assert last_slug in out


# ---------------------------------------------------------------------------
# Archive.org integration
# ---------------------------------------------------------------------------


class TestArchive:
    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_success_stores_wayback_url(
        self, mock_ref_store, mock_web_store, archived_ok
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        out = h.put(
            path="https://example.com/", selector=None, text="x", mode="append"
        )

        meta = store.create_ref.call_args.kwargs["metadata"]
        assert meta["wayback_url"] == (
            "https://web.archive.org/web/20260424/https://example.com/"
        )
        assert "archive_skipped_reason" not in meta
        assert "archived:" in out

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_skipped_records_reason(
        self, mock_ref_store, mock_web_store, archive_skipped
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        out = h.put(
            path="https://example.com/",
            selector=None,
            text="x",
            mode="append",
            archive=False,
        )

        meta = store.create_ref.call_args.kwargs["metadata"]
        assert meta["wayback_url"] is None
        assert meta["archive_skipped_reason"] == "user_optout"
        assert "skipped" in out or "user_optout" in out

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_archive_flag_forwarded_to_archive_url(
        self, mock_ref_store, mock_web_store
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        with patch(_PATCH_ARCHIVE) as archive_mock:
            archive_mock.return_value = ArchiveResult(
                skipped_reason=SkipReason.USER_OPTOUT
            )
            h = WebHandler()
            h._query_corpus_refs = lambda _s: []
            h.put(
                path="https://example.com/",
                selector=None,
                text="x",
                mode="append",
                archive=False,
            )

        # archive_url was called with requested=False.
        archive_mock.assert_called_once()
        call = archive_mock.call_args
        assert call.kwargs.get("requested") is False

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_no_archive_kwarg_passes_none(
        self, mock_ref_store, mock_web_store
    ):
        # When the caller doesn't pass ``archive``, the handler sends
        # ``requested=None`` so the env var decides.
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        with patch(_PATCH_ARCHIVE) as archive_mock:
            archive_mock.return_value = ArchiveResult(
                wayback_url="https://web.archive.org/web/x"
            )
            h = WebHandler()
            h._query_corpus_refs = lambda _s: []
            h.put(
                path="https://example.com/",
                selector=None,
                text="x",
                mode="append",
            )

        assert archive_mock.call_args.kwargs.get("requested") is None


# ---------------------------------------------------------------------------
# Append note (existing bookmark)
# ---------------------------------------------------------------------------


class TestAppendNote:
    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_append_to_existing_adds_block(
        self, mock_ref_store, mock_web_store
    ):
        existing = _bookmark_ref(slug="web:example-com")
        store = _make_store(refs=[existing])
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        out = h.put(
            path="web:example-com",
            selector=None,
            text="Additional note.",
            mode="append",
        )
        store.add_block.assert_called_once()
        assert "Note appended" in out

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_append_empty_text_raises(self, mock_ref_store, mock_web_store):
        store = _make_store(refs=[_bookmark_ref()])
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(
                path="web:example-com",
                selector=None,
                text="",
                mode="append",
            )
        assert exc.value.code == ErrorCode.PARAM_INVALID

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_append_unknown_slug_raises(
        self, mock_ref_store, mock_web_store
    ):
        store = _make_store()  # no refs
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(
                path="web:does-not-exist",
                selector=None,
                text="note",
                mode="append",
            )
        assert exc.value.code == ErrorCode.ID_NOT_FOUND


# ---------------------------------------------------------------------------
# Replace summary
# ---------------------------------------------------------------------------


class TestReplace:
    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_replace_updates_first_block(
        self, mock_ref_store, mock_web_store
    ):
        ref = _bookmark_ref()
        store = _make_store(refs=[ref])
        store.get_blocks.return_value = [
            {"node_id": "web:example-com-b0000", "text": "old"}
        ]
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        out = h.put(
            path="web:example-com",
            selector=None,
            text="new summary",
            mode="replace",
        )
        store.update_block_text.assert_called_once_with(
            "web:example-com", "web:example-com-b0000", "new summary"
        )
        assert "replaced" in out.lower()

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_replace_adds_block_when_none_exist(
        self, mock_ref_store, mock_web_store
    ):
        # Bookmark created with empty summary → no blocks.  Replace
        # must add one rather than erroring.
        ref = _bookmark_ref()
        store = _make_store(refs=[ref])
        store.get_blocks.return_value = []
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        h.put(
            path="web:example-com",
            selector=None,
            text="first summary",
            mode="replace",
        )
        store.add_block.assert_called_once()

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_replace_unknown_raises(self, mock_ref_store, mock_web_store):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(
                path="web:ghost",
                selector=None,
                text="x",
                mode="replace",
            )
        assert exc.value.code == ErrorCode.ID_NOT_FOUND

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_replace_empty_text_raises(
        self, mock_ref_store, mock_web_store
    ):
        store = _make_store(refs=[_bookmark_ref()])
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(
                path="web:example-com",
                selector=None,
                text="",
                mode="replace",
            )
        assert exc.value.code == ErrorCode.PARAM_INVALID


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_delete_marks_meta(self, mock_ref_store, mock_web_store):
        ref = _bookmark_ref()
        store = _make_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        out = h.put(
            path="web:example-com", selector=None, text="", mode="delete"
        )
        store.update_ref_metadata.assert_called_once()
        call_args = store.update_ref_metadata.call_args
        # Second positional or ``metadata=`` kwarg carries the dict.
        meta_arg = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("metadata")
        )
        assert meta_arg is not None
        assert meta_arg["deleted"] is True
        assert "deleted_at" in meta_arg
        assert "soft-deleted" in out

    @patch(_PATCH_STORE_WEB)
    @patch(_PATCH_STORE_REF)
    def test_delete_unknown_raises(self, mock_ref_store, mock_web_store):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_web_store.return_value = store

        h = WebHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(
                path="web:ghost", selector=None, text="", mode="delete"
            )
        assert exc.value.code == ErrorCode.ID_NOT_FOUND


# ---------------------------------------------------------------------------
# Collection views — /recent, /tags, /kinds
# ---------------------------------------------------------------------------


class TestCollectionViews:
    def test_recent_empty(self):
        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        store = _make_store()
        out = h._read_recent(store)
        assert "No bookmarks yet" in out

    def test_recent_lists_newest(self):
        refs = [
            _bookmark_ref(slug=f"web:item-{i}", url=f"https://example.com/{i}")
            for i in range(5)
        ]
        h = WebHandler()
        h._query_corpus_refs = lambda _s: refs
        store = _make_store(refs=refs)
        out = h._read_recent(store, limit=3)
        assert "3 recent bookmarks (of 5 total)" in out
        assert "web:item-0" in out
        assert "web:item-2" in out

    def test_tags_view(self):
        refs = [
            _bookmark_ref(slug="web:a", tags=["tool", "dev"]),
            _bookmark_ref(slug="web:b", tags=["tool"]),
            _bookmark_ref(slug="web:c", tags=["dev"]),
        ]
        h = WebHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._read_tags(_make_store(refs=refs))
        assert "tool" in out
        assert "dev" in out

    def test_kinds_view(self):
        refs = [
            _bookmark_ref(slug="web:a", kind="tool"),
            _bookmark_ref(slug="web:b", kind="tool"),
            _bookmark_ref(slug="web:c", kind="repo"),
        ]
        h = WebHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._read_kinds(_make_store(refs=refs))
        assert "tool" in out
        assert "repo" in out
        assert "2" in out  # tool count

    def test_bare_web_landing_page(self):
        refs = [_bookmark_ref(slug="web:a", kind="tool", tags=["tool"])]
        h = WebHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._list_overview(_make_store(refs=refs))
        assert "1 bookmarks" in out
        assert "Recent" in out

    def test_bare_web_landing_empty(self):
        h = WebHandler()
        h._query_corpus_refs = lambda _s: []
        out = h._list_overview(_make_store())
        assert "No bookmarks yet" in out
        assert "put(type='web'" in out


# ---------------------------------------------------------------------------
# Overview rendering
# ---------------------------------------------------------------------------


class TestReadOverview:
    def test_overview_shows_key_fields(self):
        ref = _bookmark_ref(
            slug="web:example-com-page",
            url="https://example.com/page",
            canonical="https://example.com/page",
            kind="tool",
            tags=["dev", "ops"],
            title="Example tool",
            wayback_url="https://web.archive.org/web/xxx/https://example.com/page",
        )
        h = WebHandler()
        store = _make_store(refs=[ref])
        store.get_blocks.return_value = [{"text": "This is the summary."}]
        out = h._read_overview(store, ref)
        assert "web:example-com-page" in out
        assert "[tool]" in out
        assert "https://example.com/page" in out
        assert "Example tool" in out
        assert "tags: dev, ops" in out
        assert "archived: https://web.archive.org" in out
        assert "This is the summary." in out

    def test_overview_skip_reason_rendered(self):
        ref = _bookmark_ref(slug="web:x", wayback_url=None)
        ref["meta"]["archive_skipped_reason"] = "user_optout"
        h = WebHandler()
        store = _make_store(refs=[ref])
        out = h._read_overview(store, ref)
        assert "archive: skipped" in out
        assert "user_optout" in out
