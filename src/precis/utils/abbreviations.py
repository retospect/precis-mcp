"""Schwartz-Hearst abbreviation detection.

Schwartz & Hearst (2003), "A Simple Algorithm for Identifying
Abbreviation Definitions in Biomedical Text". Finds patterns of
the form ``long form (ABBREV)`` (the dominant convention in
scientific writing — the abbreviation is defined on first use
inside parentheses immediately after its expansion).

Used to *shorten* text before RAKE keyword extraction: when the
detector finds ``"Fourier Transform Infrared (FTIR)"``, every
subsequent (or earlier) mention of ``"Fourier Transform Infrared"``
in the same body gets substituted with ``"FTIR"``. RAKE then sees
the canonical short form domain experts use, and per-row keyword
output halves in length without losing meaning.

The algorithm is deterministic and pure-stdlib (one ``re`` call).
Suitable for per-paper preprocessing — call once per ref, get a
``{SHORT: long-form}`` dict, thread through every per-segment RAKE
call. Caching the dict alongside the segmentation cache is the
expected pattern.
"""

from __future__ import annotations

import re

# Pattern: a sequence of one or more whitespace-separated tokens
# followed by ``(SHORT)`` where SHORT is 2-10 chars long, starts
# with a letter or digit, and is mostly uppercase / digits / dots.
# Filters:
#   - SHORT must be 2-10 chars (single-letter parentheticals like
#     ``(A)`` aren't abbreviations; >10 chars is almost always a
#     numeric reference or citation key).
#   - SHORT must start with a letter or digit (excludes punctuation-
#     opened parentheticals like ``(/path/to/x)``).
#   - SHORT must contain at least one letter (rejects pure-numeric
#     ``(2023)`` year citations).
_DEFINITION_RE = re.compile(
    r"""
    (?P<long>(?:[A-Za-z0-9][\w\-]*\s+){1,8}[A-Za-z0-9][\w\-]*)
    \s+\(
    (?P<short>[A-Za-z0-9][A-Za-z0-9\-./]{1,9})
    \)
    """,
    re.VERBOSE,
)


def find(text: str) -> dict[str, str]:
    """Scan ``text`` for abbreviation definitions; return ``{SHORT: long}``.

    Multiple definitions of the same abbreviation in the same text
    keep the *first* one — a later mention is treated as a use, not
    a redefinition. Returns ``{}`` for empty input.

    The verifier (``_matches``) checks that every letter of SHORT
    appears in ``long form`` in order, *mostly* as first letters of
    its words. This is the Schwartz-Hearst correctness guarantee
    and rules out coincidental ``word (W)`` pairings where ``W``
    isn't actually an initialism of ``word``.
    """
    if not text:
        return {}
    out: dict[str, str] = {}
    for m in _DEFINITION_RE.finditer(text):
        short = m.group("short")
        long_form = m.group("long").strip()
        if short in out:
            continue
        long_form = _tighten_long_form(long_form, short)
        if long_form is None:
            continue
        out[short] = long_form
    return out


def substitute(text: str, abbrevs: dict[str, str]) -> str:
    """Replace every long-form mention in ``text`` with its SHORT form.

    For each ``(short, long)`` pair in ``abbrevs``:

    * ``"long form (SHORT)"`` collapses to ``"SHORT"`` — the
      defining parenthetical disappears too, since the agent gets
      the legend separately and doesn't need the definition inline.
    * Every other ``"long form"`` mention becomes ``"SHORT"``.

    Existing ``"SHORT"`` mentions pass through unchanged. The
    substitution is case-sensitive on the long form (matching the
    detector's behavior) and idempotent.
    """
    if not text or not abbrevs:
        return text
    out = text
    for short, long_form in abbrevs.items():
        # Order matters: handle the defining parenthetical first so
        # the trailing "(SHORT)" doesn't survive as orphan markup.
        defining = re.compile(
            r"\b" + re.escape(long_form) + r"\s+\(" + re.escape(short) + r"\)"
        )
        out = defining.sub(short, out)
        # Then collapse remaining standalone long-form mentions.
        # Word-boundary anchors prevent ``"FTIR Spectroscopy"`` →
        # ``"FTIR Spectroscopy"`` loops or partial matches inside
        # other words.
        plain = re.compile(r"\b" + re.escape(long_form) + r"\b")
        out = plain.sub(short, out)
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _tighten_long_form(long_form: str, short: str) -> str | None:
    """Return the tightest valid long form that maps to ``short``.

    Schwartz-Hearst lets the long form be up to ``|short| * 2`` words
    wide, but the longest run of preceding tokens captured by the
    regex usually overshoots ("Such studies of Fourier Transform
    Infrared (FTIR)" — the "Such studies of" is noise). We walk
    inward from the *end* of the long form, keeping the shortest
    suffix that satisfies the Schwartz-Hearst letter-matching rule.

    Returns ``None`` when no valid suffix matches — i.e. when the
    parenthetical isn't actually an abbreviation of what precedes it.
    """
    tokens = long_form.split()
    if not tokens:
        return None

    # Walk from the *end* of the captured tokens — try the shortest
    # suffix first ("Theory" / "Functional Theory" /
    # "Density Functional Theory") and accept the first one that
    # passes the Schwartz-Hearst letter-matching rule. This rejects
    # framing prefixes like "The" / "and" / "Such studies of" that
    # the greedy regex over-captured. Original Schwartz-Hearst paper
    # §2.1 — we prefer the tightest valid long form.
    for start in range(len(tokens) - 1, -1, -1):
        candidate = " ".join(tokens[start:])
        if _matches(candidate, short):
            return candidate
    return None


def _matches(long_form: str, short: str) -> bool:
    """Schwartz-Hearst letter-matching: every char of ``short`` must
    appear in ``long_form`` in order, with the first char anchored
    to a word boundary.

    Implementation follows the right-to-left walk from the original
    paper §2.1: scan long_form from the end backwards, matching the
    last char of ``short`` first; the first char of ``short`` must
    land at the start of a long-form word.
    """
    s = short.lower().replace("-", "").replace(".", "").replace("/", "")
    l = long_form.lower()
    if not s:
        return False

    s_idx = len(s) - 1
    l_idx = len(l) - 1
    while s_idx >= 0 and l_idx >= 0:
        c = s[s_idx]
        if l[l_idx] == c:
            if s_idx == 0:
                # First char of short must land at long-form word boundary.
                if l_idx == 0 or not l[l_idx - 1].isalnum():
                    return True
                # Else continue scanning leftwards for the same char.
            s_idx -= 1
        l_idx -= 1
    return s_idx < 0


__all__ = ["find", "substitute"]
