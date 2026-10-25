"""Pure tests for ``precis.extract.tables`` — the deterministic
pipe-table parser behind ``get(kind='paper', view='tables')`` and the
recipe slice.

The two fixtures are hand-written stand-ins for the two prod shapes in
``docs/backlog/table-numeric-extraction.md`` (gr506762): a normal
columns-are-measurands table, and a transposed comparison table whose
columns cite other papers' values.
"""

from __future__ import annotations

import pytest

from precis.extract.tables import (
    assign_captions,
    has_citation_marker,
    header_unit,
    parse_table,
    strip_footnote,
    table_runs,
)

# First-pipe-line headers for the ``(cid, ord, header)`` triples
# ``table_runs``/``assign_captions`` take: the same header across
# ord-adjacent chunks is ingest's split_table signature for ONE
# logical table; a different header means a different table.
HA = "| Gas | coeff |"
HB = "| Site | energy |"
HC = "| adsorbate | rate |"

# Sample 1: measurands in columns, subjects in rows, units in the
# headers — one data row.
SAMPLE1 = (
    "| | Oxidation rate constant $K_i N_0 (L^{-2})$ "
    "| Saturation coverage $\\theta_{\\rm sat}$ (ML) | ... "
    "| Saturation sticking $s_{\\rm rev}$ |\n"
    "| Ni{100} | 0.0002 | 3.6 | 0.68 | 0.062 |"
)

# Sample 2: measurands in rows, cited sources in columns — no
# separator line, a two-row header, footnote markers on the values.
SAMPLE2 = (
    "| | expt | | |\n"
    "| | Wichtendahl et al. <sup>56</sup> | Dohnalek et al. <sup>44</sup> | |\n"
    "| pre-exponential, $\\log \\nu$ (s <sup>-1</sup> ) | 13 | $15 \\pm 2$ | |\n"
    "| $E_{\\rm d}^{\\;\\rm A}$ | $13.5 \\pm 1.1^a$ | $18.5 \\pm 2.0^{b}$ | |"
)


def _assert_offsets(text: str, parsed) -> None:
    for row in parsed.rows:
        for cell in row:
            assert text[cell.start : cell.end] == cell.text
    for (start, end), row in zip(parsed.row_spans, parsed.rows):
        line = text[start:end]
        assert line.lstrip().startswith("|")
        assert len(row) >= 1


