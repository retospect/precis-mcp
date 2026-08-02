"""Shared "does this chunk have an open anchored change-request?" guard.

Extracted from ``workers/executors/claude_inproc.py``'s review-ledger
writeback (rung 3b — ``_maybe_record_review_pass``), which refuses to
auto-approve a chunk carrying an open anchored change-request (a finding
filed by the ``precis-draft-reviewer`` persona as ``kind='todo'`` with
``meta.anchor=dc<id>``, not a ``kind='finding'`` child). The incremental
fanout (rung 3a, :mod:`precis.quest.review_fanout`) needs the identical
check at *mint* time: running a checker on text that's about to change
under an open request is a wasted LLM call the writeback would refuse to
approve anyway — see the module's ``only_dirty``/skip-unsettled docs.

Faithful extraction — the matching logic (anchor forms, the
``meta->>'review' IS NULL`` exclusion, the done/won't-do carve-out) is
copied verbatim from ``claude_inproc.py``, not "improved"; both call
sites depend on identical behavior.

Two entry points for the two shapes of caller:

- :func:`has_open_change_request` — takes a live ``psycopg.Connection``
  (the writeback already has one open in its transaction).
- :func:`has_open_change_request_via_store` — takes a ``store`` and opens
  its own short-lived connection (the fanout has a store, not a conn).
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection


def chunk_anchor_forms(conn: Connection, chunk_id: int) -> list[str]:
    """The anchor strings a change-request todo might carry for this chunk:
    the ADR-0036 ``dc<id>`` form (what the fanout + personas mint today) plus
    the base58 ``handle`` and its legacy ``¶handle`` variant (older/other
    write paths). Matching all three keeps the "any open request?" guard from
    missing a finding stored under a different anchor convention."""
    row = conn.execute(
        "SELECT handle FROM chunks WHERE chunk_id = %s", (chunk_id,)
    ).fetchone()
    forms = [f"dc{chunk_id}"]
    if row is not None and row[0]:
        forms += [row[0], f"¶{row[0]}"]
    return forms


def has_open_change_request(conn: Connection, chunk_id: int) -> bool:
    """True when ``chunk_id`` carries an OPEN (not done / won't-do) anchored
    change-request todo, matched across the ``dc<id>`` / base58-handle /
    legacy ``¶handle`` anchor forms.

    ``meta->>'review' IS NULL`` excludes review-MODE todos (a review-todo's
    own ``meta.anchor`` — this tick's parent + any sibling-lens review-todos
    on the same chunk); only a genuine change-request (an anchored todo with
    no ``review`` key, the shape the reviewer files a finding as) counts."""
    anchors = chunk_anchor_forms(conn, chunk_id)
    row = conn.execute(
        "SELECT 1 FROM refs r WHERE r.kind = 'todo' AND r.deleted_at IS NULL "
        "AND r.meta->>'anchor' = ANY(%s) AND r.meta->>'review' IS NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "  WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' "
        "  AND t.value IN ('done', 'won''t-do')) LIMIT 1",
        (anchors,),
    ).fetchone()
    return row is not None


def has_open_change_request_via_store(store: Any, chunk_id: int) -> bool:
    """:func:`has_open_change_request`, for a caller (e.g. the fanout) that
    holds a ``store`` rather than an open connection."""
    with store.pool.connection() as conn:
        return has_open_change_request(conn, chunk_id)


__all__ = [
    "chunk_anchor_forms",
    "has_open_change_request",
    "has_open_change_request_via_store",
]
