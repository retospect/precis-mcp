"""Text normalisation for citation metadata verification (Phase 2.5).

See ``docs/design/provenance-kind-plan.md`` § "Phase 2.5: citation metadata
verification" for the design rationale. Two normalised forms are
produced for each string; comparison succeeds if either form matches.

Pipeline:

1. ``unicodedata.normalize('NFKD', s)`` — decomposes diacritics,
   sub/superscripts, ligatures, full-width forms, compatibility forms.
2. Strip combining characters.
3. Lowercase.
4. Replace every non-alphanumeric char with a space.
5. Collapse whitespace.
6. Tokenise (split on whitespace).
7. Drop English stopwords.

The German-phonetic alternative form applies ``ä→ae, ö→oe, ü→ue,
ß→ss`` before step 1, so ``Müller`` produces tokens ``{muller}``
under NFKD-strip and ``{mueller}`` under the phonetic alt — a
surname matches if either set lines up against the comparison
target.

The token-set Jaccard metric is invariant to word order, articles,
subtitle separators, and markup leakage, while still penalising
substantive word differences (the actual signal we want when
catching wrong-paper citations).

Greek letters used semantically (``β-cell`` vs ``beta-cell``) and
math symbols (``≤``, ``±``) are deliberately *not* mapped — NFKD
doesn't touch them and a hand-built table has too many edge cases.
The diff stays visible in the report; the rendering model judges.
"""

from __future__ import annotations

import re
import unicodedata

# English stopwords dropped from both title comparison and (less
# usefully) surname comparison. Kept small and conservative —
# anything that meaningfully contributes to a title shouldn't be
# in here.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "for",
        "and",
        "to",
        "with",
        "from",
        "by",
        "at",
        "as",
    }
)


# German-phonetic transliteration table. Applied as an *alternative*
# normalised form, not a replacement — the regular NFKD path still
# runs so ``Müller`` reduces to ``muller`` there. Both forms are
# matched against the comparison target.
_GERMAN_PHONETIC: dict[str, str] = {
    "ä": "ae",
    "Ä": "Ae",
    "ö": "oe",
    "Ö": "Oe",
    "ü": "ue",
    "Ü": "Ue",
    "ß": "ss",
}


# Aggressive reverse-phonetic fold for surname comparison only. Maps
# the ASCII phonetic forms back to their unumlauted base — so a bib
# with ``Mueller`` and a Crossref record with ``Muller`` (both pure
# ASCII; no umlaut on either side) still match.
#
# Trade-off: introduces false positives on common English/Latin words
# that happen to contain ``ue``/``oe``/``ae``/``ss`` — ``Sue→Su``,
# ``Press→Pres``, ``Roe→Ro``, ``Caesar→Casar``. We accept these for
# surname comparison because the cost is bounded: a false match only
# suppresses a "metadata mismatch" warning, never causes incorrect
# data to be written. The false-negative cost it cures (real
# ``Muller↔Mueller`` pairs failing to match in pure-ASCII bibs) is
# noticeable in real-world preflight runs.
#
# NOT applied to title comparison — titles have enough tokens that
# Jaccard already handles surname-style variants implicitly, and the
# false-positive cost compounds quickly across many tokens.
#
# Order matters: ``ss`` must come last (no overlap with vowel pairs,
# but keeping the order explicit lets future maintainers reason about
# it without re-deriving the algorithm).
_REVERSE_PHONETIC_PAIRS: tuple[tuple[str, str], ...] = (
    ("ue", "u"),
    ("oe", "o"),
    ("ae", "a"),
    ("ss", "s"),
)


def _nfkd_strip(s: str) -> str:
    """NFKD decompose, strip combining chars, return the base string.

    Folds: Müller→Muller, naïve→naive, H₂O→H2O, ﬁ→fi, ²→2, full-width
    Latin → ASCII. Greek letters used semantically (β, μ, Σ) are
    untouched — NFKD has no compatibility decomposition for them.
    """
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _apply_german_phonetic(s: str) -> str:
    """Apply the German-phonetic transliteration table."""
    out = s
    for src, dst in _GERMAN_PHONETIC.items():
        out = out.replace(src, dst)
    return out


