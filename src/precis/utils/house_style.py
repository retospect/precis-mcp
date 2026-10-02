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
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MASK = "\x00"

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_CODE_INLINE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://[^\s<>)\]]+")
_HANDLE_BRACKET = re.compile(r"\[\[?[^\[\]\s]+\]\]?")
_LINK_TARGET = re.compile(r"(?<=\])\([^)\s]*\)")

_BOLD = re.compile(r"\*\*(?=\S)[^\n]+?(?<=\S)\*\*")
_ITALIC_STAR = re.compile(
    rf"(?<![*\w{_MASK}])\*(?=[^\s*])[^*\n]+?(?<=[^\s*])\*(?![*\w])"
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
    ("italic_star", _ITALIC_STAR, "drop the asterisks: write the plain word"),
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
