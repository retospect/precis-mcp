"""Assemble + render the source-backfill workspace (slice 1, read-only).

Given target draft chunks, build the eyes :class:`WorkingSet` — the targets at
``fisheye+1hop`` (their neighbourhood + reference ring), the papers they already
cite as ``summary`` cluster-TOC eyes, and the top recall candidates as
``verbatim`` (inferred/transient) chunk eyes — then render it through the
existing turn-taking persona threads composer, followed by a plain "candidate sources" list so the
gaps are legible today. Source eyes are stamped with their backfill role in the
composed render itself — ``★ cited  ← <section>`` on a cited paper, ``○
candidate`` on a recall hit (:func:`_backfill_marks`) — so the working set is
self-describing (slice 2). A per-target ``grounding`` block names ✓ cited /
⚠ under-sourced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.backfill.candidates import (
    Candidate,
    draft_cited_ref_ids,
    find_candidates,
    merge_recurrence,
)
from precis.backfill.dismissed import dismissed_ref_ids
from precis.backfill.provenance import tier_tag
from precis.utils import handle_registry
from precis.utils.working_set_render import render_working_set
from precis.workers.working_set import Provenance, WorkingSet

if TYPE_CHECKING:
    from precis.store.store import Store


def recall_embedder(store: Store | None = None) -> Any | None:
    """A cheap embedder for tick-time recall's **semantic** leg, or ``None`` (→
    lexical + citation-graph only).

    Builds the **remote** HTTP embedder when one is configured
    (``PRECIS_EMBEDDER_URL``) so the semantic leg lights up inside a planner tick
    without ever pulling torch into the agent worker or blocking on a cold local
    model. When no remote URL is set — or construction fails — returns ``None``
    and recall degrades to its lexical + citation-graph legs (which still surface
    real candidates). The corpus embedding dim is threaded through when a store is
    given so a mismatched remote model fails at the boundary rather than writing
    junk (a mismatch just degrades this leg, never the tick)."""
    from precis.config import load_config

    url = load_config().embedder_url
    if not url:
        return None
    try:
        from precis.embedder import make_embedder

        dim = store.embedding_dim() if store is not None else 1024
        return make_embedder("remote", url=url.split(",")[0].strip(), dim=dim)
    except Exception:
        return None


def _resolve_targets(store: Store, targets: list[str], *, kind: str) -> list[Any]:
    """Resolve target handles to live draft chunks, skipping any that don't
    resolve (the MCP layer validates + names the bad handle before we get
    here)."""
    resolved: list[Any] = []
    for t in targets:
        chunk = store.drafts.get_draft_chunk(t, kind=kind)
        if chunk is not None:
            resolved.append(chunk)
    return resolved


def _target_cited_refs(
    store: Store, target_chunks: list[Any], *, kind: str
) -> set[int]:
    """Ref-ids of the papers the *target sections* cite — rendered as summary
    cluster-TOC eyes (the ``★`` chunk highlight lands in slice 2). Uses the
    reference ring's ``Cited`` group per section."""
    from precis.utils.refeye import collect_ring

    out: set[int] = set()
    for tc in target_chunks:
        chunks = store.drafts.reading_order(tc.ref_id, kind=kind)
        for rid, _label in collect_ring(store, tc, chunks).get("Cited", []):
            out.add(int(rid))
    return out


