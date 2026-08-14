"""Rung 6a of the paper-writing pipeline — deterministic paper→section
placement (docs/backlog/paper-writing-pipeline.md §Integrate — the tick body,
step 1: Place).

"Sections are centroids; place = nearest section above a floor (multi-place
allowed); none clears → residual." A section's centroid is the mean of its
subtree's (heading + descendant body chunks') embeddings, renormalized; a
paper's gist is its ``card_combined`` (or head-body-chunk) embedding. Cosine
against every centroid, floor-gated, top-k, ties broken by score order.

Pure geometry — **no model calls**. Consumed by the weave tick (rung 6e); no
MCP surface yet.
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: Cosine-similarity floor a section centroid must clear to receive a paper.
#: The design (§11) flags the exact value as regime-dependent — looser in
#: Make (cast a wide net over a thin dossier), tighter in Maintain (avoid
#: diluting settled sections) — and not yet tuned per corpus/genre. 0.30 is a
#: starting default for bge-m3 cosine similarity.
DEFAULT_PLACEMENT_FLOOR = 0.30

#: Multi-place cap — a paper can land in at most this many sections per call.
DEFAULT_TOP_K = 3


class _SectionCentroid:
    __slots__ = ("handle", "section_chunk_id", "title", "vec")

    def __init__(
        self, section_chunk_id: int, handle: str, title: str, vec: np.ndarray[Any, Any]
    ) -> None:
        self.section_chunk_id = section_chunk_id
        self.handle = handle
        self.title = title
        self.vec = vec


def _subtree_chunk_ids(chunks: list[Any], root_chunk_id: int) -> list[int]:
    """The heading's own ``chunk_id`` + every descendant's, walked over a
    full ``reading_order`` list via ``parent_chunk_id`` — mirrors
    ``precis.backfill.candidates._subtree_chunks`` (kept local rather than
    imported so this rung has no dependency on the backfill module)."""
    by_id = {c.chunk_id: c for c in chunks}

    def in_section(c: Any) -> bool:
        pid: int | None = c.chunk_id
        seen: set[int] = set()
        while pid is not None and pid in by_id and pid not in seen:
            if pid == root_chunk_id:
                return True
            seen.add(pid)
            pid = by_id[pid].parent_chunk_id
        return False

    return [c.chunk_id for c in chunks if in_section(c)]


def _section_centroids(store: Any, dossier_ref_id: int) -> list[_SectionCentroid]:
    """One centroid per dossier heading — the L2-renormalized mean of its
    subtree's available chunk vectors. A heading whose whole subtree has no
    embedded chunks (or whose mean collapses to the zero vector) is skipped:
    it simply can't receive a placement yet."""
    headings = store.drafts.draft_toc(dossier_ref_id)
    if not headings:
        return []
    chunks = store.drafts.reading_order(dossier_ref_id, kind="draft")
    centroids: list[_SectionCentroid] = []
    for h in headings:
        member_ids = _subtree_chunk_ids(chunks, h.chunk_id)
        vecs = [
            v
            for cid in member_ids
            if (v := store.blocks.get_chunk_vector(cid)) is not None
        ]
        if not vecs:
            continue
        mean = np.mean(np.asarray(vecs, dtype=np.float64), axis=0)
        norm = float(np.linalg.norm(mean))
        if norm == 0.0:
            continue
        centroids.append(
            _SectionCentroid(
                section_chunk_id=h.chunk_id,
                handle=h.handle,
                title=h.title,
                vec=mean / norm,
            )
        )
    return centroids


def _cosine(a: np.ndarray[Any, Any], b: np.ndarray[Any, Any]) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def place_papers(
    store: Any,
    dossier_ref_id: int,
    paper_ref_ids: list[int],
    *,
    floor: float = DEFAULT_PLACEMENT_FLOOR,
    top_k: int = DEFAULT_TOP_K,
) -> dict[int, list[dict[str, Any]]]:
    """Place each of ``paper_ref_ids`` into ``dossier_ref_id``'s sections.

    Section centroids are computed once (a dossier with no headings yields no
    centroids, so every paper below comes back residual). For each paper, its
    gist vector (``seed_chunk_for_ref`` → ``get_chunk_vector``; ``None`` when
    unembedded or ref-less) is cosine-scored against every centroid; sections
    at or above ``floor`` are kept, sorted by score descending, and truncated
    to ``top_k`` (multi-place allowed — a paper can land in more than one
    section). An empty list for a paper means **residual** — no section
    cleared the floor (or the paper has no gist vector, or the dossier has no
    sections at all): the weave tick routes those to the residual→section
    clustering pass (§Integrate step 3) rather than forcing a bad fit.

    Returns ``{paper_ref_id: [{"section_chunk_id", "handle", "title",
    "score"}, ...]}``, one entry per input paper (never omitted, even when
    residual)."""
    centroids = _section_centroids(store, dossier_ref_id)
    out: dict[int, list[dict[str, Any]]] = {}
    for paper_ref_id in paper_ref_ids:
        rows: list[dict[str, Any]] = []
        if centroids:
            seed_cid = store.blocks.seed_chunk_for_ref(paper_ref_id)
            vec = (
                store.blocks.get_chunk_vector(seed_cid)
                if seed_cid is not None
                else None
            )
            if vec is not None:
                gist = np.asarray(vec, dtype=np.float64)
                scored = [
                    (score, c)
                    for c in centroids
                    if (score := _cosine(gist, c.vec)) >= floor
                ]
                scored.sort(key=lambda pair: pair[0], reverse=True)
                rows = [
                    {
                        "section_chunk_id": c.section_chunk_id,
                        "handle": c.handle,
                        "title": c.title,
                        "score": round(score, 4),
                    }
                    for score, c in scored[:top_k]
                ]
        out[paper_ref_id] = rows
    return out


def place_paper(
    store: Any,
    dossier_ref_id: int,
    paper_ref_id: int,
    *,
    floor: float = DEFAULT_PLACEMENT_FLOOR,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Single-paper convenience over :func:`place_papers`."""
    return place_papers(
        store, dossier_ref_id, [paper_ref_id], floor=floor, top_k=top_k
    )[paper_ref_id]


def residual_paper_ids(placements: dict[int, list[dict[str, Any]]]) -> list[int]:
    """The paper_ref_ids from a :func:`place_papers` result whose placement
    list is empty — no section cleared the floor for them."""
    return [pid for pid, rows in placements.items() if not rows]


__all__ = [
    "DEFAULT_PLACEMENT_FLOOR",
    "DEFAULT_TOP_K",
    "place_paper",
    "place_papers",
    "residual_paper_ids",
]
