"""RAKE keyword-extraction summarizer.

The seeded ``summarizers`` row (``0001_initial.sql``) names this
``rake-lemma`` and configures a scispacy lemmatizer:

.. code-block:: json

    {
      "lemmatizer": "scispacy",
      "model": "en_core_sci_sm",
      "max_keywords": 50,
      "min_phrase_words": 1,
      "max_phrase_words": 4
    }

This skeleton honours ``max_keywords`` / ``min_phrase_words`` /
``max_phrase_words`` faithfully but treats ``lemmatizer`` as
informational only — we run RAKE on lowercased surface forms with no
morphological reduction. Wiring ``scispacy`` in is a separate change
(it pulls a ~200 MB scientific spaCy model and ``scispacy`` itself
which is GPL-adjacent — a non-trivial dep decision deferred until
the worker is exercised end-to-end).

The algorithm follows Rose et al. (2010), "Automatic Keyword
Extraction from Individual Documents":

1. Tokenise on whitespace + punctuation.
2. Split into candidate phrases at stopwords / punctuation.
3. Score each *word* by ``degree(w) / freq(w)`` — degree counts
   co-occurrences (incl. self) inside any candidate phrase.
4. Score each *phrase* as the sum of its word scores.
5. Take the top-N phrases by score, dedup case-insensitively.

Pure stdlib; no NLTK, no scispacy, no model download. Fully
deterministic given the same input — important for the integration
tests that diff golden output.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import ClassVar

from psycopg import Connection

from precis.workers.base import ChunkRow, WorkerHandler

# ---------------------------------------------------------------------------
# Stoplist — short English list adequate for a skeleton.
#
# The MIT-licensed SMART stoplist (~570 words) is the usual RAKE
# default; we ship a smaller hand-curated subset focused on
# academic prose. Augmenting from a richer stoplist is a follow-up
# tracked in OPEN-ITEMS.md.
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
        "anybody",
        "anyone",
        "anything",
        "anywhere",
        "are",
        "as",
        "at",
        "b",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "c",
        "can",
        "cannot",
        "could",
        "d",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "e",
        "each",
        "either",
        "else",
        "et",
        "etc",
        "even",
        "ever",
        "every",
        "everybody",
        "everyone",
        "everything",
        "everywhere",
        "f",
        "few",
        "for",
        "from",
        "further",
        "g",
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
        "j",
        "just",
        "k",
        "l",
        "m",
        "may",
        "me",
        "might",
        "more",
        "most",
        "much",
        "must",
        "my",
        "myself",
        "n",
        "near",
        "neither",
        "never",
        "no",
        "nobody",
        "none",
        "nor",
        "not",
        "nothing",
        "now",
        "o",
        "of",
        "off",
        "often",
        "on",
        "once",
        "one",
        "only",
        "onto",
        "or",
        "other",
        "others",
        "ought",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "p",
        "per",
        "perhaps",
        "q",
        "r",
        "rather",
        "s",
        "same",
        "several",
        "shall",
        "she",
        "should",
        "so",
        "some",
        "somebody",
        "someone",
        "something",
        "somewhere",
        "still",
        "such",
        "t",
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
        "together",
        "too",
        "toward",
        "towards",
        "u",
        "under",
        "unless",
        "until",
        "up",
        "upon",
        "us",
        "use",
        "used",
        "uses",
        "using",
        "v",
        "very",
        "via",
        "w",
        "was",
        "we",
        "were",
        "what",
        "whatever",
        "when",
        "where",
        "whereas",
        "whether",
        "which",
        "while",
        "who",
        "whoever",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "x",
        "y",
        "yet",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        "z",
    ]
)


# Word: a run of unicode letters / digits / apostrophes / hyphens.
# Anything else is treated as a phrase boundary.
_WORD_RE = re.compile(r"[\w'\-]+", re.UNICODE)

# Sentence-level boundary characters. Hard breaks (period, question
# mark, exclamation, semicolon) plus newlines split the text into
# sentences before phrase extraction so a phrase never spans a
# sentence boundary.
_SENT_BREAK_RE = re.compile(r"[.!?;\n\r]+")


# ---------------------------------------------------------------------------
# Pure RAKE — exposed at module level for direct unit testing.
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

    Phrases are returned lowercased, in descending score order.
    Ties are broken by first-occurrence index (stable). Empty input
    or input with no candidate phrases returns ``[]``.

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


# ---------------------------------------------------------------------------
# Handler — wraps extract_keywords for the worker runner
# ---------------------------------------------------------------------------


class RakeLemmaHandler(WorkerHandler):
    """``summarize:rake-lemma`` worker handler.

    Persists the joined-by-``"; "`` keyword list as the
    ``chunk_summaries.text`` value.
    """

    output_table: ClassVar[str] = "chunk_summaries"
    model_column: ClassVar[str] = "summarizer"

    def __init__(
        self,
        *,
        max_keywords: int = 50,
        min_phrase_words: int = 1,
        max_phrase_words: int = 4,
        model_name: str = "rake-lemma",
    ) -> None:
        self._max_keywords = max_keywords
        self._min_phrase_words = min_phrase_words
        self._max_phrase_words = max_phrase_words
        self.model_name = model_name
        self.name = f"summarize:{model_name}"

    # ------------------------------------------------------------------
    # process — pure RAKE on chunk text
    # ------------------------------------------------------------------

    def process(self, row: ChunkRow) -> str:
        """Return the summary string (joined keywords).

        Empty chunks return the empty string and the runner persists
        a row with ``text=''``; this is intentional — an empty
        summary is a valid (if uninteresting) fact, distinct from a
        failure marker.
        """
        keywords = extract_keywords(
            row.text,
            max_keywords=self._max_keywords,
            min_phrase_words=self._min_phrase_words,
            max_phrase_words=self._max_phrase_words,
        )
        return "; ".join(keywords)

    # ------------------------------------------------------------------
    # write_ok — INSERT into chunk_summaries
    # ------------------------------------------------------------------

    def write_ok(self, conn: Connection, chunk_id: int, payload: object) -> None:
        if not isinstance(payload, str):  # pragma: no cover — defensive
            raise TypeError(
                f"RakeLemmaHandler.write_ok expected str, got {type(payload).__name__}"
            )
        conn.execute(
            """
            INSERT INTO chunk_summaries
                (chunk_id, summarizer, text, status)
            VALUES (%s, %s, %s, 'ok')
            ON CONFLICT (chunk_id, summarizer) DO UPDATE
               SET text = EXCLUDED.text,
                   status = 'ok',
                   last_error = NULL,
                   attempts = chunk_summaries.attempts + 1
            """,
            (chunk_id, self.model_name, payload),
        )


__all__ = ["RakeLemmaHandler", "extract_keywords"]
