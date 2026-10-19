"""Provenance-handle pre-pass shared by the docx and LaTeX exporters.

A bracketed record/chunk handle that is not a citation (``[ql4283070]``,
``[dr173020]``, ``[me5]``) is provenance only: both ``_render_target``
functions render it as nothing. The punctuation around it survived, leaving
``prior art (/)`` or ``Surface 's``. :func:`strip_provenance_handles` runs on
chunk text before the inline walk and removes the handle together with the
debris it would leave. Citation brackets, pinned (``>``/``+``/``~``) and
display forms are never touched.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from precis.utils import handle_registry
from precis.utils.mentions import COMPUTED_EVIDENCE_KINDS

#: Sentinel standing in for a dropped handle while the text is tidied.
_MARK = "\x00"
#: Sentinel for a whole parenthetical removed (takes its leading space too).
_GONE = "\x01"

#: ``[xx123]`` only: no pin, no ``~`` chunk suffix, no display/link form.
_BRACKET_HANDLE = re.compile(r"(?<!\[)\[([a-z]{2}\d+)\](?!\()")
_POSSESSIVE = re.compile(r"(?<!\[)\[([a-z]{2}\d+)\](?=['’]s\b)")
#: A handle group: marks joined only by ``/ , ; →`` and whitespace.
_GROUP = re.compile(_MARK + r"(?:[\s/,;→]*" + _MARK + r")*")
_PAREN = re.compile(r"\([^()]*\)")
_T = r"[ \t]"
_SEPS = r"[ \t/,;→]"
#: Code spans and math are opaque: only the prose between them is edited.
_OPAQUE = re.compile(r"(```.*?```|`[^`\n]*`|\$\$.*?\$\$|\$[^$\n]*\$)", re.DOTALL)
#: A parenthetical left holding only these (and separators) is debris.
_GLUE = frozenset(
    [
        "closed",
        "at",
        "see",
        "cf",
        "cf.",
        "via",
        "and",
        "or",
        "then",
        "from",
        "to",
        "in",
        "e.g.",
        "i.e.",
    ]
)


def is_provenance_only(kind: str, is_chunk: bool) -> bool:
    """Would ``_render_target`` print nothing for a handle of this kind?
    Papers/patents/findings cite, computed-evidence kinds print plain text,
    and a draft *chunk* is a cross-ref; every other record is dropped."""
    if kind in ("paper", "patent", "finding") or kind in COMPUTED_EVIDENCE_KINDS:
        return False
    return not (kind == "draft" and is_chunk)


def _dropped(handle: str) -> bool:
    parsed = handle_registry.parse(handle)
    return parsed is not None and is_provenance_only(parsed[0], parsed[1])


def record_title(handle: str, store: Any) -> str:
    """Title of the record behind ``handle`` via ``store``; kind name fallback."""
    parsed = handle_registry.parse(handle)
    if parsed is None:
        return handle
    kind, is_chunk, pk = parsed
    fetch = getattr(store, "fetch_refs_by_ids", None)
    if not is_chunk and callable(fetch):
        try:
            ref = fetch([pk]).get(pk)
        except Exception:  # store hiccup → kind name
            ref = None
        title = str(getattr(ref, "title", None) or "").strip()
        if title:
            return short_title(title)
    return kind


def _neutralise(title: str) -> str:
    """A title spliced into prose must not re-parse as math, markup or a ref."""
    return re.sub(r"[$*`]", "", title).replace("[", "(").replace("]", ")")


def _is_debris(inner: str) -> bool:
    """Is a parenthetical's remainder (handles already removed) only glue?"""
    rest = inner.replace(_MARK, "")
    if "[" in rest or "]" in rest:
        return False
    words = re.sub(r"[\s/,;\u2192]+", " ", rest).split()
    return all(w.lower() in _GLUE for w in words)


#: A function word a dropped handle strands at a clause end — "… under
#: [td5]." would print "… under ."; the word goes with the mark. Runs to a
#: fixpoint so a chain ("… on [a] and [b].") collapses fully.
_STRANDED_WORDS = (
    "about",
    "after",
    "against",
    "and",
    "at",
    "before",
    "by",
    "cf",
    "closed",
    "during",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "over",
    "per",
    "see",
    "then",
    "through",
    "to",
    "under",
    "via",
    "with",
    "within",
)
_STRANDED_WORD = re.compile(
    rf"(?:^|{_T}+)(?:{'|'.join(_STRANDED_WORDS)})\b{_T}*{_MARK}"
    rf"(?:{_T}+(?:{'|'.join(_STRANDED_WORDS)})\b{_T}*{_MARK})*"
    rf"(?=[,.;:!?)\n]|$)",
    re.IGNORECASE,
)


