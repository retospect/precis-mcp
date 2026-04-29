"""Metadata-as-heading rejection — pin the `**DOI:…**` regression.

The MCP critic flagged a paper where 357 of 460 blocks landed under a
single ``DOI: 10.1002/...`` pseudo-heading. ``_paper_toc.detect_heading``
now applies an anti-pattern filter to weed publisher metadata blocks
out of the heading set; this file verifies that filter end-to-end.

Pure logic, no DB. Synthetic blocks only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from precis.handlers._paper_toc import (
    _is_metadata_title,
    build_toc,
    detect_heading,
)
from precis.store.types import Block


def _block(pos: int, text: str) -> Block:
    now = datetime.now(UTC)
    return Block(
        id=pos + 1,
        ref_id=1,
        pos=pos,
        slug=None,
        text=text,
        token_count=len(text.split()),
        embedding=None,
        density=None,
        meta={},
        created_at=now,
        updated_at=now,
    )


# ── unit: _is_metadata_title ────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    [
        "DOI: 10.1002/anie.202405123",
        "doi: 10.1002/anie.202405123",
        "Keywords: photocatalysis, NOx reduction",
        "Keyword: single",
        "Authors: A. Clark, D. Chalmers",
        "Author: J. Doe",
        "Authorship: contribution statement",
        "Affiliation: University of Limerick",
        "Affiliations: 1 University of Limerick",
        "Received: 12 March 2024",
        "Accepted: 14 May 2024",
        "Published: online 2024",
        "Available online 1 June 2024",
        "Corresponding author: jane@example.com",
        "Email: jane@example.com",
        "E-mail: jane@example.com",
        "ORCID: 0000-0001-...",
        "Cite this article: Smith et al.",
        "Copyright © 2024 ACS",
        "© 2024 Elsevier",
        "License: CC-BY-4.0",
        "Funding: this work was supported …",
        "Conflict of interest statement",
        "Supplementary material",
        "Article history",
        "Submitted 1 January 2024",
        "Revised 1 March 2024",
    ],
)
def test_metadata_titles_rejected(title: str) -> None:
    assert _is_metadata_title(title), f"expected metadata: {title!r}"


@pytest.mark.parametrize(
    "title",
    [
        "Abstract",
        "Introduction",
        "Methods",
        "Results",
        "Discussion",
        "Conclusion",
        "Conclusions",
        "References",
        "Materials and Methods",
        "Computational Details",
        "Heterodiatomic Molecules",
        "Z-scheme photocatalysts",
        "DOI tracking subsection title",
    ],
)
def test_real_section_titles_accepted(title: str) -> None:
    assert not _is_metadata_title(title), f"expected heading-shaped: {title!r}"


def test_doi_anywhere_in_title_rejected() -> None:
    assert _is_metadata_title("Some heading 10.1002/anie.202405123 trailing")


def test_url_anywhere_in_title_rejected() -> None:
    assert _is_metadata_title("Visit https://example.com for more")
    assert _is_metadata_title("Heading mentioning http://x.org reference")


def test_overlong_title_rejected() -> None:
    long = "A" * 61
    assert _is_metadata_title(long)
    assert not _is_metadata_title("A" * 60)


# ── integration: detect_heading ─────────────────────────────────────


def test_doi_block_not_treated_as_heading() -> None:
    """The flagship case from the MCP critic finding."""
    block = _block(pos=2, text="**DOI: 10.1002/anie.202405123**")
    assert detect_heading(block) is None


def test_keywords_block_not_treated_as_heading() -> None:
    block = _block(pos=3, text="**Keywords: photocatalysis, NOx reduction**")
    assert detect_heading(block) is None


def test_received_date_block_not_treated_as_heading() -> None:
    block = _block(pos=4, text="**Received: 12 Mar 2024; Accepted: 14 May 2024**")
    # rejected on length cap or metadata-lead — either path is fine
    assert detect_heading(block) is None


def test_h1_marker_with_doi_text_still_rejected() -> None:
    """An ``■ **DOI…**`` artefact must still be filtered, even though
    the H1 marker is normally a strong gate."""
    block = _block(pos=1, text="■ **DOI: 10.1002/anie.x**")
    assert detect_heading(block) is None


def test_abstract_still_a_heading() -> None:
    block = _block(pos=4, text="**Abstract**")
    h = detect_heading(block)
    assert h is not None and h.level == 2 and h.title == "Abstract"


def test_introduction_h1_still_a_heading() -> None:
    block = _block(pos=10, text="■ **INTRODUCTION**")
    h = detect_heading(block)
    assert h is not None and h.level == 1 and h.title == "INTRODUCTION"


def test_md_h2_with_metadata_title_rejected() -> None:
    """Markdown fallback path also runs through the anti-pattern filter."""
    block = _block(pos=2, text="## DOI: 10.1002/anie.202405123")
    assert detect_heading(block) is None


def test_md_h1_real_title_accepted() -> None:
    block = _block(pos=0, text="# A study of nanobuds")
    h = detect_heading(block)
    assert h is not None and h.level == 1


# ── regression: full TOC against a metadata-laden synthetic paper ────


def test_toc_does_not_swallow_real_sections_under_metadata_block() -> None:
    """Pin the 357-of-460 case.

    Synthetic 20-block paper: 4 metadata blocks (DOI, Keywords,
    Authors, Received), 1 ``**Abstract**``, 1 ``■ **INTRODUCTION**``,
    14 body blocks. Pre-fix the TOC would group everything under
    ``DOI:``; post-fix we expect exactly two sections — the abstract
    H2 (top-level since no H1 precedes it) plus the INTRODUCTION H1.
    """
    blocks: list[Block] = []
    blocks.append(_block(0, "**DOI: 10.1002/anie.202405123**"))
    blocks.append(_block(1, "**Keywords: photocatalysis, NOx**"))
    blocks.append(_block(2, "**Authors: A. Clark, D. Chalmers**"))
    blocks.append(_block(3, "**Received: 12 Mar 2024**"))
    blocks.append(_block(4, "**Abstract**"))
    blocks.append(_block(5, "Lorem ipsum abstract body."))
    blocks.append(_block(6, "■ **INTRODUCTION**"))
    for pos in range(7, 20):
        blocks.append(_block(pos, f"Body paragraph {pos}."))

    toc = build_toc(blocks)

    # Top-level: implicit untitled (the metadata blocks 0-3),
    # then Abstract H2, then INTRODUCTION H1.
    titles = [s.title for s in toc]
    levels = [s.level for s in toc]
    assert "INTRODUCTION" in titles
    assert "Abstract" in titles
    # Abstract must NOT own the entire rest of the paper.
    abstract = next(s for s in toc if s.title == "Abstract")
    assert abstract.end < 6, (
        f"Abstract section leaked past its block (end={abstract.end}); "
        "metadata-rejection regressed."
    )
    # The DOI block must not appear as a section title.
    for s in toc:
        assert "DOI" not in s.title
        assert "10.1002" not in s.title


def test_toc_metadata_only_yields_no_real_sections() -> None:
    """A bundle that's *only* publisher metadata renders as the
    implicit untitled section — not a wall of bogus headings."""
    blocks = [
        _block(0, "**DOI: 10.1002/x**"),
        _block(1, "**Keywords: a, b**"),
        _block(2, "**Authors: J. Doe**"),
    ]
    toc = build_toc(blocks)
    # Exactly one implicit section spanning the whole range — no
    # fake DOI/Keywords/Authors headings.
    assert len(toc) == 1
    assert toc[0].title == ""
    assert toc[0].level == 0
    assert toc[0].start == 0 and toc[0].end == 2
