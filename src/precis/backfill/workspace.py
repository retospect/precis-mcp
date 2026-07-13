"""Assemble + render the source-backfill workspace (slice 1, read-only).

Given target draft chunks, build the eyes :class:`WorkingSet` — the targets at
``fisheye+1hop`` (their neighbourhood + reference ring), the papers they already
cite as ``summary`` cluster-TOC eyes, and the top recall candidates as
``verbatim`` (inferred/transient) chunk eyes — then render it through the
existing ADR-0051 composer, followed by a plain "candidate sources" list so the
gaps are legible today. Source eyes are stamped with their backfill role in the
composed render itself — ``★ cited  ← <section>`` on a cited paper, ``○
candidate`` on a recall hit (:func:`_backfill_marks`) — so the working set is
self-describing (slice 2). A per-target ``grounding`` block names ✓ cited /
⚠ under-sourced.
"""

from __future__ import annotations

from typing import Any

from precis.backfill.candidates import (
    Candidate,
    draft_cited_ref_ids,
    find_candidates,
)
from precis.backfill.dismissed import dismissed_ref_ids
from precis.utils import handle_registry
from precis.utils.working_set_render import render_working_set
from precis.workers.working_set import Provenance, WorkingSet


def recall_embedder(store: Any = None) -> Any | None:
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


def _resolve_targets(store: Any, targets: list[str], *, kind: str) -> list[Any]:
    """Resolve target handles to live draft chunks, skipping any that don't
    resolve (the MCP layer validates + names the bad handle before we get
    here)."""
    resolved: list[Any] = []
    for t in targets:
        chunk = store.get_draft_chunk(t, kind=kind)
        if chunk is not None:
            resolved.append(chunk)
    return resolved


def _target_cited_refs(store: Any, target_chunks: list[Any], *, kind: str) -> set[int]:
    """Ref-ids of the papers the *target sections* cite — rendered as summary
    cluster-TOC eyes (the ``★`` chunk highlight lands in slice 2). Uses the
    reference ring's ``Cited`` group per section."""
    from precis.utils.refeye import collect_ring

    out: set[int] = set()
    for tc in target_chunks:
        chunks = store.reading_order(tc.ref_id, kind=kind)
        for rid, _label in collect_ring(store, tc, chunks).get("Cited", []):
            out.add(int(rid))
    return out


def assemble(
    store: Any,
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
    # Candidates — verbatim matched chunk, inferred/transient (fades unless the
    # driver adopts it by re-focusing).
    for cand in candidates:
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
        lines.append(
            f"  {glyph} {cand.paper_handle} {cand.chunk_handle} · {lens}{where} · {title}"
        )
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
    store: Any,
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
        chunks = store.reading_order(tc.ref_id, kind=kind)
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
        marks[cand.chunk_handle] = f"{glyph} candidate · {'+'.join(cand.lenses)}{where}"
    return marks


def _render_grounding(store: Any, target_chunks: list[Any], *, kind: str) -> str:
    """Per target: the papers grounding it as ``✓`` short-cites, or a ``⚠``
    coverage warning when the claim is under-sourced. The turn-1 "which papers
    back this section" diagnostic — ``⚠ single-source`` / ``⚠ uncited
    assertion`` are weaknesses in *our* text (not candidates)."""
    from precis.utils.refeye import collect_ring
    from precis.utils.short_cite import short_cite

    lines = ["— grounding · claims WE assert —"]
    for tc in target_chunks:
        chunks = store.reading_order(tc.ref_id, kind=kind)
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
    store: Any,
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
