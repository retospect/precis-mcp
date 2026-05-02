"""LaTeX parser — split a ``.tex`` file into section-aware paragraph blocks.

A block boundary is created **either** by a blank line (paragraph
break, like plaintext) **or** by a sectioning command
(``\\part``, ``\\chapter``, ``\\section``, ``\\subsection``,
``\\subsubsection``, ``\\paragraph``, ``\\subparagraph``). The
sectioning command line itself starts a new block — the paragraphs
that follow within the same section are still split on blank lines,
so editing granularity stays paragraph-sized.

Each block additionally carries:

- ``section_level`` / ``section_title`` — set when the block **starts**
  with a sectioning command (``None`` otherwise).
- ``section_path`` — the (level, title) ancestry stack at the start of
  the block (outer to inner). Lets the search renderer show "Block in
  Methods > Kinetics".
- ``inputs`` — every ``\\input{...}`` and ``\\include{...}`` argument
  observed inside the block's text. Drives the recursive ``/toc``
  walker on :class:`precis.handlers.tex.TexHandler`.

This is **not** a full LaTeX parser — no macro expansion, no
environment grouping, no comment stripping. Source text is preserved
verbatim so anchored edits work against the original characters.

Used by :class:`precis.handlers.tex.TexHandler`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from precis.utils.plaintext_parse import PlaintextBlock
from precis.utils.slug import slug_from_text

# Canonical LaTeX sectioning levels (lower = outer). Mirrors
# ``\documentclass{book}``'s default depth ordering. ``\part`` and
# ``\chapter`` only occur in book/report classes; we accept them
# anyway so a project mixing classes still parses cleanly.
TEX_SECTION_LEVELS: dict[str, int] = {
    "part": -2,
    "chapter": -1,
    "section": 0,
    "subsection": 1,
    "subsubsection": 2,
    "paragraph": 3,
    "subparagraph": 4,
}

#: Pretty-printed names for the TOC renderer. ``\paragraph`` and
#: ``\subparagraph`` rarely surface in TOCs, but we include them for
#: completeness — users can filter by depth at render time.
TEX_SECTION_NAMES = tuple(TEX_SECTION_LEVELS.keys())


# A sectioning command at the start of a line. Allows leading whitespace,
# the optional star form (``\section*{...}``), and an optional short
# title in square brackets (``\section[short]{long}``). The captured
# group is the **long** title (between the braces).
_SECTION_RE = re.compile(
    r"^\s*\\(" + "|".join(TEX_SECTION_NAMES) + r")\*?"
    r"(?:\[[^\]]*\])?"  # optional short title for TOC
    r"\{(.*?)\}"  # long title
)

# ``\input{path}`` or ``\include{path}`` anywhere on a line. Whitespace
# inside braces is preserved (LaTeX would error on it but we don't
# enforce); empty argument is ignored upstream.
_INPUT_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")


@dataclass(frozen=True, slots=True)
class TexBlock(PlaintextBlock):
    """One block in a ``.tex`` file.

    Inherits :class:`PlaintextBlock`'s shape (``pos``, ``slug``,
    ``text``, ``line_start``, ``line_end``) and adds tex-specific
    structural metadata. All extras are optional — a paragraph that
    isn't a section heading and contains no ``\\input{}`` simply has
    ``section_level=None``, ``inputs=()``.
    """

    section_level: int | None
    """``\\section`` → 0, ``\\subsection`` → 1, ... See
    :data:`TEX_SECTION_LEVELS`. ``None`` for non-section blocks."""

    section_title: str | None
    """The text inside the sectioning command's braces. ``None`` for
    non-section blocks."""

    section_path: tuple[tuple[int, str], ...]
    """Ancestor stack at the start of this block. Each entry is
    ``(level, title)``; outer-most first. Empty tuple before the
    first sectioning command in the file."""

    inputs: tuple[str, ...]
    """Raw arguments of every ``\\input{...}`` / ``\\include{...}``
    found in this block's text. Order preserved."""


