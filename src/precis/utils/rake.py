"""RAKE — Rapid Automatic Keyword Extraction.

Rose et al. (2010), "Automatic Keyword Extraction from Individual
Documents". Statistical, language-agnostic in principle, but the
bundled stoplist is English-only — multilingual corpora pass their
own list via ``stopwords=``.

The algorithm
-------------

1. Tokenise on whitespace + punctuation.
2. Split into candidate phrases at stopwords / punctuation.
3. Score each *word* by ``degree(w) / freq(w)`` — degree counts
   co-occurrences (incl. self) inside any candidate phrase.
4. Score each *phrase* as the sum of its word scores.
5. Take the top-N phrases by score, dedup case-insensitively.

Pure stdlib; no NLTK, no scispacy, no model download. Fully
deterministic given the same input — important for tests that diff
golden output and for cache keys.

This is the single source of truth for RAKE in the codebase. The
worker (``precis.workers.summarize.RakeLemmaHandler``) imports
``extract_keywords`` from here; agent-facing search handlers
(``skill``, ``paper``) use the :func:`keyword_summary` wrapper.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Stoplist — SMART-subset focused on academic prose.
#
# Originally lived in ``precis.workers.summarize``; moved here so
# every RAKE caller in the codebase shares one stoplist. Augmenting
# from the full SMART list (~570 words) is a follow-up tracked in
# OPEN-ITEMS.md.
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "about",
        "above",
        "across",
        "after",
        "again",
        "against",
        "all",
        "almost",
        "alone",
        "along",
        "already",
        "also",
        "although",
        "always",
        "am",
        "among",
        "an",
        "and",
        "another",
        "any",
        "anyone",
        "anything",
        "anywhere",
        "are",
        "as",
        "at",
        "be",
        "became",
        "because",
        "been",
        "before",
        "behind",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "came",
        "can",
        "cannot",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "each",
        "either",
        "enough",
        "etc",
        "even",
        "ever",
        "every",
        "everyone",
        "everything",
        "for",
        "from",
        "further",
        "get",
        "got",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "however",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "made",
        "make",
        "many",
        "may",
        "me",
        "might",
        "more",
        "most",
        "much",
        "must",
        "my",
        "myself",
        "near",
        "never",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "often",
        "on",
        "once",
        "one",
        "only",
        "or",
        "other",
        "others",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "rather",
        "same",
        "see",
        "shall",
        "she",
        "should",
        "since",
        "so",
        "some",
        "someone",
        "something",
        "still",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "though",
        "through",
        "thus",
        "to",
        "too",
        "under",
        "until",
        "up",
        "upon",
        "us",
        "use",
        "used",
        "using",
        "very",
        "via",
        "was",
        "we",
        "well",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "yet",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        "z",
    ]
)


# Word: a run of unicode letters / digits / apostrophes / hyphens
# that contains at least one letter or digit. The trailing
# alphanumeric requirement rejects all-punctuation tokens — long
# runs of dashes (``"----------"``) and bare apostrophes that the
# previous looser pattern surfaced as RAKE phrases in the
# scientific-paper corpus (Phase B verification 2026-05-31 showed
# ``"------------------------------------------------"`` ranked as
# a top phrase on a paper with a horizontal-rule separator).
_WORD_RE = re.compile(r"[\w'\-]*[^\W_][\w'\-]*", re.UNICODE)

# Sentence-level boundary characters. Latin punctuation (period,
# question, exclamation, semicolon, comma) + brackets / parens /
# braces / slashes + newlines, plus CJK fullwidth punctuation
# (。 ， ； ！ ？ ・ 「」『』（）) so non-English corpora segment
# correctly too. Parens are critical: "Long Form (ABBR) more text"
# should yield ``["long form", "abbr", "more text"]`` rather than
# a single 4-word phrase that then overruns ``max_phrase_words``
# and gets discarded.
#
# Plus runs of 2+ dashes / underscores / equals (``------``,
# ``______``, ``======``) — horizontal rules and similar separators
# act as visual section breaks; treating them as phrase boundaries
# stops RAKE from joining everything on either side into one
# overlong phrase. Single dashes in hyphenated compounds
# (``metal-organic``) stay intact.
_SENT_BREAK_RE = re.compile(
    r"[.!?;,\n\r()\[\]{}/"
    r"。，；！？"  # CJK . , ; ! ?
    r"・"                          # CJK middle dot
    r"「」『』"        # 「」『』
    r"（）"                    # （）
    r"]+"
    r"|[-_=]{2,}"  # horizontal-rule-style separators
)


# ---------------------------------------------------------------------------
# Pure RAKE
# ---------------------------------------------------------------------------


def extract_keywords(
    text: str,
    *,
    max_keywords: int = 50,
    min_phrase_words: int = 1,
    max_phrase_words: int = 4,
    stopwords: frozenset[str] = _STOPWORDS,
) -> list[str]:
    """Run RAKE on ``text`` and return up to ``max_keywords`` phrases.

    Phrases are returned lowercased, in descending score order. Ties
    are broken by first-occurrence index (stable). Empty input or
    input with no candidate phrases returns ``[]``.

    ``min_phrase_words`` / ``max_phrase_words`` filter on word count
    *before* scoring — a too-long phrase isn't scored, doesn't
    contribute to word degree, and never appears in the output.
    """
    if not text or not text.strip():
        return []
    if min_phrase_words < 1:
        raise ValueError("min_phrase_words must be ≥ 1")
    if max_phrase_words < min_phrase_words:
        raise ValueError("max_phrase_words must be ≥ min_phrase_words")
    if max_keywords < 0:
        raise ValueError("max_keywords must be ≥ 0")

    phrases = _candidate_phrases(
        text,
        stopwords=stopwords,
        min_words=min_phrase_words,
        max_words=max_phrase_words,
    )
    if not phrases:
        return []

    word_scores = _word_scores(phrases)
    scored: dict[str, float] = {}
    order: dict[str, int] = {}
    for idx, phrase in enumerate(phrases):
        joined = " ".join(phrase)
        if joined in scored:
            continue
        scored[joined] = sum(word_scores[w] for w in phrase)
        order[joined] = idx

    # Sort: high score first, ties by earliest occurrence (stable).
    ranked = sorted(
        scored.items(),
        key=lambda kv: (-kv[1], order[kv[0]]),
    )
    return [phrase for phrase, _ in ranked[:max_keywords]]


def keyword_summary(
    text: str,
    *,
    top_k: int = 5,
    separator: str = ", ",
    abbreviations: dict[str, str] | None = None,
    **kwargs: object,
) -> str:
    """Agent-facing convenience: ``extract_keywords`` joined for display.

    Returns the empty string when no keywords could be extracted so
    callers can drop the column safely in TOON output. ``top_k`` maps
    to RAKE's ``max_keywords`` parameter; ``**kwargs`` forwards to
    :func:`extract_keywords` for ``min_phrase_words`` /
    ``max_phrase_words`` / ``stopwords``.

    ``abbreviations`` (optional): a ``{SHORT: long-form}`` dict from
    :mod:`precis.utils.abbreviations`. When supplied, every long-form
    occurrence in ``text`` is replaced with its SHORT form *before*
    RAKE runs. The result: keyword phrases shorten dramatically (no
    more 39-char "Fourier Transform Infrared Spectroscopy" — just
    "FTIR Spectroscopy") and use the canonical form domain experts
    write in. Pass-through is a no-op when the dict is empty / None.
    """
    if abbreviations:
        from precis.utils.abbreviations import substitute

        text = substitute(text, abbreviations)
    keywords = extract_keywords(text, max_keywords=top_k, **kwargs)  # type: ignore[arg-type]
    return separator.join(keywords) if keywords else ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidate_phrases(
    text: str,
    *,
    stopwords: frozenset[str],
    min_words: int,
    max_words: int,
) -> list[list[str]]:
    """Tokenise + split-on-stopwords. Returns list of token-lists."""
    out: list[list[str]] = []
    for sentence in _SENT_BREAK_RE.split(text.lower()):
        if not sentence.strip():
            continue
        current: list[str] = []
        for tok in _WORD_RE.findall(sentence):
            if tok in stopwords or tok.isdigit():
                if min_words <= len(current) <= max_words:
                    out.append(current)
                current = []
            else:
                current.append(tok)
        if min_words <= len(current) <= max_words:
            out.append(current)
    return out


def _word_scores(phrases: list[list[str]]) -> dict[str, float]:
    """Per-word score = degree / frequency.

    ``degree(w)`` counts the number of co-occurrences of w with any
    word inside any candidate phrase, *including itself once per
    phrase appearance*. ``frequency(w)`` is the number of phrases w
    appears in (counted once per phrase).
    """
    freq: dict[str, int] = defaultdict(int)
    deg: dict[str, int] = defaultdict(int)
    for phrase in phrases:
        n = len(phrase)
        # Each word in the phrase gets +1 freq and +n degree (the
        # canonical RAKE weighting; degree here includes self-co-
        # occurrence so single-word phrases score deg=freq=1).
        for w in phrase:
            freq[w] += 1
            deg[w] += n
    return {w: deg[w] / freq[w] for w in freq}


__all__ = ["_STOPWORDS", "extract_keywords", "keyword_summary"]
