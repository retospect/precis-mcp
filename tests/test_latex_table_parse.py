"""Tests for the LaTeX table parser (`precis.utils.table_data.parse_latex_table`)
and its wiring as `table_payload`'s third fallback (gr51405 + gr52564).

Mirrors the style of ``parse_markdown_table`` coverage in
``tests/test_draft_table.py`` — pure functions, no DB, exact expected
``{header, rows, caption}`` per case (see the frozen contract's test
matrix)."""

from __future__ import annotations

from precis.utils.table_data import parse_latex_table, table_payload

# ── parse_latex_table — success cases ──────────────────────────────────


def test_booktabs_three_cols_textbf_headers() -> None:
    tex = r"""
\begin{tabular}{lcc}
\toprule
\textbf{Element} & \textbf{Gap} & \textbf{Structure} \\
\midrule
Si & 1.12 & diamond \\
Ge & 0.67 & diamond \\
\bottomrule
\end{tabular}
"""
    assert parse_latex_table(tex) == {
        "header": ["Element", "Gap", "Structure"],
        "rows": [
            ["Si", "1.12", "diamond"],
            ["Ge", "0.67", "diamond"],
        ],
        "caption": None,
    }


def test_plain_hline_grid_header_plus_two_rows() -> None:
    tex = r"""
\begin{tabular}{|l|c|}
\hline
Name & Value \\
\hline
A & 1 \\
B & 2 \\
\hline
\end{tabular}
"""
    assert parse_latex_table(tex) == {
        "header": ["Name", "Value"],
        "rows": [
            ["A", "1"],
            ["B", "2"],
        ],
        "caption": None,
    }


def test_float_wrapper_with_caption() -> None:
    tex = r"""
\begin{table}
\centering
\caption{Cap}
\label{tab:my-table}
\begin{tabular}{lcc}
\hline
X & Y & Z \\
\hline
1 & 2 & 3 \\
\hline
\end{tabular}
\end{table}
"""
    assert parse_latex_table(tex) == {
        "header": ["X", "Y", "Z"],
        "rows": [["1", "2", "3"]],
        "caption": "Cap",
    }


def test_bare_tabular_body_starting_with_colspec() -> None:
    # Importer's seg[2] shape: the captured body *is* the tabular content,
    # beginning with the `{colspec}` argument (no `\begin{tabular}` wrapper).
    tex = r"""{lrr}
\hline
Name & A & B \\
\hline
foo & 1 & 2 \\
\hline
"""
    assert parse_latex_table(tex) == {
        "header": ["Name", "A", "B"],
        "rows": [["foo", "1", "2"]],
        "caption": None,
    }


def test_tabularx_with_width_arg_two_leading_groups_stripped() -> None:
    tex = r"""
\begin{tabularx}{\textwidth}{lXX}
\hline
Col1 & Col2 & Col3 \\
\hline
a & b & c \\
\hline
\end{tabularx}
"""
    assert parse_latex_table(tex) == {
        "header": ["Col1", "Col2", "Col3"],
        "rows": [["a", "b", "c"]],
        "caption": None,
    }


def test_escaped_ampersand_stays_one_cell_unescaped() -> None:
    tex = r"""
\begin{tabular}{ll}
\hline
Term & Meaning \\
\hline
X & A \& B \\
\hline
\end{tabular}
"""
    assert parse_latex_table(tex) == {
        "header": ["Term", "Meaning"],
        "rows": [["X", "A & B"]],
        "caption": None,
    }


def test_multicolumn_spanning_cell_padded_to_width() -> None:
    tex = r"""
\begin{tabular}{lll}
\hline
A & B & C \\
\hline
\multicolumn{2}{c}{Spanning} & tail \\
\hline
\end{tabular}
"""
    assert parse_latex_table(tex) == {
        "header": ["A", "B", "C"],
        "rows": [["Spanning", "tail", ""]],
        "caption": None,
    }


def test_numeric_looking_cells_stay_strings() -> None:
    tex = r"""
\begin{tabular}{ll}
\hline
Element & Gap \\
\hline
Si & 1.523 \\
\hline
\end{tabular}
"""
    result = parse_latex_table(tex)
    assert result == {
        "header": ["Element", "Gap"],
        "rows": [["Si", "1.523"]],
        "caption": None,
    }
    assert isinstance(result["rows"][0][1], str)


def test_comment_line_dropped() -> None:
    tex = r"""
\begin{tabular}{ll}
\hline
% this whole line is a comment and must vanish
A & B \\
\hline
1 & 2 \\ % trailing comment, keep the row
\hline
\end{tabular}
"""
    assert parse_latex_table(tex) == {
        "header": ["A", "B"],
        "rows": [["1", "2"]],
        "caption": None,
    }


# ── parse_latex_table — failure cases (return None) ────────────────────


def test_row_longer_than_header_returns_none() -> None:
    tex = r"""
\begin{tabular}{ll}
\hline
A & B \\
\hline
1 & 2 & 3 \\
\hline
\end{tabular}
"""
    assert parse_latex_table(tex) is None


def test_empty_input_returns_none() -> None:
    assert parse_latex_table("") is None


def test_prose_with_no_row_or_cell_separators_returns_none() -> None:
    assert parse_latex_table("Just some prose describing an experiment.") is None


def test_multirow_mess_yielding_long_row_returns_none() -> None:
    tex = r"""
\begin{tabular}{ll}
\hline
A & B \\
\hline
\multirow{2}{*}{X} & 1 \\
& 2 & extra \\
\hline
\end{tabular}
"""
    assert parse_latex_table(tex) is None


# ── table_payload integration ───────────────────────────────────────────


def test_payload_recovers_latex_table_when_no_meta_table() -> None:
    tex = r"""
\begin{tabular}{lcc}
\toprule
\textbf{Element} & \textbf{Gap} & \textbf{Structure} \\
\midrule
Si & 1.12 & diamond \\
\bottomrule
\end{tabular}
"""
    assert table_payload({}, tex) == {
        "header": ["Element", "Gap", "Structure"],
        "rows": [["Si", "1.12", "diamond"]],
        "caption": None,
    }


def test_payload_meta_table_wins_over_latex_text() -> None:
    tex = r"""
\begin{tabular}{ll}
\hline
Ignored & Header \\
\hline
zz & yy \\
\hline
\end{tabular}
"""
    payload = table_payload(
        {"table": {"header": ["el", "gap"], "rows": [["Si", 1.12]]}},
        tex,
    )
    assert payload == {
        "header": ["el", "gap"],
        "rows": [["Si", "1.12"]],
        "caption": None,
    }
