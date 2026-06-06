"""Semantic keyword extraction using the live embedder.

KeyBERT-style (Grootendorst) — this is a homegrown reimplementation of
the *technique* (embed candidate phrases, rank by cosine to a target
embedding, diversify with MMR), **not** the ``keybert`` PyPI package,
which is not a dependency. Named ``semantic_keywords`` to avoid
impersonating that package.

Extracts candidate noun phrases the same way RAKE does (stopword-split
within sentences, 1-4 word phrases), then scores each candidate by
cosine similarity to a *target embedding* — usually the segment's
mean chunk embedding. Top-K by score wins.

Why this beats RAKE for our TOC use case:

- **Semantic centrality, not frequency.** A phrase mentioned once
  but central to the document's topic outranks a phrase repeated
  three times in a passing reference. RAKE gets that wrong.
- **Noise rejection.** Boilerplate phrases (OCR artifacts, journal
  marginalia, citation chrome) embed far from the body's topic
  centroid and score low. RAKE happily surfaces them because
  they're locally frequent.
- **Discrimination via exclude=.** Pass the paper-wide top-K as
  ``exclude=`` so per-segment results are paper-wide-MINUS-segment
  phrases — segment rows show what's unique to each segment.

Uses bge-m3 (or any compatible embedder duck-typed via
``embed(texts) -> list[list[float]]``). When no embedder is
available, callers should fall back to RAKE
(:func:`precis.utils.rake.keyword_summary`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Protocol

from precis.utils.rake import _STOPWORDS, _candidate_phrases

# 2-8 char UPPER-CASE blocks, optionally with a digit / hyphen /
# slash inside. Catches FTIR, MOFs, UiO-66, XPS, ToF-SIMS, NMR.
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}(?:[-/][A-Z0-9]{1,8})?\b")

# Title-case 2-4 word phrases. Matches "Metal Organic Framework",
# "Z-scheme Photocatalysis", "Density Functional Theory". Excludes
# common sentence-start words by requiring at least 2 capitalised
# tokens in sequence.
_TITLE_CASE_RE = re.compile(
    r"(?:\b[A-Z][a-z]+(?:[-/][A-Z][a-z]+)?(?:\s+[A-Z][a-z]+(?:[-/][A-Z][a-z]+)?){1,3})"
)


class _EmbedderProto(Protocol):
    """Duck-type for the embedder dependency.

    Matches both ``precis.embedder.BgeM3Embedder`` and ``MockEmbedder``.
    """

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# ── public API ──────────────────────────────────────────────────────


def extract_keywords_semantic(
    text: str,
    *,
    target_embedding: Sequence[float],
    embedder: _EmbedderProto,
    top_k: int = 5,
    min_phrase_words: int = 1,
    max_phrase_words: int = 4,
    exclude: Iterable[str] = (),
    diversity_lambda: float = 0.0,
    stopwords: frozenset[str] = _STOPWORDS,
    candidates: Sequence[str] | None = None,
) -> list[str]:
    """Return up to ``top_k`` key phrases ranked by cosine to ``target_embedding``.

    Args:
        text: source text. Used for in-place candidate extraction
            when ``candidates`` is None, and always for case-recovery
            on the rendered output. Empty / whitespace input → ``[]``.
        target_embedding: vector to score candidates against. Usually
            the mean of the segment's chunk embeddings (so phrases
            close to the segment's topical centroid rank high). For
            paper-wide keywords pass the mean of the whole paper's
            chunk embeddings.
        embedder: object with ``.embed(texts) -> list[list[float]]``.
        top_k: maximum number of phrases returned.
        min_phrase_words / max_phrase_words: candidate phrase length
            bounds (same as RAKE's interface). Ignored when
            ``candidates`` is supplied.
        exclude: phrases to drop from candidates *before* scoring.
            Case-insensitive match. Used by the TOC renderer to pass
            paper-wide phrases so per-segment results are unique to
            the segment.
        diversity_lambda: 0.0 = pure top-K by score. >0 enables MMR
            (Maximal Marginal Relevance): each next phrase is
            chosen to maximize ``score(candidate) -
            λ·max(cos(candidate, already_picked))``. Reduces
            near-duplicates in the output ("metal organic
            framework" + "metal-organic frameworks").
        stopwords: passed through to candidate-phrase tokenisation.
        candidates: optional pre-filtered candidate list. When
            supplied, skip RAKE's candidate extraction entirely —
            the caller has already curated a candidate set (e.g.
            via :func:`precis.utils.rake.extract_keywords` capped at
            N + a privileged-pattern union). This is the
            *performance* hook: embedding 150 caller-supplied
            candidates is 10× faster than embedding the 1500 RAKE
            would produce on a large segment. Candidates are
            case-folded for dedup; original casing recovered from
            ``text`` at render time.

    Returns:
        Top phrases, descending score, deduped (case-insensitive),
        with the original-case form preserved from first occurrence.

    The function does one batched ``embed`` call (size = number of
    candidate phrases). Cost on bge-m3 CPU: ~10-50ms / segment with
    typical caps. Caller is expected to cache.
    """
    if not text or not text.strip():
        return []
    if top_k <= 0:
        return []

    exclude_lc = {e.lower() for e in exclude}

    # ``seen_lc`` is declared once and reused across both branches
    # below — RAKE-fallback and caller-supplied — so mypy doesn't see
    # a redefinition. Same intent, smaller diff than threading two
    # separately-named locals.
    seen_lc: set[str] = set()

    # Build the candidate set. Either use the caller-supplied list
    # (performance fast-path) or extract via RAKE's tokeniser.
    if candidates is not None:
        candidates_lc: list[str] = []
        for c in candidates:
            c_lc = c.strip().lower()
            if not c_lc or c_lc in seen_lc or c_lc in exclude_lc:
                continue
            seen_lc.add(c_lc)
            candidates_lc.append(c_lc)
        candidates = candidates_lc
    else:
        # Reuse RAKE's candidate-phrase machinery so the candidate
        # set is identical between the RAKE fallback and the
        # KeyBERT path.
        phrase_tokens = _candidate_phrases(
            text,
            stopwords=stopwords,
            min_words=min_phrase_words,
            max_words=max_phrase_words,
        )
        if not phrase_tokens:
            return []

        cand_list: list[str] = []
        for tokens in phrase_tokens:
            joined_lc = " ".join(tokens)
            if joined_lc in seen_lc or joined_lc in exclude_lc:
                continue
            seen_lc.add(joined_lc)
            cand_list.append(joined_lc)
        candidates = cand_list

    if not candidates:
        return []

    # Embed candidates in a single batch. Recover original-case
    # display strings by walking the source text for the first
    # occurrence of each lowercased candidate.
    embeddings = embedder.embed(candidates)
    if len(embeddings) != len(candidates):  # pragma: no cover — defensive
        raise RuntimeError(
            f"embedder returned {len(embeddings)} vectors "
            f"for {len(candidates)} candidates"
        )

    # Score by cosine to target. bge-m3 vectors are L2-normalised so
    # cosine == dot; we don't assume that for the target and
    # normalise both sides.
    target = _normalise(list(target_embedding))
    scored: list[tuple[str, float, list[float]]] = []
    for phrase, vec in zip(candidates, embeddings, strict=True):
        vec_n = _normalise(vec)
        score = sum(a * b for a, b in zip(target, vec_n, strict=True))
        scored.append((phrase, score, vec_n))

    scored.sort(key=lambda t: t[1], reverse=True)

    # Recover display casing for each picked phrase.
    display_for = _build_case_map(text, {c for c, _, _ in scored})

    if diversity_lambda <= 0:
        # Plain top-K.
        out: list[str] = []
        for phrase, _score, _vec in scored:
            out.append(display_for.get(phrase, phrase))
            if len(out) == top_k:
                break
        return out

    # MMR: greedy picks balancing score vs. distance from already-picked.
    picked: list[tuple[str, list[float]]] = []
    remaining = scored[:]
    while remaining and len(picked) < top_k:
        best_idx = 0
        best_mmr = float("-inf")
        for i, (phrase, score, vec) in enumerate(remaining):
            if not picked:
                penalty = 0.0
            else:
                penalty = max(
                    sum(a * b for a, b in zip(vec, p_vec, strict=True))
                    for _, p_vec in picked
                )
            mmr = score - diversity_lambda * penalty
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
        phrase, _, vec = remaining.pop(best_idx)
        picked.append((phrase, vec))
    return [display_for.get(p, p) for p, _ in picked]


# ── helpers ──────────────────────────────────────────────────────────


def _normalise(vec: list[float]) -> list[float]:
    """L2-normalise a vector. Zero-vector returns unchanged (cosine
    will then be 0, which is the right behaviour: a zero target is
    uninformative)."""
    norm_sq = sum(x * x for x in vec)
    if norm_sq == 0.0:
        return vec
    inv = norm_sq**-0.5
    return [x * inv for x in vec]


def _build_case_map(text: str, phrases_lc: set[str]) -> dict[str, str]:
    """Recover display casing for each lowercased phrase.

    Walks ``text`` looking for case-insensitive matches; first-seen
    casing wins. Phrases not found in the source (rare — should
    only happen if the candidate-phrase tokeniser normalised
    something we can't reverse) fall back to the lowercased form.
    """
    out: dict[str, str] = {}
    text_lc = text.lower()
    for phrase_lc in phrases_lc:
        idx = text_lc.find(phrase_lc)
        if idx == -1:
            continue
        out[phrase_lc] = text[idx : idx + len(phrase_lc)]
    return out


def privileged_candidates(
    text: str,
    *,
    abbreviations: Iterable[str] = (),
) -> list[str]:
    """Phrases that should always be candidates regardless of
    statistical (RAKE) rank.

    Used by the TOC pipeline together with a capped RAKE-top-N: the
    privileged set is unioned with the cap so rare-but-semantically-
    central phrases ("Z-scheme", "MOF-5", named author terms) still
    reach the KeyBERT scorer even when RAKE didn't rank them in the
    top N.

    Heuristics:

    * **UPPER-CASE acronyms** (2-8 chars, all caps + optional digit/
      hyphen) — almost always technical terms (FTIR, MOFs, XPS,
      UiO-66). Extracted as standalone tokens; words containing them
      embed as phrases naturally.
    * **Title-case multi-word phrases** ("Z-scheme Photocatalysis",
      "Membrane Electrode Assembly") — typically author-defined
      terms or proper nouns.
    * **Known abbreviations** from the supplied ``abbreviations``
      iterable (e.g. the keys of the paper-wide abbreviation
      legend) — these are domain vocabulary the paper itself
      flagged as important.

    All returned candidates are lowercased for downstream dedup;
    KeyBERT's case-recovery walks the source text to restore display
    casing.
    """
    if not text:
        return list({a.lower() for a in abbreviations})

    out: set[str] = set()

    # Abbreviations from the per-paper legend — these are the
    # canonical short forms the paper *uses*; always score them.
    for abbrev in abbreviations:
        a = abbrev.strip().lower()
        if a:
            out.add(a)

    # UPPER-CASE acronyms in the text.
    for m in _ACRONYM_RE.finditer(text):
        out.add(m.group(0).lower())

    # Title-case 2-4 word phrases.
    for m in _TITLE_CASE_RE.finditer(text):
        phrase = m.group(0).strip().lower()
        # Skip if it's just stop-style words.
        if phrase and len(phrase.split()) >= 2:
            out.add(phrase)

    return sorted(out)


def mean_embedding(embeddings: Sequence[Sequence[float]]) -> list[float]:
    """Component-wise mean of a list of vectors. Returns ``[]`` for
    empty input. Used to build the segment / paper centroids that
    KeyBERT scores against."""
    if not embeddings:
        return []
    dim = len(embeddings[0])
    sums = [0.0] * dim
    for vec in embeddings:
        for i, x in enumerate(vec):
            sums[i] += x
    n = len(embeddings)
    return [s / n for s in sums]


__all__ = ["extract_keywords_semantic", "mean_embedding", "privileged_candidates"]
