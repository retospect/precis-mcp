"""Inline reference marker parsing + stripping (ADR 0033 §8)."""

from __future__ import annotations

from precis.utils.draft_markup import (
    AUTHORING,
    CITE,
    WEB,
    XREF,
    parse_references,
    strip_markers,
)


def test_bare_cross_ref_and_citation():
    refs = parse_references("see [¶5BL5xQ] and [§miller89~4].")
    assert [(r.cls, r.target, r.surface) for r in refs] == [
        (XREF, "¶5BL5xQ", None),
        (CITE, "§miller89~4", None),
    ]


def test_display_links_classified_by_target():
    refs = parse_references(
        "[the intro](¶abc), [Miller](§m~4), [DDG](https://d.com), "
        "[as noted](memory:7x2)"
    )
    assert [(r.cls, r.surface) for r in refs] == [
        (XREF, "the intro"),
        (CITE, "Miller"),
        (WEB, "DDG"),
        (AUTHORING, "as noted"),
    ]
    assert refs[2].target == "https://d.com"
    assert refs[3].target == "memory:7x2"


def test_glossary_is_a_display_xref_to_a_term():
    # syntactically a display cross-ref; term-vs-section resolves later
    (ref,) = parse_references("the [fancy word](¶term1) appears")
    assert ref.cls == XREF and ref.surface == "fancy word" and ref.target == "¶term1"


def test_authoring_bracket_form():
    (ref,) = parse_references("background [[memory:7x2]] informs this")
    assert ref.cls == AUTHORING and ref.target == "memory:7x2" and ref.surface is None


def test_plain_brackets_are_not_references():
    # no sigil, no paren → not a reference (just brackets / a TODO note)
    assert parse_references("a [note] and [TODO] here") == []


def test_order_preserved_and_raw_captured():
    refs = parse_references("[¶a] then [x](§p~2)")
    assert [r.raw for r in refs] == ["[¶a]", "[x](§p~2)"]


def test_strip_markers_keeps_surface_drops_targets():
    text = (
        "See [the intro](¶abc) and [Miller](§m~4), per [[memory:1]]. "
        "Visit [DDG](https://d.com). Also [¶xyz]."
    )
    out = strip_markers(text)
    # display/surface words survive
    assert "the intro" in out and "Miller" in out and "DDG" in out
    # targets / handles / addresses are gone (embed sees prose, not markup)
    for gone in ("¶abc", "¶xyz", "§m~4", "memory:1", "https://d.com", "[["):
        assert gone not in out


def test_strip_is_pure_function_of_text():
    # no DB, no registry — same input → same output (so content_sha over
    # the source fully determines the embed input)
    t = "use [the term](¶t1) twice [the term](¶t1)"
    assert strip_markers(t) == strip_markers(t)
    assert "¶t1" not in strip_markers(t)