def _apply_reverse_phonetic(s: str) -> str:
    """Aggressive surname fold: ``Mueller→Muller``, ``Schroeder→Schroder``, etc.

    Applied after lowercasing so the table needs only lowercase keys.
    Sequential ``.replace`` is intentional and handles cascades —
    ``oeue`` → ``oue`` → ``ou`` — without needing a regex pass.

    See ``_REVERSE_PHONETIC_PAIRS`` for the trade-off (false positives
    on ``Sue``, ``Press``, ``Roe``, ``Caesar``; surname-only).
    """
    out = s
    for src, dst in _REVERSE_PHONETIC_PAIRS:
        out = out.replace(src, dst)
    return out


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _tokenise(s: str) -> frozenset[str]:
    """Lowercase, replace non-alnum with space, split, drop stopwords."""
    s = s.lower()
    s = _NON_ALNUM_RE.sub(" ", s)
    tokens = {t for t in s.split() if t and t not in _STOPWORDS}
    return frozenset(tokens)


def normalised_token_sets(s: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(nfkd_tokens, german_phonetic_tokens)`` for ``s``.

    The two forms are matched independently against the comparison
    target. For surnames this covers Müller↔Muller (NFKD path) and
    Müller↔Mueller (phonetic path). For titles the phonetic alt
    rarely changes anything but the cost is trivial.
    """
    nfkd_tokens = _tokenise(_nfkd_strip(s))
    phonetic_tokens = _tokenise(_nfkd_strip(_apply_german_phonetic(s)))
    return nfkd_tokens, phonetic_tokens


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Token-set Jaccard similarity ∈ [0, 1].

    ``jaccard(A, B) = |A ∩ B| / |A ∪ B|`` with the convention
    ``jaccard(∅, ∅) = 1.0`` (two empty inputs are vacuously equal)
    and ``jaccard(A, ∅) = 0.0`` for non-empty against empty.
    """
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def best_jaccard(s1: str, s2: str) -> tuple[float, frozenset[str], frozenset[str]]:
    """Compare ``s1`` and ``s2`` under both normalised forms; return the
    best Jaccard score plus the token sets of ``s1`` (for diff display).

    Returns ``(score, s1_nfkd_tokens, s2_nfkd_tokens)`` — the NFKD
    tokens are returned so the renderer can show added/removed tokens
    in human-readable form (the phonetic-alt path would be confusing
    for a Greek-letter title diff).
    """
    a_nfkd, a_phon = normalised_token_sets(s1)
    b_nfkd, b_phon = normalised_token_sets(s2)
    # Try both normalised forms; keep the best alignment.
    score = max(
        jaccard(a_nfkd, b_nfkd),
        jaccard(a_phon, b_phon),
        jaccard(a_nfkd, b_phon),
        jaccard(a_phon, b_nfkd),
    )
    return score, a_nfkd, b_nfkd


def _reverse_phonetic_tokens(s: str) -> frozenset[str]:
    """Tokenise after applying both NFKD strip AND reverse-phonetic fold."""
    return _tokenise(_apply_reverse_phonetic(_nfkd_strip(s).lower()))


def surname_matches(supplied: str, crossref: str) -> bool:
    """True iff one supplied surname matches under any normalisation.

    Three forms are produced for each side:

    1. **NFKD-strip** — Müller → muller, Schröder → schroder
    2. **German-phonetic** — Müller → mueller, ß → ss
    3. **Reverse-phonetic** (surname-only) — aggressive fold of
       ``ue/oe/ae/ss`` back to ``u/o/a/s``, so ``Mueller`` and
       ``Muller`` collapse to the same form even when both inputs
       are pure ASCII

    A match is declared when *any* form on the supplied side equals
    *any* form on the Crossref side (9 combinations total). The
    third form trades a small false-positive risk (``Sue→Su``,
    ``Press→Pres``) for substantially better recall on pure-ASCII
    German surnames; see ``_REVERSE_PHONETIC_PAIRS`` for the
    rationale.
    """
    s_nfkd, s_phon = normalised_token_sets(supplied)
    s_rev = _reverse_phonetic_tokens(supplied)
    c_nfkd, c_phon = normalised_token_sets(crossref)
    c_rev = _reverse_phonetic_tokens(crossref)
    # Empty supplied → can't claim a match
    if not (s_nfkd or s_phon or s_rev):
        return False
    supplied_forms = (s_nfkd, s_phon, s_rev)
    crossref_forms = (c_nfkd, c_phon, c_rev)
    return any(a & b for a in supplied_forms for b in crossref_forms)


__all__ = [
    "best_jaccard",
    "jaccard",
    "normalised_token_sets",
    "surname_matches",
]
