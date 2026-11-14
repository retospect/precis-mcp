"""The pathway ``view='compare'`` table lens (gr592637 slice 4):
``args={'where': {col: v|[vs]|{min,max}}, 'sort': '-col'|[…],
'columns': […], 'limit': N, 'cursor': tok}`` over the interleaved
sibling-comparison table — shown/matched/total, hidden-row aggregates,
and the literal ``next:`` call. Reuses the synthetic-pathway fixture
idiom from ``tests.test_pathway_plugin`` (real store, no compute).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.errors import BadInput
from tests.test_pathway_plugin import _handler, _store_synthetic_pathway


@pytest.fixture
def three_siblings(store: Any) -> Any:
    """Three ready pathways sharing the same substrate→target — the
    sibling set ``view='compare'`` interleaves."""
    _store_synthetic_pathway(store, "lens-pd", element="Pd")
    _store_synthetic_pathway(store, "lens-pt", element="Pt")
    _store_synthetic_pathway(store, "lens-ag", element="Ag")
    return _handler(store)


def test_compare_limit_and_cursor(three_siblings: Any) -> None:
    """limit=2 on a 3-candidate compare: the footer names the exact
    continuing call and the cursor reaches the remaining row once —
    nothing positionally dropped."""
    h = three_siblings
    body = h.get(id="lens-pd", view="compare", args={"limit": 2}).body
    assert "2 shown · 3 matched · 3 total" in body
    assert "next: get(kind='pathway'" in body
    assert "view='compare'" in body
    assert "hidden:" in body  # the unseen row's aggregates ride along

    m = re.search(r"'cursor': '([^']+)'", body)
    assert m, body
    page2 = h.get(
        id="lens-pd",
        view="compare",
        args={"limit": 2, "cursor": m.group(1)},
    ).body
    assert "1 shown · 3 matched · 3 total" in page2
    assert "cursor exhausted" in page2

    # every slug appears in exactly one page's table (above the footer —
    # the hidden-row histogram legitimately names unseen rows)
    shown_a = body.split(" shown ·")[0]
    shown_b = page2.split(" shown ·")[0]
    slugs = ("lens-pd", "lens-pt", "lens-ag")
    for slug in slugs:
        assert (slug in shown_a) != (slug in shown_b), slug


def test_compare_where_filters_rows(three_siblings: Any) -> None:
    """``where={'lever': 'Pd'}`` keeps the one Pd-slab sibling."""
    h = three_siblings
    body = h.get(id="lens-pd", view="compare", args={"where": {"lever": "Pd"}}).body
    assert "1 shown · 1 matched · 3 total" in body
    shown = body.split(" shown ·")[0]
    assert "lens-pd" in shown
    assert "lens-pt" not in shown and "lens-ag" not in shown


def test_compare_sort_desc(three_siblings: Any) -> None:
    """``sort='-RATE'`` orders worst-first — the explicit lens spelling,
    overriding the default RATE-ascending rank."""
    h = three_siblings
    body = h.get(id="lens-pd", view="compare", args={"sort": "-RATE"}).body
    assert "3 matched" in body  # all rows still there, re-ordered
    assert "next:" not in body  # a full page needs no continuation


def test_compare_stale_cursor_refused(three_siblings: Any) -> None:
    """A cursor minted under one spec refuses a changed where."""
    h = three_siblings
    body = h.get(id="lens-pd", view="compare", args={"limit": 2}).body
    cursor = re.search(r"'cursor': '([^']+)'", body).group(1)
    with pytest.raises(BadInput, match="different lens spec"):
        h.get(
            id="lens-pd",
            view="compare",
            args={
                "limit": 2,
                "cursor": cursor,
                "where": {"lever": "Pd"},
            },
        )


def test_lens_keys_refused_off_compare(three_siblings: Any) -> None:
    """The lens keys are compare-only — a typo'd view refuses with the
    compare spelling named."""
    h = three_siblings
    with pytest.raises(BadInput, match="view='compare' only"):
        h.get(id="lens-pd", view="analysis", args={"limit": 5})
