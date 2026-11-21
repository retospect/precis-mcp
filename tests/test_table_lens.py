"""The table lens (:mod:`precis.format.table_lens`, gr592637) — pure, no DB."""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.format.table_lens import (
    LensResult,
    apply_lens,
    lens_response,
    parse_lens_args,
)

COLS = ["name", "energy", "trusted", "engine"]


def _rows(n: int = 40) -> list[dict[str, Any]]:
    return [
        {
            "name": f"c{i}",
            "energy": f"{-1.0 - i * 0.1:g}" if i % 10 else "—",
            "trusted": "yes" if i % 4 == 0 else "no",
            "engine": "v1" if i < 30 else "v2",
        }
        for i in range(n)
    ]


def _render(rows: list[dict[str, Any]], cols: list[str]) -> str:
    return "\n".join(",".join(str(r[c]) for c in cols) for r in rows)


def _run(args: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> LensResult:
    spec = parse_lens_args(args, columns=COLS)
    return apply_lens(
        rows if rows is not None else _rows(),
        spec,
        columns=COLS,
        render=_render,
        call=lambda a: f"get(args={a!r})",
    )


def test_where_sort_columns_limit() -> None:
    res = _run({"where": {"trusted": "yes"}, "sort": "-energy", "limit": 5})
    assert res.total == 40
    assert res.matched == 10
    # most positive energy first among trusted; '—' (i=0,10,20,30) sink last
    expected = [
        r["name"] for r in _rows() if r["trusted"] == "yes" and r["energy"] != "—"
    ]
    assert [r["name"] for r in res.rows] == expected[:5]
    assert res.next_cursor is not None
    assert "next:" in res.footer and "cursor" in res.footer


def test_columns_select_and_unknown_column_refused() -> None:
    res = _run({"columns": ["name"], "limit": 2})
    assert res.body == "c0\nc1"
    with pytest.raises(BadInput):
        parse_lens_args({"where": {"nope": 1}}, columns=COLS)
    with pytest.raises(BadInput):
        parse_lens_args({"sort": "-nope"}, columns=COLS)


def test_range_and_set_where() -> None:
    res = _run({"where": {"energy": {"min": -2.0, "max": -1.5}, "engine": ["v1"]}})
    assert [r["name"] for r in res.rows] == ["c5", "c6", "c7", "c8", "c9"]


def test_cursor_roundtrip_and_stale() -> None:
    first = _run({"limit": 15})
    second = _run({"limit": 15, "cursor": first.next_cursor})
    names = [r["name"] for r in (*first.rows, *second.rows)]
    assert names == [f"c{i}" for i in range(30)]
    third = _run({"limit": 15, "cursor": second.next_cursor})
    assert third.next_cursor is None
    assert third.rows[-1]["name"] == "c39"
    with pytest.raises(BadInput):
        _run({"limit": 15, "where": {"trusted": "yes"}, "cursor": first.next_cursor})
    with pytest.raises(BadInput):
        _run({"cursor": "garbage"})


def test_aggregates_for_hidden_rows() -> None:
    res = _run({"limit": 10})
    assert "left out (30 rows)" in res.footer
    assert "energy: n=27" in res.footer and "min=-4" in res.footer
    assert "trusted:" in res.footer and "engine: v1=20, v2=10" in res.footer


def test_aggregates_cover_columns_not_shown() -> None:
    """gr592637 review: a projection (columns=['name']) selects the shown
    cells, never the aggregate domain — the hidden rows' numeric and trust
    aggregates still appear."""
    res = _run({"columns": ["name"], "limit": 10})
    assert res.body == "\n".join(f"c{i}" for i in range(10))
    assert "energy: n=27" in res.footer
    assert "no=23" in res.footer and "yes=7" in res.footer


def test_never_cuts_row() -> None:
    rows = _rows()
    spec = parse_lens_args({"limit": 40}, columns=COLS)
    res = apply_lens(
        rows,
        spec,
        columns=COLS,
        render=_render,
        call=lambda a: "x",
        max_bytes=350,
    )
    assert 0 < len(res.rows) < 40
    assert res.body == _render(res.rows, COLS)
    assert res.next_cursor is not None
    nxt = apply_lens(
        rows,
        parse_lens_args({"limit": 40, "cursor": res.next_cursor}, columns=COLS),
        columns=COLS,
        render=_render,
        call=lambda a: "x",
        max_bytes=350,
    )
    assert nxt.rows[0]["name"] == f"c{len(res.rows)}"


def test_response_budget_accounts_for_header_and_footer() -> None:
    rows = _rows()
    response = lens_response(
        rows,
        parse_lens_args({"limit": 40}, columns=COLS),
        columns=COLS,
        render=_render,
        call=lambda args: f"get(args={args!r})",
        header="é" * 50 + "\n",
    )
    assert response.budget_fit is not None
    fitted = response.budget_fit(440)
    assert len(fitted.encode()) <= 440
    assert "next: get(" in fitted
    assert fitted.startswith("é" * 50)
    with pytest.raises(BadInput, match="complete row and footer"):
        response.budget_fit(100)


def test_oversized_row_refused_with_projection_recovery() -> None:
    rows = [{**_rows(1)[0], "name": "é" * 1000}]
    with pytest.raises(BadInput, match="complete row and footer"):
        apply_lens(
            rows,
            parse_lens_args({}, columns=COLS),
            columns=COLS,
            render=_render,
            call=lambda args: f"get(args={args!r})",
            max_bytes=300,
        )


def test_cursor_refuses_changed_rows() -> None:
    first = _run({"limit": 1})
    with pytest.raises(BadInput, match="stale"):
        _run({"cursor": first.next_cursor}, _rows(41))
