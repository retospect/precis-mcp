"""Short gripe titles: ``Area: what's wrong``, at most :data:`TITLE_MAX` chars.

A gripe's title was its whole body text, so list views showed 90+ chars of
prose per row. :func:`short_title` derives a short title from the body's
first line (gripe system batch F slice 6); the body keeps the full text.
It is deterministic and never raises — a line with no usable clause
boundary is cut at a word boundary. ``put`` and the gardening pass
(``cli/gripe.py::shorten_titles``) both use it: a fragment title lands
and triage fixes it (gr524014, below).

Rules, in order: a first line that already fits is kept (``None``); a
leading ``Area:`` head (2-24 chars, no sentence punctuation) is kept as
the prefix; the rest is cut to its first clause (at a dash, semicolon,
sentence end, parenthesis, comma or "because" — but never inside
``(…)`` or quotes: a comma in ``f(a, b)`` or ``'a, b'`` belongs to the
expression, and cutting there leaves the title mid-call, gr500645); the
result is cut at the last word boundary that fits, and a mid-phrase cut
drops trailing function words so the title ends on a content word
(``…returns stale source on`` read as a fragment, gr514360). The cut is
silent: the create ack does not mention it (Reto 2026-10-30, gr524257);
the body keeps the full first line for triage.

gr524014 (supersedes gr520270): a first *clause* longer than the limit
has no boundary the title can break at, so even the function-word-tidied
cut lands mid-phrase ("…silently: no abnormal"). gr520270 had ``put``
refuse that case with ``next=`` naming the limit rather than store a
fragment; Reto overruled it — take the gripe anyway, triage can fix the
title — so the mid-phrase cut is stored like any other derived title.

gr528151 adds an ellipsis to that fallback at filing time, reserving one
character inside ``TITLE_MAX``. ``put`` opts into ``mark_truncation`` on
the same helper the gardening pass uses; there is no separate checked
implementation. Long explicit ``title=`` values follow the same path and
remain intact in the body, without a title-length refusal.
"""

from __future__ import annotations

import re

#: Titles at or under this length are kept as written.
TITLE_MAX = 60

#: Clause boundaries: the text before the first one is the slogan.
_CLAUSE_RE = re.compile(r"\s+[—–]\s+|\s+-\s+|;\s+|\.\s+|\s+\(|,\s+|\s+because\s+")
#: A leading ``Area:`` (a short head with no sentence punctuation).
_AREA_RE = re.compile(r"^([\w./\- ]{2,24}):\s+(.*)$")
_TRIM = ".,;:—–- "


#: Words a truncated title must not end on — connectors and auxiliaries
#: that read as a cut fragment (``…stale source on``, ``…repro for the``,
#: gr514360). The word-boundary cut drops them from the tail until a
#: content word ends the title.
_TRAILING_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "every",
        "each",
        "any",
        "some",
        "no",
        "all",
        "both",
        "either",
        "neither",
        "such",
        "same",
        "own",
        "other",
        "another",
        "much",
        "many",
        "more",
        "most",
        "few",
        "less",
        "several",
        "enough",
        "there",
        "here",
        "and",
        "or",
        "but",
        "nor",
        "so",
        "yet",
        "for",
        "if",
        "than",
        "then",
        "when",
        "while",
        "as",
        "because",
        "since",
        "until",
        "till",
        "though",
        "although",
        "whereas",
        "unless",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "done",
        "have",
        "has",
        "had",
        "having",
        "can",
        "could",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "ought",
        "i",
        "it",
        "its",
        "he",
        "her",
        "his",
        "she",
        "they",
        "their",
        "them",
        "we",
        "our",
        "us",
        "me",
        "him",
        "you",
        "your",
        "my",
        "who",
        "whom",
        "whose",
        "which",
        "what",
        "of",
        "in",
        "on",
        "at",
        "to",
        "with",
        "by",
        "from",
        "into",
        "over",
        "per",
        "via",
        "onto",
        "upon",
        "under",
        "between",
        "among",
        "about",
        "above",
        "across",
        "after",
        "against",
        "along",
        "around",
        "before",
        "behind",
        "below",
        "beneath",
        "beside",
        "besides",
        "beyond",
        "during",
        "except",
        "inside",
        "near",
        "off",
        "out",
        "outside",
        "past",
        "round",
        "through",
        "throughout",
        "toward",
        "towards",
        "underneath",
        "unlike",
        "up",
        "within",
        "without",
        "down",
        "back",
    }
)


def first_line(text: str) -> str:
    """The first non-blank line of ``text``, stripped."""
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _cut(text: str, limit: int) -> str:
    """``text`` cut at the last word boundary within ``limit`` chars.

    A word-boundary cut additionally drops trailing function words —
    ``"…returns stale source on"`` ends mid-phrase where
    ``"…returns stale source"`` reads complete.
    """
    text = text.strip().rstrip(_TRIM)
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    space = head.rfind(" ")
    if space < limit // 2:
        return text[:limit].rstrip(_TRIM)
    words = head[:space].split()
    while len(words) > 1 and words[-1].lower().strip(_TRIM) in (
        _TRAILING_FUNCTION_WORDS
    ):
        words.pop()
    return " ".join(words).rstrip(_TRIM)


def _protected(text: str) -> list[bool]:
    """Per-char mask: True inside ``(…)`` or quotes — spans where a
    clause boundary is part of the expression, not the prose."""
    inside = [False] * len(text)
    depth = 0
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            inside[i] = True
            if ch == quote:
                quote = ""
        elif ch in "'\"" and (i == 0 or not text[i - 1].isalnum()):
            # A quote after a word char is an apostrophe ("don't"), not
            # a delimiter.
            quote = ch
            inside[i] = True
        elif ch == "(":
            depth += 1
            inside[i] = True
        elif ch == ")":
            depth = max(0, depth - 1)
            inside[i] = True
        elif depth:
            inside[i] = True
    return inside


def _clause(text: str) -> str:
    """``text`` up to its first clause boundary outside parens/quotes."""
    inside = _protected(text)
    for m in _CLAUSE_RE.finditer(text):
        if not inside[m.start()]:
            return text[: m.start()] or text
    return text


def _derive(text: str, *, mark_truncation: bool = False) -> str | None:
    """The derived title for a gripe body's first line, or ``None`` when
    the first line already fits. Lenient: a mid-phrase cut (no clause
    boundary inside the limit) is returned like any other derivation."""
    line = first_line(text)
    if len(line) <= TITLE_MAX:
        return None
    area, rest = "", line
    if m := _AREA_RE.match(line):
        area, rest = m.group(1).strip(), m.group(2)
    clause = _clause(rest)
    prefix = f"{area}: " if area else ""
    limit = TITLE_MAX - len(prefix)
    marker = "…" if mark_truncation and len(clause) > limit else ""
    title = prefix + _cut(clause, limit - len(marker))
    if not title.strip(_TRIM):
        title = _cut(line, TITLE_MAX - len(marker))
    return title + marker


def short_title(text: str, *, mark_truncation: bool = False) -> str | None:
    """The derived title for a gripe body, or ``None`` when its first line
    already fits in :data:`TITLE_MAX` chars. Lenient: a mid-phrase
    fragment is still returned — see :func:`_derive`. ``mark_truncation``
    adds an ellipsis to a fragment while keeping it inside the cap; filing
    opts in, gardening retains its existing unmarked titles."""
    return _derive(text, mark_truncation=mark_truncation)
