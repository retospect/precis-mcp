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


__all__ = ["has_body_sql"]
