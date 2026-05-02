"""Tests for the section-aware LaTeX parser.

Covers `parse_tex` directly so block-grammar regressions surface
without needing the handler / store fixtures.
"""

from __future__ import annotations

from precis.utils.tex_parse import (
    TEX_SECTION_LEVELS,
    TexBlock,
    extract_inputs,
    parse_tex,
)

# ── empty / whitespace ───────────────────────────────────────────────


def test_parse_empty_returns_empty_list() -> None:
    assert parse_tex("") == []
    assert parse_tex("\n\n\n") == []
    assert parse_tex("   \n\t\n") == []


# ── plaintext-style block grammar (no sectioning) ────────────────────


def test_blank_line_paragraph_split_no_sections() -> None:
    """With no sectioning commands the parser must still split on
    blank lines, just like plaintext."""
    src = "first paragraph\nsecond line of first.\n\nsecond paragraph.\n"
    blocks = parse_tex(src)
    assert len(blocks) == 2
    assert blocks[0].text == "first paragraph\nsecond line of first."
    assert blocks[1].text == "second paragraph."
    # Section metadata is None / empty for plain paragraphs.
    for b in blocks:
        assert b.section_level is None
        assert b.section_title is None
        assert b.section_path == ()


# ── single-level sectioning ──────────────────────────────────────────


def test_section_command_starts_new_block() -> None:
    src = (
        r"\section{Introduction}"
        + "\n"
        + "First paragraph of intro.\n\n"
        + r"\section{Methods}"
        + "\n"
        + "Methods body.\n"
    )
    blocks = parse_tex(src)
    assert len(blocks) == 4
    # Section heading itself is its own one-line block.
    assert blocks[0].text == r"\section{Introduction}"
    assert blocks[0].section_level == 0
    assert blocks[0].section_title == "Introduction"
    # Body of intro is the next block.
    assert blocks[1].text == "First paragraph of intro."
    assert blocks[1].section_level is None
    assert blocks[1].section_path == ((0, "Introduction"),)
    # Second section heading + body.
    assert blocks[2].text == r"\section{Methods}"
    assert blocks[2].section_level == 0
    # Body of methods knows its parent section.
    assert blocks[3].section_path == ((0, "Methods"),)


def test_starred_section_form_is_recognised() -> None:
    """``\\section*{...}`` (no TOC entry in real LaTeX) still
    triggers a block boundary so editing granularity stays right."""
    blocks = parse_tex(r"\section*{Unnumbered}" + "\nbody.\n")
    assert blocks[0].section_level == 0
    assert blocks[0].section_title == "Unnumbered"


def test_section_with_optional_short_title() -> None:
    """``\\section[short]{long}`` — only the long form goes into
    section_title (mirrors how a TOC viewer would render the body)."""
    blocks = parse_tex(r"\section[Intro]{Introduction proper}" + "\n")
    assert blocks[0].section_title == "Introduction proper"


# ── nested sectioning ────────────────────────────────────────────────


def test_subsection_records_full_ancestry() -> None:
    src = r"\section{Outer}" + "\n\n" + r"\subsection{Inner}" + "\n" + "Inner body.\n"
    blocks = parse_tex(src)
    # outer section, inner subsection (which becomes its own block),
    # and inner body.
    titles = [(b.section_level, b.section_title) for b in blocks]
    assert (0, "Outer") in titles
    assert (1, "Inner") in titles
    # The body block sits inside both parents.
    body = [b for b in blocks if b.text.strip() == "Inner body."][0]
    assert body.section_path == ((0, "Outer"), (1, "Inner"))


def test_subsection_pops_when_returning_to_section() -> None:
    """A second \\section must pop the open \\subsection off the
    ancestor stack — the new section is a sibling, not a child."""
    src = (
        r"\section{One}"
        + "\n\n"
        + r"\subsection{One.A}"
        + "\n\n"
        + r"\section{Two}"
        + "\n"
        + "Body of two.\n"
    )
    blocks = parse_tex(src)
    body = [b for b in blocks if b.text.strip() == "Body of two."][0]
    # Body of "Two" lives only under "Two" — the previous subsection
    # has been popped.
    assert body.section_path == ((0, "Two"),)


