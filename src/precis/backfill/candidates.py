"""Recall — surface the uncited-but-relevant corpus sources for a target.

The deterministic **text lens**: seed the multi-query search from a target
section's own keywords (lexical legs) + its embedded text (semantic leg) — the
section *programs its own recall* — scope it across the source kinds (paper /
cfp / patent / datasheet, :data:`~precis.backfill.provenance.SOURCE_KINDS`), and
exclude everything the draft already cites (**Tier-0 dedup**). Returns ranked
:class:`Candidate` chunks, best first, each score down-weighted by its source's
**provenance tier** so peer-reviewed evidence outranks an equally-matched
prior-art spec. No LLM: HyDE ``answers=`` and the Tier-1 relevance cull are
model-authored layers for a later slice; here the RRF fused score is the ranker.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from precis.backfill.provenance import SOURCE_KINDS, tier_for
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.mentions import resolve_link_targets
from precis.utils.refeye import _CITED_KINDS

if TYPE_CHECKING:
    from precis.store.store import Store

#: Recall-lens ids. Slice 1 ships ``text``; slice 3 adds ``citation`` (the
#: citation-graph provable-omission lens); ``topic`` (Build 2 §G3) confirms a
#: semantic hit against the draft's own topic-category domain; ``keyword``/
#: ``number``/``finding`` land in later slices. Recorded per candidate so
#: lens-agreement drives confidence (a hit both lenses find is a stronger gap
#: than either alone).
LENS_TEXT = "text"
LENS_CITATION = "citation"
LENS_TOPIC = "topic"

#: ``topic:<slug>`` open tags (:mod:`precis.workers.classify_topics`) live
#: under this prefix — a plain ``Tag.open(f"topic:{slug}")``, so reading them
#: back off ``ref_tags_bulk`` is just an ``(namespace, value)`` prefix match.
_TOPIC_TAG_PREFIX = "topic:"

#: The stay-in-scope precision gate (G3): a semantic hit whose paper carries
#: no on-domain ``topic:`` tag is demoted (not dropped — it may be the only
#: hit found) so an off-domain neighbour (e.g. a general-graphene paper next
#: to a nanobuds draft) sinks below the on-domain candidates rather than
#: leading the gap list.
_OFF_DOMAIN_PENALTY = 0.2

#: The multi-query search caps ``queries=`` at 8 legs; cap the keyword legs we
#: seed to match (extra keywords add cost without new recall).
_MAX_KEYWORD_LEGS = 8
#: Cap on the section text embedded as the single semantic seed vector.
_SEED_TEXT_CAP = 2000


@dataclass(frozen=True, slots=True)
class Candidate:
    """One uncited-but-relevant hit the recall sweep surfaced.

    Two shapes, distinguished by :attr:`is_ref_level`:

    * **chunk-level** (the norm — ``paper``/``cfp``/``patent``/``datasheet``):
      ``chunk_handle`` is the ``pc<id>``/``qc<id>``/``pk<id>``/``dk<id>`` to open,
      ``paper_handle`` the ``pa<id>`` of its ref.
    * **ref-level** (a *lead* — ``memory``, which has **no** chunk handle):
      ``chunk_handle`` is ``""``; the candidate is addressed + rendered by its
      ``me<id>`` record handle (``paper_handle``) as a flat note eye. Its
      ``chunk_id`` still records the matched block for provenance.

    ``lenses`` records which lens(es) found it (lens-agreement = confidence).
    ``support`` names the target handles it could support (the recurrence overlay)."""

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

    @property
    def is_ref_level(self) -> bool:
        """A lead with no chunk handle (e.g. ``memory``) — opened + marked at the
        ref (``me<id>`` flat eye), not by a ``pc<id>`` chunk eye."""
        return not self.chunk_handle

    @property
    def eye_handle(self) -> str:
        """The handle the workspace opens + marks this candidate under: its chunk
        handle when chunk-addressable, else its ref handle (a lead)."""
        return self.chunk_handle or self.paper_handle


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


def draft_cited_ref_ids(store: Store, ref_id: int, *, kind: str = "draft") -> set[int]:
    """The cited-source ref_ids a draft already points at — mined from every
    chunk's body (``resolve_link_targets``, the reference ring's path), filtered
    to citeable kinds (paper/datasheet/patent/cfp) and to live refs, **plus**
    every ``[fi]`` claim-hub cite's evidence-supporter papers
    (:func:`_hub_supporter_ref_ids` — Build 2 §G1). This is the Tier-0 dedup
    set: a candidate already cited *anywhere* in the draft — directly, or as
    a hub's supporting evidence once ``[pc]``/``[pa]`` backfill to ``[fi]`` —
    is not a fresh gap."""
    chunks = store.reading_order(ref_id, kind=kind)
    hit: set[int] = set()
    for c in chunks:
        for lt in resolve_link_targets(store, c.text, exclude_ref_id=None):
            hit.add(int(lt.dst_ref_id))
    cited: set[int] = set()
    if hit:
        refs = store.fetch_refs_by_ids(list(hit))
        cited = {
            rid
            for rid, r in refs.items()
            if getattr(r, "kind", None) in _CITED_KINDS
            and getattr(r, "deleted_at", None) is None
        }
    return cited | _hub_supporter_ref_ids(store, chunks)


def _hub_supporter_ref_ids(store: Store, chunks: list[Any]) -> set[int]:
    """The evidence-supporter papers of every ``[fi]`` claim-hub these chunks
    cite (Build 2 §G1 — the load-bearing closure fix). A hub already backs
    its claim with these papers, so once ``[pc]``/``[pa]`` cites backfill to
    ``[fi]`` hub cites, dropping them from the closure would re-surface
    already-used papers as fresh "gaps" — a correctness bug taproot-ifying a
    draft would otherwise create.

    Mines the same ``[fi]``/``[pub_id]`` cites
    :func:`precis.handlers._citations_view._collect_raw_cites` partitions as
    ``done``, then unions each live claim hub's **originators +
    corroborators** (:func:`precis.taproot.seniority.derive_evidence`).
    Contradictors are deliberately excluded — a paper that contradicts the
    claim is not "already cited for this point." A finding cite that is
    *not* a live ``TAPROOT:claim`` hub (a plain chase finding) is silently
    skipped, not errored — ``derive_evidence`` only accepts hubs."""
    from precis.handlers._citations_view import _collect_raw_cites
    from precis.taproot.seniority import derive_evidence, is_claim_hub

    raw = _collect_raw_cites(store, chunks)
    hub_ids = {c.ref_id for c in raw if c.kind == "finding"}
    out: set[int] = set()
    for hub_id in hub_ids:
        if not is_claim_hub(store, hub_id):
            continue  # a plain chase finding, not a claim hub — not evidence
        evidence = derive_evidence(store, hub_id)
        out.update(e.paper_ref_id for e in evidence.originators)
        out.update(e.paper_ref_id for e in evidence.corroborators)
    return out


def seed_from_targets(
    store: Store, target_chunks: list[Any], *, kind: str = "draft"
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


def _text_lens(
    store: Store,
    embedder: Any,
    target_chunks: list[Any],
    *,
    kind: str,
    kinds: tuple[str, ...],
    exclude_ref_ids: set[int] | None,
    per_paper: int,
    limit: int,
    support: tuple[str, ...],
) -> list[Candidate]:
    """One text-lens sweep for ``target_chunks`` — the section(s) program their
    own recall (keywords + embedded text → RRF hybrid search over ``kinds``,
    Tier-0-excluded). Every hit is stamped with ``support`` (the target handle(s)
    this sweep speaks for) so the caller can see which section surfaced it, and its
    score is **down-weighted by the source's provenance tier** (:func:`tier_for`)
    so a peer-reviewed paper outranks an equally-matched prior-art datasheet."""
    keywords, seed_text = seed_from_targets(store, target_chunks, kind=kind)
    q_texts = keywords or ([seed_text[:400]] if seed_text else [])
    query_vecs: list[list[float]] = []
    if seed_text:
        vec = embed_query(embedder, seed_text)
        if vec:
            query_vecs.append(vec)
    if not (q_texts or query_vecs):
        return []
    hits = store.search_blocks_multi(
        q_texts=q_texts,
        query_vecs=query_vecs,
        mode="hybrid",
        kinds=list(kinds),
        per_paper=per_paper,
        exclude_ref_ids=sorted(exclude_ref_ids) if exclude_ref_ids else None,
        limit=limit,
    )
    out: list[Candidate] = []
    for block, ref, score in hits:
        rkind = getattr(ref, "kind", "paper") or "paper"
        # A chunk handle when the kind exposes one; ``""`` for a handle-less kind
        # (``memory`` — the lead tier), which then rides as a ref-level candidate
        # (addressed by ``me<id>``). ``try_format`` keeps a new handle-less source
        # kind from crashing the whole sweep the way ``format_handle`` would.
        handle = handle_registry.try_format(rkind, int(block.id), chunk=True) or ""
        out.append(
            Candidate(
                ref_id=int(ref.id),
                ref=ref,
                chunk_id=int(block.id),
                chunk_handle=handle,
                score=float(score) * tier_for(rkind).weight,
                support=support,
            )
        )
    return out


def _union_support(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    """Order-preserving union of two support tuples (document-order target
    handles; de-duplicated)."""
    return tuple(dict.fromkeys((*a, *b)))


def merge_recurrence(
    per_target: list[list[Candidate]], *, limit: int
) -> list[Candidate]:
    """Union per-target text-lens results **by source ref** into the recurrence
    overlay: a paper several sections independently recall keeps its best-scoring
    chunk and accrues every supporting target handle, and **cross-cutting gaps
    rank first** — a source missed in three sections is a stronger omission than
    one missed in one. Pure (no store), so the ranking is unit-testable."""
    merged: dict[int, Candidate] = {}
    for cands in per_target:
        for cand in cands:
            prev = merged.get(cand.ref_id)
            if prev is None:
                merged[cand.ref_id] = cand
                continue
            support = _union_support(prev.support, cand.support)
            keep = cand if cand.score > prev.score else prev
            merged[cand.ref_id] = replace(keep, support=support)
    out = sorted(merged.values(), key=lambda c: (len(c.support), c.score), reverse=True)
    return out[:limit]


def find_candidates(
    store: Store,
    embedder: Any,
    target_chunks: list[Any],
    *,
    kind: str = "draft",
    kinds: tuple[str, ...] | None = None,
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
    on any failure — the text lens always carries the workspace.

    **Beyond papers + provenance tiering:** ``kinds`` (default
    :data:`~precis.backfill.provenance.SOURCE_KINDS` — paper/cfp/patent/datasheet)
    scopes the recall sweep across source kinds, and each hit's score is
    down-weighted by its provenance tier so a peer-reviewed paper outranks an
    equally-matched prior-art datasheet (the tier tag + skill admonition ride
    downstream in the render).

    **Multi-focus recurrence overlay:** with more than one target the text lens
    runs *per section* (each programs its own recall) and the hits merge by source
    ref (:func:`merge_recurrence`), so every candidate carries which section(s)
    surfaced it and a source recalled across several sections ranks first. A
    single target is one sweep whose hits are attributed to it.

    **Topic/categorizer precision gate (Build 2 §G3):** ``citation_seed_ref_ids``
    doubles as the source for the draft's ``topic:`` domain (the
    :func:`draft_topic_slugs` dominant among the already-cited papers) — an
    on-domain hit is confirmed (``topic`` folded into ``.lenses``) and an
    off-domain or untagged one is demoted below the on-domain hits (never
    hard-dropped — it may be the only hit found), keeping a project like
    "nanobuds" from drifting into adjacent-but-different corpus neighbours
    (nanoribbons, general graphene). Degrades to a no-op — the existing
    semantic+citation ranking — when no cited paper carries a ``topic:`` tag
    (:mod:`precis.workers.classify_topics` is a dark/default-off pass, so
    coverage may be sparse)."""
    kinds = kinds or SOURCE_KINDS
    if len(target_chunks) > 1:
        per_target = [
            _text_lens(
                store,
                embedder,
                [tc],
                kind=kind,
                kinds=kinds,
                exclude_ref_ids=exclude_ref_ids,
                per_paper=per_paper,
                limit=limit,
                support=(tc.dc,),
            )
            for tc in target_chunks
        ]
        out = merge_recurrence(per_target, limit=limit)
    else:
        support = (target_chunks[0].dc,) if target_chunks else ()
        out = _text_lens(
            store,
            embedder,
            target_chunks,
            kind=kind,
            kinds=kinds,
            exclude_ref_ids=exclude_ref_ids,
            per_paper=per_paper,
            limit=limit,
            support=support,
        )

    # The topic gate must cover the citation lens too — it appends its
    # own (possibly off-domain) candidates onto ``out`` in place, so the
    # gate runs ONCE, after that merge, over the final combined list.
    # Gating only the text lens first (the old order) let an off-domain
    # citation-graph neighbour ride through undemoted — a defeat of the
    # "nanobuds stays on nanobuds" guarantee for exactly the lens built to
    # find provable-but-adjacent omissions.
    draft_topics = draft_topic_slugs(store, citation_seed_ref_ids or set())
    _merge_citation_lens(
        store, out, citation_seed_ref_ids, exclude_ref_ids or set(), limit
    )
    out = _apply_topic_gate(store, out, draft_topics)
    return out[:limit]