def assemble(
    store: Store,
    embedder: Any,
    targets: list[str],
    *,
    kind: str = "draft",
    per_paper: int = 1,
    max_candidates: int = 8,
) -> tuple[WorkingSet, list[Candidate], set[int]]:
    """Build the workspace for ``targets``. Returns ``(working_set, candidates,
    cited_ref_ids)``. Raises ``ValueError`` if no target resolves to a live
    chunk."""
    target_chunks = _resolve_targets(store, targets, kind=kind)
    if not target_chunks:
        raise ValueError(f"source-backfill: no live {kind} target among {targets!r}")

    # Tier-0 dedup: everything the target drafts already cite (draft-wide, so a
    # candidate cited in another section is not surfaced as a fresh gap) plus the
    # dismissed-source ledger (candidates weighed and rejected on a prior run).
    cited: set[int] = set()
    dismissed: set[int] = set()
    for ref_id in {tc.ref_id for tc in target_chunks}:
        cited |= draft_cited_ref_ids(store, ref_id, kind=kind)
        dismissed |= dismissed_ref_ids(store, ref_id)

    candidates = find_candidates(
        store,
        embedder,
        target_chunks,
        kind=kind,
        exclude_ref_ids=cited | dismissed,
        # The citation-graph lens explores the neighbourhood of what we *cite*
        # (not the wider dismissed set), while cited ∪ dismissed is excluded from
        # the results — a rejected hit stays gone without killing its neighbours.
        citation_seed_ref_ids=cited,
        per_paper=per_paper,
        limit=max_candidates,
    )

    ws = WorkingSet()
    # Targets — the edit neighbourhood + reference ring; the first is the cursor.
    # Key eyes by the universal ``.dc`` handle (``dc``/``pe<id>``) that the
    # composer parses, never the retiring legacy ``.handle``.
    for i, tc in enumerate(target_chunks):
        ws.focus(tc.dc, "fisheye+1hop")
        if i == 0:
            ws.set_cursor(tc.dc)
    # Cited papers — summary cluster-TOC (you know them; you need the reminder).
    cited_here = _target_cited_refs(store, target_chunks, kind=kind)
    refs = store.fetch_refs_by_ids(list(cited_here)) if cited_here else {}
    for rid, ref in refs.items():
        if getattr(ref, "deleted_at", None) is not None:
            continue
        rkind = getattr(ref, "kind", None)
        handle = handle_registry.try_format(rkind, rid) if rkind else None
        if handle:
            ws.focus(handle, "summary")
    # Candidates — inferred/transient (fades unless the driver adopts it by
    # re-focusing). A chunk-addressable hit opens its matched chunk verbatim; a
    # ref-level lead (memory has no chunk handle) opens the whole ref as a flat
    # summary note eye, addressed by me<id>.
    for cand in candidates:
        if cand.is_ref_level:
            ws.focus(cand.paper_handle, "summary", provenance=Provenance.INFERRED)
        else:
            ws.focus(cand.chunk_handle, "verbatim", provenance=Provenance.INFERRED)

    return ws, candidates, cited


def _render_candidate_list(candidates: list[Candidate]) -> str:
    """The plain "candidate sources" block — the uncited-but-relevant hits, the
    product of the sweep. ``○`` marks each as a gap to weigh; the lens tag is
    the (slice-1: single) recall signal, the confidence cue once more lenses
    land."""
    if not candidates:
        return (
            "— candidate sources · none found (already well-cited, or recall empty) —"
        )
    lines = [
        f"— candidate sources · not yet cited · {len(candidates)} "
        "(○ = a gap to weigh; ○○ = recurs across sections; verbatim above) —"
    ]
    for cand in candidates:
        lens = "+".join(cand.lenses)
        title = cand.title[:90] or "(untitled)"
        glyph, where = _support_overlay(cand.support)
        tier = tier_tag(getattr(cand.ref, "kind", None))
        # chunk-level: "pa5 pc10"; ref-level lead: just "me7" (no chunk handle).
        addr = (
            f"{cand.paper_handle} {cand.chunk_handle}"
            if cand.chunk_handle
            else cand.paper_handle
        )
        lines.append(f"  {glyph} {addr} · {tier} · {lens}{where} · {title}")
    return "\n".join(lines)


def _support_overlay(support: tuple[str, ...]) -> tuple[str, str]:
    """``(glyph, where)`` for a candidate's target attribution: ``○○`` + ``·
    recurs across <a> <b>`` when several sections recalled it (a cross-cutting
    gap), ``○`` + ``· supports <a>`` for a single section, ``○`` + ``""`` when
    unattributed (e.g. the doc-level citation lens)."""
    if len(support) > 1:
        return "○○", " · recurs across " + " ".join(support)
    if support:
        return "○", " · supports " + support[0]
    return "○", ""


