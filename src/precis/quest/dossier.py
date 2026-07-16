"""Quest dossier — the living research synthesis a quest owns.

Slice 4a of the quest layer (docs/proposals/quest-layer.md §Two memories). A
quest keeps *two* records: the append-only ``quest_log`` LOGBOOK (episodic —
what happened, when, immutable; :mod:`precis.quest.logbook`) and the DOSSIER — a
``draft`` the quest owns via ``dossier-of`` (semantic — the current
understanding, best leads, what's ruled out, open questions), **rewritten every
research cycle**. The dossier doubles as the autonomous loop's *rolling
context*: each tick reads the compact dossier rather than replaying the whole
logbook, so context stays bounded.

The dossier is stored as a single body chunk under a title heading, whole-
rewritten in place via ``edit_text`` each tick (stable handle, ``prev_text``
history for free). Multi-section splitting is a later refinement — a dossier is
short by construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

_RELATION = "dossier-of"

_SEED = (
    "_(No synthesis yet — this dossier is rewritten each research cycle. The "
    "first quest tick will replace this seed with the current understanding, "
    "the best leads so far, what's been ruled out, and the open questions.)_"
)


def dossier_ref_id(store: Store, quest_id: int) -> int | None:
    """The ref id of the quest's dossier draft, or ``None`` if it has none."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT src_ref_id FROM links "
            "WHERE dst_ref_id = %s AND relation = %s LIMIT 1",
            (quest_id, _RELATION),
        ).fetchone()
    return int(row[0]) if row else None


def ensure_dossier(store: Store, quest_id: int, *, title: str | None = None) -> int:
    """Return the quest's dossier ref id, creating a seeded draft if absent.

    Idempotent: the ``create_draft`` dup-guard enforces one dossier per quest,
    but we look up first so a concurrent/second call returns the existing id
    rather than raising.
    """
    existing = dossier_ref_id(store, quest_id)
    if existing is not None:
        return existing
    qref = store.get_ref(kind="quest", id=quest_id)
    stmt = (qref.title if qref and qref.title else f"quest {quest_id}").splitlines()[0]
    ref, _heading = store.create_draft(
        name=f"quest-{quest_id}-dossier",
        title=title or f"Dossier — {stmt[:80]}",
        project_ref_id=quest_id,
        meta={"dossier_of_quest": quest_id},
        relation=_RELATION,
    )
    store.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text=_SEED, split=False)
    return int(ref.id)


def read_dossier(store: Store, quest_id: int) -> tuple[int | None, str | None, str]:
    """``(dossier_ref_id, body_handle, body_text)`` for the quest.

    Returns ``(None, None, "")`` when the quest has no dossier yet. The body is
    every non-heading chunk in reading order (a single chunk today), joined —
    the rolling context a tick reads.
    """
    did = dossier_ref_id(store, quest_id)
    if did is None:
        return None, None, ""
    chunks = store.reading_order(did)
    body = [c for c in chunks if c.chunk_kind != "heading"]
    text = "\n\n".join(c.text for c in body)
    handle = body[0].dc if body else None
    return did, handle, text


def rewrite_dossier(store: Store, quest_id: int, markdown: str) -> int:
    """Whole-rewrite the quest's dossier body to ``markdown``; return its ref id.

    Ensures the dossier exists, then edits the single body chunk in place
    (``edit_text`` logs ``prev_text``). If somehow there is no body chunk yet,
    one is added.
    """
    did = ensure_dossier(store, quest_id)
    chunks = store.reading_order(did)
    body = [c for c in chunks if c.chunk_kind != "heading"]
    if body:
        # edit_text keys on the legacy ``.handle`` (the ``¶`` anchor), not the
        # universal ``.dc`` display handle — mirror the draft handler.
        store.edit_text(body[0].handle, markdown, source={"reason": "quest-tick"})
    else:  # pragma: no cover - ensure_dossier always seeds a body
        store.add_chunks(ref_id=did, chunk_kind="paragraph", text=markdown, split=False)
    return did


__all__ = ["dossier_ref_id", "ensure_dossier", "read_dossier", "rewrite_dossier"]
