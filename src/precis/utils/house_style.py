"""Math-aware house-style lint for draft prose.

The plain-prose rules of ``precis-draft-help`` ("Plain prose, no emphasis
markup") and ``docs/conventions/llm-facing-prose.md``: no em-dash
(U+2014), no ``**bold**``, no single-``*`` italic, no ``_italic_``, no
``--`` double hyphen. Pure and deterministic, the sibling of
``utils/abbreviations.py``: one chunk's text in, a list of
:class:`StyleFlag` out; advisory only, nothing here blocks a write.

The check is *math-aware*. Before any rule runs, spans that are not prose
markup are masked to ``\\x00`` (newlines kept, so line/col stay true):

* fenced and inline code (backtick spans). Decision on the backlog's open
  question: the draft model has no code-span node, but agents routinely
  type identifiers and CLI flags in backticks (``--flag``,
  ``snake_case``), so they are exempt exactly like math;
* ``$$…$$`` / ``$…$`` math, tokenised by ``export.latex._MATH`` (the
  exporter's own regex, so ``$P_5$``, ``$\\mu_B$`` and ``g-C$_3$N$_4$``
  never read as italic). The draft grammar has no ``\\(…\\)``/``\\[…\\]``;
* URLs, space-free ``[handle]`` brackets (``[fi191322]``, ``[¶ab12]``,
  ``[[surface]]``) and the ``(¶term)`` target of a markdown link.

``_italic_`` is word-bounded, so ``snake_case`` never fires.

Two ``*`` shapes are legal prose, not emphasis (gr674905): the adsorbate
star of surface chemistry — ``*NO``, ``*H``, ``*OH``, ``*NO₃⁻`` leading and
``H*``, ``O*``, ``Pd*`` trailing — and the author escape ``\*`` for a
literal star anywhere else (``utils/adsorbate_star``). The exporters read
``*`` the same way, so the lint's flags still predict the render.

A sibling finder, :func:`find_sentence_start_identifiers`, covers the
house-style rule a plain pattern cannot: a sentence that opens with a
*known* lowercase identifier (``autocatpath``, a lowercase glossary
short) reads as a typo in the export, and the no-emphasis rules above
forbid an italic/bold rescue (gr592934). It needs the draft's identifier
set, so it takes ``identifiers`` as a parameter rather than living in
``RULES`` — same masked scan, same :class:`StyleFlag` out.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from precis.utils import adsorbate_star

_MASK = "\x00"

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_CODE_INLINE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://[^\s<>)\]]+")
_HANDLE_BRACKET = re.compile(r"\[\[?[^\[\]\s]+\]\]?")
_LINK_TARGET = re.compile(r"(?<=\])\([^)\s]*\)")

_BOLD = re.compile(r"\*\*(?=\S)[^\n]+?(?<=\S)\*\*")
#: Single-``*`` emphasis — the same shape the exporters render
#: (``export/latex._MD_ITALIC``), so a flag predicts a real mis-render.
#: ``\\`` in the boundary classes keeps ``\*`` literal, and the
#: adsorbate-star fragments (``utils/adsorbate_star``, gr674905) keep
#: ``*NO``/``H*`` out — both are prose, not emphasis.
_ITALIC_STAR = re.compile(
    rf"(?<![*\w{_MASK}\\])\*(?=[^\s*])"
    rf"{adsorbate_star.STAR_NOT_FOLLOWED_BY_ELEMENT}"
    rf"[^*\n]+?(?<=[^\s*\\]){adsorbate_star.STAR_NOT_PRECEDED_BY_ELEMENT}"
    rf"\*(?![*\w])"
)
_ITALIC_UNDERSCORE = re.compile(r"(?<!\w)_(?=[^\s_])[^_\n]*?(?<=[^\s_])_(?!\w)")
_DOUBLE_HYPHEN = re.compile(r"(?<=\w)--(?=\w)|(?<=\s)--(?=\s)")
_EM_DASH = re.compile("—")

#: rule name -> (pattern, fix advice). Order is report order; ``bold`` runs
#: before ``italic_star`` and its span is masked so ``**x**`` never also
#: reads as two italics.
RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "em_dash",
        _EM_DASH,
        "split the sentence, or use a colon, comma or parentheses",
    ),
    ("bold", _BOLD, "drop the asterisks: write the plain words"),
    (
        "italic_star",
        _ITALIC_STAR,
        "drop the asterisks: write the plain word (or escape a literal star as \\*)",
    ),
    (
        "italic_underscore",
        _ITALIC_UNDERSCORE,
        "drop the underscores (they do not render): write the plain word",
    ),
    (
        "double_hyphen",
        _DOUBLE_HYPHEN,
        "use a comma or colon (a range takes an en dash)",
    ),
)

RULE_NAMES: tuple[str, ...] = tuple(r[0] for r in RULES)


@dataclass(frozen=True)
class StyleFlag:
    """One house-style violation: 1-based ``line``/``col`` into the
    original text, the offending ``snippet`` and the ``fix`` advice."""

    rule: str
    line: int
    col: int
    snippet: str
    fix: str


def _blank(m: re.Match[str]) -> str:
    return re.sub(r"[^\n]", _MASK, m.group(0))


def mask_non_prose(text: str) -> str:
    """``text`` with code, math, URLs and handle brackets replaced by
    same-length ``\\x00`` runs (newlines kept) so offsets stay valid."""
    from precis.export.latex import _MATH

    for pat in (_CODE_FENCE, _CODE_INLINE, _MATH, _URL, _HANDLE_BRACKET, _LINK_TARGET):
        text = pat.sub(_blank, text)
    return text


def find_style_flags(text: str) -> list[StyleFlag]:
    """Every house-style violation in one chunk's ``text``, ordered by
    position. Empty for clean prose or empty input."""
    if not text:
        return []
    masked = mask_non_prose(text)
    flags: list[StyleFlag] = []
    for rule, pat, fix in RULES:
        for m in pat.finditer(masked):
            start = m.start()
            line = text.count("\n", 0, start) + 1
            col = start - (text.rfind("\n", 0, start) + 1) + 1
            flags.append(StyleFlag(rule, line, col, text[start : m.end()], fix))
            if rule == "bold":
                masked = masked[:start] + _blank(m) + masked[m.end() :]
    flags.sort(key=lambda f: (f.line, f.col))
    return flags


#: A word that can open a sentence: ASCII identifier shape (``precis``,
#: ``land_slice``, ``g-c3n4``). Runs on the masked text, so a word inside
#: a code/math/URL/handle span is never a candidate.
_IDENT_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def find_sentence_start_identifiers(
    text: str, identifiers: Collection[str]
) -> list[StyleFlag]:
    """Every sentence in ``text`` that opens with a *known* lowercase
    identifier (gr592934): "autocatpath, the reaction engine, …" reads
    as a typo in the export, and house style forbids an italic/bold
    rescue — the fix is a leading 'The <identifier> …' or `` `…` ``
    inline code (renders ``\\texttt``).

    ``identifiers`` is the draft's known-lowercase set — glossary
    ``term`` shorts plus the built-ins
    (``draft/lint.py::lowercase_identifiers``). Only an exact,
    all-lowercase token match flags: ``Autocatpath`` capitalised or an
    unknown lowercase word is silent.

    Sentence starts use the exporter's own boundary predicate
    (:func:`precis.export._abbrev.at_sentence_start`): text start, a
    paragraph break, or a terminal ``.?!`` — except a mark closing a
    non-terminal abbreviation (``e.g.``, ``et al.``, ``Fig.``), so
    "e.g. autocatpath runs" mid-sentence never fires and the lint can
    never disagree with the first-use capitalisation pass. Opening
    quotes/brackets before the word still count as a sentence start."""
    if not text or not identifiers:
        return []
    from precis.export._abbrev import at_sentence_start

    masked = mask_non_prose(text)
    flags: list[StyleFlag] = []
    for m in _IDENT_WORD.finditer(masked):
        word = m.group(0)
        if word != word.lower() or word not in identifiers:
            continue
        if not at_sentence_start(masked[: m.start()]):
            continue
        line = text.count("\n", 0, m.start()) + 1
        col = m.start() - (text.rfind("\n", 0, m.start()) + 1) + 1
        flags.append(
            StyleFlag(
                "sentence_start_identifier",
                line,
                col,
                text[m.start() : m.end()],
                "open with 'The <identifier> …' or set it in `backticks` "
                "(renders \\texttt in the export)",
            )
        )
    return flags
