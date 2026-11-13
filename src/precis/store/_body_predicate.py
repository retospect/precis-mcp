"""Has-body SQL fragment — "does this ref carry extracted text?"

``refs.pdf_sha256 IS NOT NULL`` says a paper's *bytes* are held; it says
nothing about whether extraction ever produced text. A promoted ref with
no ``ord >= 0`` chunk (gr453860: ~950 on prod — Elsevier entitlement
previews, corrupt or scanned PDFs, files lost on disk, and a few that
simply never ran through Marker) is unsearchable, can host no finding and
grounds no citation. Every call site that means "has text" uses this
fragment instead of the PDF flag; call sites that genuinely mean "has a
PDF" (dedup by file, the fetch backlog, the reproducibility signal in
``paper_rank``, the sha a nanopub pins) keep ``pdf_sha256``.

Body = a ``chunks`` row with ``ord >= 0``. The ``ord < 0`` card variants a
stub carries from mint time are metadata, not body — counting them is the
mistake that made the bodiless class invisible.
"""

from __future__ import annotations


def has_body_sql(alias: str = "r", *, sub_alias: str = "hb") -> str:
    """``EXISTS (...)`` fragment: does ``<alias>`` have at least one body chunk?

    References ``<alias>.ref_id``; the caller's query must alias the
    ``refs`` row accordingly. ``sub_alias`` names the inner ``chunks`` row
    so the fragment can sit next to another ``chunks`` subquery in the
    same statement. Negate with ``NOT``. Contains no ``%`` or ``?``, so it
    is safe inside both parameterised and bare queries.
    """
    return (
        f"EXISTS (SELECT 1 FROM chunks {sub_alias} "
        f"WHERE {sub_alias}.ref_id = {alias}.ref_id AND {sub_alias}.ord >= 0)"
    )


#: Open tags that say "the text this ref holds is not a usable article body":
#: ``body:partial`` (>= 90% references/SI after its one bounded re-fetch) and
#: ``body:needs_ocr`` (extraction empty after its retry). Set by
#: ``paper_hygiene``; read-for-question gates treat a tagged ref as bodiless.
UNUSABLE_BODY_TAGS: tuple[str, ...] = ("body:partial", "body:needs_ocr")


def unusable_body_tag_sql(alias: str = "r") -> str:
    """``EXISTS (...)`` fragment: does ``<alias>`` carry an unusable-body tag?

    Pair with :func:`has_body_sql` for "has *usable* text":
    ``has_body AND NOT unusable_body_tag``. References ``<alias>.ref_id``.
    """
    tags = ", ".join(f"'{t}'" for t in UNUSABLE_BODY_TAGS)
    return (
        "EXISTS (SELECT 1 FROM ref_tags ubt JOIN tags ubtt "
        f"ON ubtt.tag_id = ubt.tag_id WHERE ubt.ref_id = {alias}.ref_id "
        f"AND ubtt.namespace = 'OPEN' AND ubtt.value IN ({tags}))"
    )


#: Ceiling on the live non-``references`` body-chunk count below which a
#: body is *thin* — the abstract-only / skeleton shape gr608581 caught a
#: census counting as full text. The same bound
#: ``paper_hygiene._FRONT_MATTER_MAX_CHUNKS`` measures the Elsevier
#: entitlement preview against (~8 front-matter chunks); a genuine paper
#: runs into the dozens. Only meaningful next to a fetcher verdict — a
#: short real paper keeps ``'full-text'``.
THIN_BODY_MAX_CHUNKS: int = 12


