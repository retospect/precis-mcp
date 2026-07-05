"""PDF → ``kind='pres'`` ingest pipeline.

Parallel to :mod:`precis.ingest.pipeline` / :mod:`precis.ingest.db_writer`
but for slide decks: one chunk per slide, ``chunk_kind='pres_slide'``,
``subtype:slides`` open tag minted on creation, no metadata cascade
(CrossRef / S2 / pdf2doi all skipped — decks don't have DOIs).

Public surface:

* :class:`PresSlide` — one slide row to insert.
* :class:`PresToWrite` — what :func:`write_pres` consumes (parallel to
  :class:`precis.ingest.db_writer.PaperToWrite`).
* :class:`PresWriteResult` — what :func:`write_pres` returns.
* :func:`extract_pres` — pipeline producer; runs Marker, groups blocks
  by page, mints slide titles.
* :func:`write_pres` — atomic INSERT cascade (refs + ref_identifiers +
  chunks). Caller owns the transaction.

Idempotency is keyed on ``pdf_sha256``: the same hash already in
``ref_identifiers`` short-circuits at :func:`precis.ingest.db_writer.probe_existing`
before we get here, regardless of kind. So a pres ingest that loses
the race to a paper-kind ingest of the same bytes (extremely rare)
returns the paper ref id — which the caller treats as a hit.

Slug-collision policy: ``pdf_sha256`` hit is silently idempotent
(merge tags only, no new ref). A miss with the *slug* already taken
suffixes ``-2``, ``-3``, … with a warning log. The numeric suffix
diverges from :func:`precis.identity.make_cite_key`'s ``a/b/c`` style
on purpose — pres slugs are user-typed in directory paths and
``lecture-3-2`` reads more naturally than ``lecture-3a``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.identity import make_node_id, make_pdf_sha256

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PresSlide:
    """One slide chunk. ``pos`` is the 0-indexed slide number; matches
    ``chunks.ord`` directly."""

    pos: int
    text: str
    slide_title: str
    page: int
    image_base64: str | None = None
    image_mime: str | None = None


@dataclass(frozen=True)
class PresToWrite:
    """Everything :func:`write_pres` needs to assemble a pres ref's
    rows across ``refs``, ``ref_identifiers``, ``pdfs``, and ``chunks``.

    ``slug`` is the requested slug; collision resolution (``-2``, …)
    happens inside the writer. ``slug_hint_was_collision_free`` is
    returned in the result so callers can log whether a suffix was
    applied.
    """

    slug: str
    title: str
    subtype: str = "slides"
    pdf_sha256: str | None = None
    pdf_page_count: int | None = None
    pdf_size_bytes: int | None = None
    pdf_storage_path: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    slides: list[PresSlide] = field(default_factory=list)


@dataclass(frozen=True)
class PresWriteResult:
    """Outcome of a successful :func:`write_pres` call."""

    ref_id: int
    slug: str
    n_slides: int
    slug_suffixed: bool


# ---------------------------------------------------------------------------
# Slug + title derivation
# ---------------------------------------------------------------------------


_SLUG_SAFE_RE = re.compile(r"[^a-z0-9]+")
_LEADING_YEAR_RE = re.compile(r"^(20\d{2})(?:[-_/.]+(\d{1,2}))?")


def kebab_slug(raw: str) -> str:
    """Lowercase, collapse runs of non-alphanumerics to ``-``, strip
    leading/trailing dashes. Used for both slugs and ``topic:`` tag
    values from path components.

    >>> kebab_slug("Matthias_Quantum Lecture 3.pdf")
    'matthias-quantum-lecture-3-pdf'
    >>> kebab_slug("  --foo--BAR--  ")
    'foo-bar'
    """
    s = _SLUG_SAFE_RE.sub("-", raw.lower())
    return s.strip("-")


def derive_pres_slug(pdf_path: Path) -> str:
    """Derive a slug from the PDF filename.

    Strategy:
    1. Kebab the stem.
    2. If it already starts with a 4-digit year (optionally followed
       by a month), keep it.
    3. Otherwise prepend ``YYYY-MM`` from the file's mtime so different
       drops of ``lecture-3.pdf`` in different months don't fight for
       the same slug.

    The result is *requested*; :func:`write_pres` runs collision
    resolution and may suffix ``-2``/``-3``.
    """
    stem_kebab = kebab_slug(pdf_path.stem)
    if not stem_kebab:
        stem_kebab = "untitled"
    if _LEADING_YEAR_RE.match(stem_kebab):
        return stem_kebab
    try:
        mtime = datetime.fromtimestamp(pdf_path.stat().st_mtime, tz=UTC)
        prefix = mtime.strftime("%Y-%m")
    except OSError:
        prefix = datetime.now(tz=UTC).strftime("%Y-%m")
    return f"{prefix}-{stem_kebab}"


def derive_pres_title(pdf_path: Path) -> str:
    """Humanize the filename stem into a default title."""
    stem = pdf_path.stem.replace("_", " ").replace("-", " ").strip()
    return stem or pdf_path.name


# ---------------------------------------------------------------------------
# extract_pres — one PDF page = one slide (fitz)
# ---------------------------------------------------------------------------

# A standalone line that is nothing but a small integer — a slide-number
# stamp. We skip it when choosing a title and trim a leading one off the
# body so "3\nTechniques\n…" reads as "Techniques".
_SLIDE_NUMBER_RE = re.compile(r"^\d{1,4}$")


def extract_pres(
    pdf_path: Path,
    *,
    slug_hint: str | None = None,
    title_hint: str | None = None,
) -> PresToWrite:
    """Build a :class:`PresToWrite` from a local slide PDF.

    1. Read bytes, compute ``pdf_sha256``.
    2. Extract text **page by page** with fitz (PyMuPDF) — a slide deck
       is one slide per PDF page by construction, so the page index *is*
       the slide index. (We deliberately do **not** route decks through
       Marker: Marker flattens to a single markdown blob and then
       re-derives per-block pages from the PDF's table of contents
       (:func:`precis.ingest.marker._assign_pages`). Decks have no TOC,
       so every block collapses onto page 0 and the whole deck lands in
       one or two giant "slides". Page-faithful fitz avoids that.)
    3. Strip running headers/footers (a slide master repeats a title or
       date band on every page) so the real per-slide heading surfaces.
    4. For each page: derive ``slide_title`` (first short non-numeric
       line, else ``Slide N``) and use the cleaned page text as the body.

    Empty pages still produce a placeholder slide so positions stay
    1:1 with the source PDF's pagination.

    Raises :class:`FileNotFoundError` if ``pdf_path`` is missing.

    NB no images are attached: the page-faithful text path drops the
    per-slide raster Marker used to base64 in. Deck bodies are text and
    that is what search indexes; page thumbnails are a possible
    follow-up (fitz ``get_pixmap``) if a visual is ever wanted.
    """
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    pdf_bytes = pdf_path.read_bytes()
    pdf_sha256 = make_pdf_sha256(pdf_bytes)
    slug = slug_hint or derive_pres_slug(pdf_path)
    title = title_hint or derive_pres_title(pdf_path)

    slides, n_pages = _extract_pres_slides(pdf_path)
    if not slides:
        # No extractable pages — image-only deck with OCR off, encrypted
        # PDF, etc. Emit a single placeholder so the ref exists and the
        # operator notices the empty body on search rather than the file
        # being swallowed silently.
        slides = [
            PresSlide(
                pos=0,
                text="",
                slide_title="Slide 1 (empty extraction)",
                page=0,
            )
        ]
        n_pages = 1

    return PresToWrite(
        slug=slug,
        title=title,
        pdf_sha256=pdf_sha256,
        pdf_page_count=n_pages,
        pdf_size_bytes=len(pdf_bytes),
        pdf_storage_path=str(pdf_path),
        meta={"source_pdf": pdf_path.name},
        slides=slides,
    )


def _extract_pres_slides(pdf_path: Path) -> tuple[list[PresSlide], int]:
    """Extract one :class:`PresSlide` per PDF page via fitz.

    Returns ``(slides, page_count)``. ``slides`` is 1:1 with the PDF's
    pages (empty pages become placeholder slides). ``page_count`` is the
    true PDF page count. Returns ``([], 0)`` if the PDF has no pages, so
    the caller can emit its own placeholder.
    """
    # Deferred imports: fitz pulls PyMuPDF; the marker helpers pull the
    # chemistry-safe text cleaner and the running-header stripper. Both
    # keep ``precis serve`` / ``precis migrate`` lean.
    import fitz

    from precis.ingest.marker import _clean_text, _strip_running_lines

    doc = fitz.open(str(pdf_path))
    total_pages = doc.page_count
    raw_pages: list[tuple[int, str]] = [
        (i, _clean_text(doc[i].get_text())) for i in range(total_pages)
    ]
    doc.close()

    if not raw_pages:
        return [], 0

    # Strip lines that repeat on ≥40% of pages (deck master header/footer).
    # Feed only non-empty pages; map the result back by page index.
    stripped = dict(
        _strip_running_lines([(i, t) for i, t in raw_pages if t], total_pages)
    )

    slides: list[PresSlide] = []
    for i, _raw in raw_pages:
        slide_title, body = _slide_from_page_text(stripped.get(i, ""), i)
        slides.append(PresSlide(pos=i, text=body, slide_title=slide_title, page=i))
    return slides, total_pages


def _slide_from_page_text(text: str, pos: int) -> tuple[str, str]:
    """Split one page's cleaned text into ``(slide_title, body)``.

    Drops a leading standalone slide-number line, then takes the first
    short (≤80 char) non-numeric line as the title and returns the
    remaining text as the body.
    """
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and _SLIDE_NUMBER_RE.match(lines[0].strip()):
        lines.pop(0)
    body = "\n".join(lines).strip()

    slide_title: str | None = None
    for ln in lines:
        s = ln.strip()
        if s and not _SLIDE_NUMBER_RE.match(s) and len(s) <= 80:
            slide_title = s
            break
    return (slide_title or f"Slide {pos + 1}"), body


# ---------------------------------------------------------------------------
# write_pres — atomic INSERT cascade
# ---------------------------------------------------------------------------


def write_pres(pres: PresToWrite, *, conn: Connection) -> PresWriteResult:
    """Insert ``pres`` into the v2 schema.

    Caller owns the transaction. Steps:

    1. Resolve the final slug — if ``pres.slug`` is taken in
       ``ref_identifiers(id_kind='cite_key')`` we suffix ``-2``,
       ``-3``, … until free, logging a warning.
    2. ``INSERT INTO pdfs ON CONFLICT DO NOTHING`` (if sha is known).
    3. ``INSERT INTO refs (kind='pres', title, pdf_sha256, meta)
       RETURNING ref_id``.
    4. ``INSERT INTO ref_identifiers`` for ``cite_key=<slug>`` and
       ``pdf_sha256=<sha>`` (latter is what the next ingest's
       ``probe_existing`` finds for idempotency).
    5. ``INSERT INTO chunks`` — one row per slide,
       ``chunk_kind='pres_slide'``, meta carries ``slide_index``,
       ``slide_title``, optional image base64.

    The ``subtype:slides`` tag and any caller-supplied ``extra_tags``
    are *not* written here — that's the caller's responsibility
    after commit (see :func:`precis.ingest.add._ingest_pres_pdf`).
    Tag application uses :func:`Store.add_tag` which has its own
    upsert semantics; keeping it outside this writer's transaction
    means a tag-validation failure can't roll back the body.
    """
    if not pres.slug:
        raise ValueError("PresToWrite.slug is required")
    if not pres.title:
        raise ValueError("PresToWrite.title is required")

    # 1. Resolve slug
    final_slug, suffixed = _resolve_pres_slug(pres.slug, conn=conn)
    if suffixed:
        log.warning(
            "write_pres: slug %r taken — using %r",
            pres.slug,
            final_slug,
        )

    # 2. pdfs row (if any)
    if pres.pdf_sha256 is not None:
        conn.execute(
            "INSERT INTO pdfs "
            "(pdf_sha256, content_hash, page_count, size_bytes, storage_path) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (
                pres.pdf_sha256,
                pres.pdf_sha256,  # pres has no separate content_hash; reuse
                pres.pdf_page_count or 0,
                pres.pdf_size_bytes or 0,
                pres.pdf_storage_path or "",
            ),
        )

    # 3. refs row
    row = conn.execute(
        "INSERT INTO refs "
        "(kind, set_by, title, provider, pdf_sha256, meta) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "RETURNING ref_id",
        (
            "pres",
            "system",
            pres.title,
            "local",
            pres.pdf_sha256,
            Jsonb(pres.meta or {}),
        ),
    ).fetchone()
    assert row is not None
    ref_id_value = row[0]
    assert isinstance(ref_id_value, int)
    ref_id: int = ref_id_value

    # 4. ref_identifiers — slug + sha. The ``cite_key`` row is what
    # ``Store.get_ref(kind='pres', id=slug)`` and friends resolve
    # against; the ``pdf_sha256`` row is the idempotency probe key.
    conn.execute(
        "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (id_kind, id_value) DO NOTHING",
        ("cite_key", final_slug, ref_id, "local"),
    )
    if pres.pdf_sha256:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id_kind, id_value) DO NOTHING",
            ("pdf_sha256", pres.pdf_sha256, ref_id, "local"),
        )

    # 5. chunks — one per slide
    chunk_rows: list[tuple[Any, ...]] = []
    for slide in pres.slides:
        slide_meta: dict[str, Any] = {
            "chunk_kind": "pres_slide",
            "slide_index": slide.pos,
            "slide_title": slide.slide_title,
            "page": slide.page,
            "node_id": make_node_id(f"pres:{final_slug}", slide.page, slide.pos),
        }
        if slide.image_base64:
            slide_meta["image_base64"] = slide.image_base64
            if slide.image_mime:
                slide_meta["image_mime"] = slide.image_mime
        chunk_rows.append(
            (
                ref_id,
                "system",
                slide.pos,
                "pres_slide",
                slide.text,
                [],  # section_path — pres has no section hierarchy
                slide.page,
                slide.page,
                None,  # token_count — populated later by the worker
                Jsonb(slide_meta),
                [],  # numerics
            )
        )
    if chunk_rows:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks "
                "(ref_id, set_by, ord, chunk_kind, text, section_path, "
                " page_first, page_last, token_count, meta, numerics) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                chunk_rows,
            )

    return PresWriteResult(
        ref_id=ref_id,
        slug=final_slug,
        n_slides=len(chunk_rows),
        slug_suffixed=suffixed,
    )


def _resolve_pres_slug(slug: str, *, conn: Connection) -> tuple[str, bool]:
    """Return ``(final_slug, suffixed)``.

    Probes ``ref_identifiers`` for a ``cite_key`` row matching
    ``slug``; if present, tries ``slug-2``, ``slug-3``, … until a
    free name is found. Bounded at 1000 candidates to surface
    pathological inputs loudly instead of looping forever.

    Race-safe note: ``ref_identifiers`` has a UNIQUE (id_kind,
    id_value) constraint, so two concurrent writers racing on the
    same slug both pass the probe but only one wins the INSERT.
    The loser's ``write_pres`` will raise on the INSERT — caller
    retries with the next ingest, which now sees the slug as taken
    and suffixes. The window is tiny; we accept the noisy retry over
    holding an advisory lock just for slug allocation.
    """
    if not _slug_taken(slug, conn=conn):
        return slug, False
    for n in range(2, 1001):
        candidate = f"{slug}-{n}"
        if not _slug_taken(candidate, conn=conn):
            return candidate, True
    raise RuntimeError(
        f"pres slug suffix progression exhausted for {slug!r} (>1000 collisions)"
    )


def _slug_taken(slug: str, *, conn: Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ref_identifiers "
        "WHERE id_kind = 'cite_key' AND id_value = %s LIMIT 1",
        (slug,),
    ).fetchone()
    return row is not None


__all__ = [
    "PresSlide",
    "PresToWrite",
    "PresWriteResult",
    "derive_pres_slug",
    "derive_pres_title",
    "extract_pres",
    "kebab_slug",
    "write_pres",
]
