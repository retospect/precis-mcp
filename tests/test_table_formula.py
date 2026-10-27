"""Table formula evaluator + footer aggregates (spreadsheet-tables slice 2).

The escape tests are the point: formula text can arrive from an agent
relaying ingested content, so every known simpleeval/object-graph payload
must be refused at compile or die as an error cell — never evaluated.
"""

from __future__ import annotations

import time

import pytest

from precis.errors import BadInput
from precis.utils.table_data import normalize_table
from precis.utils.table_formula import (
    _TableEval,
    compile_formula,
    evaluate_table,
    table_stats,
)


def _table(formula: str | None = None, **kw) -> dict:
    cols = [
        {"name": "qty"},
        {"name": "unit_price"},
    ]
    if formula is not None:
        cols.append({"name": "line_cost", "formula": formula})
    default_rows = (
        [[2, 0.5, None], [10, 0.1, None]]
        if formula is not None
        else [[2, 0.5], [10, 0.1]]
    )
    return normalize_table(
        {
            "columns": cols,
            "rows": kw.get("rows", default_rows),
            **{k: v for k, v in kw.items() if k != "rows"},
        }
    )


# ── compile_formula ──────────────────────────────────────────────────


def test_compile_rewrites_names_to_ids() -> None:
    t = _table("qty * unit_price")
    cols = t["columns"]
    out = compile_formula("qty * unit_price + 1", cols)
    assert out == f"{cols[0]['id']} * {cols[1]['id']} + 1"
    # id form round-trips (a stored formula re-compiles unchanged)
    assert compile_formula(out, cols) == out


def test_declared_formula_strict_vs_stored_ref() -> None:
    """``declared=`` is the strict path: a formula supplied THIS write
    that names no column refuses with ``options=``; the same formula as
    a stored one (``declared=None``) decays to ``#REF`` cells."""
    make = lambda: normalize_table(
        {"columns": [{"name": "cost", "formula": "missing * 2"}], "rows": [[None]]}
    )
    t, _ = evaluate_table(make())
    assert t["rows"][0][0] == "#REF"
    t = make()
    with pytest.raises(BadInput, match="unknown name 'missing'"):
        evaluate_table(t, declared={t["columns"][0]["id"]})


def test_strict_compile_names_dropped_column() -> None:
    """A stored formula restated after its column was dropped still
    refuses under strict (restated = declared), but the refusal says it
    names a dropped column and how to fix it — not a bare 'unknown
    name'."""
    t, _ = evaluate_table(_table("qty * 2"))
    qty_id = t["columns"][0]["id"]
    stored = t["columns"][2]["formula"]  # id form: '<qty_id> * 2'
    live = t["columns"][2:]  # qty dropped
    with pytest.raises(BadInput, match="dropped column") as e:
        compile_formula(stored, live, strict=True, retired={qty_id})
    assert "remove" in str(e.value.next)
    # a plain typo on the same write still gets the options= refusal
    with pytest.raises(BadInput, match="unknown name"):
        compile_formula("quanity * 2", live, strict=True, retired={qty_id})


def test_compile_unknown_name_is_the_ref_path() -> None:
    """An unresolved name is NOT a declaration fault on the lenient
    (stored-formula) path (spec §4): a column a stored formula names
    may be dropped by a later edit — the write lands and the dependent
    cells store ``#REF``."""
    t = _table()
    assert compile_formula("quanity * 2", t["columns"]) == "quanity * 2"
    t = _table("quanity * 2")
    t, notes = evaluate_table(t)
    assert all(r[2] == "#REF" for r in t["rows"])
    assert notes


# ── escape payloads: refused at compile or dead at eval ─────────────

ESCAPE_PAYLOADS = [
    "qty.__class__",
    "().__class__.__mro__",
    "().__class__.__mro__[1].__subclasses__()",
    "getattr(qty, 'real')",
    "'{0.__class__}'.format(qty)",
    "''.format_map(qty)",
    "f'{qty}'",
    "[x for x in qty]",
    "(lambda: 1)()",
    "__import__('os')",
    "qty[0]",
    "{'a': 1}",
    "(1, 2)",
    "open('/etc/passwd')",
    "qty.real",
    "x = 1",
    "import os",
    "qty if True else 0.__class__",
]


