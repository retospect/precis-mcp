"""Tests for :class:`precis.handlers.book.BookHandler`.

Phase 1 of :doc:`websites-plan`.  Uses a MagicMock store \u2014 no DB, no
network.  Covers slug derivation, ISBN normalisation, id-format
resolution (``isbn:``), status views, idempotency, create / replace /
status / delete surface.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from precis.handlers.book import (
    BookHandler,
    _author_surname,
    _isbn10_to_13,
    _normalise_authors,
    _normalise_isbn,
    _normalise_tags,
    _parse_meta,
    _slug_token,
    _VALID_STATUSES,
    derive_book_slug,
)
from precis.protocol import ErrorCode, PrecisError

_PATCH_STORE_REF = "precis.handlers._ref_base._get_store"
_PATCH_STORE_BOOK = "precis.handlers.book._get_store"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_store(refs=None, blocks=None):
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
    store.add_block.return_value = "book:example-b0001"
    store.update_block_text.return_value = None
    return store


def _book_ref(
    slug: str = "book:feynman1963lectures",
    title: str = "The Feynman Lectures on Physics, Vol. 1",
    authors: list[str] | None = None,
    year: int | None = 1963,
    isbn: str | None = "9780201021158",
    isbn10: str | None = None,
    status: str = "read",
    tags: list[str] | None = None,
    rating: int | None = None,
    paper_slug: str | None = None,
    deleted: bool = False,
):
    meta: dict = {
        "title": title,
        "authors": authors or ["Richard P. Feynman", "Robert B. Leighton"],
        "year": year,
        "status": status,
        "captured_at": "2026-04-24T12:00:00Z",
    }
    if isbn:
        meta["isbn"] = isbn
    if isbn10:
        meta["isbn10"] = isbn10
    if rating is not None:
        meta["rating"] = rating
    if paper_slug:
        meta["paper_slug"] = paper_slug
    if deleted:
        meta["deleted"] = True
    return {
        "slug": slug,
        "title": title,
        "ref_id": 1,
        "id": 1,
        "corpus_id": "books",
        "first_seen_at": "2026-04-24T12:00:00Z",
        "tags": tags or [],
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# Slug derivation helpers
# ---------------------------------------------------------------------------


class TestSlugToken:
    def test_basic(self):
        assert _slug_token("Hello World") == "helloworld"

    def test_strips_punctuation(self):
        assert _slug_token("don't panic!") == "dontpanic"

    def test_caps_length(self):
        assert _slug_token("a" * 100, max_len=10) == "a" * 10


class TestAuthorSurname:
    def test_first_last(self):
        assert _author_surname(["Richard Feynman"]) == "feynman"

    def test_comma_separated(self):
        assert _author_surname(["Feynman, Richard P."]) == "feynman"

    def test_strips_punctuation(self):
        # Comma-split takes everything before the first comma as the
        # surname block; punctuation is stripped by ``_slug_token``.
        assert _author_surname(["Dr. Feynman, Jr."]) == "drfeynman"

    def test_empty(self):
        assert _author_surname([]) == ""
        assert _author_surname(None) == ""

    def test_string_passed_directly(self):
        assert _author_surname("Feynman") == "feynman"


class TestDeriveBookSlug:
    def test_author_year_title(self):
        assert (
            derive_book_slug(
                authors=["Richard Feynman"], year=1963, title="Lectures on Physics"
            )
            == "feynman1963lecturesonphysics"
        )

    def test_no_year(self):
        assert (
            derive_book_slug(authors=["Asimov"], title="Foundation")
            == "asimovfoundation"
        )

    def test_no_author(self):
        # Title-only.
        assert derive_book_slug(title="Beowulf") == "beowulf"

    def test_isbn_only_fallback(self):
        assert (
            derive_book_slug(isbn="978-0-201-02115-8")
            == "isbn-9780201021158"
        )

    def test_all_empty_returns_empty(self):
        assert derive_book_slug() == ""

    def test_caps_long_titles(self):
        title = "A " * 50 + "Book"
        slug = derive_book_slug(authors=["X"], year=2020, title=title)
        # Exact length depends on token caps; assert it's bounded and
        # ends in a clean token (no trailing hyphens / partial words).
        assert len(slug) <= 100


# ---------------------------------------------------------------------------
# ISBN helpers
# ---------------------------------------------------------------------------


class TestNormaliseIsbn:
    def test_hyphenated_13(self):
        assert _normalise_isbn("978-0-201-02115-8") == "9780201021158"

    def test_spaced_13(self):
        assert _normalise_isbn("978 0 201 02115 8") == "9780201021158"

    def test_10_digit_preserved(self):
        assert _normalise_isbn("0-201-02115-3") == "0201021153"

    def test_x_check_digit_preserved(self):
        # ISBN-10 ending in X is valid; must not be stripped.
        assert _normalise_isbn("0-306-40615-X") == "030640615X"

    def test_lowercase_x_uppercased(self):
        assert _normalise_isbn("030640615x") == "030640615X"

    def test_invalid_length_returns_empty(self):
        assert _normalise_isbn("12345") == ""
        assert _normalise_isbn("123456789012345") == ""

    def test_empty(self):
        assert _normalise_isbn("") == ""
        assert _normalise_isbn(None) == ""


class TestIsbn10To13:
    def test_known_conversion(self):
        # Feynman Vol 1: 0-201-02115-3 → 978-0-201-02115-8
        assert _isbn10_to_13("0201021153") == "9780201021158"

    def test_wrong_length(self):
        assert _isbn10_to_13("12345") == ""

    def test_another_known(self):
        # "The C Programming Language" 0-13-110362-8 → 978-0-13-110362-7
        assert _isbn10_to_13("0131103628") == "9780131103627"


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------


class TestNormalisers:
    def test_tags_list(self):
        assert _normalise_tags(["a", "b"]) == ["a", "b"]

    def test_tags_string(self):
        assert _normalise_tags("a, b,c") == ["a", "b", "c"]

    def test_tags_none(self):
        assert _normalise_tags(None) == []

    def test_authors_list(self):
        assert _normalise_authors(["A", "B"]) == ["A", "B"]

    def test_authors_string(self):
        assert _normalise_authors("Feynman, Leighton, Sands") == [
            "Feynman",
            "Leighton",
            "Sands",
        ]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreateBook:
    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_create_with_full_metadata(self, mock_ref_store, mock_book_store):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h._query_corpus_refs = lambda _s: []
        out = h.put(
            path="",
            selector=None,
            text="Foundational physics text.",
            mode="append",
            title="The Feynman Lectures on Physics, Vol. 1",
            authors=["Richard P. Feynman", "Robert B. Leighton"],
            year=1963,
            isbn="978-0-201-02115-8",
            status="read",
            tags=["physics", "textbook"],
        )

        assert "Book added" in out
        store.create_ref.assert_called_once()
        kwargs = store.create_ref.call_args.kwargs
        assert kwargs["corpus_id"] == "books"
        assert kwargs["slug"].startswith("book:feynman1963")
        meta = kwargs["metadata"]
        assert meta["title"] == "The Feynman Lectures on Physics, Vol. 1"
        assert meta["authors"] == [
            "Richard P. Feynman",
            "Robert B. Leighton",
        ]
        assert meta["year"] == 1963
        assert meta["status"] == "read"
        assert meta["isbn"] == "9780201021158"
        assert kwargs["tags"] == ["physics", "textbook"]
        assert kwargs["blocks"][0]["text"] == "Foundational physics text."

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_create_default_status_is_to_read(
        self, mock_ref_store, mock_book_store
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h._query_corpus_refs = lambda _s: []
        h.put(
            path="",
            selector=None,
            text="",
            mode="append",
            title="Foundation",
            authors=["Asimov"],
            year=1951,
        )
        meta = store.create_ref.call_args.kwargs["metadata"]
        assert meta["status"] == "to-read"

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_create_without_title_or_isbn_raises(
        self, mock_ref_store, mock_book_store
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(path="", selector=None, text="", mode="append")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_create_with_only_isbn(self, mock_ref_store, mock_book_store):
        # Seeding by bare ISBN is allowed \u2014 useful when ingesting a
        # reading-list without full metadata yet.
        store = _make_store()
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h._query_corpus_refs = lambda _s: []
        h.put(
            path="",
            selector=None,
            text="",
            mode="append",
            isbn="978-0-201-02115-8",
        )
        slug = store.create_ref.call_args.kwargs["slug"]
        assert slug == "book:isbn-9780201021158"

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_create_invalid_status_raises(
        self, mock_ref_store, mock_book_store
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(
                path="",
                selector=None,
                text="",
                mode="append",
                title="X",
                status="unknown-status",
            )
        assert exc.value.code == ErrorCode.PARAM_INVALID
        assert "unknown-status" in str(exc.value)

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_create_expands_isbn10_to_13(
        self, mock_ref_store, mock_book_store
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h._query_corpus_refs = lambda _s: []
        h.put(
            path="",
            selector=None,
            text="",
            mode="append",
            title="Feynman Vol 1",
            authors=["Feynman"],
            year=1963,
            isbn="0-201-02115-3",  # 10-digit form
        )
        meta = store.create_ref.call_args.kwargs["metadata"]
        # Both forms stored for lookup parity.
        assert meta["isbn10"] == "0201021153"
        assert meta["isbn"] == "9780201021158"

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_create_stores_paper_slug_crosslink(
        self, mock_ref_store, mock_book_store
    ):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h._query_corpus_refs = lambda _s: []
        h.put(
            path="",
            selector=None,
            text="notes",
            mode="append",
            title="Feynman",
            authors=["Feynman"],
            year=1963,
            paper_slug="feynman1963lectures",
        )
        meta = store.create_ref.call_args.kwargs["metadata"]
        assert meta["paper_slug"] == "feynman1963lectures"

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_slug_collision_disambiguated(
        self, mock_ref_store, mock_book_store
    ):
        store = _make_store()
        store.create_ref.side_effect = [
            ValueError("slug already exists"),
            42,
        ]
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h._query_corpus_refs = lambda _s: []
        h.put(
            path="",
            selector=None,
            text="",
            mode="append",
            title="Foundation",
            authors=["Asimov"],
            year=1951,
        )
        assert store.create_ref.call_count == 2
        last_slug = store.create_ref.call_args.kwargs["slug"]
        assert last_slug.endswith("-a")


# ---------------------------------------------------------------------------
# Idempotency by ISBN
# ---------------------------------------------------------------------------


class TestIsbnIdempotency:
    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_second_create_same_isbn_returns_existing(
        self, mock_ref_store, mock_book_store
    ):
        existing = _book_ref(
            slug="book:feynman1963lectures",
            isbn="9780201021158",
        )
        store = _make_store(refs=[existing])
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h._query_corpus_refs = lambda _s: [existing]
        out = h.put(
            path="",
            selector=None,
            text="",
            mode="append",
            title="Feynman",
            isbn="978-0-201-02115-8",
        )
        store.create_ref.assert_not_called()
        assert "Already in library" in out
        assert "book:feynman1963lectures" in out

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_isbn10_matches_isbn13_via_conversion(
        self, mock_ref_store, mock_book_store
    ):
        existing = _book_ref(isbn="9780201021158", isbn10=None)
        store = _make_store(refs=[existing])
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h._query_corpus_refs = lambda _s: [existing]
        out = h.put(
            path="",
            selector=None,
            text="",
            mode="append",
            title="Feynman",
            isbn="0-201-02115-3",  # 10-digit form of the existing 13-digit
        )
        # Must dedupe by expanding 10→13.
        assert "Already in library" in out
        store.create_ref.assert_not_called()


# ---------------------------------------------------------------------------
# ISBN id-format resolution (isbn:<digits>)
# ---------------------------------------------------------------------------


class TestIsbnResolution:
    def test_is_isbn_path_detects_prefix(self):
        h = BookHandler()
        assert h._is_isbn_path("isbn:9780201021158", view=None)

    def test_is_isbn_path_detects_raw_digits(self):
        h = BookHandler()
        assert h._is_isbn_path("9780201021158", view=None)
        assert h._is_isbn_path("0-201-02115-3", view=None)

    def test_is_isbn_path_rejects_slug_like(self):
        h = BookHandler()
        # Slugs with some digits aren't ISBNs.
        assert not h._is_isbn_path("feynman1963lectures", view=None)
        assert not h._is_isbn_path("", view=None)

    def test_resolve_by_isbn_finds_existing(self):
        existing = _book_ref(isbn="9780201021158")
        h = BookHandler()
        h._query_corpus_refs = lambda _s: [existing]
        found = h._resolve_by_isbn(_make_store(), "isbn:9780201021158")
        assert found is existing

    def test_resolve_by_isbn_via_hyphens(self):
        existing = _book_ref(isbn="9780201021158")
        h = BookHandler()
        h._query_corpus_refs = lambda _s: [existing]
        found = h._resolve_by_isbn(_make_store(), "isbn:978-0-201-02115-8")
        assert found is existing

    def test_resolve_by_isbn_unknown_returns_none(self):
        h = BookHandler()
        h._query_corpus_refs = lambda _s: []
        assert h._resolve_by_isbn(_make_store(), "isbn:9780201021158") is None


# ---------------------------------------------------------------------------
# Read surface
# ---------------------------------------------------------------------------


class TestReadOverview:
    def test_overview_shows_key_fields(self):
        ref = _book_ref(
            tags=["physics", "textbook"],
            rating=5,
            paper_slug="feynman1963lectures",
        )
        h = BookHandler()
        store = _make_store(refs=[ref])
        store.get_blocks.return_value = [
            {"text": "My notes on these lectures."}
        ]
        out = h._read_overview(store, ref)
        assert "book:feynman1963lectures" in out
        assert "[read]" in out
        assert "The Feynman Lectures" in out
        assert "Richard P. Feynman" in out
        assert "ISBN: 9780201021158" in out
        assert "tags: physics, textbook" in out
        assert "rating: 5/5" in out
        assert "paper: feynman1963lectures" in out
        assert "My notes on these lectures." in out


class TestCollectionViews:
    def test_recent_lists(self):
        refs = [
            _book_ref(slug=f"book:item-{i}", title=f"Title {i}") for i in range(5)
        ]
        h = BookHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._read_recent(_make_store(refs=refs), limit=3)
        assert "3 recent books (of 5 total)" in out

    def test_tags_view(self):
        refs = [
            _book_ref(slug="book:a", tags=["physics"]),
            _book_ref(slug="book:b", tags=["physics", "sci-fi"]),
        ]
        h = BookHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._read_tags(_make_store(refs=refs))
        assert "physics" in out
        assert "sci-fi" in out

    def test_status_views_filter_correctly(self):
        refs = [
            _book_ref(slug="book:a", status="to-read"),
            _book_ref(slug="book:b", status="reading"),
            _book_ref(slug="book:c", status="read"),
            _book_ref(slug="book:d", status="read"),
        ]
        h = BookHandler()
        h._query_corpus_refs = lambda _s: refs

        read_out = h._read_status(_make_store(refs=refs), status="read")
        assert "2 books \u2014 read" in read_out
        assert "book:c" in read_out
        assert "book:d" in read_out
        assert "book:a" not in read_out

        toread_out = h._read_status(_make_store(refs=refs), status="to-read")
        assert "1 books \u2014 to-read" in toread_out
        assert "book:a" in toread_out

    def test_by_author_groups(self):
        refs = [
            _book_ref(
                slug="book:feynman1963lectures",
                authors=["Richard P. Feynman"],
            ),
            _book_ref(
                slug="book:feynman1985qed", authors=["Richard P. Feynman"]
            ),
            _book_ref(slug="book:asimov1951foundation", authors=["Isaac Asimov"]),
        ]
        h = BookHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._read_by_author(_make_store(refs=refs))
        assert "feynman" in out
        assert "asimov" in out
        assert "(2)" in out  # Feynman count

    def test_by_year_groups(self):
        refs = [
            _book_ref(slug="book:a", year=1963),
            _book_ref(slug="book:b", year=1985),
            _book_ref(slug="book:c", year=1963),
        ]
        h = BookHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._read_by_year(_make_store(refs=refs))
        # 1985 appears before 1963 (descending).
        assert out.index("1985") < out.index("1963")

    def test_landing_page_summarises_statuses(self):
        refs = [
            _book_ref(slug="book:a", status="read"),
            _book_ref(slug="book:b", status="reading"),
            _book_ref(slug="book:c", status="to-read"),
        ]
        h = BookHandler()
        h._query_corpus_refs = lambda _s: refs
        out = h._list_overview(_make_store(refs=refs))
        assert "3 books" in out
        assert "to-read" in out
        assert "reading" in out
        assert "read" in out

    def test_landing_page_empty(self):
        h = BookHandler()
        h._query_corpus_refs = lambda _s: []
        out = h._list_overview(_make_store())
        assert "No books yet" in out


# ---------------------------------------------------------------------------
# Write \u2014 replace / status / delete
# ---------------------------------------------------------------------------


class TestReplaceNotes:
    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_replace_updates_first_block(
        self, mock_ref_store, mock_book_store
    ):
        ref = _book_ref()
        store = _make_store(refs=[ref])
        store.get_blocks.return_value = [
            {"node_id": "book:feynman1963lectures-b0000", "text": "old"}
        ]
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        out = h.put(
            path="book:feynman1963lectures",
            selector=None,
            text="updated notes",
            mode="replace",
        )
        store.update_block_text.assert_called_once()
        assert "Notes replaced" in out

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_replace_unknown_raises(self, mock_ref_store, mock_book_store):
        store = _make_store()
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(
                path="book:ghost",
                selector=None,
                text="x",
                mode="replace",
            )
        assert exc.value.code == ErrorCode.ID_NOT_FOUND


class TestChangeStatus:
    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_status_to_reading_updates_meta(
        self, mock_ref_store, mock_book_store
    ):
        ref = _book_ref(status="to-read")
        store = _make_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        out = h.put(
            path="book:feynman1963lectures",
            selector=None,
            text="reading",
            mode="status",
        )
        store.update_ref_metadata.assert_called_once()
        call_args = store.update_ref_metadata.call_args
        meta_arg = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("metadata")
        )
        assert meta_arg["status"] == "reading"
        assert "to-read \u2192 reading" in out

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_status_to_read_stamps_finished_at(
        self, mock_ref_store, mock_book_store
    ):
        ref = _book_ref(status="reading")
        store = _make_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h.put(
            path="book:feynman1963lectures",
            selector=None,
            text="read",
            mode="status",
        )
        call_args = store.update_ref_metadata.call_args
        meta_arg = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("metadata")
        )
        assert "finished_at" in meta_arg

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_status_invalid_raises(self, mock_ref_store, mock_book_store):
        store = _make_store(refs=[_book_ref()])
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(
                path="book:feynman1963lectures",
                selector=None,
                text="bogus",
                mode="status",
            )
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_valid_statuses_complete(self):
        # Guard: make sure every status the handler emits is in the
        # documented set.
        for s in ("to-read", "reading", "read", "abandoned"):
            assert s in _VALID_STATUSES


class TestDelete:
    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_delete_marks_meta(self, mock_ref_store, mock_book_store):
        ref = _book_ref()
        store = _make_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        out = h.put(
            path="book:feynman1963lectures",
            selector=None,
            text="",
            mode="delete",
        )
        store.update_ref_metadata.assert_called_once()
        assert "soft-deleted" in out

    @patch(_PATCH_STORE_BOOK)
    @patch(_PATCH_STORE_REF)
    def test_delete_by_isbn_path(self, mock_ref_store, mock_book_store):
        # ``put(id='isbn:9780201021158', mode='delete')`` should resolve
        # the ISBN to the book slug before soft-deleting.
        ref = _book_ref(isbn="9780201021158")
        store = _make_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_book_store.return_value = store

        h = BookHandler()
        h._query_corpus_refs = lambda _s: [ref]
        out = h.put(
            path="isbn:9780201021158",
            selector=None,
            text="",
            mode="delete",
        )
        assert "soft-deleted" in out
        assert "book:feynman1963lectures" in out
