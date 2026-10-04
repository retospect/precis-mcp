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

import logging
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

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


#: Per-call cap on attention-queued papers from one walker touch, so a wide
#: ring never turns into a sweep.
ATTENTION_WALK_CAP = 20


_warned_db_error = False


def queue_si_on_attention_many(store: Any, ref_ids: list[int], by: str) -> int:
    """Queue one SI check for each eligible paper in ``ref_ids``; returns the
    number newly queued.

    One conditional UPDATE (no prior SELECT, one connection, one commit):
    stamps ``meta.si_fetch`` with ``trigger='attention'`` unless the paper is a
    supplement / retired / has no DOI, or already carries ``si_fetch`` or
    ``si_checked_at`` -- so attention checks a paper once, ever; only an
    explicit ``fetch-si`` re-checks. Never raises (read-path hook): a DB error
    returns 0 (warning once per process, debug after). Gated by setting
    ``si.attention_enabled`` (fail-open: only an explicit False disables).
    """
    global _warned_db_error
    if not ref_ids:
        return 0
    try:
        from precis import settings

        enabled = settings.get_bool("si.attention_enabled", store=store, default=True)
        if enabled is False:
            return 0
        with store.pool.connection() as conn:
            cur = conn.execute(
                """
                UPDATE refs SET meta = meta || jsonb_build_object(
                    'si_fetch', jsonb_build_object(
                        'requested_at', %s::text, 'by', %s::text,
                        'trigger', 'attention'))
                 WHERE ref_id = ANY(%s)
                   AND kind = 'paper'
                   AND retired_at IS NULL
                   AND COALESCE(pdf_role, 'main') <> 'supplement'
                   AND NOT jsonb_exists(meta, 'si_fetch')
                   AND NOT jsonb_exists(meta, 'si_checked_at')
                   AND EXISTS (SELECT 1 FROM ref_identifiers
                                WHERE ref_id = refs.ref_id AND id_kind = 'doi')
                """,
                (utc_stamp(), by, [int(r) for r in ref_ids]),
            )
            queued = max(cur.rowcount, 0)
            conn.commit()
        return queued
    except Exception as exc:
        level = logging.DEBUG if _warned_db_error else logging.WARNING
        _warned_db_error = True
        log.log(level, "si attention queue failed (%d refs): %s", len(ref_ids), exc)
        return 0


def queue_si_on_attention(store: Any, ref_id: int, by: str) -> bool:
    """Single-paper :func:`queue_si_on_attention_many`; True when queued."""
    return queue_si_on_attention_many(store, [ref_id], by) > 0