@pytest.mark.parametrize("payload", ESCAPE_PAYLOADS)
def test_escape_payloads_refused(payload: str) -> None:
    t = _table()
    cols = t["columns"]
    with pytest.raises(BadInput):
        compile_formula(payload, cols)
    # and nothing reaches evaluation even if fed the raw string
    ev = _TableEval()
    ev.names = {c["id"]: 1 for c in cols}
    try:
        ev.eval(payload)
    except Exception:
        pass
    else:  # pragma: no cover — a payload that evals is a security hole
        raise AssertionError(f"payload evaluated: {payload!r}")


def test_dunder_names_never_resolve() -> None:
    """A bare dunder compiles through (it's just a name — the #REF path),
    but it can never resolve to a value: eval stores #REF, and the object
    graph stays unreachable (attribute access is refused at compile)."""
    t = _table("__class__")
    t, _ = evaluate_table(t)
    assert all(r[2] == "#REF" for r in t["rows"])


def test_huge_power_capped_at_eval() -> None:
    """9**9**9 parses fine but the tightened pow cap kills it as an error
    cell — the write lands, the cell says #NUM."""
    t = _table("9**9**9")
    t, notes = evaluate_table(t)
    assert all(r[2] == "#NUM" for r in t["rows"])
    assert notes


def test_string_multiply_is_value_error() -> None:
    """No numeric-string coercion: ``*`` on a str operand is #VALUE
    (which also removes the repeat-DoS path entirely)."""
    t = _table("'x' * qty")
    t, notes = evaluate_table(t)
    assert all(r[2] == "#VALUE" for r in t["rows"])
    t = _table("qty * unit_price")
    t["rows"][0][1] = "0.5"  # text cell that looks numeric
    t, _ = evaluate_table(t)
    assert t["rows"][0][2] == "#VALUE"
    assert t["rows"][1][2] == pytest.approx(1.0)


def test_string_concat_is_value_error() -> None:
    t = _table("unit_price + 'x'")
    t, _ = evaluate_table(t)
    assert all(r[2] == "#VALUE" for r in t["rows"])


def test_percent_format_never_runs() -> None:
    """``'%0100000000d' % qty`` is a 100 MB string per cell — a str left
    operand is sprintf, not math: #VALUE, nothing formatted."""
    t = _table("'%0100000000d' % qty")
    t, _ = evaluate_table(t)
    assert all(r[2] == "#VALUE" for r in t["rows"])
    t = _table("unit_price % qty")  # numeric mod still works
    t, _ = evaluate_table(t)
    assert t["rows"][0][2] == pytest.approx(0.5)


def test_non_scalar_literals_refused_at_compile() -> None:
    """A formula may carry only scalar literals — ``b'x' * 10**9`` and
    ``b'%0999999999d' % 1`` slip past the str/bytes operand guards'
    intent (a 1 GB alloc per cell); ``1j`` and ``...`` have no
    spreadsheet meaning either."""
    cols = _table()["columns"]
    for payload in ("b'x' * 10**9", "b'%0999999999d' % 1", "b'x'", "1j", "..."):
        with pytest.raises(BadInput, match="not a scalar"):
            compile_formula(payload, cols)


def test_bytes_repeat_dies_fast_at_eval() -> None:
    """The eval-side brace: a bytes multiply/mod fed straight to
    _TableEval (compile never lets one through) is a TypeError, not a
    1 GB allocation."""
    ev = _TableEval()
    ev.names = {}
    t0 = time.monotonic()
    with pytest.raises(TypeError):
        ev.eval("b'x' * 10**9")
    with pytest.raises(TypeError):
        ev.eval("b'%0999999999d' % 1")
    assert time.monotonic() - t0 < 1.0


def test_store_scalar_caps() -> None:
    """_store_scalar is the write-time backstop: over-cap strings ->
    #VALUE, ints over ~1e308 -> #NUM (json.dumps dies at ~4300 digits)."""
    from precis.utils.table_formula import _store_scalar

    assert _store_scalar("x" * 5000) == "#VALUE"
    assert _store_scalar("x" * 4000) == "x" * 4000
    assert _store_scalar(10**400) == "#NUM"
    assert _store_scalar(10**300) == 10**300
    assert _store_scalar(float("inf")) == "#NUM"
    assert _store_scalar(None) is None


