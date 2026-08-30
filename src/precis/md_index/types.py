"""Dataclasses for the in-memory markdown index.

An `MdRepoIndex` is a snapshot of every indexable `.md`/`.markdown` file
under one configured root. It contains zero or more `MdFileEntry` rows
(one per file), each of which contains zero or more `MdBlockEntry` rows
(one per block, as split by `precis.utils.md_parse.parse_markdown`:
headings, paragraphs, fenced code, lists, tables).

Blocks carry a *heading breadcrumb* (`heading_path`) — the titles of
every ancestor heading, outermost first — computed once at index time
by walking a file's blocks in order and tracking a stack of open
headings by level. This is what lets lexical (and later semantic)
search weight a hit by the section it lives under without re-walking
the file at query time.

Line numbers are 1-indexed and inclusive on both ends, matching
`precis.utils.md_parse.MdChunk` and the unified addressing convention
used elsewhere (see `precis.python_index.types`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from precis.utils.md_parse import BlockKind


@dataclass(frozen=True, slots=True)
class MdBlockEntry:
    """One addressable block of one indexed markdown file.

    `file` is the path relative to the repo root, forward-slashed.
    `pos` is the block's 0-indexed sequential position within the file
    (matches `MdChunk.pos`). `slug` is the stable, content-derived
    per-file block slug minted by `md_parse` — unique within `file`.

    `title` is set only for `kind == 'heading'`: the heading text with
    the leading `#`s and any trailing decoration stripped. For every
    other kind it is `None`.

    `heading_path` is the breadcrumb of ancestor heading titles
    (outermost first) this block is nested under — *not* including
    the block's own title if it is itself a heading. A block at the
    top of a file with no heading above it gets an empty tuple.

    `nearest_heading_slug` is the `md_parse` slug of the closest
    enclosing heading (or the block's own slug, if the block itself is
    a heading). `None` only for blocks with no heading above them
    anywhere in the file. Handlers can use this to build a
    `file#heading-slug` style address without re-deriving the tree.

    `sha256` is the hex digest of `text` — the unit the (round-2)
    vector cache content-addresses by, so a block's embedding survives
    unrelated edits elsewhere in the same file.
    """

    file: str
    pos: int
    slug: str
    kind: BlockKind
    heading_level: int | None
    title: str | None
    heading_path: tuple[str, ...]
    nearest_heading_slug: str | None
    text: str
    line_start: int
    line_end: int
    sha256: str
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MdFileEntry:
    """One indexed markdown file: its relative path + ordered blocks."""

    file: str
    blocks: tuple[MdBlockEntry, ...]

    def block(self, slug: str) -> MdBlockEntry | None:
        """Look up a block within this file by its `md_parse` slug."""
        for b in self.blocks:
            if b.slug == slug:
                return b
        return None

    @property
    def n_blocks(self) -> int:
        return len(self.blocks)


@dataclass(frozen=True, slots=True)
class MdRepoIndex:
    """All `.md`/`.markdown` files under one configured root, indexed.

    `root` is the absolute path of the repo root. `files` is keyed by
    the repo-relative path (forward-slashed), same convention as
    `precis.python_index.types.RepoIndex.file`.
    """

    root: Path
    files: dict[str, MdFileEntry] = field(default_factory=dict)

    @classmethod
    def build(cls, root: Path, files: list[MdFileEntry]) -> MdRepoIndex:
        """Construct an `MdRepoIndex` from a list of pre-built files."""
        return cls(root=root, files={f.file: f for f in files})

    def file(self, relative_path: str) -> MdFileEntry | None:
        return self.files.get(relative_path)

    def all_blocks(self) -> list[tuple[str, MdBlockEntry]]:
        """Every `(file, block)` pair across the whole index, file by
        file, in each file's block order."""
        out: list[tuple[str, MdBlockEntry]] = []
        for entry in self.files.values():
            for b in entry.blocks:
                out.append((entry.file, b))
        return out

    @property
    def n_files(self) -> int:
        return len(self.files)

    @property
    def n_blocks(self) -> int:
        return sum(f.n_blocks for f in self.files.values())
