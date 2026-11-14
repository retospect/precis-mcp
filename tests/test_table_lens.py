"""``precis.format.table_lens`` — the shared server-side table protocol
(gr592637): ``where/sort/columns/limit/cursor`` over row dicts, shown/
matched/total counts, hidden-row aggregates, and the exact ``next:``
continuation call. Pure unit tests — no store.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.errors import BadInput
from precis.format.table_lens import (
    apply_lens,
    hidden_aggregates,
    lens_echo_args,
    match_where,
    mint_cursor,
    parse_lens_args,
    parse_sort,
    render_lens_page,
)


def _rows(n: int = 40) -> list[dict[str, Any]]:
    """``n`` candidates: trusted cycles yes/no/-, energy descends so the
    sort orders are unambiguous."""
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append(
            {
                "handle": f"st{i + 1}",
                "energy": -1.0 - 0.1 * i,
                "trusted": ("yes", "no", "-")[i % 3],
                "band": ("frontier", "dominated")[i % 2],
            }
        )
    return out


# ── where / sort / columns / limit (spec slice 1 test 1) ────────────────


def test_where_sort_columns_limit() -> None:
    """40 rows; where={'trusted':'yes'}, sort='-energy', limit=5 returns
    the 5 expected rows with total=40 and matched = the yes-tally."""
    rows = _rows(40)
    spec = parse_lens_args({"where": {"trusted": "yes"}, "sort": "-energy", "limit": 5})
    page = apply_lens(rows, spec)
    assert page.total == 40
    assert page.matched == len([r for r in rows if r["trusted"] == "yes"])
    want = sorted(
        (r for r in rows if r["trusted"] == "yes"),
        key=lambda r: -float(r["energy"]),
    )[:5]
    assert [r["handle"] for r in page.rows] == [r["handle"] for r in want]
    assert len(page.hidden) == page.matched - 5

    # column projection keeps just the named columns
    spec = parse_lens_args(
        {
            "where": {"trusted": "yes"},
            "sort": "-energy",
            "columns": ["handle", "energy"],
            "limit": 5,
        }
    )
    page = apply_lens(rows, spec)
    assert page.columns == ["handle", "energy"]
    assert all(set(r) == {"handle", "energy"} for r in page.rows)


def test_where_range_and_set_forms() -> None:
    rows = _rows(40)
    assert match_where(rows[0], {"energy": {"max": -0.5}})
    assert not match_where(rows[0], {"energy": {"min": -0.5}})
    assert match_where(rows[0], {"band": ["frontier", "awaiting"]})
    assert not match_where(rows[1], {"band": ["awaiting"]})


def test_unknown_columns_refused_by_name() -> None:
    with pytest.raises(BadInput, match="nosuchcol"):
        apply_lens(_rows(3), parse_lens_args({"where": {"nosuchcol": "x"}}))


# ── cursor roundtrip + staleness (spec slice 1 test 2) ──────────────────


def test_cursor_roundtrip_and_stale() -> None:
    """Following the cursor yields the next rows with no overlap; a cursor
    minted under a different where raises BadInput."""
    rows = _rows(40)
    spec = parse_lens_args({"where": {"trusted": "yes"}, "limit": 4})
    page = apply_lens(rows, spec)
    seen = [r["handle"] for r in page.rows]
    cursor = mint_cursor(spec, len(page.rows))

    spec2 = parse_lens_args({"where": {"trusted": "yes"}, "limit": 4, "cursor": cursor})
    page2 = apply_lens(rows, spec2)
    seen2 = [r["handle"] for r in page2.rows]
    assert seen2 and not set(seen) & set(seen2)

    # walk to exhaustion: every matched row is visited exactly once
    visited = list(seen)
    cur = cursor
    while True:
        s = parse_lens_args({"where": {"trusted": "yes"}, "limit": 4, "cursor": cur})
        p = apply_lens(rows, s)
        visited += [r["handle"] for r in p.rows]
        if s.offset + len(p.rows) >= p.matched:
            break
        cur = mint_cursor(s, s.offset + len(p.rows))
    assert sorted(visited) == sorted(r["handle"] for r in rows if r["trusted"] == "yes")

    # a changed where under the old cursor is refused, not silently resumed
    with pytest.raises(BadInput, match="different lens spec"):
        parse_lens_args({"where": {"trusted": "no"}, "limit": 4, "cursor": cursor})
    # a forged/garbage token is refused by name
    with pytest.raises(BadInput, match="not a lens cursor"):
        parse_lens_args({"cursor": "bogus-token"})


# ── hidden-row aggregates (spec slice 1 test 3) ─────────────────────────


def test_aggregates_for_hidden_rows() -> None:
    """Hidden numeric column reports n/min/max/mean; an enum column
    reports a value histogram."""
    rows = _rows(40)
    spec = parse_lens_args({"limit": 5})
    page = apply_lens(rows, spec)
    agg = hidden_aggregates(page.hidden)
    m = re.search(r"energy n=(\d+) min=(-?[\d.]+) max=(-?[\d.]+) mean=(-?[\d.]+)", agg)
    assert m, agg
    assert int(m.group(1)) == 35
    assert float(m.group(2)) == min(r["energy"] for r in rows[5:])
    assert float(m.group(3)) == max(r["energy"] for r in rows[5:])
    assert "trusted" in agg and "yes×" in agg and "no×" in agg


# ── the byte guard never cuts mid-row (spec slice 1 test 4) ─────────────


def test_never_cuts_row() -> None:
    """byte_cap drops WHOLE trailing rows only — the visible text is a
    whole number of complete rows plus the footer, never a partial cell."""
    rows = _rows(10)
    spec = parse_lens_args({"limit": 10})
    page = apply_lens(rows, spec)

    renders: list[str] = []

    def render(rs: list[dict[str, Any]]) -> str:
        text = "\n".join(f"row {r['handle']}" for r in rs)
        renders.append(text)
        return text

    out = render_lens_page(
        page, args={}, call=lambda a: "call()", render=render, byte_cap=30
    )
    body = out.splitlines()[0]
    assert body.startswith("row ") or body.startswith("(no rows")
    # every printed row line is complete — no trailing fragment
    for line in out.splitlines():
        if line.startswith("row"):
            assert re.fullmatch(r"row st\d+", line)
    # dropped rows join the hidden set — they are aggregated and paged to
    assert "hidden:" in out or "exceeds the byte budget" in out


def test_echo_args_canonicalises_the_continuation() -> None:
    spec = parse_lens_args({"where": {"trusted": "yes"}, "sort": "-energy", "limit": 7})
    echo = lens_echo_args(
        {"budget": 100, "where": {"trusted": "yes"}, "sort": "-energy"},
        spec,
        preserve=("budget",),
    )
    assert echo == {
        "budget": 100,
        "where": {"trusted": "yes"},
        "sort": "-energy",
        "limit": 7,
    }


def test_parse_sort_spellings() -> None:
    assert parse_sort("energy") == (("energy", True),)
    assert parse_sort("-energy") == (("energy", False),)
    assert parse_sort(["a", "-b"]) == (("a", True), ("b", False))
    assert parse_sort([["a", "desc"]]) == (("a", False),)
    with pytest.raises(BadInput):
        parse_sort(5)
    with pytest.raises(BadInput):
        parse_sort([["a", "sideways"]])