def _tidy(seg: str) -> str:
    """Fix spacing/separators locally around each removal sentinel."""
    seg = _GROUP.sub(_MARK, seg)
    while True:
        out = _STRANDED_WORD.sub("", seg)
        out = re.sub(rf"(\(){_T}*{_MARK}{_SEPS}*", r"\1", out)
        out = re.sub(rf"{_SEPS}*{_MARK}{_T}*\)", ")", out)
        out = re.sub(rf"([,;]){_T}*{_MARK}{_T}*[,;]", r"\1", out)
        out = re.sub(rf"{_T}+{_MARK}(?=[,.;:!?)\n]|$)", "", out)
        out = re.sub(rf"{_T}+{_MARK}{_T}+", " ", out)
        out = re.sub(rf"(?m)^({_T}*){_MARK}{_T}*", r"\1", out)
        if out == seg:
            return seg.replace(_MARK, "")
        seg = out


#: Cap for a record title spliced into prose.
_TITLE_MAX = 120
#: First-sentence end inside a title: ". " before a capital/digit/quote.
_TITLE_SENT_END = re.compile(r"[.!?]\s+(?=[A-Z0-9\"'(\[])")
_TITLE_TRAIL = ".,;:—–- \t"
#: Words a ". " can follow without ending the sentence — abbreviations
#: ("Dr. Smith", "et al. used") the boundary regex would otherwise cut at.
_TITLE_ABBREVS = {
    "al",
    "approx",
    "cf",
    "dept",
    "dr",
    "ed",
    "eds",
    "eq",
    "eqs",
    "et",
    "etc",
    "fig",
    "figs",
    "ibid",
    "inc",
    "jr",
    "ltd",
    "mr",
    "mrs",
    "ms",
    "no",
    "nos",
    "ph",
    "pp",
    "prof",
    "ref",
    "refs",
    "rev",
    "sec",
    "secs",
    "sr",
    "st",
    "tab",
    "vs",
    "viz",
    "vol",
    "vols",
}


def _title_break_ok(prefix: str) -> bool:
    """Is ". " a real sentence end? Not after an abbreviation or a
    single-letter initial ("J. R."), and not after a dotted abbrev
    ("e.g.", "i.e.", "U.S.")."""
    words = prefix.split()
    if not words:
        return True
    tok = words[-1].lower().rstrip(_TITLE_TRAIL)
    if not tok:
        return True
    if len(tok) == 1 or "." in tok:
        return False
    return tok not in _TITLE_ABBREVS


def short_title(title: str) -> str:
    """The prose-spliceable short form of a record's title: its first line
    up to the first sentence end — a todo's ``refs.title`` carries its
    notes as later sentences ("… a preprint. Reto moved it …"), and the
    whole title floods the sentence it lands in (gr493181). Past
    :data:`_TITLE_MAX` chars a word-boundary clip + ellipsis."""
    line = " ".join(str(title or "").split("\n", 1)[0].split())
    for m in _TITLE_SENT_END.finditer(line):
        if _title_break_ok(line[: m.start()]):
            line = line[: m.start()]
            break
    line = line.strip(_TITLE_TRAIL)
    if len(line) > _TITLE_MAX:
        cut = line[:_TITLE_MAX].rfind(" ")
        line = (line[:cut] if cut > _TITLE_MAX // 3 else line[:_TITLE_MAX]).strip(
            _TITLE_TRAIL
        ) + "…"
    return line


def _strip_prose(text: str, title_for: Callable[[str], str]) -> str:
    def poss(m: re.Match[str]) -> str:
        if not _dropped(m.group(1)):
            return m.group(0)
        return _neutralise(title_for(m.group(1)))

    text = _POSSESSIVE.sub(poss, text)
    text = _BRACKET_HANDLE.sub(
        lambda m: _MARK if _dropped(m.group(1)) else m.group(0), text
    )
    if _MARK not in text:
        return text

    def paren(m: re.Match[str]) -> str:
        body = m.group(0)
        if _MARK in body and _is_debris(body[1:-1]):
            return _GONE
        return body

    text = _PAREN.sub(paren, text)
    text = re.sub(rf"{_T}*{_GONE}", "", text)
    return _tidy(text)


def strip_provenance_handles(text: str, *, title_for: Callable[[str], str]) -> str:
    """Remove dropped provenance handles and the punctuation they strand.

    ``[dr1]'s`` becomes ``title_for("dr1")'s`` (title neutralised); a handle
    group goes with its joining separators; a parenthetical left empty or
    holding only glue words (``(closed at)``) is removed whole with the
    space before it. Spacing fixes are local to each removal site, and code
    spans and math are left untouched.
    """
    if "[" not in text:
        return text
    parts = _OPAQUE.split(text)  # odd indices are the opaque spans
    return "".join(
        part if i % 2 else _strip_prose(part, title_for) for i, part in enumerate(parts)
    )
