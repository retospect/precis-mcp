"""Recall — surface the uncited-but-relevant corpus sources for a target.

Slice 1 ships the deterministic **text lens**: seed the multi-query search from
a target section's own keywords (lexical legs) + its embedded text (semantic
leg) — the section *programs its own recall* — scope to ``kind='paper'``, and
exclude everything the draft already cites (**Tier-0 dedup**). Returns ranked
:class:`Candidate` chunks, best first. No LLM: HyDE ``answers=`` and the Tier-1
relevance cull are model-authored layers for a later slice; here the RRF fused
score is the ranker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.mentions import resolve_link_targets
from precis.utils.refeye import _CITED_KINDS

#: Recall-lens ids. Slice 1 ships ``text``; slice 3 adds ``citation`` (the
#: citation-graph provable-omission lens); ``keyword``/``number``/``finding``
#: land in later slices. Recorded per candidate so lens-agreement drives
#: confidence (a hit both lenses find is a stronger gap than either alone).
LENS_TEXT = "text"
LENS_CITATION = "citation"

#: The multi-query search caps ``queries=`` at 8 legs; cap the keyword legs we
#: seed to match (extra keywords add cost without new recall).
_MAX_KEYWORD_LEGS = 8
#: Cap on the section text embedded as the single semantic seed vector.
_SEED_TEXT_CAP = 2000


@dataclass(frozen=True, slots=True)
class Candidate:
    """One uncited-but-relevant hit — a paper chunk the recall sweep surfaced.

    ``chunk_handle`` is the ``pc<id>`` to open; ``paper_handle`` the ``pa<id>``
    of its ref. ``lenses`` records which lens(es) found it (lens-agreement =
    confidence). ``support`` names the target handles it could support — filled
    by the caller once claim-mapping exists (slice 2+); empty in slice 1."""

    ref_id: int
    ref: Any
    chunk_id: int
    chunk_handle: str
    score: float
    lenses: tuple[str, ...] = (LENS_TEXT,)
    support: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return " ".join((getattr(self.ref, "title", None) or "").split())

    @property
    def paper_handle(self) -> str:
        kind = getattr(self.ref, "kind", "paper") or "paper"
        return handle_registry.try_format(kind, self.ref_id) or f"{kind}:{self.ref_id}"


def _subtree_chunks(chunks: list[Any], target: Any) -> list[Any]:
    """The target node + its descendants, in reading order — a "section" is a
    heading and everything under it (mirrors ``refeye._subtree`` without
    importing a private)."""
    by_id = {c.chunk_id: c for c in chunks}

    def in_section(c: Any) -> bool:
        pid: int | None = c.chunk_id
        seen: set[int] = set()
        while pid is not None and pid in by_id and pid not in seen:
            if pid == target.chunk_id:
                return True
            seen.add(pid)
            pid = by_id[pid].parent_chunk_id
        return False

    return [c for c in chunks if in_section(c)]


def draft_cited_ref_ids(store: Any, ref_id: int, *, kind: str = "draft") -> set[int]:
    """The cited-source ref_ids a draft already points at — mined from every
    chunk's body (``resolve_link_targets``, the reference ring's path), filtered
    to citeable kinds (paper/datasheet/patent/cfp) and to live refs. This is the
    Tier-0 dedup set: a candidate already cited *anywhere* in the draft is not a
    fresh gap."""
    chunks = store.reading_order(ref_id, kind=kind)
    hit: set[int] = set()
    for c in chunks:
        for lt in resolve_link_targets(store, c.text, exclude_ref_id=None):
            hit.add(int(lt.dst_ref_id))
    if not hit:
        return set()
    refs = store.fetch_refs_by_ids(list(hit))
    return {
        rid
        for rid, r in refs.items()
        if getattr(r, "kind", None) in _CITED_KINDS
        and getattr(r, "deleted_at", None) is None
    }


def seed_from_targets(
    store: Any, target_chunks: list[Any], *, kind: str = "draft"
) -> tuple[list[str], str]:
    """Derive ``(keyword legs, seed text)`` from the target subtrees — the
    section programs its own recall. Keywords (from ``block_views``) become
    lexical legs; the joined subtree text becomes the semantic seed. Reads
    ``block_views`` once per distinct draft ref."""
    keywords: list[str] = []
    seen_kw: set[str] = set()
    texts: list[str] = []
    views_by_ref: dict[int, dict[str, dict[str, str]]] = {}
    for tc in target_chunks:
        chunks = store.reading_order(tc.ref_id, kind=kind)
        views = views_by_ref.get(tc.ref_id)
        if views is None:
            views = views_by_ref[tc.ref_id] = store.block_views(tc.ref_id)
        for c in _subtree_chunks(chunks, tc):
            texts.append(c.text or "")
            kwline = (views.get(c.handle, {}) or {}).get("keywords", "") or ""
            for raw in kwline.split(","):
                k = raw.strip()
                key = k.lower()
                if k and key not in seen_kw:
                    seen_kw.add(key)
                    keywords.append(k)
    seed_text = " ".join(" ".join(texts).split())[:_SEED_TEXT_CAP]
    return keywords[:_MAX_KEYWORD_LEGS], seed_text


def find_candidates(
    store: Any,
    embedder: Any,
    target_chunks: list[Any],
    *,
    kind: str = "draft",
    exclude_ref_ids: set[int] | None = None,
    citation_seed_ref_ids: set[int] | None = None,
    per_paper: int = 1,
    limit: int = 12,
) -> list[Candidate]:
    """Run the recall lenses for the resolved target chunks and return ranked
    candidates, best first. ``exclude_ref_ids`` is the Tier-0 dedup set (cited ∪
    dismissed); ``citation_seed_ref_ids`` is the *cited* set whose citation-graph
    neighbours the ``citation`` lens explores (kept distinct from the exclude set
    so a dismissed paper stops resurfacing without seeding its own neighbourhood).
    ``per_paper=1`` spreads the pool across papers (breadth, not depth). Degrades
    to lexical-only when the embedder is down, and the citation lens self-disables
    on any failure — the text lens always carries the workspace."""
    keywords, seed_text = seed_from_targets(store, target_chunks, kind=kind)
    q_texts = keywords or ([seed_text[:400]] if seed_text else [])
    query_vecs: list[list[float]] = []
    if seed_text:
        vec = embed_query(embedder, seed_text)
        if vec:
            query_vecs.append(vec)

    out: list[Candidate] = []
    if q_texts or query_vecs:
        hits = store.search_blocks_multi(
            q_texts=q_texts,
            query_vecs=query_vecs,
            mode="hybrid",
            kind="paper",
            per_paper=per_paper,
            exclude_ref_ids=sorted(exclude_ref_ids) if exclude_ref_ids else None,
            limit=limit,
        )
        for block, ref, score in hits:
            rkind = getattr(ref, "kind", "paper") or "paper"
            handle = handle_registry.format_handle(rkind, int(block.id), chunk=True)
            out.append(
                Candidate(
                    ref_id=int(ref.id),
                    ref=ref,
                    chunk_id=int(block.id),
                    chunk_handle=handle,
                    score=float(score),
                )
            )

    _merge_citation_lens(
        store, out, citation_seed_ref_ids, exclude_ref_ids or set(), limit
    )
    return out[:limit]


def _merge_citation_lens(
    store: Any,
    out: list[Candidate],
    citation_seed_ref_ids: set[int] | None,
    exclude_ref_ids: set[int],
    limit: int,
) -> None:
    """Fold the citation-graph lens into the text-lens ``out`` list **in place**:
    a paper both lenses find gets ``citation`` appended to its lenses (a
    lens-agreement badge — kept in text-rank position since it is already
    semantically matched); citation-only neighbours append after, filling the
    remaining slots. Never raises: a citation-lens failure leaves ``out``
    untouched so the workspace still renders."""
    if not citation_seed_ref_ids:
        return
    try:
        from precis.backfill.citation_lens import find_citation_candidates

        cite_cands = find_citation_candidates(
            store, citation_seed_ref_ids, exclude=exclude_ref_ids, limit=limit
        )
    except Exception:  # pragma: no cover — defensive; text lens still stands
        return
    by_ref = {c.ref_id: i for i, c in enumerate(out)}
    for cc in cite_cands:
        i = by_ref.get(cc.ref_id)
        if i is not None:
            existing = out[i]
            if LENS_CITATION not in existing.lenses:
                out[i] = replace(existing, lenses=(*existing.lenses, LENS_CITATION))
        else:
            out.append(cc)
