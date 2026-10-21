"""Short gripe titles: ``Area: what's wrong``, at most :data:`TITLE_MAX` chars.

A gripe's title was its whole body text, so list views showed 90+ chars of
prose per row. :func:`short_title` derives a short title from the body's
first line (gripe system batch F slice 6); the body keeps the full text.
Deterministic and never raises: ``put`` always stores the gripe, and a line
with no usable clause boundary is cut at a word boundary.

Rules, in order: a first line that already fits is kept (``None``); a
leading ``Area:`` head (2-24 chars, no sentence punctuation) is kept as
the prefix; the rest is cut to its first clause (at a dash, semicolon,
sentence end, parenthesis, comma or "because" — but never inside
``(…)`` or quotes: a comma in ``f(a, b)`` or ``'a, b'`` belongs to the
expression, and cutting there leaves the title mid-call, gr500645); the
result is cut at the last word boundary that fits.
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


def first_line(text: str) -> str:
    """The first non-blank line of ``text``, stripped."""
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _cut(text: str, limit: int) -> str:
    """``text`` cut at the last word boundary within ``limit`` chars."""
    text = text.strip().rstrip(_TRIM)
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    space = head.rfind(" ")
    cut = head[:space] if space >= limit // 2 else text[:limit]
    return cut.rstrip(_TRIM)


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


def short_title(text: str) -> str | None:
    """The derived title for a gripe body, or ``None`` when its first line
    already fits in :data:`TITLE_MAX` chars."""
    line = first_line(text)
    if len(line) <= TITLE_MAX:
        return None
    area, rest = "", line
    if m := _AREA_RE.match(line):
        area, rest = m.group(1).strip(), m.group(2)
    clause = _clause(rest)
    prefix = f"{area}: " if area else ""
    title = prefix + _cut(clause, TITLE_MAX - len(prefix))
    return title if title.strip(_TRIM) else _cut(line, TITLE_MAX)
