"""One-shot backfill: paper→paper ``cites`` links from matched bib entries
(gr513755 slice 1).

``bib_parse`` resolves each ``paper_bib_entries`` row to a DOI and — when the
cited paper is already in the corpus — a ``held_ref_id``. Since slice 1 the
match write also mints the paper-level ``cites`` edge
(:func:`precis.workers.bib_parse.ensure_bib_cites_links`); this module covers
the ~10k entries matched *before* that write existed, via
``precis backfill-bib-cites`` (dry-run by default, ``--apply`` writes).

Selection is one row per distinct ``(ref_id, held_ref_id)`` pair still missing
a paper-level edge — the NOT EXISTS check requires both chunk ids NULL because
a chunk-scoped ``cites`` edge (inbound_chase's resolved form) does not say
"the paper cites the paper" on its own. Self-cites and retired targets are
excluded in SQL so a dry-run's count is exactly what ``--apply`` would insert.
Each citing paper's edges are ensured by the shared helper in one short
transaction per ``batch_refs`` papers — a crash mid-run just re-selects the
unwritten remainder next invocation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Citing papers per write transaction — bounds one commit's size on a pool
#: whose connections carry prod's ``lock_timeout=5s``.
_DEFAULT_BATCH_REFS = 200


@dataclass
class BibCitesBackfillResult:
    """Outcome of one :func:`backfill_bib_cites` call."""

    pairs_missing: int
    papers: int
    links_ensured: int = 0


def missing_bib_cites(
    conn: Any, *, limit: int | None = None
) -> list[tuple[int, int, int]]:
    """``(ref_id, bib_entry_id, held_ref_id)`` — one row per distinct pair
    that would get a link.

    ``bib_entry_id`` is ``min(e.id)`` for the pair, matching
    :func:`~precis.workers.bib_parse.ensure_bib_cites_links`' lowest-entry-id
    provenance choice so the dry-run set and the applied set stamp the same
    ``meta.bib_entry_id``.
    """
    sql = (
        "SELECT e.ref_id, min(e.id) AS entry_id, e.held_ref_id "
        "FROM paper_bib_entries e "
        "JOIN refs held ON held.ref_id = e.held_ref_id "
        "WHERE e.held_ref_id IS NOT NULL "
        "AND e.held_ref_id <> e.ref_id "
        "AND held.retired_at IS NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM links l WHERE l.relation = 'cites' "
        "  AND l.src_ref_id = e.ref_id AND l.dst_ref_id = e.held_ref_id "
        "  AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL"
        ") GROUP BY e.ref_id, e.held_ref_id ORDER BY e.ref_id, e.held_ref_id"
    )
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT %s"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


def backfill_bib_cites(
    store: Store,
    *,
    apply: bool,
    limit: int | None = None,
    batch_refs: int = _DEFAULT_BATCH_REFS,
) -> BibCitesBackfillResult:
    """Mint the missing edges, ``batch_refs`` citing papers per transaction.

    Dry-run (``apply=False``) only counts — ``pairs_missing`` is the number of
    ``links`` rows ``--apply`` would attempt (each ``add_link`` is still
    idempotent against a concurrent writer). ``limit`` caps the pairs
    selected, ``papers`` reports how many distinct citing papers they span.
    """
    from precis.workers.bib_parse import ensure_bib_cites_links

    with store.pool.connection() as conn:
        rows = missing_bib_cites(conn, limit=limit)
        conn.commit()

    result = BibCitesBackfillResult(
        pairs_missing=len(rows), papers=len({r[0] for r in rows})
    )
    if not apply or not rows:
        return result

    by_ref: dict[int, list[tuple[int, int]]] = {}
    for ref_id, entry_id, held_ref_id in rows:
        by_ref.setdefault(ref_id, []).append((entry_id, held_ref_id))

    ref_ids = sorted(by_ref)
    for i in range(0, len(ref_ids), batch_refs):
        with store.pool.connection() as conn:
            for rid in ref_ids[i : i + batch_refs]:
                result.links_ensured += ensure_bib_cites_links(
                    store, rid, by_ref[rid], conn=conn
                )
            conn.commit()
    return result


__all__ = [
    "BibCitesBackfillResult",
    "backfill_bib_cites",
    "missing_bib_cites",
]