def _backfill_marks(
    store: Store,
    target_chunks: list[Any],
    candidates: list[Candidate],
    *,
    kind: str,
) -> dict[str, str]:
    """Handle → role-prefix map folded into the composed working set: a cited
    source paper gets ``★ cited  ← <citing section>`` (with the back-ref to the
    draft chunk(s) that cite it — a ``←`` because the reader asymmetry is real:
    the paper does not know it is cited), a recall hit gets ``○ candidate``. Keys
    are the same universal handles the composer renders flat eyes under, so a
    source eye reads its own role in place."""
    from precis.utils.refeye import collect_ring

    marks: dict[str, str] = {}
    citing: dict[int, list[str]] = {}
    for tc in target_chunks:
        chunks = store.drafts.reading_order(tc.ref_id, kind=kind)
        for rid, _label in collect_ring(store, tc, chunks).get("Cited", []):
            citing.setdefault(int(rid), []).append(tc.dc)
    refs = store.fetch_refs_by_ids(list(citing)) if citing else {}
    for rid, sections in citing.items():
        ref = refs.get(rid)
        if ref is None or getattr(ref, "deleted_at", None) is not None:
            continue
        rkind = getattr(ref, "kind", None)
        handle = handle_registry.try_format(rkind, rid) if rkind else None
        if handle:
            back = "  ".join(f"← {s}" for s in sections)
            marks[handle] = f"★ cited  {back}"
    for cand in candidates:
        glyph, where = _support_overlay(cand.support)
        tier = tier_tag(getattr(cand.ref, "kind", None))
        # Key by the handle the composer renders this candidate's eye under — its
        # chunk handle, or its ref handle for a ref-level lead (memory).
        marks[cand.eye_handle] = (
            f"{glyph} candidate · {tier} · {'+'.join(cand.lenses)}{where}"
        )
    return marks


def _render_grounding(store: Store, target_chunks: list[Any], *, kind: str) -> str:
    """Per target: the papers grounding it as ``✓`` short-cites, or a ``⚠``
    coverage warning when the claim is under-sourced. The turn-1 "which papers
    back this section" diagnostic — ``⚠ single-source`` / ``⚠ uncited
    assertion`` are weaknesses in *our* text (not candidates)."""
    from precis.utils.refeye import collect_ring
    from precis.utils.short_cite import short_cite

    lines = ["— grounding · claims WE assert —"]
    for tc in target_chunks:
        chunks = store.drafts.reading_order(tc.ref_id, kind=kind)
        cited_ids = [rid for rid, _ in collect_ring(store, tc, chunks).get("Cited", [])]
        refs = store.fetch_refs_by_ids(cited_ids) if cited_ids else {}
        cites = [
            short_cite(refs[r])
            for r in cited_ids
            if r in refs and getattr(refs[r], "deleted_at", None) is None
        ]
        if not cites:
            status = "⚠ uncited assertion"
        elif len(cites) == 1:
            status = f"⚠ single-source · ✓ {cites[0]}"
        else:
            status = "grounded in  " + " · ".join(f"✓ {c}" for c in cites)
        lines.append(f"  {tc.dc}  {status}")
    return "\n".join(lines)


def render_backfill(
    store: Store,
    embedder: Any,
    targets: list[str],
    *,
    kind: str = "draft",
    per_paper: int = 1,
    max_candidates: int = 8,
) -> str:
    """Assemble + render the whole workspace as one context: the composed
    working set (draft + cited-paper TOCs + candidate chunks), the per-target
    grounding block (✓ cited / ⚠ under-sourced), and the ○ candidate-sources
    list."""
    ws, candidates, _cited = assemble(
        store,
        embedder,
        targets,
        kind=kind,
        per_paper=per_paper,
        max_candidates=max_candidates,
    )
    target_chunks = _resolve_targets(store, targets, kind=kind)
    marks = _backfill_marks(store, target_chunks, candidates, kind=kind)
    parts = [
        render_working_set(store, ws, marks=marks),
        _render_grounding(store, target_chunks, kind=kind),
        _render_candidate_list(candidates),
    ]
    return "\n\n".join(p for p in parts if p)