class TestParseGrid:
    def test_sample1_columns_and_header_units(self) -> None:
        parsed = parse_table(1, SAMPLE1)
        assert parsed is not None
        assert parsed.header_rows == 1
        assert len(parsed.columns) == 5
        assert [c.header for c in parsed.columns] == [
            "",
            "Oxidation rate constant $K_i N_0 (L^{-2})$",
            "Saturation coverage $\\theta_{\\rm sat}$ (ML)",
            "...",
            "Saturation sticking $s_{\\rm rev}$",
        ]
        assert parsed.columns[1].header_unit == "L^-2"
        assert parsed.columns[2].header_unit == "ML"
        assert parsed.columns[4].header_unit is None
        assert not any(c.cited for c in parsed.columns)
        assert len(parsed.rows) == 1
        row = parsed.rows[0]
        assert row[0].text == "Ni{100}"
        assert row[1].numeric and row[1].value_text == "0.0002"
        assert parsed.ragged_rows == []
        assert parsed.row_label_cited == [False]
        _assert_offsets(SAMPLE1, parsed)

    def test_sample2_two_row_header_and_cited_columns(self) -> None:
        parsed = parse_table(2, SAMPLE2)
        assert parsed is not None
        assert parsed.header_rows == 2
        assert len(parsed.columns) == 4
        assert parsed.columns[1].header == "expt / Wichtendahl et al. <sup>56</sup>"
        assert parsed.columns[1].cited
        assert parsed.columns[2].header == "Dohnalek et al. <sup>44</sup>"
        assert parsed.columns[2].cited
        assert not parsed.columns[0].cited
        assert len(parsed.rows) == 2
        _assert_offsets(SAMPLE2, parsed)

    def test_sample2_footnote_cells(self) -> None:
        parsed = parse_table(2, SAMPLE2)
        assert parsed is not None
        ed = parsed.rows[1]
        assert ed[1].text == "$13.5 \\pm 1.1^a$"
        assert ed[1].value_text == "$13.5 \\pm 1.1$"
        assert ed[1].footnote == "a"
        assert ed[1].numeric
        assert ed[2].footnote == "b"
        assert ed[2].numeric
        pref = parsed.rows[0]
        assert pref[1].numeric and pref[1].value_text == "13"
        assert pref[2].numeric

    def test_separator_marks_header_end(self) -> None:
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        parsed = parse_table(3, text)
        assert parsed is not None
        assert parsed.header_rows == 1
        assert [c.header for c in parsed.columns] == ["a", "b"]
        assert [[c.text for c in r] for r in parsed.rows] == [
            ["1", "2"],
            ["3", "4"],
        ]
        _assert_offsets(text, parsed)

    def test_two_header_rows_before_separator(self) -> None:
        text = "| grp | a | b |\n| sub | x | y |\n|---|---|---|\n| 1 | 2 | 3 |"
        parsed = parse_table(4, text)
        assert parsed is not None
        assert parsed.header_rows == 2
        assert parsed.columns[0].header == "grp / sub"
        assert len(parsed.rows) == 1

    def test_ragged_row_listed(self) -> None:
        text = "| a | b | c |\n|---|---|---|\n| 1 | 2 |\n| 3 | 4 | 5 |"
        parsed = parse_table(5, text)
        assert parsed is not None
        assert parsed.ragged_rows == [0]
        _assert_offsets(text, parsed)

    def test_collapsed_run_returns_none(self) -> None:
        assert parse_table(6, "a b c d e f no pipes at all") is None
        assert parse_table(7, "| only one line |") is None
        assert parse_table(8, "") is None

    def test_row_label_cited(self) -> None:
        text = "| source [12] | v |\n|---|---|\n| ours | 1.5 |"
        parsed = parse_table(9, text)
        assert parsed is not None
        assert parsed.columns[0].cited
        assert parsed.row_label_cited == [False]
        text2 = "| src | v |\n|---|---|\n| Wichtendahl et al. [44] | 1.5 |"
        parsed2 = parse_table(10, text2)
        assert parsed2 is not None
        assert parsed2.row_label_cited == [True]

    def test_crlf_offsets_exact(self) -> None:
        # ``\r\n`` breaks keep their ``\r`` inside the split("\n")
        # line; every anchor must still slice verbatim.
        text = "| a | b |\r\n|---|---|\r\n| 1.5 | 2 |\r\n| 3 | 4\r\n"
        parsed = parse_table(11, text)
        assert parsed is not None
        assert [c.text for c in parsed.rows[0]] == ["1.5", "2"]
        assert [[c.text for c in r] for r in parsed.rows] == [
            ["1.5", "2"],
            ["3", "4"],
        ]
        _assert_offsets(text, parsed)


class TestStripFootnote:
    @pytest.mark.parametrize(
        ("cell", "value", "mark"),
        [
            ("13.5 \\pm 1.1^a", "13.5 \\pm 1.1", "a"),
            ("$13.5 \\pm 1.1^a$", "$13.5 \\pm 1.1$", "a"),
            ("$18.5 \\pm 2.0^{b}$", "$18.5 \\pm 2.0$", "b"),
            ("0.68*", "0.68", "*"),
            ("3.6†", "3.6", "†"),
            ("12<sup>a</sup>", "12", "a"),
            ("0.0002", "0.0002", None),
            ("Ni{100}", "Ni{100}", None),
        ],
    )
    def test_marks(self, cell: str, value: str, mark: str | None) -> None:
        assert strip_footnote(cell) == (value, mark)

    @pytest.mark.parametrize(
        "cell",
        [
            "L^{-2}",  # exponent, not a footnote
            "$15 \\pm 2$",  # plain math cell
            "<sup>-1</sup>",  # negative exponent markup
            "10^{-3}",  # negative exponent caret
            "\\mu^*",  # marker not after a number
            "d^*",  # label cell, no number
        ],
    )
    def test_not_footnotes(self, cell: str) -> None:
        assert strip_footnote(cell) == (cell.strip(), None)


