"""Is a chunk usable as **evidence grounding**? (gripe 245842)

An evidence edge grounded on a paper's title/author front-matter block says
"this paper exists", not "this passage supports the claim" — a vacuous
"bibliography-stub" hub. A title block is also short and dense with exactly a
citing span's topic words, so it *wins* a naive lexical match: the defect is
self-reinforcing, and every passage-selection path needs the same guard.

Two consumers, deliberately sharing one predicate rather than each rolling its
own: :mod:`precis.taproot.backfill` (the draft ``[pa]``-arm candidate pool and
the ``[pc]``-arm supporter check) and
:func:`precis.taproot.reground.candidate_passages` (the passage ranker behind
re-grounding, chase and evidence repair). It sits here rather than in either
so the dependency runs one way: both import grounding, grounding imports
neither.

The sibling exclusion is :func:`precis.taproot.reground.is_hearsay_section` —
"the paper cites the work, it didn't do it". This one is "the paper names
itself, it doesn't assert". Both answer *this passage cannot be evidence*.
"""

from __future__ import annotations

import re

__all__ = ["has_grounding_prose"]

#: Word tokens, for the length and capitalisation measures below.
_TOKEN_RE = re.compile(r"\w+")

#: A markdown table row (``| … | … |``). A grounding chunk that is a pure
#: numeric table carries no sentence but IS legitimate evidence for a numeric
#: claim, so a table escapes the prose test below.
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)

#: Rows a chunk needs before it counts as a table (header + rule + one datum).
_MIN_TABLE_ROWS = 3

#: A sentence terminator: ``.``/``!``/``?`` followed by whitespace or end of
#: text. Splitting here over-splits at initials and abbreviations, which
#: :func:`_prose_sentences` re-joins.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])(?=\s|$)")

#: A blank line — the paragraph boundary. Front matter is line-structured
#: (title line, author line, affiliation line), so a paragraph never spans
#: from the title into the correspondence footnote.
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")

#: A trailing single capital: the ``S.`` of "Anton S. Anisimov", not the end
#: of a sentence. An author list is nothing but these.
_INITIAL_RE = re.compile(r"(?:^|\s)[A-Z]\.$")

#: A trailing word whose period is an abbreviation mark, not a terminator.
_TRAILING_WORD_RE = re.compile(r"(?:^|[\s(\[])([A-Za-z]+)\.$")
_ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "al",
        "et",
        "eg",
        "ie",
        "cf",
        "vs",
        "etc",
        "fig",
        "figs",
        "eq",
        "eqs",
        "ref",
        "refs",
        "tab",
        "vol",
        "no",
        "nos",
        "pp",
        "ed",
        "eds",
        "ca",
        "approx",
        "dr",
        "prof",
        "st",
        "inc",
        "corp",
        "dept",
        "univ",
    }
)

#: Words a terminated run needs before it counts as an assertion rather than a
#: title fragment, an author line, or a caption label.
_MIN_PROSE_WORDS = 8

#: Above this share of capitalised words a "sentence" is a name list or a
#: title restatement, not prose — the backstop for a front-matter block that
#: is NOT blank-line separated, so :func:`_prose_sentences` cannot split the
#: title off the correspondence footnote. Measured over the live grounding
#: chunks this catches nothing the terminator rule misses, so it is set loose
#: (an author list runs 0.85+) to leave headroom for acronym-dense body prose
#: — "CRISPR-Cas9 targeting of BRCA1 and TP53 in HeLa cells…" is real prose.
_MAX_CAPITALISED = 0.7


def _is_false_terminator(run: str) -> bool:
    """Does ``run`` end at an initial or an abbreviation rather than at the
    end of a sentence? ("Anton S." / "Vol." / "et al.")"""
    s = run.rstrip()
    if not s.endswith("."):
        return False  # ! and ? are never abbreviation marks
    if _INITIAL_RE.search(s):
        return True
    m = _TRAILING_WORD_RE.search(s)
    return bool(m and m.group(1).lower() in _ABBREVIATIONS)


def _prose_sentences(text: str) -> list[str]:
    """The *terminated* sentences of ``text``, per paragraph, with the false
    terminators an initial or an abbreviation produces re-joined.

    A trailing run with no terminator is **not** a sentence and is dropped —
    that is exactly what a title line and an author line are.
    """
    out: list[str] = []
    for para in _PARA_SPLIT_RE.split(text):
        buf = ""
        for part in _SENT_SPLIT_RE.split(para):
            buf += part
            if not buf.rstrip().endswith((".", "!", "?")):
                continue  # unterminated so far — keep accumulating
            if _is_false_terminator(buf):
                continue  # initial / abbreviation: the sentence continues
            out.append(buf)
            buf = ""
    return out


def has_grounding_prose(text: str) -> bool:
    """Can a claim be honestly grounded *at this chunk*? (gripe 245842)

    True when the chunk carries at least one **assertion** — a terminated
    sentence of at least :data:`_MIN_PROSE_WORDS` words that is not mostly
    capitalised — or when it is a **table** (a numeric table asserts through
    its cells and is legitimate evidence for a numeric claim).

    False for a paper's title/author front-matter block: a title is a noun
    phrase with no terminator, an author list is initials, and neither
    asserts anything. Grounding an evidence edge there mints a vacuous
    "bibliography-stub" hub — the edge says "this paper exists", not "this
    passage supports the claim". Note this rejects a front-matter block whose
    *title* happens to be a full sentence ("Glymphatic dysfunction … is
    related to …"): titles carry no terminator, so they never survive
    :func:`_prose_sentences`.

    Deliberately a **prose** test, not an ``ord`` test: a paper's abstract is
    often ``ord`` 0-2 and is fine grounding, and over-rejecting is cheap here
    (the caller degrades to ``reground-nomatch``/``ungroundable`` — a skip
    that leaves the prose untouched — never to a wrong grounding).
    """
    if len(_TABLE_ROW_RE.findall(text)) >= _MIN_TABLE_ROWS:
        return True
    for sentence in _prose_sentences(text):
        words = [w for w in _TOKEN_RE.findall(sentence) if w[0].isalpha()]
        if len(words) < _MIN_PROSE_WORDS:
            continue
        capitalised = sum(1 for w in words if w[0].isupper())
        if capitalised / len(words) <= _MAX_CAPITALISED:
            return True
    return False
