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
- **Claims** (Taproot slice R1) — a cited ``[pub_id]`` that resolves to a
  live ``TAPROOT:claim`` hub explodes into its evidence: derived
  ``establishes`` originators (marked, with the grounding chunk pointer
  when the chase has populated one) plus a one-line corroborator/
  contradictor summary, via :func:`precis.taproot.seniority.derive_evidence`.
  A ``[pub_id]`` that resolves to a non-hub finding (or nothing) is left
  alone — this only mines placeholders that name a claim hub.

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

from precis.taproot.seniority import EvidenceEdge, HubEvidence, derive_evidence
from precis.utils import handle_registry
from precis.utils.mentions import resolve_link_targets
from precis.utils.pub_id_lookup import PLACEHOLDER_RE, lookup_pub_id_finding

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


def _evidence_line(edge: EvidenceEdge, *, marked: bool) -> str:
    """One originator/corroborator line: ``★ pa<id> — <title> (<year>) —
    grounding: <handle>`` (star + grounding only when applicable)."""
    handle = handle_registry.format_handle("paper", edge.paper_ref_id)
    title = " ".join((edge.title or "").split())
    if len(title) > 90:
        title = title[:89].rstrip() + "…"
    label = f"{handle} — {title}" if title else handle
    year = f" ({edge.year})" if edge.year is not None else ""
    grounding = f" — grounding: {edge.source_handle}" if edge.source_handle else ""
    prefix = "★ " if marked else ""
    return f"{prefix}{label}{year}{grounding}"


def _claim_block(ref: Any, evidence: HubEvidence, *, cap: int) -> str:
    """The Claims explosion for one cited hub — the claim line plus its
    derived evidence, capped like the rest of the ring (§6: no silent
    cap). Falls back to corroborators "as best-available" (mirroring
    ``precis resolve``'s :func:`~precis.cli.resolve._hub_evidence_cite_keys`
    policy) when no originator has been derived yet."""
    lines = [_label(ref)]
    if evidence.originators:
        shown = evidence.originators[:cap]
        lines += [f"  {_evidence_line(e, marked=True)}" for e in shown]
        overflow = len(evidence.originators) - len(shown)
        if overflow > 0:
            lines.append(f"    +{overflow} more — focus to expand")
        summary = []
        if evidence.corroborators:
            summary.append(f"+{len(evidence.corroborators)} corroborators")
        if evidence.contradictors:
            summary.append(f"⚠ {len(evidence.contradictors)} contradictors")
        if summary:
            lines.append(f"  {', '.join(summary)}")
    elif evidence.corroborators:
        lines.append("  (no originator derived yet — best-available below)")
        shown = evidence.corroborators[:cap]
        lines += [f"  {_evidence_line(e, marked=False)}" for e in shown]
        overflow = len(evidence.corroborators) - len(shown)
        if overflow > 0:
            lines.append(f"    +{overflow} more — focus to expand")
        if evidence.contradictors:
            lines.append(f"  ⚠ {len(evidence.contradictors)} contradictors")
    else:
        lines.append("  (no evidence derived yet)")
        if evidence.contradictors:
            lines.append(f"  ⚠ {len(evidence.contradictors)} contradictors")
    return "\n".join(lines)


def _mine_claim_hub_ids(
    store: Any, span: list[_Chunk], *, exclude_ref_id: int
) -> list[int]:
    """First-seen-ordered claim-hub ref_ids cited via ``[pub_id]`` in
    ``span`` (Taproot slice R1) — the ring's ``resolve_link_targets`` walk
    doesn't mine this placeholder grammar, so it's mined here separately.
    A pub_id that resolves to nothing, or to a non-hub finding, is skipped
    — left to the existing (currently: invisible) behaviour."""
    seen: set[int] = set()
    ordered: list[int] = []
    for c in span:
        for pub_id in PLACEHOLDER_RE.findall(c.text or ""):
            lookup = lookup_pub_id_finding(store, pub_id)
            if lookup is None or not lookup["is_hub"]:
                continue
            hub_ref_id = lookup["ref_id"]
            if hub_ref_id == exclude_ref_id or hub_ref_id in seen:
                continue
            seen.add(hub_ref_id)
            ordered.append(hub_ref_id)
    return ordered


def _render_claims_group(
    store: Any, hub_ref_ids: list[int], *, cap: int
) -> list[tuple[int, str]]:
    if not hub_ref_ids:
        return []
    refs = store.fetch_refs_by_ids(hub_ref_ids)
    entries: list[tuple[int, str]] = []
    for hub_ref_id in hub_ref_ids:
        ref = refs.get(hub_ref_id)
        if ref is None or getattr(ref, "deleted_at", None) is not None:
            continue
        evidence = derive_evidence(store, hub_ref_id)
        entries.append((hub_ref_id, _claim_block(ref, evidence, cap=cap)))
    return entries


def render_reference_ring(
    store: Any,
    target: _Chunk,
    chunks: list[_Chunk],
    *,
    cap: int = _RING_CAP,
) -> str:
    """Assemble the reference ring for the section rooted at ``target`` (§6).

    ``chunks`` is the whole ref's ``reading_order``. Returns the rendered ring
    (grouped Cited / Cross-refs / Notes / Claims, capped with an overflow
    line), or a single ``— no references —`` line when the section points
    at nothing."""
    return render_ring_groups(collect_ring(store, target, chunks, cap=cap), cap=cap)


def collect_ring(
    store: Any,
    target: _Chunk,
    chunks: list[_Chunk],
    *,
    cap: int = _RING_CAP,
) -> dict[str, list[tuple[int, str]]]:
    """The reference ring as **grouped ``(ref_id, label)`` pairs** — the
    dedup-able form, so a multi-eye composer can merge rings across eyes by
    ``ref_id`` before rendering. ``render_reference_ring`` is a thin renderer
    over this. Groups: ``Cited`` / ``Cross-refs`` / ``Notes`` / ``Claims``
    (empty groups omitted); order within a group is first-seen. ``cap``
    only bounds the Claims explosion's per-hub originator list (collected
    eagerly, unlike the other groups' entry-count cap which is applied at
    render time) — pass the same value you'll render with to keep the two
    caps in sync."""
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

    # ── claims: [pub_id] cites that resolve to a live TAPROOT:claim hub ───
    # (Taproot slice R1) — a separate mining pass, since resolve_link_targets
    # doesn't mine this placeholder grammar.
    claim_hub_ids = _mine_claim_hub_ids(store, span, exclude_ref_id=draft_ref)

    all_ids = (set(outbound) | inbound) - {draft_ref}
    groups: dict[str, list[tuple[int, str]]] = {
        "Cited": [],
        "Cross-refs": [],
        "Notes": [],
        "Claims": _render_claims_group(store, claim_hub_ids, cap=cap),
    }
    if all_ids:
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
        if name == "Claims":
            # Each entry is a pre-rendered multi-line evidence block
            # (Taproot slice R1), not a flat label — citation order, not
            # alphabetical, and only the first line gets the bullet.
            for _rid, block in entries[:cap]:
                block_lines = block.split("\n")
                lines.append(f"  · {block_lines[0]}")
                lines.extend(f"    {bl}" for bl in block_lines[1:])
        else:
            for _rid, label in sorted(entries, key=lambda e: e[1])[:cap]:
                lines.append(f"  · {label}")
        overflow = len(entries) - cap
        if overflow > 0:
            lines.append(f"  +{overflow} more — focus to expand")
    return "\n".join(lines) if any_rendered else "— no references —"
