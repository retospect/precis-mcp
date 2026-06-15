"""Unit tests for the abstract-preview chunk picker (no DB).

Exercises the pure ``_pick_abstract_text`` heuristic used by
``BlocksMixin.abstract_previews`` to populate the web papers hover
card from leading body chunks.
"""

from __future__ import annotations

from precis.store._blocks_ops import (
    _looks_like_abstract,
    _pick_abstract_text,
    _strip_abstract_label,
)


def test_prefers_explicit_abstract_section() -> None:
    items = [
        ("Some Title", "Title"),
        ("Jane Smith, Bob Jones", "Authors"),
        (
            "Abstract We investigate the thermodynamics of X and find a "
            "novel phase transition at 300 K under ambient pressure.",
            "Abstract",
        ),
        ("1. Introduction Long body text that is also quite substantial " * 5, "Body"),
    ]
    pick = _pick_abstract_text(items)
    # The abstract chunk wins over the longer intro, label stripped.
    assert pick.startswith("We investigate the thermodynamics")
    assert "Abstract" not in pick[:10]


def test_falls_back_to_first_substantial_paragraph() -> None:
    long_para = "x" * 250
    items = [("short title", "Title"), (long_para, "Body"), ("y" * 300, "Body")]
    assert _pick_abstract_text(items) == long_para


def test_falls_back_to_longest_when_none_substantial() -> None:
    items = [("aa", "Title"), ("bbbb", "Authors"), ("cc", "Body")]
    assert _pick_abstract_text(items) == "bbbb"


def test_empty_items_returns_empty() -> None:
    assert _pick_abstract_text([]) == ""


def test_strip_abstract_label_variants() -> None:
    assert _strip_abstract_label("Abstract: hello") == "hello"
    assert _strip_abstract_label("ABSTRACT — hello") == "hello"
    assert _strip_abstract_label("abstract. hello") == "hello"
    assert _strip_abstract_label("No label here") == "No label here"


def test_looks_like_abstract_by_section_and_text() -> None:
    assert _looks_like_abstract("anything", "Abstract")
    assert _looks_like_abstract("Abstract We show...", "Body")
    assert not _looks_like_abstract("Intro text", "Introduction")


def test_short_abstract_chunk_is_skipped() -> None:
    # An "Abstract" heading chunk with no prose should not win; fall
    # through to the substantial paragraph.
    long_para = "z" * 250
    items = [("Abstract", "Abstract"), (long_para, "Body")]
    assert _pick_abstract_text(items) == long_para
