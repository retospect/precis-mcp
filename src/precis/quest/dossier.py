"""Quest dossier — the living research synthesis a quest owns.

Slice 4a of the quest layer (docs/proposals/quest-layer.md §Two memories). A
quest keeps *two* records: the append-only ``quest_log`` LOGBOOK (episodic —
what happened, when, immutable; :mod:`precis.quest.logbook`) and the DOSSIER — a
``draft`` the quest owns via ``dossier-of`` (semantic — the current
understanding, best leads, what's ruled out, open questions), **rewritten every
research cycle**. The dossier doubles as the autonomous loop's *rolling
context*: each tick reads the compact dossier rather than replaying the whole
logbook, so context stays bounded.

ADR 0064 §A splits the dossier body into **two chunks**: a **narrative**
paragraph (the whole-rewritten prose synthesis, ``edit_text``'d in place each
tick, stable handle, ``prev_text`` history for free) and one **pinned ledger**
paragraph (``meta.pinned='ledger'``, set via ``patch_chunk_meta`` — ADR 0051's
plan-marker precedent, no new chunk_kind, no migration) that survives every
whole-rewrite untouched, mutated only by explicit :func:`append_ledger_entry`
calls. The ledger holds the *strategic* tried/ruled-out/open ledger (a whole
abandoned *direction*, not a single ruled-out structure — that per-candidate
ledger already lives on ``structure`` tags, see ``tick.py``'s
``_ruled_out_handles``) so the loop can't silently lose its own trail on a
rewrite that drops a rule-out from the free prose (the catpath dead-3-days
spin). :func:`read_dossier` still joins the whole body (the ``view='dossier'``
handler + history rely on it); only the tick *prompt* separates narrative from
ledger (:func:`read_narrative`, :func:`read_ledger`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

_RELATION = "dossier-of"

_SEED = (
    "_(No synthesis yet — this dossier is rewritten each research cycle. The "
    "first quest tick will replace this seed with the current understanding, "
    "the best leads so far, what's been ruled out, and the open questions.)_"
)

#: Ledger section keys → their markdown heading. Order here is the ledger's
#: rendered order (:func:`_render_ledger`) and `append_ledger_entry`'s clamp
#: target for an unrecognised ``section`` is ``"open"``.
_SECTION_HEADINGS: dict[str, str] = {
    "tried": "## Tried",
    "ruled-out": "## Ruled out",
    "open": "## Open",
}
_SECTION_ORDER = ("tried", "ruled-out", "open")
_LEDGER_PLACEHOLDER = "(none yet)"


def _parse_ledger(text: str) -> dict[str, list[str]]:
    """Parse the pinned ledger chunk's markdown into ``{section: [bullet]}``.

    Tolerant of the placeholder line and of anything outside a recognised
    ``## `` heading (dropped) — a human editing the ledger by hand only needs
    to keep the three headings for their edits to round-trip.
    """
    sections: dict[str, list[str]] = {k: [] for k in _SECTION_ORDER}
    heading_to_key = {v: k for k, v in _SECTION_HEADINGS.items()}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in heading_to_key:
            current = heading_to_key[stripped]
            continue
        if current is None or not stripped.startswith("- "):
            continue
        bullet = stripped[2:].strip()
        if bullet and bullet != _LEDGER_PLACEHOLDER:
            sections[current].append(bullet)
    return sections


def _render_ledger(sections: dict[str, list[str]]) -> str:
    """Serialize ``{section: [bullet]}`` back to the ledger's markdown."""
    parts = []
    for key in _SECTION_ORDER:
        bullets = sections.get(key) or []
        body = (
            "\n".join(f"- {b}" for b in bullets)
            if bullets
            else f"- {_LEDGER_PLACEHOLDER}"
        )
        parts.append(f"{_SECTION_HEADINGS[key]}\n{body}")
    return "\n\n".join(parts) + "\n"


_LEDGER_SEED = _render_ledger({k: [] for k in _SECTION_ORDER})


def dossier_ref_id(store: Store, quest_id: int) -> int | None:
    """The ref id of the quest's dossier draft, or ``None`` if it has none."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT src_ref_id FROM links "
            "WHERE dst_ref_id = %s AND relation = %s LIMIT 1",
            (quest_id, _RELATION),
        ).fetchone()
    return int(row[0]) if row else None


def _find_ledger_chunk(chunks: list[Any]) -> Any | None:
    for c in chunks:
        if c.chunk_kind != "heading" and (c.meta or {}).get("pinned") == "ledger":
            return c
    return None


def _ensure_ledger_chunk_for_ref(store: Store, dossier_id: int) -> str:
    """:func:`ensure_ledger_chunk`, given an already-resolved dossier ref id.

    Internal — avoids the ``ensure_dossier`` ↔ ``ensure_ledger_chunk``
    recursion (``ensure_dossier`` calls this directly after seeding the
    narrative rather than going back through the public entry point).
    """
    chunks = store.reading_order(dossier_id)
    found = _find_ledger_chunk(chunks)
    if found is not None:
        return str(found.handle)
    created = store.add_chunks(
        ref_id=dossier_id, chunk_kind="paragraph", text=_LEDGER_SEED, split=False
    )
    handle = str(created[0].handle)
    store.patch_chunk_meta(handle, {"pinned": "ledger"})
    return handle


def ensure_ledger_chunk(store: Store, quest_id: int) -> str:
    """Return the handle of the quest's pinned ledger chunk.

    Idempotent, and heals a dossier that predates ADR 0064 §A (narrative-only,
    no ledger chunk yet) by creating+pinning one lazily — a live quest grows
    its ledger on its next read/append rather than needing a migration.
    Creates the dossier itself (via :func:`ensure_dossier`) if the quest has
    none yet.
    """
    did = ensure_dossier(store, quest_id)
    return _ensure_ledger_chunk_for_ref(store, did)


def ensure_dossier(store: Store, quest_id: int, *, title: str | None = None) -> int:
    """Return the quest's dossier ref id, creating a seeded draft if absent.

    Idempotent: the ``create_draft`` dup-guard enforces one dossier per quest,
    but we look up first so a concurrent/second call returns the existing id
    rather than raising. A fresh dossier is seeded with BOTH the narrative
    body and the pinned ledger; an *existing* dossier is returned as-is (a
    pre-A dossier heals its ledger lazily via :func:`ensure_ledger_chunk`, not
    here — see the module docstring).
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
    _ensure_ledger_chunk_for_ref(store, ref.id)
    return int(ref.id)


