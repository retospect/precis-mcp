"""Derive an approve payload from a hub's own chunk-grounded evidence.

The passages half of what the web approve form pre-fills
(``precis_web.nanopub_render``) and what ``view='mint-preflight'`` gates when
the caller supplies no payload: one implementation, so the web dry-run and
the MCP preflight agree by construction (``docs/backlog/hub-mint-gate-fixes.md``
section 1, gr530995). Per grounding chunk: a quote candidate (the
citation-marker-free sentence most relevant to the claim, markup-folded) and
a snip validated unique within the paper by the same helpers the mint gates
run. A human still trims and attests; ``fields`` stay empty.
"""

from __future__ import annotations

import re
from typing import Any

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_DIGIT_RUN = re.compile(r"\d+")


def _suggest_quote_snip(store: Any, chunk: Any, claim: str) -> tuple[str, str]:
    """A gate-passing starting point from the grounding chunk: the
    citation-marker-free sentence most lexically relevant to the claim as
    the quote candidate (the reviewer trims it to the assertion —
    freeze-at-review still means a human decides exactly what the
    signature covers), and a snip validated unique-within-paper with the
    same helpers the mint gates run. Newlines split too, and ``**`` spans
    are disqualified outright — both keep markdown heading residue
    ("Introduction**\n\nThe debate…") out of the candidate pool.

    Candidates are tried in relevance order until one yields a unique
    snip. Body chunks overlap at their seams (a chunk's first sentence is
    routinely the previous chunk's last), so the most relevant sentence
    can be one that occurs verbatim in two chunks — every 8-token window
    of it then matches twice and no snip exists inside it. Falling through
    to the next sentence keeps the prefill submittable; the old
    best-sentence-only pick handed the reviewer an empty snip that the
    gate refused on submit (first seen on fi191121, 2026-09-16). Only when
    no candidate carries a unique window does the top sentence go out with
    an empty snip.

    Ranking puts the claim's numerals first: a sentence carrying the
    claim's digit runs outranks one with more shared words, so a
    measurement claim gets the sentence that states the number rather
    than a figure caption or table header that shares its vocabulary
    (the 2026-08-17 nanobud batch review). When no sentence qualifies, the
    whole chunk is offered only if it is itself marker-free; otherwise the
    quote is left blank for the reviewer rather than prefilled with
    citation residue the gate would refuse."""
    from precis.nanopub import evidence as ev
    from precis.nanopub import snip as sniplib

    claim_tokens = set(sniplib.tokens(claim))
    claim_numerals = set(_DIGIT_RUN.findall(claim))
    candidates = [
        s.strip()
        for s in _SENTENCE_SPLIT.split(chunk.text or "")
        if len(sniplib.tokens(s)) >= 6 and "**" not in s and not ev.citation_markers(s)
    ]
    haystacks = [c.text for c in ev.paper_body_chunks(store, chunk.ref_id)]
    if not candidates:
        whole = (chunk.text or "").strip()
        if ev.citation_markers(whole):
            return "", ""
        return sniplib.fold_markup(whole), _unique_snip(whole, haystacks)
    ranked = sorted(
        candidates,
        key=lambda s: (
            len(claim_numerals & set(_DIGIT_RUN.findall(s))),
            len(claim_tokens & set(sniplib.tokens(s))),
            len(sniplib.tokens(s)),
        ),
        reverse=True,
    )
    for quote in ranked:
        snip = _unique_snip(quote, haystacks)
        if snip:
            return sniplib.fold_markup(quote), snip
    return sniplib.fold_markup(ranked[0]), ""


def _unique_snip(quote: str, haystacks: list[str]) -> str:
    """The first 8-token window of ``quote`` that occurs exactly once
    across ``haystacks`` (the paper's live body chunks), or ``""``."""
    from precis.nanopub import snip as sniplib

    toks = sniplib.tokens(quote)
    for i in range(max(1, len(toks) - 7)):
        candidate = " ".join(toks[i : i + 8])
        if sniplib.count_matches(candidate, haystacks) == 1:
            return candidate
    return ""


def _anchor_fields(src: Any) -> dict[str, str]:
    """``source_uri`` / ``accession`` for a prefill passage, present only
    when the source has them — a paper's DOI stays the plain ``doi`` key;
    an edgar filing's SEC identity travels with the passage."""
    out: dict[str, str] = {}
    if src.source_uri:
        out["source_uri"] = src.source_uri
    if src.accession:
        out["accession"] = src.accession
    if src.source_url:
        out["source_url"] = src.source_url
    return out


def _prefill_chunk_order(store: Any, bundle: Any, scan: Any = None) -> list[Any]:
    """The hub's grounding chunks in prefill order. A claim that needs a
    body passage — the depth policy says body-required, or it names a
    method/measurement term — gets figure/table captions, then methods and
    results, then other body, with the abstract and front matter last, and
    within a tier the chunk carrying more of the claim's terms first
    (:func:`~precis.nanopub.term_coverage.order_for_prefill`; D2 of
    docs/backlog/threads/claims-and-evidence.md — fi189535 prefilled an
    abstract plus a definition sentence while its TEM caption and STS
    passage were attached). Reorders only: no chunk is dropped. Any other
    claim keeps the edge order."""
    from precis.nanopub import term_coverage
    from precis.workers.hub_refine import DEPTH_BODY_REQUIRED, claim_depth_policy

    chunks = list(bundle.grounding_chunks)
    if claim_depth_policy(bundle.sentence) == DEPTH_BODY_REQUIRED or (
        term_coverage.names_method(bundle.sentence)
    ):
        return term_coverage.order_for_prefill(
            store, chunks, bundle.sentence, scan=scan
        )
    return chunks


def derived_payload(
    store: Any, bundle: Any, scan: Any = None, *, source_fallback: bool = True
) -> dict[str, Any]:
    """``{"passages": [...], "fields": {}}`` from ``bundle``'s grounding
    chunks: quote + unique snip suggested per chunk, for the reviewer to
    trim and attest. With no chunk-grounded evidence and ``source_fallback``,
    up to three blank-quote passages naming the bundle's sources (the web
    form's starting rows); without it the passage list is empty."""
    by_ref = {s.ref_id: s for s in bundle.sources}
    passages: list[dict[str, Any]] = []
    for chunk in _prefill_chunk_order(store, bundle, scan):
        src = by_ref.get(chunk.ref_id)
        if src is None:
            continue
        quote, snip = _suggest_quote_snip(
            store, chunk, f"{bundle.sentence} {bundle.body}"
        )
        passages.append(
            {
                "doi": src.doi or "",
                **_anchor_fields(src),
                "pdf_sha256": src.pdf_sha256 or "",
                "quote": quote,
                "snip": snip,
                "chunk_id": chunk.chunk_id,
                "role": src.role,
            }
        )
    if not passages and source_fallback:
        passages = [
            {
                "doi": s.doi or "",
                **_anchor_fields(s),
                "pdf_sha256": s.pdf_sha256 or "",
                "quote": "",
                "snip": "",
                "role": s.role,
            }
            for s in bundle.sources[:3]
        ]
    return {"passages": passages, "fields": {}}
