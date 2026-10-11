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


# ── opt-in finding / claim-hub kinds ────────────────────────────────────

#: Ref kinds the categorizers (``classify_topics``, ref-level ``axis:*``) skip
#: unless an operator opts in via a ``service_config`` row (see
#: :func:`opt_in_service`). Taproot claim hubs are ``finding`` refs tagged
#: ``TAPROOT:claim`` (~4k on prod); enabling them spends LLM credit, so they
#: are default-OFF (gr182078).
OPT_IN_KINDS = frozenset({"finding"})


def opt_in_service(kind: str) -> str:
    """``service_config`` service name that opts ``kind`` into the
    categorizers (default-OFF; flip with ``service prio`` / ``/categorizers``)."""
    return f"categorize_kind:{kind}"


#: SQL predicate (alias ``r``): editorial review notes (``TAPROOT:review``)
#: are findings but not claims, so they are never categorized.
NOT_REVIEW_NOTE_SQL = """
    NOT EXISTS (
      SELECT 1 FROM ref_tags rvt JOIN tags rvg ON rvg.tag_id = rvt.tag_id
      WHERE rvt.ref_id = r.ref_id AND rvg.namespace = 'TAPROOT'
        AND rvg.value = 'review'
    )
"""


def effective_kinds(
    base: list[str], candidates: Any = OPT_IN_KINDS, enabled: Any = ()
) -> list[str]:
    """``base`` plus each opt-in kind that is both a ``candidates`` entry
    (the pass/axis declares it applies) and in ``enabled`` (operator flipped
    its ``categorize_kind:<kind>`` service on)."""
    on = set(enabled or ())
    extra = [k for k in sorted(set(candidates or ()) & OPT_IN_KINDS) if k in on]
    return [*base, *(k for k in extra if k not in base)]


def finding_text(title: str, meta: Any) -> str:
    """Classifier text for a finding / claim hub: the claim sentence plus its
    ``meta.scope`` (a finding has no abstract or body beyond the sentence)."""
    scope = (meta or {}).get("scope") if isinstance(meta, dict) else None
    parts = [str(title or "").strip()]
    if isinstance(scope, dict):
        bits = [f"{k}: {v}" for k, v in scope.items() if v not in (None, "", [], {})]
        if bits:
            parts.append("Scope: " + "; ".join(bits))
    return "\n".join(p for p in parts if p)
