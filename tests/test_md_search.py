"""Unit tests for `precis.md_index.search` — lexical block scoring.

Pure logic over an in-memory `MdRepoIndex`; no DB, no embedder.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from precis.md_index import index_repo
from precis.md_index.search import score_block, search_blocks


def _write(repo: Path, relpath: str, content: str) -> Path:
    file = repo / relpath
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return file


def test_no_match_scores_zero(tmp_path: Path) -> None:
    _write(tmp_path, "doc.md", "# Widgets\n\nAll about widgets.\n")
    idx = index_repo(tmp_path)
    file_entry = idx.file("doc.md")
    assert file_entry is not None
    para = file_entry.blocks[1]
    assert score_block(para, "gizmos") == 0.0


def test_empty_query_scores_zero(tmp_path: Path) -> None:
    _write(tmp_path, "doc.md", "# Widgets\n\nAll about widgets.\n")
    idx = index_repo(tmp_path)
    file_entry = idx.file("doc.md")
    assert file_entry is not None
    para = file_entry.blocks[1]
    assert score_block(para, "") == 0.0
    assert score_block(para, "   ") == 0.0


def test_heading_match_outweighs_body_match(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "doc.md",
        """
        # Deployment

        Notes about widgets and gizmos, nothing to do with the title.
        """,
    )
    idx = index_repo(tmp_path)
    file_entry = idx.file("doc.md")
    assert file_entry is not None
    heading, para = file_entry.blocks

    heading_score = score_block(heading, "deployment")
    body_score = score_block(para, "widgets")
    assert heading_score > body_score
    assert heading_score > 0
    assert body_score > 0


def test_body_block_inherits_heading_breadcrumb_score(tmp_path: Path) -> None:
    """A paragraph under 'Deployment' should score for a 'deployment'
    query even though the word never appears in its own body text —
    because its heading breadcrumb includes the ancestor title."""
    _write(
        tmp_path,
        "doc.md",
        """
        # Deployment

        This paragraph never repeats the section title.
        """,
    )
    idx = index_repo(tmp_path)
    file_entry = idx.file("doc.md")
    assert file_entry is not None
    _, para = file_entry.blocks

    assert "deployment" not in para.text.lower()
    assert score_block(para, "deployment") > 0.0


def test_multi_term_query_sums_term_hits(tmp_path: Path) -> None:
    _write(tmp_path, "doc.md", "# Title\n\nfoo bar baz.\n")
    idx = index_repo(tmp_path)
    file_entry = idx.file("doc.md")
    assert file_entry is not None
    para = file_entry.blocks[1]

    one_term = score_block(para, "foo")
    two_terms = score_block(para, "foo bar")
    assert two_terms > one_term


def test_search_blocks_ranks_and_filters(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        """
        # Deploying to prod

        How to ship after making changes.
        """,
    )
    _write(
        tmp_path,
        "b.md",
        """
        # Unrelated

        Nothing about the topic here.
        """,
    )
    idx = index_repo(tmp_path)

    hits = search_blocks(idx, "deploy")
    assert hits, "expected at least one hit"
    top_score, top_file, top_block = hits[0]
    assert top_file == "a.md"
    assert top_score > 0

    # b.md never matches.
    assert all(f != "b.md" for _, f, _ in hits)


def test_search_blocks_respects_limit(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "doc.md",
        """
        # Widgets

        widgets widgets widgets.

        More widgets prose here.

        Even more widgets prose here.
        """,
    )
    idx = index_repo(tmp_path)
    all_hits = search_blocks(idx, "widgets")
    assert len(all_hits) >= 3

    limited = search_blocks(idx, "widgets", limit=1)
    assert len(limited) == 1
    assert limited[0] == all_hits[0]


def test_search_blocks_deterministic_tie_order(tmp_path: Path) -> None:
    """Equal-scoring hits sort by (file, pos) so output is stable."""
    _write(tmp_path, "a.md", "# X\n\nsame same.\n")
    _write(tmp_path, "b.md", "# X\n\nsame same.\n")
    idx = index_repo(tmp_path)

    hits = search_blocks(idx, "same")
    files_in_order = [f for _, f, _ in hits if f in ("a.md", "b.md")]
    # a.md sorts before b.md at equal score.
    assert files_in_order.index("a.md") < files_in_order.index("b.md")