def parse_tex(content: str) -> list[TexBlock]:
    """Split a ``.tex`` buffer into section-aware paragraph blocks.

    Boundaries:

    - One or more blank lines → paragraph break.
    - A line starting with a sectioning command → new block (the
      command line is the first line of the new block).

    Empty / whitespace-only input → empty list.
    """
    lines = content.splitlines()
    n = len(lines)
    out: list[TexBlock] = []
    taken: set[str] = set()
    i = 0
    pos = 0
    # Stack of currently-open section ancestors as (level, title).
    # Updated whenever we open a new sectioning block.
    ancestors: list[tuple[int, str]] = []

    while i < n:
        # Skip blank lines between blocks.
        if not lines[i].strip():
            i += 1
            continue

        start = i
        first_line = lines[i]
        section_match = _SECTION_RE.match(first_line)

        if section_match:
            # The sectioning command terminates the previous block (if
            # any) and starts a new one. Update the ancestor stack:
            # pop everything at or below this level, then push self.
            command = section_match.group(1)
            level = TEX_SECTION_LEVELS[command]
            title = section_match.group(2).strip()
            section_level: int | None = level
            section_title: str | None = title
            # Snapshot ancestors BEFORE pushing so this block records
            # its parent chain (not itself).
            section_path_snapshot = tuple(ancestors)
            # Pop the stack down to this level's parent depth.
            while ancestors and ancestors[-1][0] >= level:
                ancestors.pop()
            ancestors.append((level, title))
        else:
            section_level = None
            section_title = None
            section_path_snapshot = tuple(ancestors)

        block_lines = [first_line]
        i += 1
        # A sectioning command is always a *one-line* block — the body
        # that follows is the next block, even when no blank line
        # separates them. This keeps editing granularity right (you
        # can edit a heading without touching its body) and keeps the
        # TOC view from showing prose under the heading.
        # For non-section blocks, consume non-blank lines until either
        # a blank line OR a sectioning command starts a fresh block.
        if section_match is None:
            while i < n and lines[i].strip() and not _SECTION_RE.match(lines[i]):
                block_lines.append(lines[i])
                i += 1

        text = "\n".join(block_lines)
        slug = _mint_slug(text, taken)
        inputs = tuple(_INPUT_RE.findall(text))

        out.append(
            TexBlock(
                pos=pos,
                slug=slug,
                text=text,
                line_start=start + 1,
                line_end=start + len(block_lines),
                section_level=section_level,
                section_title=section_title,
                section_path=section_path_snapshot,
                inputs=inputs,
            )
        )
        pos += 1

    return out


def _mint_slug(text: str, taken: set[str]) -> str:
    """Stable, unique slug for a block (same shape as plaintext).

    Derivation: first 5 words slugified + 6-char sha1 hash. Same shape
    as :func:`precis.utils.plaintext_parse._mint_slug` so downstream
    code (anchored edit, search-result rendering) doesn't have to
    branch by kind.
    """
    first_words = " ".join(text.split()[:5])
    base = slug_from_text(first_words, max_len=24)
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    base = f"{base}-{h}" if base else f"p-{h}"

    if base not in taken:
        taken.add(base)
        return base

    for n in range(2, 10000):  # pragma: no cover — exotic collision path
        candidate = f"{base}-{n}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise ValueError(f"unreachable: more than 10k collisions on {base!r}")


def extract_inputs(content: str) -> list[str]:
    """Convenience: every ``\\input{...}`` / ``\\include{...}`` arg in
    ``content``, in source order. Used by the recursive ``/toc`` walker
    to discover child files without re-parsing into blocks."""
    return list(_INPUT_RE.findall(content))


__all__ = [
    "TEX_SECTION_LEVELS",
    "TEX_SECTION_NAMES",
    "TexBlock",
    "extract_inputs",
    "parse_tex",
]
