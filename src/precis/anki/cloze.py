"""Brace-aware cloze scanner — pure, anki-free.

Anki (and a plain regex) end a cloze at the first literal ``}}``, so a LaTeX
answer whose own closing braces touch (``{{c1::\\sqrt{x^{2}}::latex}}``) is cut
short. This scanner knows brace depth: a ``}`` that closes a ``{`` opened inside
the answer is a single-brace closer, never half of the cloze's own ``}}``.

Scan rule, from ``{{c<digits>::``: a nested ``{{c<digits>::`` opens a nested
cloze (its own ``}}`` belongs to it); a single ``{`` raises depth; a ``}`` at
depth > 0 lowers it; a ``}}`` at depth 0 closes the cloze. The optional
``::hint`` is everything after the first ``::``, as in Anki. A cloze whose
single braces do not balance (an answer that is a lone ``{``) falls back to
Anki's own rule, the first ``}}`` closes; one with no ``}}`` at all leaves the
text from there untouched.

:func:`space_adjacent_braces` is the write-door fix: a single-brace closer
immediately followed by another ``}`` gets one space, so Anki's own first-``}}``
scan then lands on the real end (``\\sqrt{x^{2} }``).
"""

from __future__ import annotations

import re

_OPEN_RE = re.compile(r"\{\{c\d+::")


def _walk(text: str, start: int) -> tuple[str, str, int, int] | None:
    """Scan the cloze opening at ``text[start]``.

    Returns ``(normalised, stripped, end, spaced)``: the cloze with spacing
    applied (markup kept), its answer-only form, the index just past its
    closing ``}}``, and how many spaces were inserted. ``None`` if unterminated.
    """
    m = _OPEN_RE.match(text, start)
    assert m is not None
    i = m.end()
    n = len(text)
    norm: list[str] = []
    strip: list[str] = []
    split_at: int | None = None  # len(strip) at the first '::'
    depth = 0
    spaced = 0
    while i < n:
        if _OPEN_RE.match(text, i):
            inner = _walk(text, i)
            if inner is None:
                return None
            norm.append(inner[0])
            strip.append(inner[1])
            spaced += inner[3]
            i = inner[2]
            continue
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
                norm.append("}")
                strip.append("}")
                i += 1
                if i < n and text[i] == "}":
                    norm.append(" ")
                    strip.append(" ")
                    spaced += 1
                continue
            if text.startswith("}}", i):
                answer = "".join(strip)
                if split_at is not None:
                    answer = answer[:split_at]
                head = m.group(0)
                return head + "".join(norm) + "}}", answer, i + 2, spaced
        elif c == ":" and text.startswith("::", i):
            if split_at is None:
                split_at = sum(len(s) for s in strip)
            norm.append("::")
            strip.append("::")
            i += 2
            continue
        norm.append(c)
        strip.append(c)
        i += 1
    # Single braces never balanced: Anki's rule, the first ``}}`` closes.
    end = text.find("}}", m.end())
    if end < 0:
        return None
    raw = text[m.end() : end]
    return text[start : end + 2], raw.split("::", 1)[0], end + 2, 0


def _rewrite(text: str, *, strip: bool) -> tuple[str, int]:
    out: list[str] = []
    spaced = 0
    i = 0
    while True:
        m = _OPEN_RE.search(text, i)
        if m is None:
            out.append(text[i:])
            break
        out.append(text[i : m.start()])
        res = _walk(text, m.start())
        if res is None:  # unterminated: the rest stays untouched
            out.append(text[m.start() :])
            break
        out.append(res[1] if strip else res[0])
        spaced += res[3]
        i = res[2]
    return "".join(out), spaced


def has_cloze(text: str) -> bool:
    """True if ``text`` holds at least one well-formed cloze deletion."""
    i = 0
    while (m := _OPEN_RE.search(text, i)) is not None:
        res = _walk(text, m.start())
        if res is not None:
            return True
        i = m.end()
    return False


def strip_cloze(text: str) -> str:
    """Replace every cloze with its answer (hint dropped)."""
    return _rewrite(text, strip=True)[0]


def space_adjacent_braces(text: str) -> tuple[str, int]:
    """Return ``(text, n)``: ``text`` with one space inserted after every
    single-brace closer inside a cloze answer that touches another ``}``, and
    the number of spaces inserted. Idempotent; text outside clozes untouched."""
    return _rewrite(text, strip=False)
