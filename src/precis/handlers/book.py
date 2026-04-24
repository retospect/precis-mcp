"""BookHandler — curated book notes with optional ISBN.

Phase 1 of :doc:`docs/websites-plan`.  Deliberately shallow: author,
year, ISBN, reading status, your notes.  For *deep* book content (full
text, figures, chunked search), run the PDF through ``acatome-extract``
— it lands as a :class:`paper` ref and this book-handler cross-links to
it via ``meta.paper_slug``.

URI schemes:

- ``book:<slug>`` — primary addressing.  Slug is derived from
  ``<author-lastname><year><short-title>`` when all three are known;
  falls back gracefully for pre-ISBN / self-published / obscure books
  where some fields are missing.
- ``isbn:<digits>`` — alternative id format (like ``doi:`` / ``arxiv:``
  on ``paper``).  Resolves to the same record when an ISBN is present
  in ``meta.isbn`` or ``meta.isbn10``.  Normalised to the 13-digit form
  with all non-digits stripped.

Agent usage::

    # Create
    put(type='book',
        title='The Feynman Lectures on Physics, Vol. 1',
        authors=['Richard P. Feynman', 'Robert B. Leighton', 'Matthew Sands'],
        year=1963, isbn='978-0-201-02115-8',
        status='read', text='Definitive intro physics text.')

    # Views
    get(id='book:/recent')
    get(id='book:/to-read')
    get(id='book:/reading')
    get(id='book:/read')
    get(id='book:/by-author')

    # Addressing
    get(id='book:feynman1963lectures')
    get(id='isbn:9780201021158')          # same record
    get(id='isbn:0-201-02115-3')          # 10-digit form also accepted
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from precis.handlers._ref_base import RefHandler, _get_store, _parse_tags
from precis.protocol import ErrorCode, PrecisError, extract_kwargs
from precis.uri import SEP

log = logging.getLogger(__name__)

#: Valid reading statuses — used for options= on PARAM_INVALID.
_VALID_STATUSES: tuple[str, ...] = ("to-read", "reading", "read", "abandoned")

#: Maximum characters for the title fragment in a derived slug.
_SLUG_TITLE_MAX = 30


# ─────────────────────────────────────────────────────────────────────
# Helpers — slug derivation
# ─────────────────────────────────────────────────────────────────────


_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _slug_token(s: str, max_len: int = _SLUG_TITLE_MAX) -> str:
    """Lowercase, ascii, no punctuation, length-capped slug token."""
    cleaned = _SLUG_STRIP_RE.sub("", s.lower())
    return cleaned[:max_len]


def _author_surname(authors) -> str:
    """Extract a surname slug-token from the first author.

    Handles both ``'First Last'`` and ``'Last, First'`` forms, returns
    a lowercase alpha-only token.  Returns ``''`` if no usable author.
    """
    if not authors:
        return ""
    if isinstance(authors, str):
        first = authors
    else:
        first = str(authors[0]) if len(authors) else ""
    if not first:
        return ""
    if "," in first:
        surname = first.split(",", 1)[0].strip()
    else:
        # Space-separated: last word.
        parts = first.strip().split()
        surname = parts[-1] if parts else ""
    return _slug_token(surname, max_len=20)


def derive_book_slug(
    *,
    authors=None,
    year: int | None = None,
    title: str = "",
    isbn: str = "",
) -> str:
    """Derive a ``book:`` slug body from metadata.

    Priority:

    1. ``<surname><year><title-token>`` when surname + year + title all
       resolve to non-empty tokens.
    2. ``<surname><title-token>`` when year is unknown.
    3. ``<title-token>`` when author is unknown.
    4. ``isbn-<digits>`` as a last resort when nothing else is available
       (e.g. ingesting a bare ISBN).

    Returns the body only — the handler prepends ``book:``.
    """
    surname = _author_surname(authors)
    title_tok = _slug_token(title)
    parts: list[str] = []
    if surname:
        parts.append(surname)
    if year:
        parts.append(str(year))
    if title_tok:
        parts.append(title_tok)

    slug = "".join(parts)
    if slug:
        return slug

    digits = _normalise_isbn(isbn) if isbn else ""
    if digits:
        return f"isbn-{digits}"
    return ""


# ─────────────────────────────────────────────────────────────────────
# Helpers — ISBN
# ─────────────────────────────────────────────────────────────────────


_ISBN_DIGITS_RE = re.compile(r"[^0-9Xx]")


def _normalise_isbn(isbn: str) -> str:
    """Return the canonical digit form of ``isbn`` or ``""`` if invalid.

    Accepts ISBN-10 or ISBN-13 in any common formatting (hyphens,
    spaces, mixed case).  For ISBN-10 the trailing ``X`` check digit
    is preserved.  No checksum validation — that's out of Phase 1
    scope; we just strip decoration so two agents writing
    ``978-0-201-02115-8`` and ``9780201021158`` de-dupe.
    """
    if not isbn:
        return ""
    cleaned = _ISBN_DIGITS_RE.sub("", isbn).upper()
    if len(cleaned) == 10 or len(cleaned) == 13:
        return cleaned
    return ""


def _isbn10_to_13(isbn10: str) -> str:
    """Convert a 10-digit ISBN to the 13-digit form.

    Used to expand idempotency lookups — a book stored with
    ``isbn='0201021153'`` should still be found by an ``isbn:978…``
    query.  Returns ``""`` if the input isn't exactly 10 chars.
    """
    if len(isbn10) != 10:
        return ""
    base = "978" + isbn10[:9]
    # ISBN-13 check digit: mod 10 of weighted sum.
    total = sum(
        (int(d) if d.isdigit() else 0) * (1 if i % 2 == 0 else 3)
        for i, d in enumerate(base)
    )
    check = (10 - (total % 10)) % 10
    return base + str(check)


# ─────────────────────────────────────────────────────────────────────
# Meta / time helpers
# ─────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_meta(ref: dict) -> dict:
    raw = ref.get("meta") or ref.get("metadata") or {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _fmt_ts(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return raw


def _normalise_tags(tags) -> list[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t).strip() for t in tags if str(t).strip()]


def _normalise_authors(authors) -> list[str]:
    """Accept ``['A', 'B']`` or ``'A, B'`` (author list, not names)."""
    if not authors:
        return []
    if isinstance(authors, str):
        return [a.strip() for a in authors.split(",") if a.strip()]
    return [str(a).strip() for a in authors if str(a).strip()]


# ─────────────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────────────


class BookHandler(RefHandler):
    """Handler for the ``book:`` scheme (and ``isbn:`` id format).

    See module docstring for design rationale.  Parallel to
    :class:`precis.handlers.memory.MemoryHandler` with book-specific
    metadata, status-filtered views, and ISBN resolution.
    """

    scheme = "book"
    #: ``isbn:`` is an *id format*, not a separate kind.  Registered in
    #: :class:`precis.registry.KindSpec.schemes` so the URI parser routes
    #: ``isbn:<digits>`` to this handler.
    schemes = ("book", "isbn")
    writable = True
    corpus_id = "books"
    views = {
        **RefHandler.views,
        "recent": "_read_recent_view",
        "tags": "_read_tags_view",
        "to-read": "_read_toread_view",
        "reading": "_read_reading_view",
        "read": "_read_read_view",
        "by-author": "_read_by_author_view",
        "by-year": "_read_by_year_view",
    }
    allowed_modes = {"append", "add", "replace", "delete", "status", "note"}
    extensions: set[str] = set()

    _ref_noun = "book"
    _ref_emoji = "📚"
    _slug_prefix = "book"

    # ── Read dispatch ────────────────────────────────────────────────

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
        **kwargs,
    ) -> str:
        store = _get_store()

        # isbn:<digits> — resolve to the book slug, then fall through.
        if self._is_isbn_path(path, view):
            ref = self._resolve_by_isbn(store, path)
            if ref is None:
                raise PrecisError(
                    ErrorCode.ID_NOT_FOUND,
                    cause=f"book: no record for ISBN {path!r}",
                    next=(
                        "Create: put(type='book', isbn='<digits>', "
                        "title='...', authors=['...'])"
                    ),
                )
            # Rewrite path so the per-ref code path uses the slug.
            path = ref.get("slug", "") or ""

        # Bare ``book:`` landing.
        if (not path or path == "/") and not view and not selector and not query:
            return self._list_overview(store)

        # Collection-level views.
        for key, method in (
            ("recent", self._read_recent),
            ("tags", self._read_tags),
            ("to-read", self._read_status),
            ("reading", self._read_status),
            ("read", self._read_status),
            ("by-author", self._read_by_author),
            ("by-year", self._read_by_year),
        ):
            if path in (f"/{key}", key) or view == key:
                if key in ("to-read", "reading", "read"):
                    return self._read_status(store, status=key)
                if key == "recent":
                    limit_raw = kwargs.get("top_k") or 20
                    try:
                        limit = int(limit_raw)
                    except (TypeError, ValueError):
                        limit = 20
                    return self._read_recent(store, limit=limit)
                return method(store)

        return super().read(
            path, selector, view, subview, query, summarize, depth, page, **kwargs
        )

    # ── View dispatchers (uniform signature) ─────────────────────────

    def _read_recent_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="book/recent")
        return self._read_recent(store)

    def _read_tags_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="book/tags")
        return self._read_tags(store)

    def _read_toread_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="book/to-read")
        return self._read_status(store, status="to-read")

    def _read_reading_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="book/reading")
        return self._read_status(store, status="reading")

    def _read_read_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="book/read")
        return self._read_status(store, status="read")

    def _read_by_author_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="book/by-author")
        return self._read_by_author(store)

    def _read_by_year_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="book/by-year")
        return self._read_by_year(store)

    # ── Overview rendering ───────────────────────────────────────────

    def _read_overview(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        meta = _parse_meta(ref)
        tags = _parse_tags(ref)

        authors = meta.get("authors") or []
        year = meta.get("year")
        status = meta.get("status", "to-read")
        isbn = meta.get("isbn") or meta.get("isbn10", "")
        publisher = meta.get("publisher", "")
        pages = meta.get("pages")
        paper_slug = meta.get("paper_slug")
        rating = meta.get("rating")

        lines: list[str] = [f"📚 {slug}  [{status}]"]
        if title:
            lines.append(f"   {title}")
        if authors:
            lines.append(f"   by {', '.join(authors)}" + (f" ({year})" if year else ""))
        elif year:
            lines.append(f"   ({year})")
        if publisher:
            lines.append(f"   publisher: {publisher}")
        if pages:
            lines.append(f"   {pages} pages")
        if isbn:
            lines.append(f"   ISBN: {isbn}")
        if tags:
            lines.append(f"   tags: {', '.join(tags)}")
        if rating is not None:
            lines.append(f"   rating: {rating}/5")
        if paper_slug:
            lines.append(
                f"   paper: {paper_slug}  "
                f"(full content — get(id='{paper_slug}'))"
            )
        if meta.get("deleted"):
            lines.append("   [deleted]")
        lines.append("")

        try:
            blocks = store.get_blocks(slug, block_type="text")
        except Exception:  # noqa: BLE001
            blocks = []
        if blocks:
            preview = (blocks[0].get("text") or "").strip()
            if preview:
                lines.append(preview)
                lines.append("")

        lines.append("Next:")
        lines.append(
            f"  put(id='{slug}', text='…', mode='replace')   — rewrite notes"
        )
        lines.append(
            f"  put(id='{slug}', text='reading', mode='status')  — change status"
        )
        lines.append(f"  get(id='{slug}{SEP}0..5')                   — read blocks")
        return "\n".join(lines)

    def _list_overview(self, store) -> str:
        refs = self._query_corpus_refs(store)
        if not refs:
            return (
                "📚 No books yet.\n\n"
                "Create one:\n"
                "  put(type='book', title='…', authors=['…'], year=YYYY,\n"
                "      status='to-read', text='notes')\n"
            )

        # Group by status for an at-a-glance summary.
        status_counts: dict[str, int] = {}
        for r in refs:
            s = _parse_meta(r).get("status", "to-read")
            status_counts[s] = status_counts.get(s, 0) + 1

        lines = [f"📚 {len(refs)} books", ""]
        lines.append("Status:")
        for s in ("to-read", "reading", "read", "abandoned"):
            n = status_counts.get(s, 0)
            if n:
                lines.append(f"  {n:>3}  {s}")
        lines.append("")

        lines.append("Recent (top 5):")
        for r in refs[:5]:
            lines.append(self._list_entry(r))
        lines.append("")

        lines.append("Next:")
        lines.append("  get(id='book:/recent')   — last 20")
        lines.append("  get(id='book:/reading')  — currently reading")
        lines.append("  get(id='book:/to-read')  — backlog")
        lines.append("  get(id='book:/read')     — finished")
        lines.append("  search(query='…', type='book')")
        return "\n".join(lines)

    def _list_entry(self, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        meta = _parse_meta(ref)
        status = meta.get("status", "?")
        authors = meta.get("authors") or []
        year = meta.get("year") or ""
        author_token = authors[0].split()[-1] if authors else "?"
        return f"  {slug}  [{status}]  {author_token} {year}  {title[:60]}"

    def _list_header(self, count: int, grep: str = "") -> str:
        extra = f" (grep={grep!r})" if grep else ""
        return f"📚 {count} books{extra}"

    # ── Collection views ─────────────────────────────────────────────

    def _read_recent(self, store, *, limit: int = 20) -> str:
        refs = self._query_corpus_refs(store)
        if not refs:
            return "📚 No books yet."
        recent = refs[:limit]
        lines = [f"📚 {len(recent)} recent books (of {len(refs)} total)", ""]
        for r in recent:
            lines.append(self._list_entry(r))
        return "\n".join(lines)

    def _read_tags(self, store) -> str:
        refs = self._query_corpus_refs(store)
        counts: dict[str, int] = {}
        for r in refs:
            for t in r.get("tags") or []:
                counts[t] = counts.get(t, 0) + 1
        if not counts:
            return "📚 No tagged books yet."
        lines = [f"📚 tags ({len(counts)} distinct)", ""]
        for tag, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>3}  {tag}")
        return "\n".join(lines)

    def _read_status(self, store, *, status: str) -> str:
        refs = self._query_corpus_refs(store)
        filtered = [r for r in refs if _parse_meta(r).get("status") == status]
        if not filtered:
            return f"📚 No books with status={status!r}."
        lines = [f"📚 {len(filtered)} books — {status}", ""]
        for r in filtered:
            lines.append(self._list_entry(r))
        return "\n".join(lines)

    def _read_by_author(self, store) -> str:
        refs = self._query_corpus_refs(store)
        if not refs:
            return "📚 No books yet."
        buckets: dict[str, list[dict]] = {}
        for r in refs:
            authors = _parse_meta(r).get("authors") or []
            key = (
                authors[0].split()[-1] if authors else "(unknown)"
            ).lower()
            buckets.setdefault(key, []).append(r)

        lines = [f"📚 {len(refs)} books by author", ""]
        for surname in sorted(buckets):
            entries = buckets[surname]
            lines.append(f"  {surname} ({len(entries)}):")
            for r in entries:
                lines.append("  " + self._list_entry(r))
        return "\n".join(lines)

    def _read_by_year(self, store) -> str:
        refs = self._query_corpus_refs(store)
        if not refs:
            return "📚 No books yet."
        buckets: dict[int | str, list[dict]] = {}
        for r in refs:
            year = _parse_meta(r).get("year") or "(unknown)"
            buckets.setdefault(year, []).append(r)

        # Sort: known years descending, "(unknown)" last.
        keys = sorted(
            [k for k in buckets if isinstance(k, int)], reverse=True
        ) + [k for k in buckets if not isinstance(k, int)]

        lines = [f"📚 {len(refs)} books by year", ""]
        for year in keys:
            entries = buckets[year]
            lines.append(f"  {year} ({len(entries)}):")
            for r in entries:
                lines.append("  " + self._list_entry(r))
        return "\n".join(lines)

    # ── Write dispatch ───────────────────────────────────────────────

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        store = _get_store()

        if mode in ("append", "add", "create"):
            return self._create_book(store, path, text, **kwargs)

        if mode == "replace":
            return self._replace_notes(store, path, text, **kwargs)

        if mode == "status":
            return self._change_status(store, path, text, **kwargs)

        if mode == "delete":
            return self._delete_book(store, path)

        return super().put(path, selector, text, mode, **kwargs)

    # ── Create ──────────────────────────────────────────────────────

    def _create_book(self, store, path: str, text: str, **kwargs) -> str:
        title = (kwargs.get("title") or "").strip()
        authors = _normalise_authors(kwargs.get("authors"))
        year = kwargs.get("year")
        try:
            year_int = int(year) if year is not None else None
        except (ValueError, TypeError):
            year_int = None

        isbn_raw = kwargs.get("isbn") or ""
        isbn_digits = _normalise_isbn(isbn_raw)

        # Need *something* to identify the book.
        if not title and not isbn_digits:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=(
                    "book: title= or isbn= required for creation.  Books "
                    "without a known title can be seeded by ISBN alone; "
                    "everything else needs at least a title."
                ),
                next=(
                    "put(type='book', title='...', authors=['...'], "
                    "year=YYYY, text='notes')"
                ),
            )

        status = (kwargs.get("status") or "to-read").strip()
        if status not in _VALID_STATUSES:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"book: status={status!r} not recognised",
                options=list(_VALID_STATUSES),
            )

        tags = _normalise_tags(kwargs.get("tags"))
        rating = kwargs.get("rating")
        publisher = (kwargs.get("publisher") or "").strip() or None
        pages = kwargs.get("pages")
        paper_slug = (kwargs.get("paper_slug") or "").strip() or None

        # Slug derivation + explicit override.
        if path and (path.startswith("book:") or not is_path_http(path)):
            slug = path if path.startswith("book:") else f"book:{path}"
        else:
            body = derive_book_slug(
                authors=authors,
                year=year_int,
                title=title,
                isbn=isbn_raw,
            )
            if not body:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    cause=(
                        "book: could not derive a slug from the provided "
                        "author / year / title / ISBN."
                    ),
                    next="Supply at least title= plus one of authors= or isbn=",
                )
            slug = f"book:{body}"

        # Idempotency: look up by ISBN first, then by slug.
        if isbn_digits:
            existing = self._find_by_isbn(store, isbn_digits)
            if existing is not None:
                return (
                    f"📚 Already in library: {existing.get('slug')}\n"
                    f"   ISBN: {isbn_digits}\n\n"
                    "Next:\n"
                    f"  get(id='{existing.get('slug')}')          — view\n"
                    f"  put(id='{existing.get('slug')}', text='…', "
                    "mode='replace') — update notes"
                )

        meta: dict = {
            "title": title,
            "authors": authors,
            "year": year_int,
            "status": status,
            "captured_at": _now_iso(),
        }
        if isbn_digits:
            if len(isbn_digits) == 13:
                meta["isbn"] = isbn_digits
            elif len(isbn_digits) == 10:
                meta["isbn10"] = isbn_digits
                expanded = _isbn10_to_13(isbn_digits)
                if expanded:
                    meta["isbn"] = expanded
        if publisher:
            meta["publisher"] = publisher
        if pages is not None:
            try:
                meta["pages"] = int(pages)
            except (ValueError, TypeError):
                pass
        if rating is not None:
            try:
                meta["rating"] = int(rating)
            except (ValueError, TypeError):
                pass
        if paper_slug:
            meta["paper_slug"] = paper_slug

        blocks = (
            [{"text": text, "block_type": "text", "section_path": []}]
            if text
            else []
        )

        # Slug-collision disambiguation.
        base_slug = slug
        suffix = 0
        while True:
            try:
                store.create_ref(
                    slug=slug,
                    corpus_id=self.corpus_id,
                    title=title or meta.get("isbn") or slug.split(":", 1)[1],
                    metadata=meta,
                    tags=tags if tags else None,
                    blocks=blocks,
                )
                break
            except ValueError as exc:
                msg = str(exc).lower()
                if "already exists" in msg and suffix < 26:
                    suffix += 1
                    slug = f"{base_slug}-{chr(96 + suffix)}"
                    continue
                raise PrecisError(
                    ErrorCode.ID_AMBIGUOUS,
                    cause=f"book: could not create '{slug}': {exc}",
                ) from exc

        lines = [f"📚 Book added: {slug}  [{status}]"]
        if title:
            lines.append(f"   {title}")
        if authors:
            author_str = ", ".join(authors)
            year_str = f" ({year_int})" if year_int else ""
            lines.append(f"   by {author_str}{year_str}")
        if meta.get("isbn"):
            lines.append(f"   ISBN: {meta['isbn']}")
        if paper_slug:
            lines.append(f"   cross-linked to paper: {paper_slug}")
        lines.append("")
        lines.append("Next:")
        lines.append(f"  get(id='{slug}')                        — view")
        lines.append(
            f"  put(id='{slug}', text='…', mode='replace') — update notes"
        )
        return "\n".join(lines)

    # ── Replace notes ────────────────────────────────────────────────

    def _replace_notes(self, store, path: str, text: str, **kwargs) -> str:
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="book: id= required for replace",
            )
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="book: text= required for replace",
            )
        slug = self._resolve_book_slug(store, path)
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"book: no record {slug!r}",
            )

        blocks = store.get_blocks(slug, block_type="text")
        if blocks:
            node_id = blocks[0].get("node_id")
            if node_id:
                store.update_block_text(slug, node_id, text)
        else:
            store.add_block(slug, text=text, block_type="text")

        return f"📚 Notes replaced: {slug}"

    # ── Change status ────────────────────────────────────────────────

    def _change_status(self, store, path: str, text: str, **kwargs) -> str:
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="book: id= required for status",
            )
        new_status = (text or "").strip()
        if new_status not in _VALID_STATUSES:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"book: status={new_status!r} not recognised",
                options=list(_VALID_STATUSES),
            )
        slug = self._resolve_book_slug(store, path)
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"book: no record {slug!r}",
            )
        meta = _parse_meta(ref)
        old = meta.get("status", "to-read")
        meta["status"] = new_status
        if new_status == "read" and "finished_at" not in meta:
            meta["finished_at"] = _now_iso()
        store.update_ref_metadata(slug, meta, merge=True)
        return f"📚 {slug}: {old} → {new_status}"

    # ── Delete ───────────────────────────────────────────────────────

    def _delete_book(self, store, path: str) -> str:
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="book: id= required for delete",
            )
        slug = self._resolve_book_slug(store, path)
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"book: no record {slug!r}",
            )
        meta = _parse_meta(ref)
        meta["deleted"] = True
        meta["deleted_at"] = _now_iso()
        store.update_ref_metadata(slug, meta, merge=True)
        return f"📚 Book soft-deleted: {slug}"

    # ── ISBN resolution ──────────────────────────────────────────────

    def _is_isbn_path(self, path: str, view: str | None) -> bool:
        """True if ``path`` looks like an ISBN (plain digits, possibly hyphenated).

        Also catches the case where the URI parser routes
        ``isbn:9780201021158`` to us with ``path='9780201021158'``; in
        that case the scheme has already been stripped.
        """
        if not path:
            return False
        if path.startswith("isbn:"):
            return True
        # If it's a raw digit string of 10 or 13 chars (post-normalise),
        # treat as ISBN.  Guard against slug-with-digits by requiring the
        # whole string is ``[0-9X-]+`` after trim.
        candidate = path.strip()
        if re.fullmatch(r"[0-9Xx\-\s]{10,17}", candidate):
            normalised = _normalise_isbn(candidate)
            return bool(normalised)
        return False

    def _resolve_by_isbn(self, store, path: str) -> dict | None:
        """Find the book ref for an ``isbn:`` path."""
        raw = path.removeprefix("isbn:") if path.startswith("isbn:") else path
        digits = _normalise_isbn(raw)
        if not digits:
            return None
        return self._find_by_isbn(store, digits)

    def _find_by_isbn(self, store, isbn_digits: str) -> dict | None:
        """Scan the corpus for a book whose meta matches ``isbn_digits``."""
        candidates = {isbn_digits}
        if len(isbn_digits) == 10:
            expanded = _isbn10_to_13(isbn_digits)
            if expanded:
                candidates.add(expanded)
        # We don't back-convert 13→10 because ISBN-13 has no unique 10
        # equivalent (the 979- prefix has no 10-form at all).

        for r in self._query_corpus_refs(store):
            meta = _parse_meta(r)
            if meta.get("isbn") in candidates:
                return r
            if meta.get("isbn10") in candidates:
                return r
        return None

    def _resolve_book_slug(self, store, path: str) -> str:
        """Return the canonical ``book:<slug>`` for a user-supplied id.

        Accepts ``book:<slug>``, bare ``<slug>``, ``isbn:<digits>``, or
        a bare ISBN digit string.
        """
        if path.startswith("book:"):
            return path
        if path.startswith("isbn:") or re.fullmatch(r"[0-9Xx\-\s]{10,17}", path):
            ref = self._resolve_by_isbn(store, path)
            if ref is None:
                raise PrecisError(
                    ErrorCode.ID_NOT_FOUND,
                    cause=f"book: no record for ISBN {path!r}",
                )
            return ref.get("slug", "") or f"book:{path}"
        return f"book:{path}"

    # ── Corpus query ─────────────────────────────────────────────────

    def _query_corpus_refs(self, store) -> list[dict]:
        """Return all non-deleted books, newest first."""
        try:
            from acatome_store.models import Ref
            from sqlalchemy import select
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                cause="book: acatome-store not installed",
                next="pip install precis-mcp[paper]",
            ) from exc

        with store._Session() as session:
            stmt = (
                select(Ref)
                .where(Ref.corpus_id == self.corpus_id)
                .order_by(Ref.first_seen_at.desc())
            )
            rows = session.execute(stmt).scalars().all()
            results: list[dict] = []
            for r in rows:
                d = r.to_dict()
                meta = _parse_meta(d)
                if meta.get("deleted"):
                    continue
                d["tags"] = _parse_tags(d)
                results.append(d)
            return results


def is_path_http(path: str) -> bool:
    """Tiny helper — ``True`` if ``path`` begins with an http(s) scheme.

    Used to detect when a user accidentally passes a URL as a book id.
    """
    return path.startswith("http://") or path.startswith("https://")
