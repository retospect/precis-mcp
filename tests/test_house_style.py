"""Math-aware house-style lint (utils/house_style.py)."""

from __future__ import annotations

from precis.utils.house_style import find_style_flags


def test_math_subscripts_do_not_fire() -> None:
    text = r"The $P_5$ and $\mu_B$ levels in g-C$_3$N$_4$ and $$E_F = x_1$$ agree."
    assert find_style_flags(text) == []


def test_three_distinct_rules_with_line_col() -> None:
    text = "We tailor -- and improve --\nthe **best** result — really."
    flags = find_style_flags(text)
    got = [(f.rule, f.line, f.col, f.snippet) for f in flags]
    assert ("double_hyphen", 1, 11, "--") in got
    assert ("double_hyphen", 1, 26, "--") in got
    assert ("bold", 2, 5, "**best**") in got
    assert ("em_dash", 2, 21, "—") in got
    assert {f.rule for f in flags} == {"em_dash", "bold", "double_hyphen"}


def test_italics_flagged_and_bold_not_double_counted() -> None:
    flags = find_style_flags("a *word* and _other_ and **b**")
    assert [f.rule for f in flags] == ["italic_star", "italic_underscore", "bold"]


def test_identifiers_handles_urls_code_do_not_fire() -> None:
    text = (
        "Use snake_case and my_var_name, cite [fi123] and [¶ab_12], see "
        "https://x.org/a_b_c--d and `--flag` or `_x_` and [t](¶a_b_c) ok."
    )
    assert find_style_flags(text) == []


def test_range_and_empty() -> None:
    assert find_style_flags("") == []
    assert find_style_flags("pages 3–5 and a 2 * 3 * 4 product") == []


def test_write_hint_skips_non_prose_chunk_kinds() -> None:
    from precis.handlers._draft_lint import house_style_hint

    text = "a -- b, **bold**"
    assert house_style_hint(text, "table") == ""
    assert house_style_hint(text, "equation") == ""
    assert "house style" in house_style_hint(text, "paragraph")
    assert "house style" in house_style_hint(text, "figure")
    assert "house style" in house_style_hint(text)
