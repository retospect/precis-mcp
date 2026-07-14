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

from typing import Any

from precis.store.types import Tag

#: Closed-tag prefix for the ledger — one ``DISMISSED_SOURCE:<ref_id>`` per
#: dismissed paper, on the draft ref. Upper-case to satisfy the schema's
#: ``tags_namespace_check`` (namespace = upper(namespace)).
DISMISS_NS = "DISMISSED_SOURCE"


def resolve_source_ref_id(store: Any, value: Any) -> int | None:
    """Resolve a dismissal value to a **source ref_id**, robust to every form a
    model actually writes.

    The find-phase instruction shows the model a candidate line carrying *both*
    the record handle ``pa<id>`` and the chunk handle ``pc<id>``, and asks it to
    dismiss "the number in the ``pa<id>`` handle". In practice a model pastes
    whatever is in front of it — the bare number, the whole ``pa889`` handle, or
    even the ``pc7710`` chunk handle. The old ledger accepted **only** a bare
    integer, so a handle-form paste was silently dropped: the dismissal never
    stuck, the candidate resurfaced every run, and the pass never converged
    (exactly the non-convergence the ledger exists to prevent). This resolver is
    that convergence-critical tolerance — it accepts:

    * a bare ref-id (``889`` / ``"889"``);
    * a **record** handle (``pa889`` — the pk *is* the ref_id);
    * a **chunk** handle (``pc7710`` — resolved chunk→owning-ref via the store).

    Returns ``None`` for anything it cannot resolve (a genuinely malformed value
    is skipped, not guessed)."""
    if isinstance(value, int):
        return value
    v = str(value).strip()
    if not v:
        return None
    if v.lstrip("-").isdigit():
        return int(v)
    from precis.utils import handle_registry

    parsed = handle_registry.parse(v)
    if parsed is None:
        return None
    _kind, is_chunk, pk = parsed
    if not is_chunk:
        return pk  # record handle: the primary key IS the ref_id
    # chunk handle (pc<id>/pk<id>/…) → its owning ref, via a light lookup
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM chunks WHERE chunk_id = %s", (pk,)
            ).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


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
    (``pa889``/``pc7710``) rather than the bare number still suppresses the
    candidate. A value that resolves to nothing is skipped, not guessed."""
    out: set[int] = set()
    for tag in store.tags_for(draft_ref_id):
        if getattr(tag, "prefix", None) != DISMISS_NS:
            continue
        rid = resolve_source_ref_id(store, tag.value)
        if rid is not None:
            out.add(rid)
    return out
