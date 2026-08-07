"""Dossier — the living research synthesis a *process* owns.

Slice 4a of the quest layer (docs/proposals/quest-layer.md §Two memories),
generalized per ADR 0064 §B (docs/proposals/dossier-owner-generalization.md):
a dossier belongs to a **process, never an artifact**. A quest is the process
that owns one today, but the owner is now **any ref** (``owner_id``) — a
standing topic review or a paper-writing pipeline can own a dossier by the same
rule, and a "paper" is just a render/export of a process's dossier. The owner
coupling lived entirely in this module's Python (the ``dossier-of`` /
``has-dossier`` relation is already owner-agnostic — migration 0067, no kind
constraint), so widening it is migration-free.

An owning process keeps *two* records: the append-only ``quest_log`` LOGBOOK
(episodic — what happened, when, immutable; :mod:`precis.quest.logbook`) and the
DOSSIER — a ``draft`` the owner holds via ``dossier-of`` (semantic — the current
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
rewrite that drops a rule-out from the free prose (the autocatpath dead-3-days
spin). :func:`read_dossier` still joins the whole body (the ``view='dossier'``
handler + history rely on it); only the tick *prompt* separates narrative from
ledger (:func:`read_narrative`, :func:`read_ledger`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

_RELATION = "dossier-of"

#: The owner's reader-facing PAPER draft — a SEPARATE draft from the
#: dossier (the dossier is the internal thinking substrate; the paper is
#: what a human reads). Mirrors ``dossier-of`` exactly (asymmetric,
#: auto-mirrored inverse ``has-paper``), but this module does not mint the
#: paper draft — that pipeline is unbuilt (docs/design/paper-writing-
#: pipeline.md). :func:`paper_ref_id` is a read-only resolver so callers
#: (the quest web dashboard) can link to a paper when one exists and
#: degrade gracefully when it doesn't. Keep in sync with the `relations`
#: seed in migration 0089_paper_of_relation.sql.
_PAPER_RELATION = "paper-of"

_SEED = (
    "_(No synthesis yet — this dossier is rewritten each research cycle. The "
    "first tick will replace this seed with the current understanding, "
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

#: The second pinned chunk (Slice 4c-4): the candidate lineage tree. Unlike
#: the ledger (model-authored, additive), this one is entirely CODE-
#: regenerated in place every tick via :func:`update_frontier_tree` — see
#: that function's docstring.
_FRONTIER_TREE_PINNED = "frontier-tree"
_FRONTIER_TREE_SEED = "_(No candidates yet.)_\n"


def dossier_ref_id(store: Store, owner_id: int) -> int | None:
    """The ref id of the owner's dossier draft, or ``None`` if it has none.

    Resolution is via the ``dossier-of`` edge, **not** the denormalized
    ``meta.dossier_of_owner`` back-pointer — so a pre-0064-§B dossier carrying
    only the legacy ``meta.dossier_of_quest`` key resolves identically, with no
    migration or backfill (ADR 0064 §B). Excludes a soft-deleted dossier draft
    — the ``dossier-of`` link row can outlive a ``delete()`` of the draft
    itself, and a caller resolving this id should see "no dossier" rather
    than a tombstoned ref.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT l.src_ref_id FROM links l "
            "JOIN refs r ON r.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = %s "
            "AND r.deleted_at IS NULL LIMIT 1",
            (owner_id, _RELATION),
        ).fetchone()
    return int(row[0]) if row else None


def paper_ref_id(store: Store, owner_id: int) -> int | None:
    """The ref id of the owner's reader-facing PAPER draft, or ``None``.

    Resolved via the ``paper-of`` edge — the same shape as
    :func:`dossier_ref_id`, but a distinct draft: the dossier is the
    process's internal rewritten synthesis, the paper is a separate,
    human-facing projection of it (docs/decisions/0064-dossier-thinking-
    substrate-and-paper-projection.md). Nothing in this module creates a
    paper draft — that pipeline doesn't exist yet — so this always
    returns ``None`` until some other writer links one in with
    ``rel='paper-of'``. Excludes a soft-deleted paper draft — see
    :func:`dossier_ref_id`.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT l.src_ref_id FROM links l "
            "JOIN refs r ON r.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = %s "
            "AND r.deleted_at IS NULL LIMIT 1",
            (owner_id, _PAPER_RELATION),
        ).fetchone()
    return int(row[0]) if row else None


def _owner_title(store: Store, owner_id: int) -> str:
    """The owner ref's title (any kind), used only as the dossier's default name.

    A direct ``refs`` read rather than ``store.get_ref(kind=…, id=…)`` — the
    latter requires a hardcoded ``kind`` (that was the quest coupling ADR 0064
    §B removes), and the title is only a cosmetic seed, so no ``Store``
    abstraction is warranted.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
    return str(row[0]) if row and row[0] else f"ref {owner_id}"


