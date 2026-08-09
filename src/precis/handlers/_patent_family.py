"""Deterministic patent-family lookups (Phase 2,
docs/backlog/patent-evidence-parity.md).

DOCDB family identity is EPO-authoritative data — ``refs.meta['family_id']``,
parsed at ingest by ``_patent_xml.py`` / stored by ``_patent_ingest.py`` — not
a *judged* identity, so unlike a taproot hub this is a pure read-side helper,
never a node kind. Two entry points:

* :func:`family_members` — every live, ingested ``patent`` ref sharing a
  ``family_id`` (stub or full ingest, undistinguished).
* :func:`family_representative` — the earliest-published member among them,
  deterministic tiebreak on cite-key slug ascending. "Published" prefers
  ``meta['publication_date']`` (finer-grained; populated whenever OPS served
  one) and falls back to ``refs.year`` for a ref that predates that
  ``year=`` fix; a member with neither sorts last.

Pure lookup — no writes, no LLM calls. ``_patent_ingest.py``'s simple-family
stubbing decision calls both (does a full member already exist? what's the
current representative to link the new stub to?); the cites view
(docs/backlog/patent-evidence-parity.md Phase 3) and hub-refine reuse the
same two functions rather than re-deriving family grouping. Stub-vs-full is
NOT distinguished by :func:`family_representative` — a family's
earliest-published member can itself be a stub, since ingest order need not
match publication order. A caller that specifically needs "a member bearing
an actual grounding passage" (Phase 3's render-time selection policy) filters
that separately; it is not this module's job.
"""

from __future__ import annotations

from typing import Any

from precis.store import Ref
from precis.store._mappers import _REFS_COLS_ALIASED, _row_to_ref


def family_members(store: Any, family_id: str | None) -> list[Ref]:
    """Every live ``patent`` ref carrying ``family_id`` in its meta.

    Unordered (see :func:`family_representative` for the deterministic
    order). ``[]`` for a falsy/unknown ``family_id`` — never raises, since
    ``family_id`` is an optional field on any given patent ref.
    """
    if not family_id:
        return []
    sql = f"""
        SELECT {_REFS_COLS_ALIASED}
        FROM refs r
        WHERE r.kind = 'patent' AND r.deleted_at IS NULL
          AND r.meta->>'family_id' = %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (family_id,)).fetchall()
    return [_row_to_ref(row) for row in rows]


def _publication_sort_key(ref: Ref) -> tuple[bool, str, str]:
    """Ascending sort key: earliest-published first, undated members last.

    ``meta['publication_date']`` (``YYYY-MM-DD``) wins when present.
    ``refs.year`` (older patents ingested before that column was populated
    won't have it either, but new ones always do) degrades to the string
    ``"YYYY-99-99"`` so a same-year dated peer is never displaced by a
    coarser year-only record purely on string comparison. Cite-key slug is
    the final deterministic tiebreak — two members published the same day
    (or both undated) still resolve to one stable representative.
    """
    meta = ref.meta or {}
    pub_date = meta.get("publication_date")
    date_key: str | None
    if isinstance(pub_date, str) and pub_date:
        date_key = pub_date
    elif ref.year is not None:
        date_key = f"{ref.year:04d}-99-99"
    else:
        date_key = None
    return (date_key is None, date_key or "", ref.slug or "")


def family_representative(store: Any, family_id: str | None) -> Ref | None:
    """The deterministic family representative for ``family_id``.

    Earliest-published ingested member; ties broken by cite-key slug
    ascending (see :func:`_publication_sort_key`). ``None`` for an unknown
    family or one with zero ingested members yet — callers must treat that
    as "no family context available," not as an error, since ``family_id``
    is optional per-ref and a family can be represented by exactly one
    ingested member (which trivially returns itself).
    """
    members = family_members(store, family_id)
    if not members:
        return None
    return sorted(members, key=_publication_sort_key)[0]


__all__ = ["family_members", "family_representative"]