def test_bigint_chain_stays_fast() -> None:
    """The review's repro: c0='999**999', then columns of 60-factor
    self-products — bignum growth must die as #NUM, not a 70 s write."""
    cols = [{"name": "c0", "formula": "999**999"}]
    for k in (1, 2):
        cols.append({"name": f"c{k}", "formula": "*".join([f"c{k - 1}"] * 60)})
    t = normalize_table({"columns": cols, "rows": [[None] * 3]})
    t0 = time.monotonic()
    t, _ = evaluate_table(t)
    assert time.monotonic() - t0 < 1.0
    assert t["rows"][0] == ["#NUM", "#NUM", "#NUM"]
    # and the mult bound alone: two under-pow-cap factors over the bit cap
    t = normalize_table(
        {
            "columns": [{"name": "c", "formula": "10**200 * 10**200"}],
            "rows": [[None]],
        }
    )
    t, _ = evaluate_table(t)
    # 10**400 doesn't fit the storage cap either way
    assert t["rows"][0][0] == "#NUM"
    # the review's verbatim payload — over the pow bit bound
    t = normalize_table(
        {"columns": [{"name": "c", "formula": "999**999*999**999"}], "rows": [[None]]}
    )
    t, _ = evaluate_table(t)
    assert t["rows"][0][0] == "#NUM"
    # the mult/add bit bound itself: caller-supplied giant-int cells
    t = normalize_table(
        {
            "columns": [
                {"name": "a"},
                {"name": "b", "formula": "a * a"},
                {"name": "c", "formula": "a + a"},
            ],
            "rows": [[10**1500, None, None]],
        }
    )
    t, _ = evaluate_table(t)
    assert t["rows"][0][1] == "#NUM" and t["rows"][0][2] == "#NUM"


def test_round_negative_ndigits_bounded() -> None:
    """The review's repro: ``round(1, -10**7)`` took ~10 s for ONE cell —
    ``round(int, -k)`` divides by ``10**k`` and the wall-clock deadline
    only runs between evals. ``-ndigits`` past ``x``'s bit length yields
    0 without materialising the divisor."""
    t = _table("round(1, -10**7)")
    t0 = time.monotonic()
    t, _ = evaluate_table(t)
    assert time.monotonic() - t0 < 1.0
    assert all(r[2] == 0 for r in t["rows"])
    # a huge magnitude that still lands within x's bits rounds honestly
    t = normalize_table(
        {
            "columns": [{"name": "a"}, {"name": "b", "formula": "round(a, -2)"}],
            "rows": [[10**300, None], [14, None], [16, None]],
        }
    )
    t0 = time.monotonic()
    t, _ = evaluate_table(t)
    assert time.monotonic() - t0 < 1.0
    assert t["rows"][0][1] == 10**300
    assert t["rows"][1][1] == 0 and t["rows"][2][1] == 0  # nearest 100: 0


def test_footer_sum_over_bigint_cells() -> None:
    """A caller-supplied cell over float range must not crash the footer:
    ``float(10**400)`` is OverflowError — the cell is skipped."""
    t = _footer_table({"qty": "sum"}, rows=[["a", 2], ["b", 10**400]])
    t, notes = evaluate_table(t)
    qty_id = t["columns"][1]["id"]
    assert t["footer_values"][qty_id]["value"] == 2
    assert any("skipped 1" in n for n in notes)


def test_deep_nesting_refused() -> None:
    t = _table()
    with pytest.raises(BadInput):
        compile_formula("(" * 600 + "1" + ")" * 600, t["columns"])


def test_eval_cell_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import precis.utils.table_formula as tf

    monkeypatch.setattr(tf, "_EVAL_CELL_CAP", 10)
    cols = [{"name": "a"}, {"name": "b", "formula": "a*2"}]
    t = normalize_table(
        {
            "header": ["a", "b"],
            "rows": [[i, None] for i in range(11)],
            "columns": cols,
        }
    )
    with pytest.raises(BadInput, match="cap"):
        evaluate_table(t)


# ── evaluation semantics ─────────────────────────────────────────────


def test_formula_column_evaluates_and_stores_id_form() -> None:
    t = _table("qty * unit_price")
    t, notes = evaluate_table(t)
    assert notes == []
    assert t["rows"][0][2] == pytest.approx(1.0)
    assert t["rows"][1][2] == pytest.approx(1.0)
    line_col = next(c for c in t["columns"] if c["name"] == "line_cost")
    # stored in id form — no column name survives
    assert line_col["formula"] != "qty * unit_price"
    assert "qty" not in line_col["formula"]


def test_formula_on_formula_orders_by_dependency() -> None:
    t = normalize_table(
        {
            "rows": [[2, 0.5, None, None]],
            "columns": [
                {"name": "qty"},
                {"name": "unit_price"},
                # declared before its dependency on purpose
                {"name": "taxed", "formula": "line_cost * 1.2"},
                {"name": "line_cost", "formula": "qty * unit_price"},
            ],
        }
    )
    t, notes = evaluate_table(t)
    assert notes == []
    assert t["rows"][0][2] == pytest.approx(1.2)
    assert t["rows"][0][3] == pytest.approx(1.0)


