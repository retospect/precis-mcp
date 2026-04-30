"""Unit tests for ``precis.utils.edit_resolve``.

Pure tests — no I/O, no fixtures, no DB. Cover the resolution
algorithm: literal find, anchor filter, match policies, error shapes,
splice correctness, line-number arithmetic.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.utils.edit_resolve import (
    DRY_RUN_MODES,
    EditOp,
    apply_edit,
    classify_diff_hunks,
    find_candidates,
    format_unified_diff,
    normalize_dry_run,
    render_dry_run_full,
    render_dry_run_header,
)

# ---------------------------------------------------------------------------
# EditOp construction validation
# ---------------------------------------------------------------------------


def test_editop_rejects_unknown_op() -> None:
    with pytest.raises(BadInput, match="unknown edit op"):
        EditOp(op="rewrite", find="x", text="y")  # type: ignore[arg-type]


def test_editop_requires_nonempty_find() -> None:
    with pytest.raises(BadInput, match="find= is required"):
        EditOp(op="edit", find="", text="y")


def test_editop_rejects_unknown_match_policy() -> None:
    with pytest.raises(BadInput, match="unknown match policy"):
        EditOp(op="edit", find="x", text="y", match="random")  # type: ignore[arg-type]


def test_editop_nth_requires_match_nth() -> None:
    with pytest.raises(BadInput, match="only valid with match='nth'"):
        EditOp(op="edit", find="x", text="y", nth=2)


def test_editop_match_nth_requires_nth_value() -> None:
    with pytest.raises(BadInput, match="match='nth' requires nth="):
        EditOp(op="edit", find="x", text="y", match="nth")


def test_editop_match_nth_rejects_zero_or_negative() -> None:
    with pytest.raises(BadInput, match="positive int"):
        EditOp(op="edit", find="x", text="y", match="nth", nth=0)


def test_editop_insert_requires_where() -> None:
    with pytest.raises(BadInput, match="requires where="):
        EditOp(op="insert", find="x", text="y")


def test_editop_insert_rejects_match_all() -> None:
    with pytest.raises(BadInput, match="does not allow match='all'"):
        EditOp(op="insert", find="x", text="y", where="before", match="all")


def test_editop_edit_rejects_where() -> None:
    with pytest.raises(BadInput, match="only valid for mode='insert'"):
        EditOp(op="edit", find="x", text="y", where="before")


# ---------------------------------------------------------------------------
# Literal find — basic cases
# ---------------------------------------------------------------------------


def test_finds_single_literal_match() -> None:
    op = EditOp(op="edit", find="fox", text="cat")
    cands = find_candidates("the fox jumps over the fence.", op)
    assert len(cands) == 1
    assert cands[0].start == 4
    assert cands[0].end == 7
    assert cands[0].line_no == 1


def test_finds_no_match_when_absent() -> None:
    op = EditOp(op="edit", find="dog", text="cat")
    cands = find_candidates("the fox jumps over the fence.", op)
    assert cands == []


def test_finds_multiple_matches_in_order() -> None:
    op = EditOp(op="edit", find="the", text="a", match="all")
    cands = find_candidates("the fox jumps over the fence.", op)
    assert len(cands) == 2
    assert [c.start for c in cands] == [0, 19]


def test_overlapping_matches_all_seen() -> None:
    """find='aa' in 'aaaa' yields 3 candidates: positions 0, 1, 2."""
    op = EditOp(op="edit", find="aa", text="b", match="all")
    cands = find_candidates("aaaa", op)
    assert [c.start for c in cands] == [0, 1, 2]


def test_line_numbers_are_1_indexed() -> None:
    buffer = "line1\nline2\nline3 has fox\nline4\n"
    op = EditOp(op="edit", find="fox", text="cat")
    cands = find_candidates(buffer, op)
    assert len(cands) == 1
    assert cands[0].line_no == 3


def test_line_numbers_respect_base_line() -> None:
    buffer = "alpha\nbeta has fox\n"
    op = EditOp(op="edit", find="fox", text="cat", base_line=42)
    cands = find_candidates(buffer, op)
    assert cands[0].line_no == 43  # local L2 → absolute L43 (42 + 2 - 1)


# ---------------------------------------------------------------------------
# Anchor filter
# ---------------------------------------------------------------------------


def test_before_anchor_filters_to_one() -> None:
    """'over <the> fence' is the disambiguating example from the spec."""
    op = EditOp(op="edit", find="the", text="a", before="over ")
    cands = find_candidates("the fox jumps over the fence.", op)
    assert len(cands) == 1
    assert cands[0].start == 19


def test_after_anchor_filters_to_one() -> None:
    op = EditOp(op="edit", find="the", text="a", after=" fence")
    cands = find_candidates("the fox jumps over the fence.", op)
    assert len(cands) == 1
    assert cands[0].start == 19


def test_before_and_after_anchors_combined() -> None:
    op = EditOp(op="edit", find="the", text="a", before="over ", after=" fence")
    cands = find_candidates("the fox jumps over the fence.", op)
    assert len(cands) == 1


def test_anchor_at_buffer_start_is_rejected_when_before_too_long() -> None:
    """A `before` anchor that doesn't fit before the match position
    correctly filters that candidate out."""
    op = EditOp(op="edit", find="the", text="a", before="XYZ ")
    cands = find_candidates("the fox", op)
    # No 'XYZ ' precedes 'the' anywhere → 0 candidates.
    assert cands == []


def test_anchor_strict_whitespace() -> None:
    """before='over ' (one space) does not match 'over  ' (two)."""
    op = EditOp(op="edit", find="the", text="a", before="over ")
    cands = find_candidates("over  the fence", op)
    # The literal anchor 'over ' (5 chars) before 'the' would require
    # bytes 'over ' immediately preceding 'the' at offset 6 → bytes
    # [1..6] = 'ver  '. No match.
    assert cands == []


# ---------------------------------------------------------------------------
# select_candidates / apply_edit — match policies
# ---------------------------------------------------------------------------


def test_match_unique_passes_with_one_match() -> None:
    op = EditOp(op="edit", find="fox", text="cat")
    result = apply_edit("the fox jumps", op)
    assert result.new_buffer == "the cat jumps"
    assert result.n_matches == 1


def test_match_unique_errors_with_multiple_matches() -> None:
    op = EditOp(op="edit", find="the", text="a")
    with pytest.raises(BadInput) as excinfo:
        apply_edit("the fox over the fence", op)
    msg = str(excinfo.value)
    assert "2 matches" in msg
    assert "match='unique' requires exactly 1" in msg
    # The error message lists each candidate's line number.
    assert "L1" in msg


def test_match_unique_error_lists_candidates() -> None:
    op = EditOp(op="edit", find="x", text="y")
    buffer = "x first\nx second\nx third\n"
    with pytest.raises(BadInput) as excinfo:
        apply_edit(buffer, op)
    msg = str(excinfo.value)
    assert "L1" in msg and "L2" in msg and "L3" in msg


def test_match_first_picks_earliest() -> None:
    op = EditOp(op="edit", find="the", text="a", match="first")
    result = apply_edit("the fox over the fence", op)
    assert result.new_buffer == "a fox over the fence"


def test_match_all_replaces_every_occurrence() -> None:
    op = EditOp(op="edit", find="the", text="a", match="all")
    result = apply_edit("the fox over the fence", op)
    assert result.new_buffer == "a fox over a fence"
    assert result.n_matches == 2


def test_match_nth_picks_specified() -> None:
    op = EditOp(op="edit", find="the", text="a", match="nth", nth=2)
    result = apply_edit("the fox over the fence", op)
    assert result.new_buffer == "the fox over a fence"


def test_match_nth_errors_when_out_of_range() -> None:
    op = EditOp(op="edit", find="the", text="a", match="nth", nth=5)
    with pytest.raises(BadInput, match="nth=5 but only 2"):
        apply_edit("the fox over the fence", op)


# ---------------------------------------------------------------------------
# Not-found error — actionable hints
# ---------------------------------------------------------------------------


def test_not_found_error_carries_region_label() -> None:
    op = EditOp(op="edit", find="missing", text="x", region_label="notes/foo.md~intro")
    with pytest.raises(BadInput) as excinfo:
        apply_edit("hello world", op)
    msg = str(excinfo.value)
    assert "notes/foo.md~intro" in msg
    assert "'missing'" in msg


def test_not_found_error_includes_fuzzy_nearest() -> None:
    """When the literal isn't found, the error should suggest the
    closest line."""
    op = EditOp(op="edit", find="dpoamine", text="dopamine")
    buffer = "dopamine is a neurotransmitter\nserotonin is too\n"
    with pytest.raises(BadInput) as excinfo:
        apply_edit(buffer, op)
    msg = str(excinfo.value)
    # Best fuzzy match should be the dopamine line.
    assert "dopamine" in msg


# ---------------------------------------------------------------------------
# Splice correctness
# ---------------------------------------------------------------------------


def test_apply_edit_replaces_text_in_place() -> None:
    op = EditOp(op="edit", find="2020", text="2024")
    result = apply_edit("see foo et al. (2020) for context", op)
    assert result.new_buffer == "see foo et al. (2024) for context"


def test_apply_edit_with_empty_text_deletes_match() -> None:
    op = EditOp(op="edit", find=" deprecated", text="")
    result = apply_edit("call deprecated function", op)
    assert result.new_buffer == "call function"


def test_apply_edit_match_all_handles_offset_shift_correctly() -> None:
    """When replacing many matches with longer text, byte offsets
    must not drift. Splice end-to-start is the correctness check."""
    op = EditOp(op="edit", find="x", text="LONGER", match="all")
    result = apply_edit("x x x", op)
    assert result.new_buffer == "LONGER LONGER LONGER"


def test_apply_edit_no_op_raises() -> None:
    """find=text=text is a no-op and should error rather than silently
    succeed."""
    op = EditOp(op="edit", find="hello", text="hello")
    with pytest.raises(BadInput, match="no change"):
        apply_edit("hello world", op)


# ---------------------------------------------------------------------------
# Insert mode
# ---------------------------------------------------------------------------


def test_insert_before_puts_text_in_front_of_anchor() -> None:
    op = EditOp(op="insert", find="fence", text="big ", where="before")
    result = apply_edit("over the fence", op)
    assert result.new_buffer == "over the big fence"


def test_insert_after_puts_text_behind_anchor() -> None:
    op = EditOp(op="insert", find="fence", text=" yard", where="after")
    result = apply_edit("over the fence", op)
    assert result.new_buffer == "over the fence yard"


def test_insert_uses_anchor_for_disambiguation() -> None:
    """Insert before 'the' specifically the one near 'fence'."""
    op = EditOp(
        op="insert",
        find="the",
        text="VERY_",
        where="before",
        before="over ",
    )
    result = apply_edit("the fox jumps over the fence", op)
    assert result.new_buffer == "the fox jumps over VERY_the fence"


# ---------------------------------------------------------------------------
# Edited spans report
# ---------------------------------------------------------------------------


def test_edited_spans_single_line() -> None:
    op = EditOp(op="edit", find="fox", text="cat")
    result = apply_edit("the fox jumps", op)
    assert result.edited_spans == ((1, 1),)


def test_edited_spans_match_all_returns_in_order() -> None:
    op = EditOp(op="edit", find="x", text="y", match="all")
    buffer = "x line1\nthen\nx line3\n"
    result = apply_edit(buffer, op)
    # Two spans, in document order (line 1, line 3).
    assert result.edited_spans == ((1, 1), (3, 3))


def test_edited_spans_multiline_replacement() -> None:
    """Replacement text adds newlines → end_line shifts by that count."""
    op = EditOp(op="edit", find="X", text="A\nB\nC")
    result = apply_edit("at L1\nthen X\nlater\n", op)
    # X is on L2; replacement adds 2 newlines so the post-edit span
    # covers L2 through L4.
    assert result.edited_spans == ((2, 4),)


def test_edited_spans_respect_base_line() -> None:
    op = EditOp(op="edit", find="fox", text="cat", base_line=100)
    result = apply_edit("alpha\nthe fox\n", op)
    # 'fox' is local L2 → absolute L101.
    assert result.edited_spans == ((101, 101),)


# ---------------------------------------------------------------------------
# normalize_dry_run — coercion + validation
# ---------------------------------------------------------------------------


def test_normalize_dry_run_false_is_none() -> None:
    assert normalize_dry_run(False) is None
    assert normalize_dry_run(None) is None


def test_normalize_dry_run_true_is_diff() -> None:
    """The bool ``True`` aliases the default ``"diff"`` shape."""
    assert normalize_dry_run(True) == "diff"


def test_normalize_dry_run_accepts_known_modes() -> None:
    for mode in DRY_RUN_MODES:
        assert normalize_dry_run(mode) == mode


def test_normalize_dry_run_rejects_unknown_string() -> None:
    with pytest.raises(BadInput, match="dry_run must be"):
        normalize_dry_run("brief")


def test_normalize_dry_run_rejects_random_type() -> None:
    with pytest.raises(BadInput, match="dry_run must be"):
        normalize_dry_run(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# format_unified_diff — standard difflib shape
# ---------------------------------------------------------------------------


def test_unified_diff_emits_standard_headers() -> None:
    pre = "alpha\nbeta\ngamma\n"
    post = "alpha\nBETA\ngamma\n"
    diff = format_unified_diff(pre, post, file_label="x.md")
    assert "--- a/x.md" in diff
    assert "+++ b/x.md" in diff
    assert "-beta" in diff
    assert "+BETA" in diff


def test_unified_diff_empty_when_no_change() -> None:
    """``unified_diff`` returns an empty iterable when buffers match."""
    diff = format_unified_diff("hello\n", "hello\n", file_label="x.md")
    assert diff == ""


def test_unified_diff_respects_n_context() -> None:
    """Three lines of context above and below the changed line by default."""
    pre = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
    post = pre.replace("line5", "L5")
    diff = format_unified_diff(pre, post, file_label="x", n_context=1)
    # n=1 → only one line of context above and below the change.
    assert "line4" in diff
    assert "line6" in diff
    assert "line3" not in diff
    assert "line7" not in diff


# ---------------------------------------------------------------------------
# classify_diff_hunks — within vs outside edited spans
# ---------------------------------------------------------------------------


def test_classify_hunks_overlap_with_edited_spans() -> None:
    pre = "a\nb\nc\nd\ne\n"
    post = "a\nb\nC\nd\ne\n"
    # The edit changed line 3 ('c' -> 'C'); the agent's span covers L3.
    within, outside = classify_diff_hunks(pre, post, ((3, 3),))
    assert within == 1
    assert outside == 0


def test_classify_hunks_outside_edited_spans() -> None:
    """A change at L1 with edited_spans=((5,5),) is incidental."""
    pre = "a\nb\nc\nd\ne\n"
    post = "A\nb\nc\nd\ne\n"
    within, outside = classify_diff_hunks(pre, post, ((5, 5),))
    assert within == 0
    assert outside == 1


def test_classify_hunks_mixed() -> None:
    """Changes both inside and outside edited spans count separately."""
    pre = "a\nb\nc\nd\ne\nf\n"
    post = "A\nb\nc\nd\nE\nf\n"
    # Two single-line changes: L1 (outside), L5 (within).
    within, outside = classify_diff_hunks(pre, post, ((5, 5),))
    assert within == 1
    assert outside == 1


def test_classify_hunks_zero_when_no_change() -> None:
    within, outside = classify_diff_hunks("x\n", "x\n", ((1, 1),))
    assert within == 0
    assert outside == 0


# ---------------------------------------------------------------------------
# render_dry_run_header — table-form metadata block
# ---------------------------------------------------------------------------


def test_dry_run_header_includes_label_and_spans() -> None:
    lines = render_dry_run_header(
        region_label="notes--foo~intro",
        edited_spans=((42, 42),),
        match_policy="unique",
    )
    assert any("notes--foo~intro" in line for line in lines)
    assert any("L42" in line for line in lines)
    assert any("'unique'" in line for line in lines)


def test_dry_run_header_renders_extras_aligned() -> None:
    lines = render_dry_run_header(
        region_label="x",
        edited_spans=((1, 1),),
        match_policy="all",
        extras=[("ast.parse:", "ok"), ("ruff:", "no changes")],
    )
    # Extras appear after the universal header lines.
    assert any("ast.parse:" in line and "ok" in line for line in lines)
    assert any("ruff:" in line and "no changes" in line for line in lines)


def test_dry_run_header_handles_no_spans() -> None:
    lines = render_dry_run_header(
        region_label="x",
        edited_spans=(),
        match_policy="unique",
    )
    # Shouldn't crash, and should report (none).
    assert any("(none)" in line or "spans:" in line for line in lines)


def test_dry_run_header_renders_multi_line_span() -> None:
    lines = render_dry_run_header(
        region_label="x",
        edited_spans=((42, 50),),
        match_policy="unique",
    )
    assert any("L42-50" in line for line in lines)


# ---------------------------------------------------------------------------
# render_dry_run_full — post-edit lines with context markers
# ---------------------------------------------------------------------------


def test_dry_run_full_marks_edited_lines_with_chevron() -> None:
    """``> `` marks edited lines; space marks context."""
    post = "before1\nbefore2\nEDITED\nafter1\nafter2\n"
    body = render_dry_run_full(
        post,
        edited_spans=((3, 3),),
        region_label="x",
        n_context=2,
    )
    # Edited line gets `>`, context lines get a leading space.
    edited_line = next(l for l in body.splitlines() if "EDITED" in l)
    assert edited_line.startswith(">")
    before_line = next(l for l in body.splitlines() if "before1" in l)
    assert before_line.startswith(" ")


def test_dry_run_full_emits_one_section_per_span() -> None:
    post = "L1\nL2\nL3\nL4\nL5\nL6\nL7\n"
    body = render_dry_run_full(
        post,
        edited_spans=((1, 1), (5, 5)),
        region_label="x",
        n_context=1,
    )
    # Two sections, both labelled with the region.
    headers = [l for l in body.splitlines() if l.startswith("# x")]
    assert len(headers) == 2


def test_dry_run_full_falls_back_when_no_spans() -> None:
    body = render_dry_run_full(
        "post\n",
        edited_spans=(),
        region_label="x",
    )
    assert "no edited spans" in body or "view='raw'" in body
