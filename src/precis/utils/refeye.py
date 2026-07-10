"""The reference ring — the ``fisheye+1hop`` extent (ADR 0051 §6, refeye slice).

Where the spatial fisheye (``utils.fisheye``) walks the *reading-order* graph —
"what is physically near this node" — the reference ring walks the **reference
graph**: "what does this section *point at*, one edge out." Focus a section at
``fisheye+1hop`` and everything it references instant-appears around it:

- **Cited** — papers / datasheets / patents the section cites
  (``[§slug~N]`` / ``paper:slug`` mined from the body).
- **Cross-refs** — other draft/plan chunks it links (``[[dc41]]`` / ``[¶h]``).
- **Notes** — memories and notes **linked to** the section (inbound
  ``related-to`` / ``see-also`` edges materialised by the mentions autolinker,
  ``utils.mentions``) — the "things noted on this."

It follows **edges only** (deterministic, zero false positives). A memory that
is merely *about* the section but was never linked is a similarity hit —
that is ``search``'s job, a separate ``+recall`` rung, not a hop.

Pure **read-time assembly** over existing primitives (``extract_handles`` /
``resolve_link_targets`` for outbound, ``links_for`` for inbound) — no new
storage, no authoring-time edge. Ships dark until the render-loop wires
``Extent.HOP1`` in. The ring is rendered by *kind* (a paper is not a tree
node), capped per group with a visible overflow line — no silent truncation.
"""

from __future__ import annotations

from typing import Any, Protocol

from precis.utils import handle_registry
from precis.utils.mentions import resolve_link_targets

#: Relations that carry *meaning* (as opposed to structure/plumbing). The ring
#: follows these — which is where linked memories/notes live — and ignores the
#: structural ones (``plan-of`` / ``draft-of`` / ``parent`` / ``touched``).
SEMANTIC_RELATIONS: frozenset[str] = frozenset(
    {
        "related-to",
        "see-also",
        "supports",
        "derived-from",
        "generalises",
        "corrects",
        "refutes",
        "cites",
    }
)

#: Kind → ring group. Anything unlisted falls into "Notes" (memory / finding /
#: gripe / conv / todo …) — the "noted on this" bucket.
_CITED_KINDS: frozenset[str] = frozenset({"paper", "datasheet", "patent", "cfp"})
_XREF_KINDS: frozenset[str] = frozenset({"draft", "plan"})

#: Max entries rendered per group before the overflow line (§6: no silent cap).
_RING_CAP = 8


class _Chunk(Protocol):
    chunk_id: int
    ref_id: int
    text: str
    parent_chunk_id: int | None


def _subtree(chunks: list[_Chunk], target: _Chunk) -> list[_Chunk]:
    """The target node + its descendants, in reading order — a "section" is a
    heading and everything under it."""
    by_id = {c.chunk_id: c for c in chunks}

    def in_section(c: _Chunk) -> bool:
        pid: int | None = c.chunk_id
        seen: set[int] = set()
        while pid is not None and pid in by_id and pid not in seen:
            if pid == target.chunk_id:
                return True
            seen.add(pid)
            pid = by_id[pid].parent_chunk_id
        return False

    return [c for c in chunks if in_section(c)]


def _label(ref: Any) -> str:
    """A ``<handle> — <title>`` line for a referenced ref, rendered by kind."""
    kind = getattr(ref, "kind", "?")
    try:
        handle = handle_registry.format_handle(kind, int(ref.id))
    except Exception:
        handle = f"{kind}:{getattr(ref, 'slug', None) or ref.id}"
    slug = getattr(ref, "slug", None)
    lead = f"{kind}:{slug}" if slug and kind in _CITED_KINDS else handle
    title = " ".join((getattr(ref, "title", None) or "").split())
    if len(title) > 90:
        title = title[:89].rstrip() + "…"
    return f"{lead} — {title}" if title else lead


def _group_for(kind: str) -> str:
    if kind in _CITED_KINDS:
        return "Cited"
    if kind in _XREF_KINDS:
        return "Cross-refs"
    return "Notes"


def render_reference_ring(
    store: Any,
    target: _Chunk,
    chunks: list[_Chunk],
    *,
    cap: int = _RING_CAP,
) -> str:
    """Assemble the reference ring for the section rooted at ``target`` (§6).

    ``chunks`` is the whole ref's ``reading_order``. Returns the rendered ring
    (grouped Cited / Cross-refs / Notes, capped with an overflow line), or a
    single ``— no references —`` line when the section points at nothing."""
    return render_ring_groups(collect_ring(store, target, chunks), cap=cap)


def collect_ring(
    store: Any,
    target: _Chunk,
    chunks: list[_Chunk],
) -> dict[str, list[tuple[int, str]]]:
    """The reference ring as **grouped ``(ref_id, label)`` pairs** — the
    dedup-able form, so a multi-eye composer can merge rings across eyes by
    ``ref_id`` before rendering. ``render_reference_ring`` is a thin renderer
    over this. Groups: ``Cited`` / ``Cross-refs`` / ``Notes`` (empty groups
    omitted); order within a group is first-seen."""
    span = _subtree(chunks, target)
    draft_ref = target.ref_id

    # ── outbound: refs mined from the section's body text ────────────────
    # (ref_id → the pos it was cited at, first-seen wins). resolve_link_targets
    # already unions kind:id mentions + universal [[handle]]s + patent nums.
    outbound: dict[int, int | None] = {}
    for c in span:
        for lt in resolve_link_targets(store, c.text, exclude_ref_id=None):
            outbound.setdefault(lt.dst_ref_id, lt.dst_pos)

    # ── inbound: notes/memories LINKED to this section (edges, not search) ──
    inbound: set[int] = set()
    for link in store.links_for(draft_ref, direction="in"):
        if getattr(link, "relation", None) not in SEMANTIC_RELATIONS:
            continue
        # Section-scope when the edge lands on a chunk in this subtree; keep
        # ref-level (whole-draft) notes too. (Chunk-id scoping of inbound edges
        # is a refinement — links_for projects pos, not chunk_id.)
        inbound.add(int(link.src_ref_id))

    all_ids = (set(outbound) | inbound) - {draft_ref}
    groups: dict[str, list[tuple[int, str]]] = {
        "Cited": [],
        "Cross-refs": [],
        "Notes": [],
    }
    if not all_ids:
        return {name: g for name, g in groups.items() if g}
    refs = store.fetch_refs_by_ids(list(all_ids))
    for rid in all_ids:
        ref = refs.get(rid)
        if ref is None or getattr(ref, "deleted_at", None) is not None:
            continue
        groups[_group_for(getattr(ref, "kind", "?"))].append((rid, _label(ref)))
    return {name: g for name, g in groups.items() if g}


def render_ring_groups(
    groups: dict[str, list[tuple[int, str]]],
    *,
    cap: int = _RING_CAP,
    header: str = "— referenced (1 hop) —",
) -> str:
    """Render collected ring groups — capped per group with a visible overflow
    line (§6: no silent cap). ``— no references —`` when every group is empty."""
    lines: list[str] = [header]
    any_rendered = False
    for name, entries in groups.items():
        if not entries:
            continue
        any_rendered = True
        lines.append(f"{name}:")
        for _rid, label in sorted(entries, key=lambda e: e[1])[:cap]:
            lines.append(f"  · {label}")
        overflow = len(entries) - cap
        if overflow > 0:
            lines.append(f"  +{overflow} more — focus to expand")
    return "\n".join(lines) if any_rendered else "— no references —"