def test_formula_cycle_stores_marker_and_lands() -> None:
    """Spec §4: a dependency cycle is not a refusal — every column on the
    loop stores #CYCLE, is never evaluated, and a warning names it."""
    t = normalize_table(
        {
            "header": ["a", "b"],
            "rows": [[1, None]],
            "columns": [
                {"name": "a", "formula": "b + 1"},
                {"name": "b", "formula": "a + 1"},
            ],
        }
    )
    t, notes = evaluate_table(t)
    assert t["rows"][0] == ["#CYCLE", "#CYCLE"]
    assert any("cycle" in n and "a" in n and "b" in n and "next:" in n for n in notes)


def test_cycle_dependents_propagate_the_marker() -> None:
    t = normalize_table(
        {
            "columns": [
                {"name": "a", "formula": "b + 1"},
                {"name": "b", "formula": "a + 1"},
                {"name": "c", "formula": "a * 2"},
            ],
            "rows": [[None, None, None]],
        }
    )
    t, _ = evaluate_table(t)
    assert t["rows"][0] == ["#CYCLE", "#CYCLE", "#CYCLE"]


def test_topo_long_chain_is_iterative() -> None:
    n = 3000
    cols = [{"name": "c0", "formula": "1"}]
    cols += [{"name": f"c{k}", "formula": f"c{k - 1} + 1"} for k in range(1, n)]
    t = normalize_table({"columns": cols, "rows": [[None] * n]})
    t, _ = evaluate_table(t)
    assert t["rows"][0][-1] == n  # c0=1 … c2999=3000


def test_error_markers_propagate_original_code() -> None:
    """An error-marker operand propagates ITS marker — never degrades to
    #VALUE — and the first error operand in evaluation order wins."""
    t = normalize_table(
        {
            "columns": [
                {"name": "a", "formula": "missing * 2"},  # -> #REF
                {"name": "b", "formula": "a + 1"},  # #REF operand -> #REF
                {"name": "c", "formula": "1/0"},  # -> #DIV/0
                {"name": "d", "formula": "c + a"},  # #DIV/0 first -> #DIV/0
                {"name": "e", "formula": "a + c"},  # #REF first -> #REF
            ],
            "rows": [[None] * 5],
        }
    )
    t, _ = evaluate_table(t)
    assert t["rows"][0] == ["#REF", "#REF", "#DIV/0", "#DIV/0", "#REF"]


def test_error_markers_propagate_through_if_and_eq() -> None:
    """if_ and == are not exempt: a marker operand decides the cell."""
    t = normalize_table(
        {
            "columns": [
                {"name": "a", "formula": "1/0"},
                {"name": "b", "formula": "if_(a, 1, 2)"},
                {"name": "c", "formula": "a == a"},
                {"name": "d", "formula": "'%s!' % a"},
            ],
            "rows": [[None] * 4],
        }
    )
    t, _ = evaluate_table(t)
    assert t["rows"][0] == ["#DIV/0", "#DIV/0", "#DIV/0", "#DIV/0"]


def test_text_result_starting_with_hash_is_not_an_error() -> None:
    """The note check matches the marker set, not a '#' prefix: a stored
    text cell like '#draft' read by a formula is an operand, and a
    legit '#'-leading result would not be reported as a fault."""
    t = _table("if_(qty > 0, '#ok', 'no')")
    t, notes = evaluate_table(t)
    assert t["rows"][0][2] == "#ok"
    assert notes == []


def test_div_zero_stores_marker_and_lands() -> None:
    t = _table("unit_price / qty", rows=[[0, 0.5, None], [2, 0.1, None]])
    t, notes = evaluate_table(t)
    assert t["rows"][0][2] == "#DIV/0"
    assert t["rows"][1][2] == pytest.approx(0.05)
    assert any("#DIV/0" in n for n in notes)


def test_type_error_stores_value_marker() -> None:
    t = _table("qty + unit_price", rows=[["n/a", 0.5, None]])
    t, notes = evaluate_table(t)
    assert t["rows"][0][2] == "#VALUE"
    assert notes


def test_ifexp_and_whitelist_functions() -> None:
    t = _table("if_(qty > 5, unit_price, 0) + min(qty, 3)")
    t, notes = evaluate_table(t)
    assert notes == []
    assert t["rows"][0][2] == pytest.approx(2.0)  # qty=2 → 0 + 2
    assert t["rows"][1][2] == pytest.approx(3.1)  # qty=10 → 0.1 + 3


