"""The dismissed-source ledger (source-backfill slice 4, leading edge).

When the model — or a human — judges a surfaced candidate *not* worth citing,
that verdict has to stick: the next backfill run over the same draft must not
re-propose it, or the loop nags forever (and the plan-tick-spin detector would
eventually bubble it as a failure). The ledger is a per-draft record of dismissed
paper refs, read into the **Tier-0 exclude set** next to the already-cited papers
(:func:`precis.backfill.candidates.draft_cited_ref_ids`).

Stored as a controlled tag ``DISMISSED_SOURCE:<paper_ref_id>`` on the *draft* ref
— machine-written, migration-free, deduped by the tag unique, and cheap to read
back (the namespace is upper-case per the ``tags_namespace_check`` constraint). A
dismissal is a *suppression*, not a citation: recall never seeds a citation-graph
**neighbourhood** from a dismissed paper (that is what the cited set is for), it
only drops the paper from results. So the graph around what you actually cite is
still explored while a rejected hit stays gone.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.store.types import Tag

log = logging.getLogger(__name__)

#: Closed-tag prefix for the ledger — one ``DISMISSED_SOURCE:<ref_id>`` per
#: dismissed paper, on the draft ref. Upper-case to satisfy the schema's
#: ``tags_namespace_check`` (namespace = upper(namespace)).
DISMISS_NS = "DISMISSED_SOURCE"


def _ref_id_for_chunk(store: Any, chunk_id: int) -> int | None:
    """The owning ref of a body chunk, or ``None``. A light single-row lookup."""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM chunks WHERE chunk_id = %s", (chunk_id,)
            ).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def _ref_id_for_cite_key(store: Any, slug: str) -> int | None:
    """The ref a ``cite_key`` slug names (``wang2020``), or ``None``. Best-effort:
    a live paper-family ref carrying that identifier."""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT ri.ref_id
                  FROM ref_identifiers ri
                  JOIN refs r ON r.ref_id = ri.ref_id
                 WHERE ri.id_kind = 'cite_key' AND ri.id_value = %s
                   AND r.deleted_at IS NULL
                 LIMIT 1
                """,
                (slug,),
            ).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def resolve_source_ref_id(store: Any, value: Any) -> int | None:
    """Resolve a dismissal value to a **source ref_id**, robust to every form a
    model actually writes.

    The find-phase instruction shows the model a candidate line carrying *both*
    the record handle ``pa<id>`` and the chunk handle ``pc<id>``, and asks it to
    dismiss "the number in the ``pa<id>`` handle". In practice a model pastes
    whatever is in front of it. The old ledger accepted **only** a bare integer,
    so a handle-form paste was silently dropped: the dismissal never stuck, the
    candidate resurfaced every run, and the pass never converged (exactly the
    non-convergence the ledger exists to prevent). This resolver is that
    convergence-critical tolerance — it tries, most-reliable first, every form a
    dismissal is plausibly written as:

    1. a bare ref-id (``889`` / ``"889"``);
    2. a **record** handle (``pa889`` — the pk *is* the ref_id);
    3. a **chunk** handle (``pc7710`` — resolved chunk→owning-ref via the store);
    4. the ``kind:id`` canonical link-target form (``paper:889``);
    5. a ``cite_key`` **slug** (``wang2020``).

    Returns ``None`` only when *all* of those fail — a genuinely malformed value
    is skipped, not guessed. Callers on the read path make that drop **loud**
    (see :func:`dismissed_ref_ids`); the write path (:func:`dismiss_source`)
    raises, since a caller minting a dismissal can react synchronously."""
    if isinstance(value, int):
        return value
    v = str(value).strip()
    if not v:
        return None
    # (1) bare ref-id
    if v.lstrip("-").isdigit():
        return int(v)
    from precis.utils import handle_registry

    # (2)/(3) a 2-char handle — record (pk == ref_id) or chunk (→ owning ref)
    parsed = handle_registry.parse(v)
    if parsed is not None:
        _kind, is_chunk, pk = parsed
        return _ref_id_for_chunk(store, pk) if is_chunk else pk
    # (4) kind:id canonical link-target form (paper:889) — id is the ref_id
    if ":" in v:
        head, _, tail = v.rpartition(":")
        if head.isalpha() and tail.strip().isdigit():
            return int(tail.strip())
    # (5) a cite_key slug (wang2020) — the last, query-backed recovery
    return _ref_id_for_cite_key(store, v)


def dismiss_source(
    store: Any,
    draft_ref_id: int,
    paper_ref_id: int | str,
    *,
    reason: str | None = None,
    conn: Any = None,
) -> None:
    """Record that ``paper_ref_id`` was weighed and rejected for this draft, so
    recall stops resurfacing it. Idempotent (the tag unique folds a repeat).
    ``reason`` is kept as an audit ``ref_event`` (the tag itself is just the id —
    it is the ledger; the reason is provenance).

    ``paper_ref_id`` accepts a bare ref-id **or** a handle (``pa889`` / ``pc7710``);
    it is normalised to the canonical numeric ref-id before storage (see
    :func:`resolve_source_ref_id`), so the ledger is uniform regardless of what
    form the caller had in hand."""
    rid = resolve_source_ref_id(store, paper_ref_id)
    if rid is None:
        raise ValueError(
            f"cannot resolve dismissal source {paper_ref_id!r} to a ref_id"
        )
    store.add_tag(draft_ref_id, Tag.closed(DISMISS_NS, str(rid)), conn=conn)
    if reason:
        store.append_event(
            draft_ref_id,
            source="backfill",
            event="dismissed",
            payload={"paper_ref_id": rid, "reason": reason},
            conn=conn,
        )


def dismissed_ref_ids(store: Any, draft_ref_id: int) -> set[int]:
    """The paper ref_ids dismissed for this draft — the ledger read back into the
    Tier-0 exclude set. Each tag value is resolved robustly
    (:func:`resolve_source_ref_id`), so a model that dismissed by handle
    (``pa889``/``pc7710``), ``kind:id``, or a slug still suppresses the candidate.

    A value that survives *every* recovery path is a real problem — someone
    intended to dismiss a source and it isn't happening, which resurfaces the
    candidate forever — so the drop is made **loud** (a warning naming the ref +
    the offending value) rather than silent. It is not raised: this is the
    workspace-render read path, and one malformed ledger tag must not blow up the
    whole backfill tick."""
    out: set[int] = set()
    for tag in store.tags_for(draft_ref_id):
        if getattr(tag, "prefix", None) != DISMISS_NS:
            continue
        rid = resolve_source_ref_id(store, tag.value)
        if rid is not None:
            out.add(rid)
        else:
            log.warning(
                "backfill: unresolvable DISMISSED_SOURCE value %r on ref %s — "
                "dismissal dropped (candidate will resurface); re-dismiss by its "
                "pa<id>/pc<id> handle or bare ref-id",
                tag.value,
                draft_ref_id,
            )
    return out