def body_state_sql(alias: str = "r") -> str:
    """``CASE`` expression → ``'full-text'`` | ``'thin-text'`` |
    ``'card-only'`` | ``'stub'``.

    'full-text' = a live non-card, non-``references`` body chunk exists
    (``ord >= 0`` — stricter than :func:`has_body_sql`, which counts any
    body chunk including ``references``); 'thin-text' = those chunks
    exist but number at most :data:`THIN_BODY_MAX_CHUNKS` AND the ref's
    newest ``fetcher:`` event is ``no_oa_version`` — an abstract or
    skeleton, not article body (gr608581); 'card-only' = only ``card_*``
    chunks (``ord < 0``); 'stub' = no live chunks at all. The same
    four-way vocabulary :meth:`Store.paper_fetch_status` reports per
    paper — kept here so facet/grouping SQL shares it. References
    ``<alias>.ref_id``; contains no ``%``, safe inside parameterised
    queries.
    """
    return (
        "CASE WHEN (SELECT count(*) FROM chunks f "
        f"WHERE f.ref_id = {alias}.ref_id AND f.retired_at IS NULL "
        "AND f.ord >= 0 AND f.chunk_kind <> 'references') "
        f"BETWEEN 1 AND {THIN_BODY_MAX_CHUNKS} "
        "AND (SELECT e.event FROM ref_events e "
        f"WHERE e.ref_id = {alias}.ref_id "
        "AND starts_with(e.source, 'fetcher:') "
        "ORDER BY e.ts DESC LIMIT 1) = 'no_oa_version' THEN 'thin-text' "
        "WHEN EXISTS (SELECT 1 FROM chunks f "
        f"WHERE f.ref_id = {alias}.ref_id AND f.retired_at IS NULL "
        "AND f.ord >= 0 AND f.chunk_kind <> 'references') THEN 'full-text' "
        "WHEN EXISTS (SELECT 1 FROM chunks cd "
        f"WHERE cd.ref_id = {alias}.ref_id AND cd.retired_at IS NULL "
        "AND cd.ord < 0) THEN 'card-only' "
        "ELSE 'stub' END"
    )


#: Storage-ord base of the ``paper_note`` band — the same
#: ``handlers/paper.py::_NOTE_ORD_BASE`` / ``_links_ops`` mirror: note
#: number K sits at ``ord = -1_000_000 + K - 1``, so K reads back as
#: ``ord - (-1_000_000) + 1``.
_PAPER_NOTE_ORD_BASE = -1_000_000


def read_note_sql(alias: str = "r", *, sub_alias: str = "rn") -> str:
    """``EXISTS (...)`` fragment: does ``<alias>`` carry a visible
    ``read-paper:`` closing note?

    The ``precis-read-paper`` pass ends by appending a ``paper_note``
    chunk whose text opens ``read-paper:`` — the corpus's only
    per-paper "already read" marker (gr653528: search can filter it,
    so a sweep never re-reads). A note *hidden* via
    ``delete(..., args={'comment': K})`` doesn't count: the hide lives
    in ``refs.meta->'hidden_comments'`` (K ↔ ord as
    :data:`_PAPER_NOTE_ORD_BASE`), the chunk stays live — the SQL has
    to mask it back out. References ``<alias>.ref_id`` and
    ``<alias>.meta``; contains no ``%``, safe inside parameterised
    queries. Negate with ``NOT``.
    """
    return (
        f"EXISTS (SELECT 1 FROM chunks {sub_alias} "
        f"WHERE {sub_alias}.ref_id = {alias}.ref_id "
        f"AND {sub_alias}.retired_at IS NULL "
        f"AND {sub_alias}.chunk_kind = 'paper_note' "
        f"AND starts_with(lower({sub_alias}.text), 'read-paper:') "
        f"AND NOT (COALESCE({alias}.meta->'hidden_comments', '[]'::jsonb) "
        f"@> to_jsonb({sub_alias}.ord - ({_PAPER_NOTE_ORD_BASE}) + 1)))"
    )


def ingested_at_sql(alias: str = "r", *, sub_alias: str = "ig") -> str:
    """Scalar ``COALESCE(...)`` expression: when ``<alias>`` landed in
    the corpus.

    The landing stamp — NOT ``refs.created_at``: a stub's ref row is
    minted at fetch-queue time, its body lands whenever a later ingest
    fills it. Same ``(ref-created, body-created)`` split
    :meth:`Store.paper_ingest_rate` counts landings on: earliest live
    non-``references`` body chunk (``ord >= 0``), falling back to
    ``refs.created_at`` for card-only stubs and chunkless refs.
    References ``<alias>.ref_id`` and ``<alias>.created_at``; contains
    no ``%``, safe inside parameterised queries.
    """
    return (
        f"COALESCE((SELECT min({sub_alias}.created_at) FROM chunks {sub_alias} "
        f"WHERE {sub_alias}.ref_id = {alias}.ref_id "
        f"AND {sub_alias}.retired_at IS NULL AND {sub_alias}.ord >= 0 "
        f"AND {sub_alias}.chunk_kind <> 'references'), {alias}.created_at)"
    )


__all__ = [
    "THIN_BODY_MAX_CHUNKS",
    "UNUSABLE_BODY_TAGS",
    "body_state_sql",
    "has_body_sql",
    "ingested_at_sql",
    "read_note_sql",
    "unusable_body_tag_sql",
]