class TestHeaderUnit:
    @pytest.mark.parametrize(
        ("header", "unit"),
        [
            ("Oxidation rate constant $K_i N_0 (L^{-2})$", "L^-2"),
            ("Saturation coverage $\\theta_{\\rm sat}$ (ML)", "ML"),
            ("pre-exponential, $\\log \\nu$ (s <sup>-1</sup> )", "s^-1"),
            ("desorption energy [kJ/mol]", "kJ/mol"),
            ("Saturation sticking $s_{\\rm rev}$", None),
            ("expt", None),
            ("Wichtendahl et al. <sup>56</sup>", None),
            ("rate (blorf)", None),
        ],
    )
    def test_units(self, header: str, unit: str | None) -> None:
        assert header_unit(header) == unit

    def test_last_group_wins(self) -> None:
        assert header_unit("rate (a) constant (K)") == "K"


class TestCitationMarker:
    @pytest.mark.parametrize(
        "cell",
        [
            "Wichtendahl et al. <sup>56</sup>",
            "Wichtendahl et al.",
            "ET AL values",
            "values [12]",
            "values [12, 13]",
            "see ref. 4",
            "cf. Refs.",
            "Dohnalek^{44}",
            "<sup>56</sup>",
        ],
    )
    def test_markers(self, cell: str) -> None:
        assert has_citation_marker(cell)

    @pytest.mark.parametrize(
        "cell",
        [
            "13.5 \\pm 1.1",
            "Ni{100}",
            "10^{12}",  # positive exponent, not a citation
            "L^{-2}",
            "prefactor",
        ],
    )
    def test_not_markers(self, cell: str) -> None:
        assert not has_citation_marker(cell)


class TestTableRuns:
    """Logical-table grouping over ``(cid, ord, first_pipe_line)``."""

    def test_grouping_rules(self) -> None:
        # Same header + ord-adjacent merges (ingest's split_table
        # repeats the header on every part); a header-less chunk joins
        # the adjacent run; a different header or an ord gap breaks it.
        assert table_runs(
            [
                (1, 1, HA),
                (2, 2, HA),
                (3, 3, None),
                (4, 4, HC),
                (6, 6, HC),
            ]
        ) == [[(1, 1), (2, 2), (3, 3)], [(4, 4)], [(6, 6)]]


