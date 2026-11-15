"""Tests for the shared table-lens protocol (gr592637).

Pure unit tests — no store: the lens operates on row dicts a view has
already built. Covers the protocol contract from the gripe: where /
sort / columns / limit, the cursor round-trip (and its stale-args
refusal), per-column aggregates over the hidden rows, and the byte
guard's whole-rows-only guarantee.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.format.table_lens import apply_lens

_CALL = "get(kind='quest', id='qu1', view='results'"


def _rows(n: int = 40) -> list[dict[str, object]]:
    """40 rows: half trusted, energies 0..-39 (newer = lower), an enum
    ``engine`` column, one free-text column."""
    rows = []
    for i in range(n):
        rows.append(
            {
                "ref_id": 1000 + i,
                "handle": f"st{1000 + i}",
                "name": f"cand-{i}",
                "trusted": "yes" if i % 2 == 0 else "no",
                "energy": f"{-i / 10:.3f}",
                "engine": "emt" if i % 3 else "mace",
                "note": f"n{i}",
            }
        )
    return rows


def test_where_sort_columns_limit() -> None:
    page = apply_lens(
        _rows(),
        where={"trusted": "yes"},
        sort="-energy",
        limit=5,
        internal=("ref_id",),
    )
    # trusted=yes rows are the evens; -energy is plain descending, so
    # the least-negative energies lead: i = 0, 2, 4, 6, 8.
    assert [r["handle"] for r in page.rows] == [
        "st1000",
        "st1002",
        "st1004",
        "st1006",
        "st1008",
    ]
    assert page.total == 40
    assert page.matched == 20
    assert page.shown == 5
    assert page.next_cursor is not None
    # Columns default to the first-seen union minus internals.
    assert "ref_id" not in page.columns
    assert page.columns[:2] == ["handle", "name"]


def test_where_forms_and_columns_projection() -> None:
    rows = _rows()
    page = apply_lens(
        rows,
        where={"engine": ["mace"], "energy": {"min": -1.5, "max": -0.5}},
        columns=["handle", "energy"],
        sort="energy",
        internal=("ref_id",),
    )
    # mace rows: i % 3 == 0. energy in [-1.5, -0.5] → i in 5..15, so
    # i in {6, 9, 12, 15}; ascending energy is most-negative first.
    assert [r["handle"] for r in page.rows] == ["st1015", "st1012", "st1009", "st1006"]
    assert all(set(r) == {"handle", "energy"} for r in page.rows)
    assert page.columns == ["handle", "energy"]


def test_cursor_roundtrip_and_stale() -> None:
    rows = _rows()
    first = apply_lens(rows, sort="handle", limit=10)
    seen: list[str] = [r["handle"] for r in first.rows]
    cursor = first.next_cursor
    assert cursor is not None
    while cursor is not None:
        page = apply_lens(rows, cursor=cursor)
        handles = [r["handle"] for r in page.rows]
        assert not set(handles) & set(seen), "cursor page overlaps a previous one"
        seen.extend(handles)
        cursor = page.next_cursor
    assert len(seen) == 40
    assert len(set(seen)) == 40

    # A changed where alongside the cursor is refused, not silently
    # re-cut; so is a garbage token.
    with pytest.raises(BadInput, match="cursor"):
        apply_lens(rows, cursor=first.next_cursor, where={"trusted": "no"})
    with pytest.raises(BadInput):
        apply_lens(rows, cursor="not-a-token")
    # Pinned args may be re-stated verbatim; the page continues.
    again = apply_lens(
        rows,
        cursor=first.next_cursor,
        sort="handle",
        limit=10,
    )
    assert [r["handle"] for r in again.rows] != [r["handle"] for r in first.rows]


def test_aggregates_for_hidden_rows() -> None:
    page = apply_lens(
        _rows(),
        where={"trusted": "yes"},
        sort="-energy",
        limit=5,
        internal=("ref_id",),
    )
    # 15 matched rows stay hidden: 5 shown. The numeric energy column
    # reports count/min/max/mean; the enum engine column a histogram.
    agg = "; ".join(page.aggregate_lines)
    assert "energy n=15" in agg
    assert "min=-3.8" in agg  # i=38 → -3.8
    assert "max=-1" in agg  # i=10 → -1.0 (the page took i=0..8)
    assert "mean=" in agg
    assert "engine {" in agg
    assert "mace:" in agg and "emt:" in agg
    # The trusted column the where hid nothing from: all hidden rows yes.
    assert "trusted {yes: 15}" in agg


def test_never_cuts_row() -> None:
    """The byte guard moves in whole rows only: a full page defers its
    tail to the cursor, a row wider than the budget by itself is omitted
    whole (never cut mid-row) and named in the footer, and every other
    row stays reachable."""
    rows = [{"handle": f"st{i}", "blob": "x" * 500} for i in range(10)] + [
        {"handle": "st-big", "blob": "y" * 10_000}
    ]
    seen: list[str] = []
    page = apply_lens(rows, sort="handle", byte_budget=2048)
    while True:
        shown = [r["handle"] for r in page.rows]
        # every shown row is intact — nothing is a partial blob
        assert all(r["blob"] == "x" * 500 for r in page.rows)
        assert "st-big" not in shown
        assert not set(shown) & set(seen)
        seen.extend(shown)
        if page.next_cursor is None:
            break
        page = apply_lens(rows, cursor=page.next_cursor)
    assert sorted(seen) == sorted(f"st{i}" for i in range(10))
    first = apply_lens(rows, sort="handle", byte_budget=2048)
    assert first.skipped_wide == 1
    assert "wider than the byte budget" in first.footer(_CALL)


def test_footer_names_the_next_call() -> None:
    page = apply_lens(_rows(), limit=5)
    footer = page.footer(_CALL)
    assert footer.startswith("lens: 5 of 40 rows")
    assert "hidden-row aggregates:" in footer
    assert f"next: {_CALL}, args={{'cursor': " in footer
    # exhausted read: no next line
    last = apply_lens(_rows()[:3], limit=5)
    assert last.next_cursor is None
    assert "next:" not in last.footer(_CALL)


def test_bad_lens_args_raise_bad_input() -> None:
    with pytest.raises(BadInput):
        apply_lens(_rows(), where={"energy": {"min": "low"}})
    with pytest.raises(BadInput):
        apply_lens(_rows(), columns="handle")
    with pytest.raises(BadInput):
        apply_lens(_rows(), limit=0)
    with pytest.raises(BadInput):
        apply_lens(_rows(), sort=["-energy", 7])
