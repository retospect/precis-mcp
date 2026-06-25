"""Regex find / substitute primitives for draft chunks — the vi-style
``/pattern`` grep and ``:%s/a/b/`` substitute over a draft's prose.

Pure text ops: no store, no I/O. The handler owns scope resolution (which
chunks) and the writes; this module owns *only* "given a compiled pattern
and a chunk's text, what matches / what would the replacement be". That
split keeps these unit-testable and makes Python ``re`` the single source
of truth for both find and substitute (no Postgres-``~`` dialect drift).

The replacement string is a Python ``re`` template, so backreferences work:
``sub('\\*\\*(\\w+)\\*\\*', r'\\1', text)`` strips ``**bold**`` to ``bold``.

``re.MULTILINE`` is always on, so ``^``/``$`` anchor per physical line
inside a multi-line chunk (a list, a fenced block). Case-fold (``i``) and
dot-all (``s``) are opt-in via the ``flags`` string. Substitution always
replaces *every* occurrence in a chunk (there is no per-line "first only"
notion — a chunk is not a line).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from precis.errors import BadInput

#: A regex longer than this is rejected before compilation — a cheap guard
#: against a pasted-in monster pattern. Real find/replace patterns are short;
#: this is nowhere near a legitimate ceiling.
MAX_PATTERN_LEN = 1000

#: Chunk kinds whose ``text`` is **not** hand-editable prose, so a
#: substitution must skip them: a ``table``'s markdown is *derived* from its
#: canonical ``meta.table`` (editing the text is rejected, ADR 0035 §1), and
#: a ``figure`` is an image blob whose ``text`` is only the caption — but the
#: caption is provenance-bearing and edited through its own path. Find still
#: reads them (read-only); substitute leaves them untouched and reports them.
DERIVED_KINDS = frozenset({"table", "figure"})

#: The ``flags`` letters we accept, vi/sed-style. ``m`` is a no-op (multiline
#: is always on) but accepted so an author can write it without an error.
_FLAG_BITS: dict[str, int] = {
    "i": re.IGNORECASE,
    "s": re.DOTALL,
    "m": re.MULTILINE,  # already default; accepted as a harmless alias
}


@dataclass(frozen=True)
class Match:
    """One regex hit inside a chunk: the 1-based line within the chunk, the
    0-based column, the matched substring, and the whole physical line it
    sits on (for a readable grep row)."""

    line_no: int
    col: int
    matched: str
    line: str


def compile_pattern(pattern: str, flags: str = "") -> re.Pattern[str]:
    """Compile ``pattern`` under ``re.MULTILINE`` plus any opt-in ``flags``
    letters (``i`` case-fold, ``s`` dot-all). Raises :class:`BadInput` on an
    over-long pattern, an unknown flag letter, or a malformed regex — never a
    raw ``re.error`` — so the agent gets a recovery hint, not a 500."""
    if pattern is None or pattern == "":
        raise BadInput(
            "find/sub requires a non-empty pattern",
            next="search(kind='draft', mode='regex', q='\\*\\*\\w+\\*\\*', scope='<slug>')",
        )
    if len(pattern) > MAX_PATTERN_LEN:
        raise BadInput(
            f"regex pattern too long ({len(pattern)} > {MAX_PATTERN_LEN} chars)",
            next="narrow the pattern — find/replace patterns are short",
        )
    bits = re.MULTILINE
    for ch in flags or "":
        if ch not in _FLAG_BITS:
            raise BadInput(
                f"unknown regex flag {ch!r} — use 'i' (ignore case) and/or 's' (dot-all)",
                next="flags='i' to case-fold; flags='is' for both",
            )
        bits |= _FLAG_BITS[ch]
    try:
        return re.compile(pattern, bits)
    except re.error as exc:
        raise BadInput(
            f"invalid regex {pattern!r}: {exc}",
            next="check the pattern — it is Python regex (\\w, \\d, groups, …)",
        ) from exc


def find_in_text(rx: re.Pattern[str], text: str) -> list[Match]:
    """Every non-overlapping match of ``rx`` in one chunk's ``text``, each
    located to its line/column for a grep row. An empty-string match (e.g.
    a pattern that can match nothing, like ``a*``) is skipped so a degenerate
    pattern doesn't report a "hit" at every position."""
    if not text:
        return []
    # Precompute line-start offsets so each match maps to a 1-based line.
    line_starts = [0]
    for m in re.finditer(r"\n", text):
        line_starts.append(m.end())
    out: list[Match] = []
    for mo in rx.finditer(text):
        if mo.start() == mo.end():
            continue
        # Bisect for the line index without importing bisect on the hot path.
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= mo.start():
                lo = mid
            else:
                hi = mid - 1
        line_start = line_starts[lo]
        line_end = text.find("\n", line_start)
        if line_end == -1:
            line_end = len(text)
        out.append(
            Match(
                line_no=lo + 1,
                col=mo.start() - line_start,
                matched=mo.group(0),
                line=text[line_start:line_end],
            )
        )
    return out


def _split_unescaped(s: str, delim: str) -> list[str]:
    """Split ``s`` on every ``delim`` not preceded by a backslash, and turn a
    ``\\<delim>`` back into a literal ``<delim>`` in each part (so ``s/a\\/b/c/``
    yields the pattern ``a/b``). Other backslash escapes are left intact for
    the regex engine."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            buf.append(nxt if nxt == delim else ch + nxt)
            i += 2
            continue
        if ch == delim:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def parse_sed(expr: str) -> tuple[str, str, str]:
    """Parse a vi/sed ``s/find/replace/flags`` substitution string into
    ``(find, replace, flags)``. The delimiter is whatever follows ``s`` (so
    ``s|a|b|`` works when the pattern contains ``/``). The ``g`` flag is
    accepted but dropped — substitution is always global within a chunk.
    Raises :class:`BadInput` on a malformed expression."""
    e = expr.strip()
    if len(e) < 3 or e[0] != "s":
        raise BadInput(
            f"not a substitution expression: {expr!r}",
            next="write s/find/replace/  (or pass sub={'find':…, 'replace':…})",
        )
    delim = e[1]
    if delim.isalnum() or delim.isspace() or delim == "\\":
        raise BadInput(
            f"bad s/// delimiter {delim!r} — use a punctuation char, e.g. s/a/b/",
            next="s/find/replace/   or   s|find|replace|",
        )
    parts = _split_unescaped(e[2:], delim)
    if len(parts) < 3:
        raise BadInput(
            f"unterminated substitution {expr!r} — need s{delim}find{delim}replace{delim}",
            next=f"close it: s{delim}find{delim}replace{delim}",
        )
    find, replace = parts[0], parts[1]
    flags = (parts[2] if len(parts) > 2 else "").replace("g", "")  # g is implicit
    return find, replace, flags


def sub_in_text(rx: re.Pattern[str], repl: str, text: str) -> tuple[str, int]:
    """Apply ``rx`` → ``repl`` to one chunk's ``text``, replacing **every**
    occurrence. Returns ``(new_text, n_substitutions)``. ``repl`` is a Python
    ``re`` template, so ``\\1`` / ``\\g<name>`` backreferences resolve.
    Raises :class:`BadInput` on a bad backreference in ``repl`` (e.g. ``\\9``
    with no 9th group) rather than a raw ``re.error``."""
    try:
        return rx.subn(repl, text)
    except re.error as exc:
        raise BadInput(
            f"invalid replacement {repl!r}: {exc}",
            next="check backreferences — \\1 needs a 1st capture group in the pattern",
        ) from exc
