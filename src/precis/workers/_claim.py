"""Shared claim query for the per-ref body-text sweeps.

``bib_parse``, ``bib_retag``, ``paper_glossary`` and ``classify_topics`` each
select "live refs of these kinds that have body chunks and are not yet done",
oldest ``ref_id`` first, as ``(ref_id, title)``. They differ only in four
things, which are the parameters here: the ref kinds, the SQL predicate (and
binds) that says "not done yet", whether a claim-time attempt lease excludes
recently-attempted refs, and whether the SELECT takes ``FOR UPDATE OF r SKIP
LOCKED``.

Pure SQL plumbing; the caller owns the connection and its commit.
"""

from __future__ import annotations

from typing import Any

from precis.workers import ref_lease

__all__ = ["claim_batch"]


def claim_batch(
    conn: Any,
    *,
    kinds: list[str],
    not_done_sql: str,
    params: dict[str, Any],
    limit: int,
    ref_ids: list[int] | None = None,
    lease_ns: str | None = None,
    skip_locked: bool = False,
) -> list[tuple[int, str]]:
    """``(ref_id, title)`` for up to ``limit`` live refs with body chunks.

    Args:
        kinds: ``refs.kind`` values to sweep.
        not_done_sql: SQL boolean expression (over alias ``r``) that is true
            while the ref still needs work; may use binds from ``params``.
        params: bind values for ``not_done_sql``.
        ref_ids: restrict the sweep to these refs (targeted backfill / tests).
        lease_ns: when set, also exclude refs under an unexpired attempt lease
            in this namespace (pass the already-built
            ``ref_lease.attempt_ns(...)`` value).
        skip_locked: append ``FOR UPDATE OF r SKIP LOCKED``.
    """
    ref_filter = "AND r.ref_id = ANY(%(ref_ids)s)" if ref_ids else ""
    lease = ref_lease.exclude_clause("r.ref_id", "attempt_ns") if lease_ns else ""
    lock = "FOR UPDATE OF r SKIP LOCKED" if skip_locked else ""
    sql = f"""
        SELECT r.ref_id, r.title
        FROM refs r
        WHERE r.kind = ANY(%(kinds)s) AND r.retired_at IS NULL
          {ref_filter}
          AND EXISTS (
            SELECT 1 FROM chunks c
            WHERE c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
          )
          AND ({not_done_sql})
          {lease}
        ORDER BY r.ref_id
        LIMIT %(limit)s
        {lock}
    """
    binds: dict[str, Any] = {**params, "kinds": list(kinds), "limit": limit}
    if lease_ns:
        binds["attempt_ns"] = lease_ns
    if ref_ids:
        binds["ref_ids"] = list(ref_ids)
    rows = conn.execute(sql, binds).fetchall()
    return [(int(r[0]), str(r[1] or "")) for r in rows]