def read_dossier(store: Store, quest_id: int) -> tuple[int | None, str | None, str]:
    """``(dossier_ref_id, body_handle, body_text)`` for the quest.

    Returns ``(None, None, "")`` when the quest has no dossier yet. The body
    is every non-heading chunk in reading order (narrative + pinned ledger,
    once both exist), joined — the ``view='dossier'`` handler + history read
    this whole-body join; only the tick *prompt* separates narrative from
    ledger (:func:`read_narrative`, :func:`read_ledger`).
    """
    did = dossier_ref_id(store, quest_id)
    if did is None:
        return None, None, ""
    chunks = store.reading_order(did)
    body = [c for c in chunks if c.chunk_kind != "heading"]
    text = "\n\n".join(c.text for c in body)
    handle = body[0].dc if body else None
    return did, handle, text


def read_narrative(store: Store, quest_id: int) -> str:
    """The model-rewritten narrative only (no pinned ledger, no heading).

    Feeds the tick prompt's ``{dossier}`` slot — the ledger is surfaced
    separately (:func:`read_ledger`) as an explicit constraint, not folded
    into the rewritable prose. Returns ``""`` when the quest has no dossier.
    """
    did = dossier_ref_id(store, quest_id)
    if did is None:
        return ""
    chunks = store.reading_order(did)
    body = [
        c
        for c in chunks
        if c.chunk_kind != "heading" and (c.meta or {}).get("pinned") != "ledger"
    ]
    return "\n\n".join(c.text for c in body)


def read_ledger(store: Store, quest_id: int) -> str:
    """The pinned ledger chunk's raw markdown text.

    Heals a pre-A dossier with no ledger yet (:func:`ensure_ledger_chunk`),
    so this always returns a well-formed (if empty) ledger for any live
    quest, migration-free.
    """
    handle = ensure_ledger_chunk(store, quest_id)
    did = dossier_ref_id(store, quest_id)
    assert did is not None  # ensure_ledger_chunk just guaranteed a dossier
    for c in store.reading_order(did):
        if c.handle == handle:
            return str(c.text)
    return ""  # pragma: no cover - handle was just resolved above


def append_ledger_entry(store: Store, quest_id: int, section: str, text: str) -> bool:
    """Append one bullet under ``section``'s heading in the pinned ledger.

    ``section`` is one of ``tried`` / ``ruled-out`` / ``open``; an
    unrecognised value clamps to ``open``. A blank ``text`` is a no-op.
    Idempotent: a byte-identical bullet already under that heading is
    skipped, not duplicated — this is what dedups the re-propose loop across
    ticks. Heals a pre-A dossier lacking a ledger chunk on the way in
    (:func:`ensure_ledger_chunk`). Returns ``True`` iff a new bullet was
    appended.
    """
    stripped_text = text.strip()
    if not stripped_text:
        return False
    key = section if section in _SECTION_HEADINGS else "open"
    handle = ensure_ledger_chunk(store, quest_id)
    did = dossier_ref_id(store, quest_id)
    assert did is not None  # ensure_ledger_chunk just guaranteed a dossier
    chunk = next(c for c in store.reading_order(did) if c.handle == handle)
    sections = _parse_ledger(chunk.text)
    if any(existing == stripped_text for existing in sections[key]):
        return False
    sections[key].append(stripped_text)
    store.edit_text(handle, _render_ledger(sections), source={"reason": "quest-ledger"})
    return True


def rewrite_dossier(store: Store, quest_id: int, markdown: str) -> int:
    """Whole-rewrite the quest's dossier NARRATIVE to ``markdown``; return its
    ref id.

    Ensures the dossier exists, then edits the narrative body chunk in place
    (``edit_text`` logs ``prev_text``) — the pinned ledger chunk
    (``meta.pinned == 'ledger'``) is explicitly excluded, so it survives
    every rewrite byte-identical (ADR 0064 §A). If somehow there is no
    narrative chunk yet, one is added.
    """
    did = ensure_dossier(store, quest_id)
    chunks = store.reading_order(did)
    body = [
        c
        for c in chunks
        if c.chunk_kind != "heading" and (c.meta or {}).get("pinned") != "ledger"
    ]
    if body:
        # edit_text keys on the legacy ``.handle`` (the ``¶`` anchor), not the
        # universal ``.dc`` display handle — mirror the draft handler.
        store.edit_text(body[0].handle, markdown, source={"reason": "quest-tick"})
    else:  # pragma: no cover - ensure_dossier always seeds a narrative body
        store.add_chunks(ref_id=did, chunk_kind="paragraph", text=markdown, split=False)
    return did


__all__ = [
    "append_ledger_entry",
    "dossier_ref_id",
    "ensure_dossier",
    "ensure_ledger_chunk",
    "read_dossier",
    "read_ledger",
    "read_narrative",
    "rewrite_dossier",
]
