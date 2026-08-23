"""Unit tests for `precis.md_index.indexer`.

Pure logic — no DB, no network. Each test writes a tiny markdown tree
to a tmp_path and indexes it. Mirrors the shape of
`tests/test_python_indexer.py`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.md_index import MdRepoIndex, index_file, index_repo
from precis.md_index.indexer import _walk_md_files


def _write(repo: Path, relpath: str, content: str) -> Path:
    """Write `content` (after dedent) to `repo / relpath`, creating parents."""
    file = repo / relpath
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return file


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------


def test_walk_finds_md_and_markdown_files(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "# A\n")
    _write(tmp_path, "b.markdown", "# B\n")
    _write(tmp_path, "c.txt", "not markdown\n")

    found = sorted(p.name for p in _walk_md_files(tmp_path))
    assert found == ["a.md", "b.markdown"]


def test_walk_skips_dot_dirs_and_skip_dirs(tmp_path: Path) -> None:
    _write(tmp_path, "keep/doc.md", "# Keep\n")
    _write(tmp_path, ".git/doc.md", "# Hidden\n")
    _write(tmp_path, "node_modules/doc.md", "# Cruft\n")
    _write(tmp_path, ".hidden/doc.md", "# Dotdir\n")

    found = sorted(p.relative_to(tmp_path).as_posix() for p in _walk_md_files(tmp_path))
    assert found == ["keep/doc.md"]


def test_walk_never_follows_symlinks(tmp_path: Path) -> None:
    # gr239368: a symlinked dir can loop the walk (self-link) or escape the
    # root (link to an outside tree); a symlinked file escapes it too.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text("# Leak\n", encoding="utf-8")
    root = tmp_path / "root"
    _write(root, "real.md", "# Real\n")
    (root / "loop").symlink_to(root, target_is_directory=True)
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "filelink.md").symlink_to(outside / "leak.md")

    found = sorted(p.relative_to(root).as_posix() for p in _walk_md_files(root))
    assert found == ["real.md"]


def test_walk_stable_sorted_order(tmp_path: Path) -> None:
    _write(tmp_path, "z.md", "# Z\n")
    _write(tmp_path, "a.md", "# A\n")
    _write(tmp_path, "m/b.md", "# B\n")

    found = [p.relative_to(tmp_path).as_posix() for p in _walk_md_files(tmp_path)]
    assert found == ["a.md", "m/b.md", "z.md"]


def test_walk_on_missing_root_yields_nothing(tmp_path: Path) -> None:
    assert list(_walk_md_files(tmp_path / "nope")) == []


# ---------------------------------------------------------------------------
# index_file / index_repo — shape
# ---------------------------------------------------------------------------


def test_index_repo_indexes_every_file(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "# A\n\nHello.\n")
    _write(tmp_path, "sub/b.md", "# B\n\nWorld.\n")

    idx = index_repo(tmp_path)
    assert isinstance(idx, MdRepoIndex)
    assert idx.n_files == 2
    assert set(idx.files) == {"a.md", "sub/b.md"}
    assert idx.root == tmp_path.resolve()


def test_index_repo_on_nonexistent_root_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        index_repo(tmp_path / "does-not-exist")


def test_index_file_splits_into_blocks(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "doc.md",
        """
        # Title

        A paragraph of prose.

        ```python
        x = 1
        ```
        """,
    )
    entry = index_file(path, file_relative="doc.md")
    assert entry.file == "doc.md"
    kinds = [b.kind for b in entry.blocks]
    assert kinds == ["heading", "paragraph", "code"]
    assert entry.n_blocks == 3


def test_block_sha256_is_stable_and_content_derived(tmp_path: Path) -> None:
    path = _write(tmp_path, "doc.md", "# Title\n\nSame text.\n")
    entry1 = index_file(path, file_relative="doc.md")
    entry2 = index_file(path, file_relative="doc.md")
    assert entry1.blocks[1].sha256 == entry2.blocks[1].sha256

    # Different text -> different hash.
    other = _write(tmp_path, "other.md", "# Title\n\nDifferent text.\n")
    entry3 = index_file(other, file_relative="other.md")
    assert entry3.blocks[1].sha256 != entry1.blocks[1].sha256


def test_file_block_lookup_by_slug(tmp_path: Path) -> None:
    path = _write(tmp_path, "doc.md", "# Title\n\nA paragraph.\n")
    entry = index_file(path, file_relative="doc.md")
    heading = entry.blocks[0]
    assert entry.block(heading.slug) is heading
    assert entry.block("does-not-exist") is None


def test_repo_index_all_blocks_and_counts(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "# A\n\nOne.\n")
    _write(tmp_path, "b.md", "# B\n\nTwo.\n\nThree.\n")

    idx = index_repo(tmp_path)
    pairs = idx.all_blocks()
    assert len(pairs) == idx.n_blocks == 5
    files_seen = {f for f, _ in pairs}
    assert files_seen == {"a.md", "b.md"}


# ---------------------------------------------------------------------------
# Heading breadcrumb computation
# ---------------------------------------------------------------------------


def test_heading_title_stripped_of_hashes(tmp_path: Path) -> None:
    path = _write(tmp_path, "doc.md", "## My Title ##\n")
    entry = index_file(path, file_relative="doc.md")
    assert entry.blocks[0].title == "My Title"


def test_heading_path_nesting(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "doc.md",
        """
        # Top

        Intro paragraph.

        ## Middle

        Middle paragraph.

        ### Deep

        Deep paragraph.
        """,
    )
    entry = index_file(path, file_relative="doc.md")

    top = next(b for b in entry.blocks if b.title == "Top")
    assert top.heading_path == ()

    intro = next(b for b in entry.blocks if b.kind == "paragraph" and "Intro" in b.text)
    assert intro.heading_path == ("Top",)

    middle = next(b for b in entry.blocks if b.title == "Middle")
    assert middle.heading_path == ("Top",)

    deep = next(b for b in entry.blocks if b.title == "Deep")
    assert deep.heading_path == ("Top", "Middle")

    deep_para = next(
        b for b in entry.blocks if b.kind == "paragraph" and "Deep paragraph" in b.text
    )
    assert deep_para.heading_path == ("Top", "Middle", "Deep")


def test_heading_path_pops_siblings_not_just_children(tmp_path: Path) -> None:
    """A same-or-shallower-level heading closes deeper open headings."""
    path = _write(
        tmp_path,
        "doc.md",
        """
        # Top

        ## A

        ### A1

        ## B

        Under B.
        """,
    )
    entry = index_file(path, file_relative="doc.md")
    b_heading = next(b for b in entry.blocks if b.title == "B")
    assert b_heading.heading_path == ("Top",)

    under_b = next(b for b in entry.blocks if b.kind == "paragraph")
    assert under_b.heading_path == ("Top", "B")


def test_nearest_heading_slug(tmp_path: Path) -> None:
    path = _write(tmp_path, "doc.md", "# Title\n\nA paragraph.\n")
    entry = index_file(path, file_relative="doc.md")
    heading, para = entry.blocks
    assert para.nearest_heading_slug == heading.slug
    assert heading.nearest_heading_slug == heading.slug


def test_block_with_no_heading_above_it(tmp_path: Path) -> None:
    path = _write(tmp_path, "doc.md", "No heading here, just prose.\n")
    entry = index_file(path, file_relative="doc.md")
    assert len(entry.blocks) == 1
    b = entry.blocks[0]
    assert b.heading_path == ()
    assert b.nearest_heading_slug is None
    assert b.title is None