def test_chapter_above_section_in_hierarchy() -> None:
    """``\\chapter`` is at level -1, above ``\\section`` (0). A
    section after a chapter must record the chapter as its parent."""
    src = r"\chapter{Big}" + "\n\n" + r"\section{Medium}" + "\n" + "body.\n"
    blocks = parse_tex(src)
    body = [b for b in blocks if b.text == "body."][0]
    assert body.section_path == ((-1, "Big"), (0, "Medium"))


def test_part_at_top_of_hierarchy() -> None:
    src = (
        r"\part{Part I}"
        + "\n\n"
        + r"\chapter{Ch}"
        + "\n\n"
        + r"\section{S}"
        + "\n\n"
        + r"\subsection{SS}"
        + "\n"
        + "leaf.\n"
    )
    blocks = parse_tex(src)
    leaf = [b for b in blocks if b.text == "leaf."][0]
    assert leaf.section_path == (
        (-2, "Part I"),
        (-1, "Ch"),
        (0, "S"),
        (1, "SS"),
    )


def test_all_levels_in_table_are_recognised() -> None:
    """Every command in TEX_SECTION_LEVELS must produce a section
    block — guards against future additions to the table without
    a corresponding regex update."""
    for command in TEX_SECTION_LEVELS:
        src = "\\" + command + "{hello}\n"
        blocks = parse_tex(src)
        assert len(blocks) == 1, f"{command!r} did not parse"
        assert blocks[0].section_level == TEX_SECTION_LEVELS[command]
        assert blocks[0].section_title == "hello"


# ── \input{} / \include{} extraction ────────────────────────────────


def test_input_in_paragraph_recorded_in_block_meta() -> None:
    src = (
        r"\section{Main}"
        + "\n\n"
        + r"\input{chapters/intro}"
        + "\n\n"
        + r"\input{chapters/methods.tex} more text \include{appendix}"
        + "\n"
    )
    blocks = parse_tex(src)
    # Find the block containing the multi-input line.
    multi = [b for b in blocks if "more text" in b.text][0]
    assert multi.inputs == ("chapters/methods.tex", "appendix")
    # The single-input block also records its target.
    solo = [
        b for b in blocks if "chapters/intro" in b.text and "more text" not in b.text
    ][0]
    assert solo.inputs == ("chapters/intro",)


def test_extract_inputs_helper_matches_block_inputs() -> None:
    src = r"prefix \input{a} mid \include{b/c} suffix \input{d.tex}"
    assert extract_inputs(src) == ["a", "b/c", "d.tex"]


def test_no_input_means_empty_tuple() -> None:
    blocks = parse_tex("plain paragraph with no commands.\n")
    assert blocks[0].inputs == ()


# ── line spans + slugs (sanity) ──────────────────────────────────────


def test_line_spans_match_source_positions() -> None:
    src = (
        "para one.\n"  # L1
        "still para one.\n"  # L2
        "\n"  # L3 (blank)
        r"\section{Two}" + "\n"  # L4
        "body of two.\n"  # L5
    )
    blocks = parse_tex(src)
    assert blocks[0].line_start == 1
    assert blocks[0].line_end == 2
    # Section heading.
    assert blocks[1].line_start == 4
    assert blocks[1].line_end == 4
    # Section body.
    assert blocks[2].line_start == 5
    assert blocks[2].line_end == 5


def test_slugs_are_stable_across_reparse() -> None:
    src = r"\section{Methods}" + "\n\n" + "Detailed analysis paragraph.\n"
    a = parse_tex(src)
    b = parse_tex(src)
    assert [bl.slug for bl in a] == [bl.slug for bl in b]


def test_slugs_unique_within_file() -> None:
    """Two paragraphs with identical leading words must still produce
    distinct slugs (hash suffix differentiates)."""
    src = "the quick brown fox jumps over.\n\nthe quick brown fox jumps under.\n"
    blocks = parse_tex(src)
    assert blocks[0].slug != blocks[1].slug


# ── TexBlock satisfies the PlaintextBlock duck-type ──────────────────


def test_texblock_satisfies_plaintextblock_protocol() -> None:
    """The block_ingest / _find_block helpers expect ``pos``, ``slug``,
    ``text``, ``line_start``, ``line_end`` on every parsed block.
    Confirm TexBlock keeps that shape after subclassing."""
    blocks = parse_tex(r"\section{x}" + "\nbody.\n")
    b = blocks[0]
    assert isinstance(b, TexBlock)
    for attr in ("pos", "slug", "text", "line_start", "line_end"):
        assert hasattr(b, attr), f"TexBlock missing required attr {attr}"
