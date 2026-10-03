"""SI-attachment vocabulary and parent lookups shared by ingest, the worker,
the paper handler and the store's citation resolver.

Relation choice (build 1): the spec names ``supplements`` / ``has-supplement``
but adding a relation needs a ``relations`` seed migration, which build 1 was
told not to ship. The existing ``part-of`` / ``contains`` pair carries the same
meaning (the SI is part of the paper) and the edge is stamped
``links.meta.role = 'supplement'`` so it is distinguishable from the component
BOM use. Swapping to the dedicated pair later is the two constants below plus
a backfill of the existing edges.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: Stored edge direction: SI ref (src) -> parent paper (dst).
SI_RELATION = "part-of"
#: The inverse, read from the parent's side.
SI_INVERSE_RELATION = "contains"
#: ``refs.pdf_role`` of an SI ref.
SI_PDF_ROLE = "supplement"
#: ``links.meta`` stamp marking an SI edge.
SI_LINK_META: dict[str, Any] = {"role": "supplement"}


def utc_stamp() -> str:
    """Microsecond-precision UTC ISO stamp, ``Z``-suffixed. Fixed width, so
    the lexicographic ``si_checked_at < requested_at`` claim predicate is a
    correct time comparison."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def supplement_parent(conn: Any, ref_id: int) -> tuple[int, str | None] | None:
    """``(parent_ref_id, parent_cite_key)`` when ``ref_id`` is a live SI ref
    with an SI edge to a live parent, else ``None``.

    One round trip. The query itself also requires ``pdf_role = 'supplement'``,
    but callers on a hot path (``resolve_handle``, the paper overview) already
    hold the ref's ``pdf_role`` and must check it BEFORE calling, so an
    ordinary paper never pays this query.
    """
    row = conn.execute(
        """
        SELECT p.ref_id,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = p.ref_id AND id_kind = 'cite_key')
          FROM refs s
          JOIN links l ON l.src_ref_id = s.ref_id
                      AND l.relation = %s
                      AND l.meta->>'role' = 'supplement'
          JOIN refs p ON p.ref_id = l.dst_ref_id AND p.retired_at IS NULL
         WHERE s.ref_id = %s
           AND s.pdf_role = 'supplement'
         ORDER BY l.link_id
         LIMIT 1
        """,
        (SI_RELATION, ref_id),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), (str(row[1]) if row[1] is not None else None)


def supplement_children(
    conn: Any, parent_ref_id: int
) -> list[tuple[int, str | None, str]]:
    """``(ref_id, cite_key, title)`` of the live SI refs of ``parent_ref_id``."""
    rows = conn.execute(
        """
        SELECT s.ref_id,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = s.ref_id AND id_kind = 'cite_key'),
               s.title
          FROM links l
          JOIN refs s ON s.ref_id = l.src_ref_id AND s.retired_at IS NULL
                     AND s.pdf_role = 'supplement'
         WHERE l.dst_ref_id = %s
           AND l.relation = %s
           AND l.meta->>'role' = 'supplement'
         ORDER BY s.ref_id
        """,
        (parent_ref_id, SI_RELATION),
    ).fetchall()
    return [
        (int(r[0]), (str(r[1]) if r[1] is not None else None), str(r[2] or ""))
        for r in rows
    ]