def _find_pinned_chunk(chunks: list[Any], pinned: str) -> Any | None:
    """The non-heading chunk whose ``meta.pinned == pinned`` — shared by the
    ledger (``"ledger"``) and the frontier tree (``"frontier-tree"``, Slice
    4c-4); each pinned value picks out one distinct chunk."""
    for c in chunks:
        if c.chunk_kind != "heading" and (c.meta or {}).get("pinned") == pinned:
            return c
    return None


def _ensure_ledger_chunk_for_ref(store: Store, dossier_id: int) -> str:
    """:func:`ensure_ledger_chunk`, given an already-resolved dossier ref id.

    Internal — avoids the ``ensure_dossier`` ↔ ``ensure_ledger_chunk``
    recursion (``ensure_dossier`` calls this directly after seeding the
    narrative rather than going back through the public entry point).
    """
    chunks = store.reading_order(dossier_id)
    found = _find_pinned_chunk(chunks, "ledger")
    if found is not None:
        return str(found.handle)
    created = store.add_chunks(
        ref_id=dossier_id, chunk_kind="paragraph", text=_LEDGER_SEED, split=False
    )
    handle = str(created[0].handle)
    store.patch_chunk_meta(handle, {"pinned": "ledger"})
    return handle


def _ensure_frontier_tree_chunk_for_ref(store: Store, dossier_id: int) -> str:
    """The frontier-tree sibling of :func:`_ensure_ledger_chunk_for_ref` —
    creates the ``meta.pinned='frontier-tree'`` chunk (seeded empty) if
    absent, else returns its existing handle. Internal — see
    :func:`update_frontier_tree` for the public, code-regenerating entry
    point."""
    chunks = store.reading_order(dossier_id)
    found = _find_pinned_chunk(chunks, _FRONTIER_TREE_PINNED)
    if found is not None:
        return str(found.handle)
    created = store.add_chunks(
        ref_id=dossier_id,
        chunk_kind="paragraph",
        text=_FRONTIER_TREE_SEED,
        split=False,
    )
    handle = str(created[0].handle)
    store.patch_chunk_meta(handle, {"pinned": _FRONTIER_TREE_PINNED})
    return handle


def update_frontier_tree(store: Store, owner_id: int) -> str:
    """Regenerate the owner's pinned frontier-tree chunk from the current
    candidate lineage (:func:`precis.quest.frontier.render_frontier_tree`).

    Called at the end of each quest tick, **after harvest**, so the tree
    reflects freshly-measured barriers/energies (:mod:`precis.quest.tick`).
    Unlike the ledger (model-authored, additive across ticks), this chunk is
    entirely **code**-regenerated — whole-rewritten in place on every call,
    never touched by the model, and excluded from the narrative rewrite the
    same way the ledger is (:func:`rewrite_dossier`, :func:`read_narrative`
    — both filter on ANY ``meta.pinned`` value now, not just ``"ledger"``).
    Creates the dossier (and the chunk) if the owner has none yet. Returns
    the chunk's handle.
    """
    from precis.quest.frontier import render_frontier_tree

    did = ensure_dossier(store, owner_id)
    handle = _ensure_frontier_tree_chunk_for_ref(store, did)
    markdown = render_frontier_tree(store, owner_id)
    store.edit_text(handle, markdown, source={"reason": "quest-frontier-tree"})
    return handle


def ensure_ledger_chunk(store: Store, owner_id: int) -> str:
    """Return the handle of the owner's pinned ledger chunk.

    Idempotent, and heals a dossier that predates ADR 0064 §A (narrative-only,
    no ledger chunk yet) by creating+pinning one lazily — a live owner grows
    its ledger on its next read/append rather than needing a migration.
    Creates the dossier itself (via :func:`ensure_dossier`) if the owner has
    none yet.
    """
    did = ensure_dossier(store, owner_id)
    return _ensure_ledger_chunk_for_ref(store, did)


