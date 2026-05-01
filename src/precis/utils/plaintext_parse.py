"""Plaintext parser — split a ``.txt`` / ``.log`` file into paragraphs.

Simpler sibling of :mod:`precis.utils.md_parse`. There is no block
grammar for plaintext — a "block" is just a paragraph, i.e. a run
of non-blank lines separated from its neighbours by one or more
blank lines. Block slugs are content-derived (first ~5 words + 6-hex
hash of the full text) so they're stable across re-ingest even when
surrounding paragraphs shift.

Used by :class:`precis.handlers.plaintext.PlaintextHandler`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from precis.utils.slug import slug_from_text


@dataclass(frozen=True, slots=True)
class PlaintextBlock:
    """One paragraph in a plaintext file."""

    pos: int
    """0-indexed sequential position in the file."""

    slug: str
    """Stable, content-derived slug. Survives re-ingest."""

    text: str
    """Raw source text of this paragraph (newlines preserved)."""

    line_start: int
    """1-indexed source line where this paragraph starts."""

    line_end: int
    """1-indexed source line where this paragraph ends (inclusive)."""


def parse_plaintext(content: str) -> list[PlaintextBlock]:
    """Split a plaintext file into paragraph blocks.

    Rules:

    - A blank line (or run of blank lines) is a paragraph break.
    - Leading/trailing blank lines in the file are dropped.
    - A lone non-blank line is a one-line paragraph.
    - Indentation is preserved verbatim — the parser never rewrites
      leading whitespace.

    Empty / whitespace-only input returns an empty list.
    """
    lines = content.splitlines()
    n = len(lines)
    out: list[PlaintextBlock] = []
    taken: set[str] = set()
    i = 0
    pos = 0

    while i < n:
        # Skip blank lines between paragraphs.
        if not lines[i].strip():
            i += 1
            continue

        start = i
        para_lines = [lines[i]]
        i += 1
        while i < n and lines[i].strip():
            para_lines.append(lines[i])
            i += 1
        text = "\n".join(para_lines)
        slug = _mint_slug(text, taken)
        out.append(
            PlaintextBlock(
                pos=pos,
                slug=slug,
                text=text,
                line_start=start + 1,
                line_end=start + len(para_lines),
            )
        )
        pos += 1

    return out


def _mint_slug(text: str, taken: set[str]) -> str:
    """Return a stable, unique slug for a paragraph.

    Derivation: first 5 words (non-whitespace) of the paragraph,
    slugified, plus a 6-char sha1 content hash. Collisions inside
    the same file are disambiguated with a numeric suffix — same
    shape as markdown's paragraph slug minter so downstream code
    and agent muscle memory carry over.
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


__all__ = ["PlaintextBlock", "parse_plaintext"]


# ---------------------------------------------------------------------------
# Back-compat with md_parse's file-slug helpers. Plaintext uses the
# same path-encoding scheme but with different extensions, so we
# expose thin wrappers that accept a tuple of allowed extensions and
# delegate to the markdown helpers (they only care about the slug
# shape, not the extension).
# ---------------------------------------------------------------------------

_PLAINTEXT_EXTENSIONS: tuple[str, ...] = (".txt", ".log")


def plaintext_extensions() -> tuple[str, ...]:
    """Extensions treated as plaintext by the handler + walker."""
    return _PLAINTEXT_EXTENSIONS


_STRIP_EXT_RE = re.compile(r"\.(txt|log)$", re.IGNORECASE)


def strip_plaintext_ext(rel_path: str) -> tuple[str, str]:
    """Return ``(base_without_extension, extension)``.

    ``notes/log-2026.txt`` → ``("notes/log-2026", ".txt")``.
    Unrecognised extensions fall through with an empty ``extension``
    string so the caller can reject them.
    """
    m = _STRIP_EXT_RE.search(rel_path)
    if m is None:
        return rel_path, ""
    return rel_path[: m.start()], rel_path[m.start() :].lower()