class TestAssignCaptions:
    """One-to-one pairing of caption chunks to a ref's logical tables.

    ``tables`` is ``(chunk_id, ord, first_pipe_line)`` in ord order —
    ord-adjacent runs *with the same header* are one logical table
    (ingest's split_table), and the group number is what a numbered
    caption must equal; ``neighbours`` is ``(chunk_id, ord, text)`` of
    the non-table chunks around them."""

    def test_split_table_chunks_share_caption(self) -> None:
        # The Opus-review repro: ingest's split_table makes ord-adjacent
        # same-header chunks — 10/11/12 are ONE logical table
        # (Table 1), 14 is Table 2. Numbering by chunk position gave
        # chunk 11 "Table 2".
        got = assign_captions(
            [(10, 10, HA), (11, 11, HA), (12, 12, HA), (14, 14, HB)],
            [
                (9, 9, "Table 1. Rates"),
                (13, 13, "Table 2. Kinetic parameters at 300 K"),
            ],
        )
        assert got == {
            10: (9, "Table 1. Rates"),
            11: (9, "Table 1. Rates"),
            12: (9, "Table 1. Rates"),
            14: (13, "Table 2. Kinetic parameters at 300 K"),
        }

    def test_adjacent_different_headers_stay_separate(self) -> None:
        # Round-3 repro: two *different* tables printed back to back are
        # ord-adjacent but carry different first pipe lines — merging
        # them would give chunk 11 Table 1's caption and push
        # "Table 2." onto chunk 13.
        got = assign_captions(
            [(10, 10, HA), (11, 11, HB), (13, 13, HC)],
            [
                (9, 9, "Table 1. A"),
                (12, 12, "Table 2. B"),
            ],
        )
        assert got == {
            10: (9, "Table 1. A"),
            11: (12, "Table 2. B"),
        }

    def test_same_header_merges_and_none_joins(self) -> None:
        # 10/11 share a header and 12 is a no-grid piece (None) — all
        # one logical table; 13 is ord-adjacent but headed differently,
        # so it is Table 2 on its own.
        got = assign_captions(
            [(10, 10, HA), (11, 11, HA), (12, 12, None), (13, 13, HB)],
            [
                (9, 9, "Table 1. A"),
                (14, 14, "Table 2. B"),
            ],
        )
        assert got == {
            10: (9, "Table 1. A"),
            11: (9, "Table 1. A"),
            12: (9, "Table 1. A"),
            13: (14, "Table 2. B"),
        }

    def test_captions_below_tables_pair_in_order(self) -> None:
        # T1(10) C1(11) T2(12) C2(13): each caption names its own table
        # even though both sit within T2's window.
        got = assign_captions(
            [(20, 10, HA), (21, 12, HB)],
            [(30, 11, "Table 1. First."), (31, 13, "Table 2. Second.")],
        )
        assert got == {
            20: (30, "Table 1. First."),
            21: (31, "Table 2. Second."),
        }

    def test_missing_caption_never_borrowed(self) -> None:
        # T2's caption is missing; "Table 1" is in T2's window but the
        # number doesn't match its running order → no caption.
        got = assign_captions(
            [(20, 10, HA), (21, 12, HB)],
            [(30, 11, "Table 1. First.")],
        )
        assert got == {20: (30, "Table 1. First.")}

    def test_prose_reference_is_not_a_caption(self) -> None:
        # "Table 2 shows…" lacks the . : or ** delimiter after the
        # number — an in-text reference, not a caption.
        assert (
            assign_captions(
                [(20, 10, HA), (21, 12, HB)],
                [(30, 11, "Table 2 shows the rates rising with T.")],
            )
            == {}
        )
        # 'Tables in the appendix' doesn't open "Table <n>" at all.
        assert (
            assign_captions([(20, 10, HA)], [(30, 9, "Tables in the appendix")]) == {}
        )

    def test_one_caption_serves_one_table(self) -> None:
        # An unnumbered-label caption between two tables is claimed by
        # the nearer side (before beats after on a distance tie) — it
        # can never attach to both.
        got = assign_captions(
            [(20, 10, HA), (21, 12, HB)],
            [(30, 11, "Table S1. Supplementary rates.")],
        )
        assert got == {21: (30, "Table S1. Supplementary rates.")}

    def test_numbered_match_beats_unnumbered_tie(self) -> None:
        got = assign_captions(
            [(20, 10, HA)],
            [(30, 9, "Table S1. Supplementary."), (31, 11, "Table 1. First.")],
        )
        assert got == {20: (31, "Table 1. First.")}

    def test_roman_numeral_number(self) -> None:
        got = assign_captions(
            [(20, 10, HA), (21, 12, HB), (22, 14, HC), (23, 16, HA)],
            [(30, 15, "Table IV. Fourth.")],
        )
        assert got == {23: (30, "Table IV. Fourth.")}

    def test_tie_goes_before(self) -> None:
        assert assign_captions(
            [(6, 6, HA)],
            [(20, 5, "Table 1. before"), (21, 7, "Table 1. after")],
        ) == {6: (20, "Table 1. before")}

    def test_window(self) -> None:
        # ord 9 is three away from the table at ord 6 — out of window.
        assert assign_captions([(6, 6, HA)], [(15, 9, "Table 1. far")]) == {}

    def test_truncates_to_300(self) -> None:
        long_cap = "Table 1. " + "x" * 400
        got = assign_captions([(6, 6, HA)], [(1, 5, long_cap)])
        cid, cap = got[6]
        assert cid == 1 and len(cap) == 300
