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


def dismiss_source(
    store: Any,
    draft_ref_id: int,
    paper_ref_id: int,
    *,
    reason: str | None = None,
    conn: Any = None,
) -> None:
    """Record that ``paper_ref_id`` was weighed and rejected for this draft, so
    recall stops resurfacing it. Idempotent (the tag unique folds a repeat).
    ``reason`` is kept as an audit ``ref_event`` (the tag itself is just the id —
    it is the ledger; the reason is provenance)."""
    store.add_tag(
        draft_ref_id, Tag.closed(DISMISS_NS, str(int(paper_ref_id))), conn=conn
    )
    if reason:
        store.append_event(
            draft_ref_id,
            source="backfill",
            event="dismissed",
            payload={"paper_ref_id": int(paper_ref_id), "reason": reason},
            conn=conn,
        )


def dismissed_ref_ids(store: Any, draft_ref_id: int) -> set[int]:
    """The paper ref_ids dismissed for this draft — the ledger read back into the
    Tier-0 exclude set. Tolerant of a malformed value (skips non-numeric)."""
    out: set[int] = set()
    for tag in store.tags_for(draft_ref_id):
        if (
            getattr(tag, "prefix", None) == DISMISS_NS
            and tag.value.lstrip("-").isdigit()
        ):
            out.add(int(tag.value))
    return out