# ── whole-draft roll-up (Build 2 §G2) ─────────────────────────────────


def _top_level_section_handles(store: Store, ref_id: int, *, kind: str) -> list[str]:
    """The whole-draft roll-up's per-section targets — every depth-0
    (top-level) heading's ``.dc`` handle. A heading-less/flat draft (no
    sub-headings at all) falls back to every top-level body chunk, so a
    short heading-less draft still gets scanned rather than returning
    nothing."""
    chunks = store.drafts.reading_order(ref_id, kind=kind)
    headings = [
        c.dc for c in chunks if c.chunk_kind == "heading" and c.parent_chunk_id is None
    ]
    if headings:
        return headings
    return [c.dc for c in chunks if c.parent_chunk_id is None]


def assemble_draft(
    store: Store,
    embedder: Any,
    ref_id: int,
    *,
    kind: str = "draft",
    per_paper: int = 1,
    section_pool: int = 8,
    max_candidates: int = 20,
) -> tuple[list[Candidate], set[int], list[str], bool]:
    """The whole-draft gap-finder roll-up (Build 2 §G2): run the
    section-scoped sweep (:func:`assemble`) once per top-level section, then
    merge every section's candidates by source ref
    (:func:`~precis.backfill.candidates.merge_recurrence` — the same
    recurrence overlay a multi-target ``assemble`` call already folds several
    *sub*-targets through within one section), so a source several sections
    independently recall ranks first and a paper cited in one section is
    excluded draft-wide (the Tier-0 dedup closure is draft-wide already, not
    per-section). Returns ``(candidates, cited_ref_ids, section_handles,
    truncated)`` — ``truncated`` is set (never a silent drop) when more
    distinct candidates were found than ``max_candidates`` keeps."""
    sections = _top_level_section_handles(store, ref_id, kind=kind)
    if not sections:
        return [], set(), [], False
    per_section: list[list[Candidate]] = []
    cited: set[int] = set()
    for handle in sections:
        try:
            _ws, cands, sec_cited = assemble(
                store,
                embedder,
                [handle],
                kind=kind,
                per_paper=per_paper,
                max_candidates=section_pool,
            )
        except ValueError:
            continue  # a heading with no live chunk — skip, don't abort the roll-up
        per_section.append(cands)
        cited |= sec_cited
    pool_cap = sum(len(cs) for cs in per_section) or 1
    merged = merge_recurrence(per_section, limit=pool_cap)  # no internal truncation
    truncated = len(merged) > max_candidates
    return merged[:max_candidates], cited, sections, truncated


def render_backfill_draft(
    store: Store,
    embedder: Any,
    ref: Any,
    *,
    kind: str = "draft",
    per_paper: int = 1,
    section_pool: int = 8,
    max_candidates: int = 20,
) -> str:
    """Render the whole-draft source-backfill gap-finder (Build 2 §G2): the
    aggregate ○ candidate list merged across every section, plus a
    closure/scan summary. Deliberately **not** the full per-section eyes
    working set + grounding block :func:`render_backfill` composes — at
    whole-draft scale (dozens of sections) that would be unreadable; narrow
    to a section (``view='backfill'`` on a ``dc<id>``) for that detail."""
    candidates, cited, sections, truncated = assemble_draft(
        store,
        embedder,
        ref.id,
        kind=kind,
        per_paper=per_paper,
        section_pool=section_pool,
        max_candidates=max_candidates,
    )
    title = ref.title or ref.slug or str(ref.id)
    lines = [
        f"# {title} — whole-draft source-backfill",
        "",
        f"{len(sections)} section(s) scanned · {len(cited)} paper(s) "
        "already cited (closure)",
    ]
    if truncated:
        lines.append(
            f"showing the top {max_candidates} candidates — narrow to a "
            "section (get(kind='draft', id='dc<id>', view='backfill')) for "
            "that section's full sweep"
        )
    lines.append("")
    lines.append(_render_candidate_list(candidates))
    return "\n".join(lines)
