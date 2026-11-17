"""Deterministic overlapping keyword facets over a ranked, matched-chunk pool.

Facets use exact cleaned phrases, so their labels are stateless filters.
Universal phrases cannot form a facet: each drill must shrink the paper set.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass


def clean_keyword(raw: str) -> str:
    """Strip markup and reject formula fragments rather than advertise junk."""
    text = re.sub(r"\\[a-zA-Z]+", " ", raw.lower())
    text = re.sub(r"[$*{}<>]", "", text)
    text = " ".join(text.split()).strip(" ,;:")
    if "_" in text or not re.search(r"[a-z]{3}", text):
        return ""
    return text


@dataclass(frozen=True)
class KeywordCluster:
    """One filter and its distinct paper members in relevance order."""

    label: str
    papers: tuple[int, ...]
    keywords: tuple[str, ...]


def cluster_pool(
    hits: Iterable[tuple[int, int, Iterable[str]]],
    query_terms: Iterable[str],
    max_clusters: int = 8,
    coverage: float = 0.9,
) -> tuple[list[KeywordCluster], tuple[int, ...]]:
    """Greedily cover papers with discriminating facets, then rank by best hit.

    Returns facets and the papers with no usable keyworded matched chunk.
    Counts overlap; uncovered keyworded papers are not mislabelled as empty.
    """
    ranks: dict[int, int] = {}
    terms: dict[str, set[int]] = defaultdict(set)
    keyworded: set[int] = set()
    query_words = set(re.findall(r"[\w-]+", " ".join(query_terms).lower()))
    for ref_id, rank, keywords in hits:
        ranks[ref_id] = min(rank, ranks.get(ref_id, rank))
        for raw in keywords:
            term = clean_keyword(raw)
            if term:
                keyworded.add(ref_id)
                terms[term].add(ref_id)
    candidates = {
        term: members
        for term, members in terms.items()
        if 2 <= len(members) < len(ranks)
        and not set(re.findall(r"[\w-]+", term)) <= query_words
    }
    term_order = {term: i for i, term in enumerate(terms)}
    covered: set[int] = set()
    clusters: list[KeywordCluster] = []
    while candidates and len(clusters) < max_clusters:
        term = min(
            candidates,
            key=lambda t: (
                -len(candidates[t] - covered),
                len(candidates[t]),
                min(ranks[r] for r in candidates[t]),
                term_order[t],
            ),
        )
        members = candidates.pop(term)
        if not members - covered:
            break
        ordered = tuple(sorted(members, key=lambda r: (ranks[r], r)))
        related = sorted(
            (t for t in candidates if candidates[t] == members),
        )
        clusters.append(KeywordCluster(term, ordered, (term, *related[:3])))
        covered.update(members)
        if len(covered) >= coverage * len(keyworded):
            break
    clusters.sort(key=lambda c: (min(ranks[r] for r in c.papers), c.label))
    none = tuple(sorted(set(ranks) - keyworded, key=lambda r: (ranks[r], r)))
    return clusters, none