def _topic_slugs(tag_pairs: list[tuple[str, str]]) -> set[str]:
    """The bare ``<slug>`` set out of a ref's ``ref_tags_bulk`` pairs, for the
    open ``topic:<slug>`` tags among them (any other tag is ignored). V2
    stores an open tag's namespace as the ``'OPEN'`` sentinel (see
    ``store._tags_ops``'s ``_tag_to_namespace_value``), which
    ``ref_tags_bulk`` returns verbatim — not the v1 ``Tag.open``'s lowercase
    ``'open'``."""
    return {
        v[len(_TOPIC_TAG_PREFIX) :]
        for ns, v in tag_pairs
        if ns == "OPEN" and v.startswith(_TOPIC_TAG_PREFIX)
    }


def draft_topic_slugs(store: Store, cited_ref_ids: set[int]) -> set[str]:
    """The draft's topic-category domain (Build 2 §G3) — the ``topic:<slug>``
    tag(s) **dominant** among ``cited_ref_ids`` (the citation closure): the
    slug(s) tied for the highest occurrence count across the cited papers'
    ``topic:`` tags. Empty when ``cited_ref_ids`` is empty or none of them
    carry a ``topic:`` tag — ``classify_topics`` is a dark/default-off pass,
    so sparse-to-absent coverage is expected; callers must treat an empty
    result as "no domain derivable" and skip the gate rather than filter
    everything out."""
    if not cited_ref_ids:
        return set()
    counts: Counter[str] = Counter()
    for pairs in store.ref_tags_bulk(sorted(cited_ref_ids)).values():
        counts.update(_topic_slugs(pairs))
    if not counts:
        return set()
    top = max(counts.values())
    return {slug for slug, n in counts.items() if n == top}


