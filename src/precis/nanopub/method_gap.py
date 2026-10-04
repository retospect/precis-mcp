"""Method-gap candidates — the DB-free core of ``hub_refine``'s method-gap arm.

A claim hub whose evidence passages never name a method the sentence names
(fi189535: TEM and STS in the claim, neither in any attached passage) is
under-evidenced even when the evidence *papers* carry the missing passage —
a figure caption, a methods paragraph, the SI. :func:`select_candidates`
picks, for each such term, the few chunks of those papers that literally
carry it (the acronym or its in-paper expansion, via
:func:`precis.taproot.coverage.find_term`), ranked the way the approve page
ranks suggestions (:func:`~precis.nanopub.term_coverage.rank_for_claim`:
captions, then methods/results, then other body, abstract last; chunks of
the hub's own evidence papers before SI chunks among equals).
``workers.hub_refine`` judges them with the widen verifier and attaches the
ones that verify; this module only chooses.

**v1 scope**: acronym and mode terms only. A bare number ("5 nm", "0.3 eV")
is carried by so many unrelated chunks that a literal hit says nothing about
support; numbers stay a reviewer-side warning
(:mod:`precis.nanopub.term_coverage`), never a search term here.

Three caps bound the spend: :data:`PER_TERM` candidates per uncovered term
and :data:`PER_HUB` per hub per pass (a chunk that carries several uncovered
terms is one candidate and counts toward each term's quota), and
:data:`ATTEMPTS_PER_TERM` judged attempts per term per claim version (the
caller counts them off its memo and passes the spent terms as
``skip_terms``): at most 1 per term per pass, at most 2 per term per claim
version.

SI refs are not searched in v1: no live evidence edge in prod comes from an
SI ref yet, and counting a parent paper plus its own SI as two independent
sources needs parent-aware source counting first.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from precis.nanopub.term_coverage import PaperChunk, heading_sizes, rank_for_claim
from precis.taproot import coverage
from precis.taproot.coverage import KIND_NUMBER, AcronymMap, Term

#: Candidates judged per uncovered term / per hub per pass.
PER_TERM = 1
PER_HUB = 4
#: Judged attempts per term per ``claim_sha`` (a transient LLM failure counts).
ATTEMPTS_PER_TERM = 2

#: ``meta.widen.via`` / memo ``via`` stamp — makes the arm's yield measurable.
VIA = "method-gap"


@dataclass(frozen=True, slots=True)
class GapCandidate:
    """One chunk to judge, with the uncovered term(s) it was picked for."""

    chunk: PaperChunk
    terms: tuple[str, ...]


def gap_term_texts(sentence: str, passages: list[str]) -> list[str]:
    """Cheap pre-check, no chunks needed: the acronym/mode terms ``sentence``
    names that ``passages`` lack, under an acronym map read off the passages
    alone. A weaker map can only report *more* uncovered terms than
    :func:`gap_terms` with the papers' full text, never fewer, so ``[]`` here
    is final."""
    amap = coverage.acronym_map(passages)
    return [
        t.text
        for t in coverage.uncovered_terms(sentence, passages, amap)
        if t.kind != KIND_NUMBER
    ]


def has_gap(sentence: str, passages: list[str]) -> bool:
    """:func:`gap_term_texts` is non-empty."""
    return bool(gap_term_texts(sentence, passages))


def gap_terms(
    sentence: str,
    passages: list[str],
    chunks: list[PaperChunk],
    abstracts: dict[int, str],
) -> tuple[list[Term], AcronymMap]:
    """The uncovered acronym/mode terms of ``sentence`` against ``passages``
    (numbers dropped, see the module docstring), and the acronym map read off
    the evidence papers that expanded them. ``[]`` with no passage text."""
    amap = coverage.acronym_map(
        passages + list(abstracts.values()) + [c.text for c in chunks]
    )
    missing = coverage.uncovered_terms(sentence, passages, amap)
    return [t for t in missing if t.kind != KIND_NUMBER], amap


def select_candidates(
    sentence: str,
    passages: list[str],
    chunks: list[PaperChunk],
    abstracts: dict[int, str],
    *,
    evidence_refs: Collection[int],
    skip_chunk_ids: Collection[int],
    skip_terms: Collection[str] = (),
    per_term: int = PER_TERM,
    per_hub: int = PER_HUB,
) -> list[GapCandidate]:
    """Up to ``per_hub`` chunks (``per_term`` for each uncovered term) of
    ``chunks`` that carry a term ``sentence`` names and ``passages`` lack.

    ``skip_chunk_ids`` (already linked, or already judged for this hub) are
    never offered; terms in ``skip_terms`` (attempts spent) are not searched. ``evidence_refs`` are the hub's own evidence papers: their
    chunks rank ahead of other papers' (SI) among equal tiers."""
    terms, amap = gap_terms(sentence, passages, chunks, abstracts)
    terms = [t for t in terms if t.text not in set(skip_terms)]
    if not terms:
        return []
    skip = set(skip_chunk_ids)
    live = [c for c in chunks if c.chunk_id not in skip]
    prepared = {c.chunk_id: coverage.prepare(c.text) for c in live}
    sizes = heading_sizes(chunks)

    chosen: dict[int, list[str]] = {}
    order: list[PaperChunk] = []
    for term in terms:
        if len(chosen) >= per_hub:
            break
        hits = [
            c
            for c in live
            if coverage.find_term(term, prepared[c.chunk_id], amap) is not None
        ]
        if not hits:
            continue
        ranked = [
            hits[i]
            for i in rank_for_claim(
                sentence,
                hits,
                abstracts,
                grounding_refs=set(evidence_refs),
                sizes=sizes,
            )
        ]
        quota = per_term
        # A chunk picked for an earlier term that also carries this one
        # already serves it — it spends this term's quota, not the hub's.
        for c in ranked:
            if c.chunk_id in chosen and quota > 0:
                chosen[c.chunk_id].append(term.text)
                quota -= 1
        for c in ranked:
            if quota <= 0 or len(chosen) >= per_hub:
                break
            if c.chunk_id not in chosen:
                chosen[c.chunk_id] = [term.text]
                order.append(c)
                quota -= 1
    return [GapCandidate(chunk=c, terms=tuple(chosen[c.chunk_id])) for c in order]
