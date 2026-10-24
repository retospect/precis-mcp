"""Continuation-table header reconcile (gr514187): a repeated header row
emitted in scrambled cell order over canonical-order data gets rewritten
to the previous table's header."""

from __future__ import annotations

from precis.ingest.marker import _reconcile_continuation_table_headers

_A = "| Coverage, θ | E F , σ (eV/site) | Number of FCC sites | Maps Number |"
_A_SEP = "|-------------|-------------------|---------------------|-------------|"
_B = "| Maps Number | Coverage, θ | Number of FCC sites | E F , σ (eV/site) |"
_B_SEP = "|-------------|-------------|---------------------|-------------------|"


def _tbl(header: str, sep: str, rows: list[str]) -> dict:
    return {"type": "table", "text": "\n".join([header, sep, *rows])}


def test_scrambled_continuation_header_rewritten() -> None:
    blocks = [
        _tbl(_A, _A_SEP, ["| 0 | 0 | 1 | 0 |"]),
        _tbl(_B, _B_SEP, ["| 0.43 | -0.432 | 7 | 89 |"]),
        _tbl(_B, _B_SEP, ["| 0.5 | -0.48 | 8 | 109 |"]),
    ]
    out = _reconcile_continuation_table_headers(blocks)
    first = out[1]["text"].split("\n")
    second = out[2]["text"].split("\n")
    assert first[0] == _A and first[1] == _A_SEP
    assert second[0] == _A and second[1] == _A_SEP
    # Data rows untouched — they were already canonical-order.
    assert "| 0.43 | -0.432 | 7 | 89 |" in out[1]["text"]


def test_different_table_untouched() -> None:
    blocks = [
        _tbl(_A, _A_SEP, ["| 0 | 0 | 1 | 0 |"]),
        _tbl("| Figure | ECI (meV/site) |", "|---|---|", ["| Empty | -426 |"]),
    ]
    out = _reconcile_continuation_table_headers(blocks)
    assert out[1]["text"].split("\n")[0] == "| Figure | ECI (meV/site) |"


def test_prose_break_ends_run() -> None:
    blocks = [
        _tbl(_A, _A_SEP, ["| 0 | 0 | 1 | 0 |"]),
        {"type": "text", "text": "Table 2: Effective cluster interactions"},
        _tbl(_B, _B_SEP, ["| 0.43 | -0.432 | 7 | 89 |"]),
    ]
    out = _reconcile_continuation_table_headers(blocks)
    assert out[2]["text"].split("\n")[0] == _B


def test_same_order_header_noop() -> None:
    blocks = [
        _tbl(_A, _A_SEP, ["| 0 | 0 | 1 | 0 |"]),
        _tbl(_A, _A_SEP, ["| 0.4 | -0.4 | 5 | 16 |"]),
    ]
    out = _reconcile_continuation_table_headers(blocks)
    assert out[1]["text"].split("\n")[0] == _A
