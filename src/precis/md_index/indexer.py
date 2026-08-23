"""Markdown-tree indexer.

`index_repo(root)` walks a directory, splits every `.md`/`.markdown`
file into blocks with `precis.utils.md_parse.parse_markdown`, and
builds an `MdRepoIndex`. `index_file(...)` parses one file and is a
useful unit-test seam (mirrors `precis.python_index.indexer.
index_module`).

The walk skips a fixed set of cruft directories and any directory
whose name starts with `.` — the same `_SKIP_DIRS` set the `python`
kind uses. It is **deliberately copied here rather than imported**:
the two kinds index different file types for different purposes, and
a shared import would wire an otherwise-independent module together
for the sake of five lines that rarely change. If the two constants
ever need to diverge (e.g. an md-only build directory), copying means
that's a one-line, one-module change.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path

from precis.md_index.types import MdBlockEntry, MdFileEntry, MdRepoIndex
from precis.utils.md_parse import MdBlock, parse_markdown

log = logging.getLogger(__name__)


# Directories that are never markdown under management. Matched by
# name only. See module docstring for why this duplicates
# `precis.python_index.indexer._SKIP_DIRS` instead of importing it.
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".env",
        "env",
        "node_modules",
        "dist",
        "build",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "site-packages",
        ".eggs",
    }
)

# Extensions treated as markdown. Both are common in this repo and in
# the wild; `.markdown` is rare but cheap to accept.
_MD_SUFFIXES = frozenset({".md", ".markdown"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def index_repo(root: Path) -> MdRepoIndex:
    """Build an `MdRepoIndex` for every `.md`/`.markdown` file under `root`."""
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    files: list[MdFileEntry] = []
    for md_file in _walk_md_files(root):
        try:
            file_relative = md_file.relative_to(root).as_posix()
        except ValueError:
            continue
        files.append(index_file(md_file, file_relative=file_relative))

    return MdRepoIndex.build(root=root, files=files)


def index_file(path: Path, *, file_relative: str) -> MdFileEntry:
    """Parse one markdown file into an `MdFileEntry`.

    `file_relative` is what's recorded on each block's `file` field —
    the caller (walker or cache) supplies it relative to whatever root
    is in play.
    """
    source = path.read_text(encoding="utf-8")
    blocks = parse_markdown(source)
    return MdFileEntry(
        file=file_relative,
        blocks=tuple(_build_entries(blocks, file_relative=file_relative)),
    )


# ---------------------------------------------------------------------------
# Block-entry construction — heading breadcrumb bookkeeping.
# ---------------------------------------------------------------------------


def _build_entries(blocks: list[MdBlock], *, file_relative: str) -> list[MdBlockEntry]:
    """Turn `md_parse` blocks into `MdBlockEntry` rows, computing each
    block's heading breadcrumb along the way.

    Maintains a stack of `(level, title, slug)` for headings currently
    "open" (i.e. every ancestor of the block being processed). On each
    heading, pop entries whose level is `>=` the new heading's level —
    they've been closed by this heading — before reading off the
    breadcrumb, then push the new heading. Non-heading blocks read the
    breadcrumb without touching the stack.
    """
    out: list[MdBlockEntry] = []
    stack: list[tuple[int, str, str]] = []  # (level, title, slug)

    for mb in blocks:
        if mb.kind == "heading":
            level = mb.heading_level or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            heading_path = tuple(title for _, title, _ in stack)
            title = _heading_title(mb.text)
            nearest_slug: str | None = mb.slug
            out.append(
                _entry_from_block(
                    mb,
                    file_relative=file_relative,
                    title=title,
                    heading_path=heading_path,
                    nearest_heading_slug=nearest_slug,
                )
            )
            stack.append((level, title, mb.slug))
        else:
            heading_path = tuple(title for _, title, _ in stack)
            nearest_slug = stack[-1][2] if stack else None
            out.append(
                _entry_from_block(
                    mb,
                    file_relative=file_relative,
                    title=None,
                    heading_path=heading_path,
                    nearest_heading_slug=nearest_slug,
                )
            )

    return out


def _entry_from_block(
    mb: MdBlock,
    *,
    file_relative: str,
    title: str | None,
    heading_path: tuple[str, ...],
    nearest_heading_slug: str | None,
) -> MdBlockEntry:
    sha = hashlib.sha256(mb.text.encode("utf-8")).hexdigest()
    return MdBlockEntry(
        file=file_relative,
        pos=mb.pos,
        slug=mb.slug,
        kind=mb.kind,
        heading_level=mb.heading_level,
        title=title,
        heading_path=heading_path,
        nearest_heading_slug=nearest_heading_slug,
        text=mb.text,
        line_start=mb.line_start,
        line_end=mb.line_end,
        sha256=sha,
        meta=dict(mb.meta),
    )


def _heading_title(raw: str) -> str:
    """Extract the title text from a raw ATX heading line.

    `md_parse`'s heading regex already validated the shape (1-6 `#`s,
    a space, the title, optional trailing `#`s) — this just strips
    the decoration back off. `'## Title ##'` -> `'Title'`.
    """
    s = raw.strip().lstrip("#").strip()
    return s.rstrip("#").strip()


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------


def _walk_md_files(root: Path) -> Iterable[Path]:
    """Yield every `.md`/`.markdown` file under `root`, depth-first,
    sorted within each directory for stable output order. Skips
    `_SKIP_DIRS` and any directory whose name starts with `.`.

    Symlinks are never followed: `root` is a sandbox boundary, and a
    symlinked directory can loop the walk or escape it entirely.
    """
    if not root.is_dir():
        return
    entries = sorted(root.iterdir(), key=lambda p: p.name)
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            yield from _walk_md_files(entry)
        elif entry.is_file() and entry.suffix in _MD_SUFFIXES:
            yield entry
