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

Also holds :func:`is_machine_owned_draft` — the sibling "should a reviewer
even be pointed at this draft?" guard. A dossier (or its ``paper-of``
reader-facing projection) is a *process's* machine-managed body: exactly
one whole-rewritten narrative chunk plus code-managed pinned chunks
(:mod:`precis.quest.dossier`), not hand-authored prose. Its markdown-
looking single-chunk shape is that process's intentional storage format,
not authoring debt — a reviewer lens applying the ``precis-draft-reviewer``
persona correctly, repeatedly flags it ("written with markdown inside a
draft, refactor to draft chunks"), and a ``plan_tick`` agent executing that
change-request once actually "fixed" it, silently destroying the quest's
attempt-tree ledger (quest 202469 / dossier 202546, Aug 2026 — see
``docs/backlog/dossier-present-tense-refinement.md``). Both scanners that
decide which drafts to fan review-todos over —
:func:`precis.quest.review_fanout.mint_review_fanout` and
:func:`precis.quest.weave_review.mint_weave_reviews` — call this before
minting, so the class of finding is never produced for a machine-owned
draft in the first place. Complements (does not replace) the independent
hard refusal at the ``handlers/draft.py`` write boundary — that stops a
*minted* change-request from being executed; this stops one from being
minted (and queued as a wasted agent job) at all.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection


def chunk_anchor_forms(conn: Connection, chunk_id: int) -> list[str]:
    """The anchor strings a change-request todo might carry for this chunk:
    the universal handles ``dc<id>`` form (what the fanout + personas mint today) plus
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


#: The relations an owning *process* uses to mark a ``draft`` as its
#: machine-managed body — mirrors ``precis.quest.dossier``'s private
#: ``_RELATION``/``_PAPER_RELATION`` (duplicated as literal strings, not
#: imported: these are DB relation slugs, not Python identifiers, and every
#: guard built on them — this one, and ``handlers/draft.py``'s independent
#: copy at the write boundary — should keep working even if the dossier
#: module's internals move).
_DOSSIER_RELATION = "dossier-of"
_PAPER_RELATION = "paper-of"
MACHINE_OWNED_RELATIONS: tuple[str, ...] = (_DOSSIER_RELATION, _PAPER_RELATION)


def is_machine_owned_draft(store: Any, draft_ref_id: int) -> bool:
    """True iff ``draft_ref_id`` is the SOURCE of an outbound ``dossier-of``/
    ``paper-of`` link — i.e. it is a process's machine-managed body (a quest
    dossier, or its reader-facing paper projection), not a hand-authored
    draft. See the module docstring for why a reviewer scanner must not fan
    findings over one of these."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND relation = ANY(%s) LIMIT 1",
            (draft_ref_id, list(MACHINE_OWNED_RELATIONS)),
        ).fetchone()
    return row is not None


__all__ = [
    "MACHINE_OWNED_RELATIONS",
    "chunk_anchor_forms",
    "has_open_change_request",
    "has_open_change_request_via_store",
    "is_machine_owned_draft",
]
