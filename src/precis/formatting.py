"""Markdown ↔ DOCX run formatting conversion."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class FormattedRun:
    """A text run with formatting properties."""

    text: str
    bold: bool = False
    italic: bool = False
    superscript: bool = False
    subscript: bool = False
    strike: bool = False
    url: str = ""  # non-empty = hyperlink
    cite_key: str = ""  # non-empty = citation reference [@key]


def runs_to_markdown(runs: list[FormattedRun]) -> str:
    """Convert formatted runs to Markdown text."""
    parts = []
    for r in runs:
        t = r.text
        if r.cite_key:
            t = f"[@{r.cite_key}]"
        elif r.url:
            t = f"[{t}]({r.url})"
        if r.superscript:
            t = f"<sup>{t}</sup>"
        if r.subscript:
            t = f"<sub>{t}</sub>"
        if r.bold and r.italic:
            t = f"***{t}***"
        elif r.bold:
            t = f"**{t}**"
        elif r.italic:
            t = f"*{t}*"
        if r.strike:
            t = f"~~{t}~~"
        parts.append(t)
    return "".join(parts)


# Patterns for parsing Markdown back to runs
_PATTERNS = [
    # Bold+italic
    (re.compile(r"\*\*\*(.+?)\*\*\*"), {"bold": True, "italic": True}),
    # Bold
    (re.compile(r"\*\*(.+?)\*\*"), {"bold": True}),
    # Italic
    (re.compile(r"\*(.+?)\*"), {"italic": True}),
    # Strikethrough
    (re.compile(r"~~(.+?)~~"), {"strike": True}),
    # Superscript
    (re.compile(r"<sup>(.+?)</sup>"), {"superscript": True}),
    # Subscript
    (re.compile(r"<sub>(.+?)</sub>"), {"subscript": True}),
    # Hyperlink
    (re.compile(r"\[(.+?)\]\((.+?)\)"), {"url": True}),
    # Citation: [@key]
    (re.compile(r"\[@([^\]\s]+)\]"), {"cite_key": True}),
]


# ---------------------------------------------------------------------------
# List utilities (shared by word.py and tex.py)
# ---------------------------------------------------------------------------

# Matches "- item" or "  - item" (unordered)
BULLET_RE = re.compile(r"^(\s*)-\s+(.*)$")
# Matches "1. item" or "  1. item" (ordered)
NUMBER_RE = re.compile(r"^(\s*)\d+\.\s+(.*)$")
# Quick test — is this line a list item?
LIST_ITEM_RE = re.compile(r"^(\s*)(?:-|\d+\.)\s")


def parse_list_prefix(text: str) -> tuple[str, int, str] | None:
    """Parse a markdown list prefix.

    Returns ``('bullet'|'number', indent_level, content)`` or *None*.
    """
    m = BULLET_RE.match(text)
    if m:
        return ("bullet", len(m.group(1)) // 2, m.group(2))
    m = NUMBER_RE.match(text)
    if m:
        return ("number", len(m.group(1)) // 2, m.group(2))
    return None


def strip_list_prefix(text: str) -> str:
    """Strip markdown list prefix if present, returning just the content."""
    parsed = parse_list_prefix(text)
    if parsed:
        return parsed[2]
    return text


def list_prefix(list_type: str, ilvl: int) -> str:
    """Generate a markdown list prefix for the given type and indent level."""
    indent = "  " * ilvl
    if list_type == "number":
        return f"{indent}1. "
    return f"{indent}- "


# Split consecutive [@key]: bib definitions crammed onto one line
_BIB_SPLIT_RE = re.compile(r"(?=\[@[^\]\s]+\]:\s)")


def group_paragraphs(text: str) -> list[str]:
    """Split text into chunks, grouping consecutive list items together.

    Non-list lines become individual chunks.  Consecutive list lines are
    joined with ``\\n`` into a single chunk so the handler can emit them
    as one logical list block.  Multiple ``[@key]: ...`` bibliography
    definitions on one line are split into separate chunks.
    """
    lines = [ln for ln in text.split("\n") if ln.strip()]
    chunks: list[str] = []
    current_list: list[str] = []

    for line in lines:
        stripped = line.strip()
        if LIST_ITEM_RE.match(stripped):
            current_list.append(stripped)
        else:
            if current_list:
                chunks.append("\n".join(current_list))
                current_list = []
            # Split consecutive bib definitions on one line
            parts = _BIB_SPLIT_RE.split(stripped)
            for p in parts:
                p = p.strip()
                if p:
                    chunks.append(p)

    if current_list:
        chunks.append("\n".join(current_list))
    return chunks


def markdown_to_runs(text: str) -> list[FormattedRun]:
    """Parse Markdown text into FormattedRun objects.

    Handles bold, italic, superscript, subscript, strikethrough, and hyperlinks.
    Unrecognized text becomes plain runs.
    """
    if not text:
        return []

    # Use a token-based approach: find all formatting spans, sort by position
    spans: list[tuple[int, int, FormattedRun]] = []

    for pattern, props in _PATTERNS:
        for m in pattern.finditer(text):
            if "url" in props:
                run = FormattedRun(text=m.group(1), url=m.group(2))
            elif "cite_key" in props:
                key = m.group(1)
                run = FormattedRun(text=f"[@{key}]", cite_key=key)
            else:
                run = FormattedRun(text=m.group(1), **props)
            spans.append((m.start(), m.end(), run))

    if not spans:
        return [FormattedRun(text=text)]

    # Sort by start position, resolve overlaps (first match wins)
    spans.sort(key=lambda s: s[0])
    result: list[FormattedRun] = []
    pos = 0

    for start, end, run in spans:
        if start < pos:
            continue  # overlapping span, skip
        if start > pos:
            plain = text[pos:start]
            if plain:
                result.append(FormattedRun(text=plain))
        result.append(run)
        pos = end

    # Trailing plain text
    if pos < len(text):
        result.append(FormattedRun(text=text[pos:]))

    return result
