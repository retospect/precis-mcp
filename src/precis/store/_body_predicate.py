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


def body_state_sql(alias: str = "r") -> str:
    """``CASE`` expression → ``'full-text'`` | ``'card-only'`` | ``'stub'``.

    'full-text' = a live non-card, non-``references`` body chunk exists
    (``ord >= 0`` — stricter than :func:`has_body_sql`, which counts any
    body chunk including ``references``); 'card-only' = only ``card_*``
    chunks (``ord < 0``); 'stub' = no live chunks at all. The same
    three-way vocabulary :meth:`Store.paper_fetch_status` reports per
    paper — kept here so facet/grouping SQL shares it. References
    ``<alias>.ref_id``; contains no ``%``, safe inside parameterised
    queries.
    """
    return (
        "CASE WHEN EXISTS (SELECT 1 FROM chunks f "
        f"WHERE f.ref_id = {alias}.ref_id AND f.retired_at IS NULL "
        "AND f.ord >= 0 AND f.chunk_kind <> 'references') THEN 'full-text' "
        "WHEN EXISTS (SELECT 1 FROM chunks cd "
        f"WHERE cd.ref_id = {alias}.ref_id AND cd.retired_at IS NULL "
        "AND cd.ord < 0) THEN 'card-only' "
        "ELSE 'stub' END"
    )


__all__ = [
    "UNUSABLE_BODY_TAGS",
    "body_state_sql",
    "has_body_sql",
    "unusable_body_tag_sql",
]