def ensure_dossier(store: Store, owner_id: int, *, title: str | None = None) -> int:
    """Return the owner's dossier ref id, creating a seeded draft if absent.

    ``owner_id`` is any ref — a quest today, or any other process that owns a
    living synthesis (ADR 0064 §B). Idempotent: the ``create_draft`` dup-guard
    enforces one dossier per owner, but we look up first so a concurrent/second
    call returns the existing id rather than raising. A fresh dossier is seeded
    with BOTH the narrative body and the pinned ledger; an *existing* dossier
    is returned as-is (a pre-A dossier heals its ledger lazily via
    :func:`ensure_ledger_chunk`, not here — see the module docstring).
    """
    existing = dossier_ref_id(store, owner_id)
    if existing is not None:
        return existing
    stmt = _owner_title(store, owner_id).splitlines()[0]
    ref, _heading = store.create_draft(
        name=f"dossier-{owner_id}",
        title=title or f"Dossier — {stmt[:80]}",
        project_ref_id=owner_id,
        meta={"dossier_of_owner": owner_id},
        relation=_RELATION,
    )
    store.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text=_SEED, split=False)
    _ensure_ledger_chunk_for_ref(store, ref.id)
    return int(ref.id)


def read_dossier(store: Store, owner_id: int) -> tuple[int | None, str | None, str]:
    """``(dossier_ref_id, body_handle, body_text)`` for the owner.

    Returns ``(None, None, "")`` when the owner has no dossier yet. The body
    is every non-heading chunk in reading order (narrative + pinned ledger,
    once both exist), joined — the ``view='dossier'`` handler + history read
    this whole-body join; only the tick *prompt* separates narrative from
    ledger (:func:`read_narrative`, :func:`read_ledger`).
    """
    did = dossier_ref_id(store, owner_id)
    if did is None:
        return None, None, ""
    chunks = store.reading_order(did)
    body = [c for c in chunks if c.chunk_kind != "heading"]
    text = "\n\n".join(c.text for c in body)
    handle = body[0].dc if body else None
    return did, handle, text


def read_narrative(store: Store, owner_id: int) -> str:
    """The model-rewritten narrative only (no pinned chunk, no heading).

    Feeds the tick prompt's ``{dossier}`` slot — the ledger is surfaced
    separately (:func:`read_ledger`) as an explicit constraint, and the
    frontier tree is a code-rendered artifact, neither folded into the
    rewritable prose. Excludes ANY pinned chunk (``meta.pinned`` truthy —
    ``"ledger"`` or ``"frontier-tree"``), not just the ledger, so a future
    pinned chunk needs no code change here. Returns ``""`` when the owner
    has no dossier.
    """
    did = dossier_ref_id(store, owner_id)
    if did is None:
        return ""
    chunks = store.reading_order(did)
    body = [
        c
        for c in chunks
        if c.chunk_kind != "heading" and not (c.meta or {}).get("pinned")
    ]
    return "\n\n".join(c.text for c in body)


def read_ledger(store: Store, owner_id: int) -> str:
    """The pinned ledger chunk's raw markdown text.

    Heals a pre-A dossier with no ledger yet (:func:`ensure_ledger_chunk`),
    so this always returns a well-formed (if empty) ledger for any live
    owner, migration-free.
    """
    handle = ensure_ledger_chunk(store, owner_id)
    did = dossier_ref_id(store, owner_id)
    assert did is not None  # ensure_ledger_chunk just guaranteed a dossier
    for c in store.reading_order(did):
        if c.handle == handle:
            return str(c.text)
    return ""  # pragma: no cover - handle was just resolved above


def append_ledger_entry(store: Store, owner_id: int, section: str, text: str) -> bool:
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
    handle = ensure_ledger_chunk(store, owner_id)
    did = dossier_ref_id(store, owner_id)
    assert did is not None  # ensure_ledger_chunk just guaranteed a dossier
    chunk = next(c for c in store.reading_order(did) if c.handle == handle)
    sections = _parse_ledger(chunk.text)
    if any(existing == stripped_text for existing in sections[key]):
        return False
    sections[key].append(stripped_text)
    store.edit_text(handle, _render_ledger(sections), source={"reason": "quest-ledger"})
    return True


def rewrite_dossier(store: Store, owner_id: int, markdown: str) -> int:
    """Whole-rewrite the owner's dossier NARRATIVE to ``markdown``; return its
    ref id.

    Ensures the dossier exists, then edits the narrative body chunk in place
    (``edit_text`` logs ``prev_text``) — ANY pinned chunk (``meta.pinned``
    truthy: the ledger, and the code-regenerated frontier tree — Slice 4c-4)
    is explicitly excluded, so each survives every rewrite byte-identical
    (ADR 0064 §A). If somehow there is no narrative chunk yet, one is added.
    """
    did = ensure_dossier(store, owner_id)
    chunks = store.reading_order(did)
    body = [
        c
        for c in chunks
        if c.chunk_kind != "heading" and not (c.meta or {}).get("pinned")
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
    "paper_ref_id",
    "read_dossier",
    "read_ledger",
    "read_narrative",
    "rewrite_dossier",
    "update_frontier_tree",
]
