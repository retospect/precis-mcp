"""Markdown bullet text → structured ``ulist``/``item`` chunks.

Parser unit tests plus the two ends that matter: ``add_chunks`` converts at
the door, and the outline collapses the result to one row.
"""

from __future__ import annotations

import pytest

from precis.draft.mdlist import count_items, parse_list_block


def _flat(node, depth=0):
    """(depth, chunk_kind, text) in DFS order — the shape the store inserts."""
    out = [(depth, node.chunk_kind, node.text)]
    for c in node.children:
        out.extend(_flat(c, depth + 1))
    return out


def test_flat_bullets_become_a_ulist_of_items() -> None:
    node = parse_list_block("- alpha\n- beta")
    assert _flat(node) == [
        (0, "ulist", ""),
        (1, "item", "alpha"),
        (1, "item", "beta"),
    ]
    assert count_items(node) == 2


def test_indent_nests_a_sublist_under_the_item_above() -> None:
    node = parse_list_block("- top\n    - inner\n    - inner2\n- top2")
    assert _flat(node) == [
        (0, "ulist", ""),
        (1, "item", "top"),
        (2, "ulist", ""),
        (3, "item", "inner"),
        (3, "item", "inner2"),
        (1, "item", "top2"),
    ]
    assert count_items(node) == 4


def test_numbered_markers_open_an_olist() -> None:
    assert parse_list_block("1. one\n2) two").chunk_kind == "olist"


def test_tab_indent_nests_like_spaces() -> None:
    node = parse_list_block("- top\n\t- inner")
    assert [k for _, k, _ in _flat(node)] == ["ulist", "item", "ulist", "item"]


@pytest.mark.parametrize(
    "block",
    [
        "ordinary prose with no bullets at all",
        "prose first\n- then a bullet",  # must *open* with a bullet
        "```\n- fenced code is not a list\n```",
        "- bullets\n1. then numbers",  # two lists, one container can't hold both
        "",
    ],
)
def test_declines_anything_that_is_not_cleanly_one_list(block: str) -> None:
    assert parse_list_block(block) is None


def test_lazy_continuation_joins_into_the_item_above() -> None:
    # The corpus hard-wraps items, at the bullet's indent and at column 0.
    node = parse_list_block("- a claim that runs\n  past one line\nand another\n- b")
    assert _flat(node) == [
        (0, "ulist", ""),
        (1, "item", "a claim that runs past one line and another"),
        (1, "item", "b"),
    ]


def test_a_single_bullet_is_not_a_list() -> None:
    """A dash-led paragraph that runs on would otherwise convert under
    lazy continuation; one genuine single-item list in the whole corpus
    is the price."""
    assert parse_list_block("- the dash opens a clause\nand it runs on") is None


def test_blank_lines_between_items_stay_one_list() -> None:
    # ``_split_blocks`` splits on blank lines before the parser sees a
    # block, but a caller reaching the parser directly gets loose lists.
    node = parse_list_block("- a\n\n- b")
    assert count_items(node) == 2
