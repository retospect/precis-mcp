"""Render-time first-use abbreviation expansion, shared by the docx and
LaTeX exporters (gr491599).

Both exporters walk prose in reading order and hand each plain-text run to
:func:`expand`, which splits it into literal text and :class:`Use` tokens
and updates the shared ``seen``/``used`` sets. Each exporter only decides
how a :class:`Use` looks (docx: ``long (SHORT)`` text; LaTeX: the ``\\gls``
family), so the two formats cannot disagree on which occurrence defines a
short or what is left alone — they used to, as two hand-mirrored copies.

The rules, and why:

- A use is a whole, case-sensitive word, longest short first; a trailing
  ``s`` is the plural. A short after a hyphen is part of another word
  (``h-BN`` is not ``BN``), but a short before one is a compound that
  counts (``CNB-based`` → ``carbon nanobud (CNB)-based``): otherwise a
  short used only in compounds was never defined or listed (L1).
- Two shorts joined by a dash or slash, both new (``HOMO–LUMO``), are
  defined together, ``long1–long2 (S1–S2)``, not as two interleaved
  expansions.
- Nothing inside a URL, DOI or an exporter placeholder expands: it would
  corrupt the link and use up the first use (M6).
- An authored ``long (SHORT)`` is matched before the single shorts, so a
  long form that contains another short (``functionalized CNB (FCNB)``)
  is not eaten from inside (H3). It collapses into the one definition;
  once the short is introduced the authored span stays verbatim.
- A long form written just before ``(SHORT)`` but outside this run (link
  text, a bold span) marks the short defined without a second expansion.
- A first use inside parentheses brackets its short, ``(long [SHORT], …)``,
  instead of nesting parentheses.
- A definition that starts a sentence is capitalised, except after a
  non-terminal abbreviation like ``et al.``, ``e.g.`` or ``Fig.`` (M7).
- In a heading every use is the bare short and does not count as the
  first use (M2): a section title is not where the reader meets the
  definition, and LaTeX's ``\\gls`` there leaks into PDF bookmarks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

#: Spans no abbreviation expands inside: URLs, DOIs, and the exporters'
#: own ``\\x00<n>\\x00`` stash placeholders.
_PROTECTED = (
    r"(?:https?://|ftp://|www\.)\S+"
    r"|\bdoi:\s*\S+"
    r"|\b10\.\d{4,9}/\S+"
    r"|\x00\d+\x00"
)

#: The text before a sentence-initial word: a terminal mark, optional
#: closing quotes/brackets, whitespace.
_SENTENCE_END = re.compile(r"[.?!][)\]\"'’”]*\s+$")

#: Abbreviations whose period does not end the sentence.
_NON_TERMINAL = re.compile(
    r"(?:\bet\s+al|\be\.g|\bi\.e|\bcf|\bvs|\bviz|\bca|\bapprox|\bresp"
    r"|\b[Ff]igs?|\b[Ee]qs?|\b[Rr]efs?|\b[Ss]ecs?|\b[Tt]abs?)\.\s+$"
)

#: Opening emphasis, quotes and brackets directly before a word.
_OPENERS = re.compile(r"[*_\"'“‘(\[]+$")

Mode = Literal["first", "later", "bare", "unset"]


@dataclass(frozen=True)
class Use:
    """One occurrence of a defined short.

    ``mode``: ``first`` defines it (``long (SHORT)``); ``later`` is the bare
    short after its definition; ``bare`` is the bare short that did not
    count as the definition (a heading); ``unset`` is the bare short whose
    long form the author wrote just before it, so it now counts as
    defined. ``capital`` (first only): the long form opens a sentence.
    ``bracket`` (first only): inside parentheses, so ``long [SHORT]``.
    ``pair`` (first only): ``(separator, second short)`` of a jointly
    defined ``A–B`` pair."""

    short: str
    plural: bool = False
    mode: Mode = "later"
    capital: bool = False
    bracket: bool = False
    pair: tuple[str, str] | None = None


Segment = str | Use


def plural_long(long: str) -> str:
    """English plural of a long form's last word (``spectroscopy`` →
    ``spectroscopies``); a form already ending in ``s`` is unchanged
    (``density of states``)."""
    if not long or long[-1].lower() == "s":
        return long
    if re.search(r"[^aeiou]y$", long, re.IGNORECASE):
        return long[:-1] + "ies"
    if re.search(r"(?:x|z|ch|sh)$", long, re.IGNORECASE):
        return long + "es"
    return long + "s"


def at_sentence_start(before: str, *, empty: bool = True) -> bool:
    """Does a word placed after ``before`` start a sentence? ``empty`` is
    the answer for blank ``before`` (a chunk start, or unknown context).
    Opening markup right before the word (``**``, a quote, ``(``) is not
    text: ``**DFT**`` opening a chunk still starts the sentence."""
    before = _OPENERS.sub("", before)
    if not before.strip():
        return empty
    return bool(_SENTENCE_END.search(before)) and not _NON_TERMINAL.search(before)


def _long_alts(long: str) -> str:
    """Regex alternation of a long form, its plural and its ``s``-less
    stem, longest first."""
    alts = {long, plural_long(long)}
    if long.lower().endswith("s"):
        alts.add(long[:-1])
    return "(?:" + "|".join(map(re.escape, sorted(alts, key=len, reverse=True))) + ")"


@dataclass(frozen=True)
class _Patterns:
    #: (short, authored ``long (SHORT)`` regex), longest long form first.
    spans: tuple[tuple[str, re.Pattern[str]], ...]
    #: short → regex for its long form ending the text before ``(SHORT)``.
    lookback: dict[str, re.Pattern[str]]
    main: re.Pattern[str]


@lru_cache(maxsize=64)
def _patterns(items: tuple[tuple[str, str], ...]) -> _Patterns:
    longs = {s: lf.strip() for s, lf in items if s}
    alt = "|".join(map(re.escape, sorted(longs, key=len, reverse=True)))
    with_long = sorted((s for s in longs if longs[s]), key=lambda s: -len(longs[s]))
    spans = tuple(
        (
            s,
            re.compile(
                rf"(?<![\w-]){_long_alts(longs[s])}\s*\({re.escape(s)}(s)?\)",
                re.IGNORECASE,
            ),
        )
        for s in with_long
    )
    lookback = {
        s: re.compile(
            rf"(?<![\w-]){_long_alts(longs[s])}(?:\*{{1,2}}|_)?\s*\($", re.IGNORECASE
        )
        for s in with_long
    }
    main = re.compile(
        rf"(?P<prot>{_PROTECTED})"
        r"|\x03(?P<tok>\d+)\x03"
        rf"|(?<![\w-])(?P<a>{alt})(?P<sep>[-–—/])(?P<b>{alt})(?![\w-])"
        rf"|(?<![\w-])(?P<s>{alt})(?P<pl>s)?(?!\w)"
    )
    return _Patterns(spans=spans, lookback=lookback, main=main)


def expand(
    text: str,
    abbrevs: dict[str, str],
    seen: set[str],
    used: set[str] | None = None,
    *,
    preceding: str | None = None,
    heading: bool = False,
) -> list[Segment]:
    """Split ``text`` into literal strings and :class:`Use` tokens.

    ``seen`` (shorts already defined) and ``used`` (shorts that occurred at
    all) are updated in place and threaded through the whole export.
    ``preceding`` is the already-rendered text before ``text`` in the same
    paragraph: it decides sentence starts, open parentheses and an authored
    long form outside the run; ``None`` means unknown (never a sentence
    start at the run's head). ``heading`` makes every use bare."""
    if not text or not any(abbrevs):
        return [text] if text else []
    pats = _patterns(tuple(sorted(abbrevs.items())))
    spans: list[tuple[str, str, bool]] = []  # (short, authored text, plural)
    for short, rx in pats.spans:

        def _stash(m: re.Match[str], short: str = short) -> str:
            spans.append((short, m.group(0), bool(m.group(1))))
            return f"\x03{len(spans) - 1}\x03"

        text = rx.sub(_stash, text)
    lead = preceding or ""
    out: list[Segment] = []
    pos = 0

    def emit(seg: Segment) -> None:
        if isinstance(seg, str) and out and isinstance(out[-1], str):
            out[-1] += seg
        elif seg:
            out.append(seg)

    def unstash(s: str) -> str:
        return re.sub(r"\x03(\d+)\x03", lambda m: spans[int(m.group(1))][1], s)

    for m in pats.main.finditer(text):
        if m.group("prot") is not None:
            continue
        before = lead + unstash(text[: m.start()])
        capital = at_sentence_start(before, empty=preceding is not None)
        bracket = before.count("(") > before.count(")")
        emit(unstash(text[pos : m.start()]))
        pos = m.end()
        if m.group("tok") is not None:
            short, span, plural = spans[int(m.group("tok"))]
            if used is not None:
                used.add(short)
            if heading or short in seen:
                emit(span)
                continue
            seen.add(short)
            emit(Use(short, plural, "first", capital, bracket))
            continue
        a, sep, b = m.group("a"), m.group("sep"), m.group("b")
        if a is not None and f"{a}{sep}{b}" not in abbrevs:
            if used is not None:
                used.update((a, b))
            if not heading and a not in seen and b not in seen and a != b:
                seen.update((a, b))
                emit(Use(a, False, "first", capital, bracket, (sep, b)))
                continue
            emit(_single(a, False, seen, heading, capital, bracket))
            emit(sep)
            emit(_single(b, False, seen, heading, False, bracket))
            continue
        # A registered compound (``GNR-FET``) the pair branch matched first.
        short = f"{a}{sep}{b}" if a is not None else m.group("s")
        plural = bool(m.group("pl"))
        if used is not None:
            used.add(short)
        lookback = pats.lookback.get(short)
        authored = (
            lookback is not None
            and text[m.end() : m.end() + 1] == ")"
            and lookback.search(before) is not None
        )
        emit(_single(short, plural, seen, heading, capital, bracket, authored))
    emit(unstash(text[pos:]))
    return out


def _single(
    short: str,
    plural: bool,
    seen: set[str],
    heading: bool,
    capital: bool,
    bracket: bool,
    authored: bool = False,
) -> Use:
    """The :class:`Use` for one lone occurrence; ``authored``: the long
    form was written right before ``(SHORT)``."""
    if heading:
        return Use(short, plural, "bare")
    if short in seen:
        return Use(short, plural, "later")
    seen.add(short)
    if authored:
        return Use(short, plural, "unset")
    return Use(short, plural, "first", capital, bracket)


def plain(use: Use, abbrevs: dict[str, str]) -> str:
    """A :class:`Use` as plain text: ``long (SHORT)`` for a definition,
    the bare short otherwise."""
    s = use.short + ("s" if use.plural else "")
    long = abbrevs.get(use.short, "").strip()
    if use.mode != "first" or not long:
        return s
    if use.pair is not None:
        sep, b = use.pair
        long = f"{long}{sep}{abbrevs.get(b, '').strip() or b}"
        s = f"{use.short}{sep}{b}"
    elif use.plural:
        long = plural_long(long)
    if use.capital:
        long = long[0].upper() + long[1:]
    return f"{long} [{s}]" if use.bracket else f"{long} ({s})"
