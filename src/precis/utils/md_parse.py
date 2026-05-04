"""Markdown parser — split a `.md` file into typed blocks.

Phase-6 v2 design. Much smaller than v1's 360 LOC parser:

- One regex for each block-level construct (heading, fenced code,
  table, list, thematic break).
- One block per logical chunk (heading line, paragraph,
  fenced-code block, list block, table block).
- 1-indexed `line_start` / `line_end` recorded so the handler can
  do in-place edits via the source text.
- Stable, deterministic per-block slugs derived from content (so
  re-ingestion preserves block identity even when surrounding
  paragraphs shift). Heading slugs come from the heading title;
  paragraph slugs from a 6-char content hash.

The parser is pure: input is text, output is a list of
:class:`MdBlock`. No DB, no IO. The handler wraps it.

Front-matter, footnotes, and embedded images are left in their
home block (not split out) — phase 6's job is *useful*
chunk-addressing, not a full Pandoc-compatible AST.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from precis.utils.slug import slug_from_text

BlockKind = Literal["heading", "paragraph", "code", "list", "table"]


# ── Regex toolkit ─────────────────────────────────────────────────────

# ATX headings: 1–6 hashes, then space, then title. We deliberately
# don't accept setext (=== / ---) headings — they're rare and
# trip up over thematic breaks.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

# Fenced code block. Group 1 = the fence char (` or ~), group 2 = the
# fence run (3+ of that char), group 3 = optional language.
_FENCE_RE = re.compile(r"^(?P<fence>(?P<ch>[`~])(?P=ch){2,})\s*(?P<lang>\S*)\s*$")

# Table: a row is `| ... |`. The separator row is what makes a real
# table (vs a paragraph that happens to start with `|`).
_TABLE_ROW_RE = re.compile(r"^\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\|[\s:|\-]+\|\s*$")

# Lists: ordered (1. / 1)) or unordered (-, *, +).
_LIST_ITEM_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+\S")

# Thematic break: --- / *** / ___ on its own line. We discard these.
_THEMATIC_BREAK_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")


@dataclass(frozen=True, slots=True)
class MdBlock:
    """A single addressable chunk of a markdown file."""

    pos: int
    """0-indexed sequential position in the file."""

    slug: str
    """Stable, content-derived slug. Survives re-ingest."""

    text: str
    """Raw source text of this block (newlines preserved)."""

    kind: BlockKind
    """What this block is."""

    heading_level: int | None = None
    """For headings only - the H-level (1..6)."""

    line_start: int = 0
    """1-indexed source line where this block starts."""

    line_end: int = 0
    """1-indexed source line where this block ends (inclusive)."""

    meta: dict[str, str] = field(default_factory=dict)
    """Per-kind metadata (e.g. ``{'lang': 'python'}`` for code blocks)."""


def block_meta(mb: MdBlock) -> dict[str, Any]:
    """Render an :class:`MdBlock` as the dict shape the store expects on
    :attr:`BlockInsert.meta`.

    Promoted from a private helper in ``handlers/markdown.py`` so the
    Perplexity handler (and any future markdown-fed kind) can share
    one block-meta builder rather than each re-deriving the layout.
    """
    out: dict[str, Any] = {
        "kind": mb.kind,
        "line_start": mb.line_start,
        "line_end": mb.line_end,
    }
    if mb.heading_level is not None:
        out["heading_level"] = mb.heading_level
    if mb.meta:
        out.update(mb.meta)
    return out


def parse_markdown(content: str) -> list[MdBlock]:
    """Split a markdown document into typed blocks.

    Idempotent: parsing the same content twice yields the same blocks
    with the same slugs. Empty input returns an empty list.
    """
    lines = content.splitlines()
    n = len(lines)
    out: list[MdBlock] = []
    taken: set[str] = set()
    i = 0
    pos = 0

    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # Blank line — skip.
        if not stripped:
            i += 1
            continue

        # Thematic break — drop entirely.
        if _THEMATIC_BREAK_RE.match(raw):
            i += 1
            continue

        # ── Heading ─────────────────────────────────────────────
        m_h = _HEADING_RE.match(stripped)
        if m_h:
            level = len(m_h.group(1))
            title = m_h.group(2).strip()
            slug = _mint_slug(title, "heading", taken)
            out.append(
                MdBlock(
                    pos=pos,
                    slug=slug,
                    text=raw,
                    kind="heading",
                    heading_level=level,
                    line_start=i + 1,
                    line_end=i + 1,
                )
            )
            pos += 1
            i += 1
            continue

        # ── Fenced code ────────────────────────────────────────
        m_f = _FENCE_RE.match(raw)
        if m_f:
            fence = m_f.group("fence")
            lang = m_f.group("lang") or ""
            start = i
            collected = [raw]
            i += 1
            while i < n:
                line = lines[i]
                collected.append(line)
                # The closing fence must be the same char and at least
                # as long as the opener.
                if line.strip().startswith(fence[0] * len(fence)) and (
                    line.strip() == line.strip()[0] * len(line.strip())
                ):
                    i += 1
                    break
                i += 1
            text = "\n".join(collected)
            slug = _mint_slug(text, "code", taken)
            out.append(
                MdBlock(
                    pos=pos,
                    slug=slug,
                    text=text,
                    kind="code",
                    line_start=start + 1,
                    line_end=i,
                    meta={"lang": lang} if lang else {},
                )
            )
            pos += 1
            continue

        # ── Table ──────────────────────────────────────────────
        # A table needs at least a row + separator row. We scan ahead
        # to confirm before committing.
        if _TABLE_ROW_RE.match(stripped):
            # Look ahead for a separator on the next non-blank line.
            j = i + 1
            if j < n and _TABLE_SEP_RE.match(lines[j].strip()):
                start = i
                table_lines = [raw, lines[j]]
                i = j + 1
                while i < n and _TABLE_ROW_RE.match(lines[i].strip()):
                    table_lines.append(lines[i])
                    i += 1
                text = "\n".join(table_lines)
                slug = _mint_slug(text, "table", taken)
                out.append(
                    MdBlock(
                        pos=pos,
                        slug=slug,
                        text=text,
                        kind="table",
                        line_start=start + 1,
                        line_end=start + len(table_lines),
                    )
                )
                pos += 1
                continue
            # No separator → treat as paragraph (fall through).

        # ── List ───────────────────────────────────────────────
        if _LIST_ITEM_RE.match(raw):
            start = i
            list_lines = [raw]
            i += 1
            # A list continues until a blank line OR a heading/fence.
            # Continuation lines (indented) and subsequent items both
            # join the same block.
            while i < n:
                ln = lines[i]
                if not ln.strip():
                    break
                if _HEADING_RE.match(ln.strip()):
                    break
                if _FENCE_RE.match(ln):
                    break
                list_lines.append(ln)
                i += 1
            text = "\n".join(list_lines)
            slug = _mint_slug(text, "list", taken)
            out.append(
                MdBlock(
                    pos=pos,
                    slug=slug,
                    text=text,
                    kind="list",
                    line_start=start + 1,
                    line_end=start + len(list_lines),
                )
            )
            pos += 1
            continue

        # ── Paragraph ──────────────────────────────────────────
        start = i
        para_lines = [raw]
        i += 1
        while i < n:
            ln = lines[i]
            if not ln.strip():
                break
            if _HEADING_RE.match(ln.strip()):
                break
            if _FENCE_RE.match(ln):
                break
            if _TABLE_ROW_RE.match(ln.strip()):
                break
            if _LIST_ITEM_RE.match(ln):
                break
            if _THEMATIC_BREAK_RE.match(ln):
                break
            para_lines.append(ln)
            i += 1
        text = "\n".join(para_lines)
        slug = _mint_slug(text, "paragraph", taken)
        out.append(
            MdBlock(
                pos=pos,
                slug=slug,
                text=text,
                kind="paragraph",
                line_start=start + 1,
                line_end=start + len(para_lines),
            )
        )
        pos += 1

    return out


# ── Slug minting ─────────────────────────────────────────────────────


def _mint_slug(text: str, kind: BlockKind, taken: set[str]) -> str:
    """Return a stable, unique slug for a block.

    Headings use a slugified title. Other kinds use 5 leading words +
    a 6-char content hash. Collisions inside the same file are
    disambiguated with a numeric suffix.
    """
    if kind == "heading":
        # Strip leading hashes from heading text before slugifying.
        title = text.lstrip("#").strip()
        base = slug_from_text(title, max_len=40)
    else:
        # Strip markdown decoration that would bias the slug.
        clean = _strip_md_decoration(text)
        first_words = " ".join(clean.split()[:5])
        base = slug_from_text(first_words, max_len=24)

    # Always append a content hash for non-heading kinds. This makes
    # block identity content-stable: two paragraphs with the same
    # leading words get *different* slugs.
    if kind != "heading":
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
        base = f"{base}-{h}" if base else f"p-{h}"

    if not base:
        # Defensive: pure-symbol heading fallback.
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
        base = f"h-{h}"

    if base not in taken:
        taken.add(base)
        return base

    # Collision (rare for non-heading; common for "Conclusion" etc.).
    for n in range(2, 10000):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise ValueError(f"unreachable: more than 10k collisions on {base!r}")


_DECORATION_RE = re.compile(r"[*_`~\[\]()!#>]+")


def _strip_md_decoration(text: str) -> str:
    """Remove markdown decoration so the slug looks human-readable."""
    return _DECORATION_RE.sub(" ", text)


# ── File-slug helpers ────────────────────────────────────────────────


_FILE_SEP = "--"
_FILE_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")


def file_slug_from_path(rel_path: str) -> str:
    """Encode a relative file path as a file-kind ref slug.

    ``notes/meeting-2024.md`` → ``notes--meeting-2024``
    ``deep/nested/foo.md``   → ``deep--nested--foo``

    The extension is stripped. Path separators become ``--``.
    Each segment is normalized (lowercased, `_` and `-` preserved,
    other chars become `-`). Empty segments are dropped.
    """
    # Strip extension.
    if "." in rel_path:
        base, _, _ext = rel_path.rpartition(".")
    else:
        base = rel_path
    parts = [p for p in base.replace("\\", "/").split("/") if p]
    normalized = [_normalize_segment(p) for p in parts]
    normalized = [s for s in normalized if s]
    if not normalized:
        raise ValueError(f"no usable path segments in {rel_path!r}")
    return _FILE_SEP.join(normalized)


def path_from_file_slug(slug: str, ext: str = ".md") -> str:
    """Inverse of :func:`file_slug_from_path` (best-effort).

    Any segment-internal hyphens are preserved; only the ``--``
    separator is interpreted as a directory boundary.
    """
    parts = slug.split(_FILE_SEP)
    return "/".join(parts) + ext


def is_valid_file_slug(slug: str) -> bool:
    """A file-kind ref slug must look like a slug (no path traversal)."""
    if not slug:
        return False
    # No '..' segments — defence in depth against path traversal even
    # though the handler also resolves+checks against the configured
    # root.
    for seg in slug.split(_FILE_SEP):
        if not _FILE_SLUG_RE.match(seg):
            return False
        if seg in ("", ".", ".."):
            return False
    return True


def _normalize_segment(seg: str) -> str:
    """Lowercase + collapse non-[a-z0-9_-] to '-' + trim hyphens."""
    out = []
    for ch in seg.lower():
        if ch.isalnum() or ch in ("_", "-"):
            out.append(ch)
        else:
            out.append("-")
    s = "".join(out).strip("-_")
    # Collapse runs of '-' or '_' for niceness.
    return re.sub(r"[-_]{2,}", "-", s)