# ── footer aggregates ────────────────────────────────────────────────


def _footer_table(footer: dict, rows: list | None = None) -> dict:
    t = normalize_table(
        {
            "header": ["part", "qty"],
            "rows": rows if rows is not None else [["a", 2], ["b", 10], ["c", None]],
            "columns": [{"name": "part"}, {"name": "qty"}],
            "footer": footer,
        }
    )
    return t


def test_footer_sum_and_skip_count() -> None:
    t = _footer_table({"qty": "sum"})
    t, notes = evaluate_table(t)
    qty_id = t["columns"][1]["id"]
    assert t["footer"] == {qty_id: "sum"}
    assert t["footer_values"][qty_id] == {"value": 12.0, "skipped": 1}
    assert any("skipped 1" in n for n in notes)


def test_footer_all_funcs() -> None:
    # exercise each func on one table
    t = _footer_table({})
    cols = t["columns"]
    t["footer"] = {cols[1]["id"]: "mean"}
    t, _ = evaluate_table(t)
    assert t["footer_values"][cols[1]["id"]]["value"] == pytest.approx(6.0)
    for f, want in (
        ("sum", 12.0),
        ("median", 6.0),
        ("min", 2.0),
        ("max", 10.0),
        ("count", 2),  # non-empty cells
    ):
        t["footer"] = {cols[1]["id"]: f}
        t, _ = evaluate_table(t)
        assert t["footer_values"][cols[1]["id"]]["value"] == pytest.approx(want), f
    t["footer"] = {cols[1]["id"]: "std"}
    t, _ = evaluate_table(t)
    assert t["footer_values"][cols[1]["id"]]["value"] == pytest.approx(
        32**0.5
    )  # SAMPLE std (ddof=1) of [2, 10] — spreadsheet STDEV


def test_footer_int_sum_is_int_and_count_notes() -> None:
    t = _footer_table({"qty": "sum"})
    t, notes = evaluate_table(t)
    qty_id = t["columns"][1]["id"]
    v = t["footer_values"][qty_id]["value"]
    assert v == 12 and isinstance(v, int)  # an int sum displays as an int
    # but a single float cell flips it: [1.5, 1.5] shows 3.0, not 3
    t2 = _footer_table({"qty": "sum"}, rows=[["a", 1.5], ["b", 1.5]])
    t2, _ = evaluate_table(t2)
    v2 = t2["footer_values"][t2["columns"][1]["id"]]["value"]
    assert v2 == 3.0 and isinstance(v2, float)
    # count has no "skipped" note — its empties are the data
    t["footer"] = {qty_id: "count"}
    t, notes = evaluate_table(t)
    assert t["footer_values"][qty_id]["value"] == 2
    assert not any("skipped" in n for n in notes)


def test_footer_std_under_two_values_is_none() -> None:
    t = _footer_table({"qty": "std"}, rows=[["a", 2]])
    t, _ = evaluate_table(t)
    qty_id = t["columns"][1]["id"]
    assert t["footer_values"][qty_id]["value"] is None


def test_footer_on_formula_column() -> None:
    t = _table("qty * unit_price")
    cols = t["columns"]
    t["footer"] = {cols[2]["id"]: "sum"}
    t, notes = evaluate_table(t)
    assert t["footer_values"][cols[2]["id"]]["value"] == pytest.approx(2.0)


def test_footer_empty_column_is_none() -> None:
    t = _footer_table({"qty": "sum"}, rows=[["a", None]])
    t, _ = evaluate_table(t)
    qty_id = t["columns"][1]["id"]
    assert t["footer_values"][qty_id]["value"] is None


# ── stats view ───────────────────────────────────────────────────────


def test_table_stats() -> None:
    t = _footer_table({})
    t, _ = evaluate_table(t)
    lines = table_stats(t)
    assert len(lines) == 2
    qty_line = next(l for l in lines if l.startswith("qty"))
    # n counts numeric cells; nonempty counts them separately
    assert "n=2" in qty_line and "nonempty=2" in qty_line and "empty=1" in qty_line
    assert "mean=6" in qty_line and "range=[2, 10]" in qty_line
    part_line = lines[0]
    assert "n=0" in part_line and "nonempty=3" in part_line
    assert "no numeric cells" in part_line
    # a single numeric value shows std=None, not nan
    t2 = _footer_table({}, rows=[["a", 5]])
    t2, _ = evaluate_table(t2)
    qty_line = next(l for l in table_stats(t2) if l.startswith("qty"))
    assert "std=None" in qty_line
