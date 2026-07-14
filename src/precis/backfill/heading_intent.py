"""Heading-intent notes — the teleological layer (source-backfill slice 8b.1).

A durable note on each heading: **what belongs under it and why it exists**. It
serves three readers at once — the **writer** (a hierarchical prompt: the intent
breadcrumb root→…→leaf shapes what the leaf writes), the **structural stance** (a
coherence map), and the **reader** (a presence marker). It is the structural
memory that stops a many-tick / many-agent edit from losing the plot.

Substrate — **no new kind, no migration**:

- a ``memory`` ref, so the intent **embeds** → searchable + recallable (it can
  surface as a ``○ [own-note]`` LEAD in a topically-adjacent section's sweep);
- ``meta.anchor = '<heading handle>'`` — the precise heading it is about, reusing
  the change-request anchor convention already read by ``_render_anchor_context``;
- ``meta.heading_intent = 'hard' | 'soft'`` — **hard** = a structural commitment,
  **soft** = a revisable intent. Stored in ``meta`` (not a closed tag) so an
  upsert / hard↔soft flip is a plain overwrite, with no closed-prefix swap dance.

**Never exported.** It is a *separate* ``memory`` ref anchored to the heading, not
a chunk of the draft, so it physically cannot enter the export chunk stream;
``memory`` is non-exportable anyway (:func:`precis.export.guard_exportable`). The
render surfaces it as *keyed meta*, outside the sacred-content quotes.

**Known limitation (re-anchoring).** The anchor is a ``dc<id>`` chunk handle, and a
heading *rename* goes through DELETE+INSERT (new ``chunk_id``), which orphans the
intent. :func:`prune_dangling` reaps orphans; *following* an intent through a
rename needs stable per-node ids (the same plumbing slice 8a wants) and is
deferred. Editing the *content under* a heading does not touch the heading chunk,
so ordinary section work keeps the intent attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from precis.store.types import BlockInsert

#: ``meta.heading_intent`` values.
HARD = "hard"
SOFT = "soft"

#: The ``meta`` key that both marks a memory as a heading-intent note and carries
#: its strength (``hard``/``soft``). Its presence (``meta ? 'heading_intent'``) is
#: the discriminator every query filters on.
_META_KEY = "heading_intent"

#: Chunk kind a memory's body prose lives in (migration 0050) — the embed source,
#: so the intent is recallable.
_BODY_KIND = "memory_body"

#: Longest auto-derived title (first body line, capped) when no explicit title.
_TITLE_MAX = 80


@dataclass(frozen=True)
class Intent:
    """One heading-intent note: the ``memory`` ref, the heading it is anchored to,
    its prose, and whether it is a ``hard`` commitment or a ``soft`` intent."""

    ref_id: int
    heading_handle: str
    text: str
    title: str
    hard: bool

    @property
    def strength(self) -> str:
        return HARD if self.hard else SOFT


def _derive_title(text: str) -> str:
    """A short header from the first non-empty body line (capped)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:_TITLE_MAX].rstrip()
    return "heading intent"