def _apply_topic_gate(
    store: Store, candidates: list[Candidate], draft_topics: set[str]
) -> list[Candidate]:
    """The stay-in-scope precision gate (Build 2 §G3): partitions
    ``candidates`` into on-domain (a ``topic:`` tag in ``draft_topics`` —
    confirmed via ``topic`` folded into ``.lenses``) and off-domain/untagged
    (demoted — score scaled by :data:`_OFF_DOMAIN_PENALTY`), returning
    on-domain first so an off-domain hit can never outrank an on-domain one
    regardless of raw score or cross-section recurrence. A no-op (returns
    ``candidates`` unchanged) when ``draft_topics`` is empty — the domain
    can't be derived, so degrade to the existing semantic+citation ranking
    rather than gating on nothing."""
    if not draft_topics or not candidates:
        return candidates
    tags = store.ref_tags_bulk([c.ref_id for c in candidates])
    on_domain: list[Candidate] = []
    off_domain: list[Candidate] = []
    for cand in candidates:
        if _topic_slugs(tags.get(cand.ref_id, [])) & draft_topics:
            if LENS_TOPIC not in cand.lenses:
                cand = replace(cand, lenses=(*cand.lenses, LENS_TOPIC))
            on_domain.append(cand)
        else:
            off_domain.append(replace(cand, score=cand.score * _OFF_DOMAIN_PENALTY))
    return on_domain + off_domain


def _merge_citation_lens(
    store: Store,
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
