"""Title-to-body agreement score for papers (gr264184, backlog item 5).

Nothing else compares a paper's ``title`` with its body, so a swapped title
(pa693: a WSe2 title on a MoS2 body) only surfaces when someone reads a
chunk. :func:`title_body_score` is the lexical first cut: the fraction of the
title's distinctive terms that occur in the leading body text, with
formula/material tokens (``WSe2``, ``MoS2``, ``Li4Ti5O12``) weighted heavily
because they are the hardest to match by accident.

Pure function, no IO. The read-only prod sweep is ``scripts/title-body-sweep``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Weight of a formula/material token relative to an ordinary word (1.0).
FORMULA_WEIGHT = 4.0
#: A title formula absent from the body caps the review-ranking score here.
FORMULA_MISS_CAP = 0.35
#: Weight of an acronym-like token (all caps, 2+ letters: ``HRTEM``).
ACRONYM_WEIGHT = 2.0

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# Capitalised, digit inside, 2+ letters: WSe2, MoS2, Li4Ti5O12, H2O2.
_FORMULA_RE = re.compile(r"^(?=(?:.*[A-Za-z]){2})[A-Z][A-Za-z]*\d[A-Za-z0-9]*$")
_ACRONYM_RE = re.compile(r"^[A-Z][A-Z0-9]{1,}$")

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "between",
        "by",
        "can",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "over",
        "that",
        "the",
        "their",
        "these",
        "this",
        "those",
        "to",
        "toward",
        "towards",
        "under",
        "up",
        "upon",
        "use",
        "used",
        "using",
        "via",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "with",
        "within",
        "without",
        "not",
        "no",
        "new",
        "novel",
        "via",
        "versus",
        "vs",
        "based",
        "study",
        "studies",
        "analysis",
        "approach",
        "method",
        "methods",
        "case",
        "review",
        "role",
        "effect",
        "effects",
        "evidence",
        "investigation",
        "development",
        "application",
        "applications",
        "properties",
        "property",
        "performance",
        "high",
        "low",
        "large",
        "small",
        "first",
        "second",
        "one",
        "two",
        "three",
        "using",
        "towards",
        "understanding",
        "insights",
        "enhanced",
        "improved",
        "efficient",
    ]
)


@dataclass(frozen=True)
class TitleBodyScore:
    """Weighted agreement of title terms with body text, 0.0-1.0."""

    score: float
    n_terms: int
    matched: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def missing_formulas(self) -> tuple[str, ...]:
        """Missing terms that are formula/material tokens (strongest signal)."""
        return tuple(t for t in self.missing if _FORMULA_RE.match(t))

    @property
    def rank_score(self) -> float:
        """Sort key: a missing formula caps the score (pa693: WSe2 vs MoS2)."""
        if self.missing_formulas:
            return min(self.score, FORMULA_MISS_CAP)
        return self.score


_PLACEHOLDER_RE = re.compile(
    r"^(microsoft (word|powerpoint|excel)\b"
    r"|template for electronic submission"
    r"|submitted manuscript"
    r"|arxiv:\d|oup accepted manuscript"
    r"|(un)?titled\b|title\.?$|session title|preprint\b"
    r"|\[?\(?no title\)?\]?$)"
    r"|\.(docx?|indd|pdf|tex|cdr|qxp|pmd|p65|mps|ind|dvi|ps|rtf|pub|pptx?)$",
    re.IGNORECASE,
)


def is_placeholder_title(title: str) -> bool:
    """True for extraction-artifact titles: ``Microsoft Word - x.doc``,
    ``foo.indd``, ``Untitled``, ``(no title)``, or at most two words.

    These are a retitling problem, not a title/body swap, so the sweep lists
    them separately and keeps them out of the mismatch ranking.
    """
    t = unicodedata.normalize("NFKC", title or "").strip()
    return len(t.split()) <= 2 or _PLACEHOLDER_RE.search(t) is not None


def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("ations", "ation", "ings", "ing", "ies", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def title_terms(title: str) -> dict[str, float]:
    """Distinctive title terms -> weight (formulas and acronyms weigh more)."""
    title = unicodedata.normalize("NFKC", title)
    out: dict[str, float] = {}
    for tok in _WORD_RE.findall(title):
        if _FORMULA_RE.match(tok):
            out[tok] = FORMULA_WEIGHT
        elif _ACRONYM_RE.match(tok):
            out[tok] = ACRONYM_WEIGHT
        else:
            low = tok.lower()
            if len(low) < 4 or low in _STOP:
                continue
            out.setdefault(low, 1.0)
    return out


def _formula_pattern(f: str) -> re.Pattern[str]:
    # Body extraction often splits subscripts: "WSe 2", "WSe\n2".
    parts = re.split(r"(\d+)", f)
    pat = r"\s?".join(re.escape(p) for p in parts if p)
    return re.compile(r"(?<![A-Za-z0-9])" + pat + r"(?![a-z])")


def title_body_score(title: str, body: str) -> TitleBodyScore | None:
    """Score how well *title* is supported by *body* (leading body text).

    Returns ``None`` when there is nothing to judge (no distinctive title
    terms, or an empty body).
    """
    terms = title_terms(title)
    body = unicodedata.normalize("NFKC", body or "")
    if not terms or not body.strip():
        return None
    body_stems = {_stem(w) for w in _WORD_RE.findall(body)}
    body_words = {w.lower() for w in _WORD_RE.findall(body)}
    matched: list[str] = []
    missing: list[str] = []
    for term, weight in terms.items():
        if weight == FORMULA_WEIGHT:
            hit = _formula_pattern(term).search(body) is not None
        elif weight == ACRONYM_WEIGHT:
            hit = term.lower() in body_words or term in body
        else:
            hit = term in body_words or _stem(term) in body_stems
        (matched if hit else missing).append(term)
    total = sum(terms.values())
    got = sum(terms[t] for t in matched)
    return TitleBodyScore(got / total, len(terms), tuple(matched), tuple(missing))