def _intent_ref_for(store: Any, heading_handle: str) -> int | None:
    """The ref_id of the live intent note anchored to ``heading_handle``, or
    ``None`` — the upsert probe (one intent per heading)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE kind = 'memory' AND deleted_at IS NULL "
            "  AND meta ? %s AND meta->>'anchor' = %s "
            "ORDER BY ref_id LIMIT 1",
            (_META_KEY, heading_handle),
        ).fetchone()
    return int(row[0]) if row else None


def set_intent(
    store: Any,
    heading_handle: str,
    text: str,
    *,
    hard: bool = False,
    title: str | None = None,
) -> int:
    """Create or update the intent note for ``heading_handle`` (one per heading —
    upsert on the anchor). Returns the memory ref_id.

    The body is written as a ``memory_body`` chunk (DELETE+INSERT on update, so the
    embedding cascade re-runs — a heading-intent stays recallable). A hard↔soft
    flip is applied here; the *skill* decides whether flipping a **hard** intent is
    warranted (a structural event), not this function."""
    strength = HARD if hard else SOFT
    existing = _intent_ref_for(store, heading_handle)
    if existing is not None:
        store.update_ref(
            existing,
            title=title or _derive_title(text),
            meta_patch={"anchor": heading_handle, _META_KEY: strength},
        )
        store.insert_blocks(
            existing,
            [BlockInsert(pos=0, text=text, meta={"chunk_kind": _BODY_KIND})],
            replace=True,
        )
        return existing
    ref = store.insert_ref(
        kind="memory",
        slug=None,
        title=title or _derive_title(text),
        meta={"anchor": heading_handle, _META_KEY: strength},
    )
    store.insert_blocks(
        ref.id,
        [BlockInsert(pos=0, text=text, meta={"chunk_kind": _BODY_KIND})],
    )
    return int(ref.id)


def _rows_to_intents(rows: list[tuple[Any, ...]]) -> dict[str, Intent]:
    out: dict[str, Intent] = {}
    for ref_id, title, anchor, strength, body in rows:
        if not anchor:
            continue
        out[str(anchor)] = Intent(
            ref_id=int(ref_id),
            heading_handle=str(anchor),
            text=str(body or ""),
            title=str(title or ""),
            hard=(strength == HARD),
        )
    return out


def intents_for(store: Any, heading_handles: list[str]) -> dict[str, Intent]:
    """Map each heading handle to its intent note (absent handles are omitted).

    The deterministic surfacing channel — the render (breadcrumb up + siblings
    across) reads exactly this; attached notes always show up, so a writer never
    gambles on rediscovering its own."""
    handles = [h for h in dict.fromkeys(heading_handles) if h]
    if not handles:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.title, r.meta->>'anchor', r.meta->>%s, "
            "       (SELECT ch.text FROM chunks ch "
            "          WHERE ch.ref_id = r.ref_id "
            "            AND ch.chunk_kind = %s "
            "          ORDER BY ch.ord LIMIT 1) "
            "  FROM refs r "
            " WHERE r.kind = 'memory' AND r.deleted_at IS NULL "
            "   AND r.meta ? %s AND r.meta->>'anchor' = ANY(%s)",
            (_META_KEY, _BODY_KIND, _META_KEY, handles),
        ).fetchall()
    return _rows_to_intents(rows)


def intents_for_draft(
    store: Any, draft_ref_id: int, *, kind: str = "draft"
) -> dict[str, Intent]:
    """Every heading-intent in a draft, keyed by heading handle — walks the draft's
    heading chunks and looks each one up. (Live headings only; an orphan whose
    heading was deleted is not returned — that's :func:`prune_dangling`'s job.)"""
    chunks = store.reading_order(draft_ref_id, kind=kind)
    handles = [c.dc for c in chunks if getattr(c, "chunk_kind", None) == "heading"]
    return intents_for(store, handles)


def retire_intent(store: Any, ref_id: int, *, conn: Any = None) -> None:
    """Retire (soft-delete) an intent note — the heading it belonged to is gone or
    the section was cut."""
    store.soft_delete_ref(int(ref_id), conn=conn)


def prune_dangling(store: Any) -> list[int]:
    """Retire every heading-intent whose anchored heading chunk no longer resolves
    (deleted / merged / renamed). The deterministic hygiene heal (slice 8b.4) — the
    counterpart to ``paper_hygiene`` repointing links off soft-deleted refs. Returns
    the retired ref_ids. Kind-agnostic: the anchor's own handle selects the table."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->>'anchor' FROM refs "
            "WHERE kind = 'memory' AND deleted_at IS NULL AND meta ? %s",
            (_META_KEY,),
        ).fetchall()
    retired: list[int] = []
    for ref_id, anchor in rows:
        alive = bool(anchor) and store.resolve_handle(str(anchor)) is not None
        if not alive:
            store.soft_delete_ref(int(ref_id))
            retired.append(int(ref_id))
    return retired
