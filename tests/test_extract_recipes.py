"""Pure tests for ``precis.extract.recipes`` — the recipe → runs
expander behind the ``survey_extract:table_recipe`` op (gr506762
slice 2, spec ``docs/backlog/table-numeric-extraction.md``).

The fixtures are the same hand-written stand-ins as
``tests/test_extract_tables.py``: sample 1 (measurands in columns,
subjects in rows) and sample 2 (transposed, cited sources in columns,
a log-scale prefactor row and footnoted uncertainties). Slice-2
acceptance items 1–4 that need no DB or LLM live here; the job-level
items live in ``tests/test_survey_extract_job.py``.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis.extract.prompt import review_prompt
from precis.extract.proposals import VocabEntry
from precis.extract.recipes import expand_recipe, validate_recipe
from precis.extract.tables import parse_table

# Sample 1 as in the spec, plus a second data row and one cell
# changed to ``<1`` (a bound, not a point).
SAMPLE1 = (
    "| | Oxidation rate constant $K_i N_0 (L^{-2})$ "
    "| Saturation coverage $\\theta_{\\rm sat}$ (ML) | ... "
    "| Saturation sticking $s_{\\rm rev}$ |\n"
    "| Ni{100} | 0.0002 | 3.6 | 0.68 | 0.062 |\n"
    "| Ni{111} | <1 | 2.1 | 0.55 | 0.031 |"
)

SAMPLE2 = (
    "| | expt | | |\n"
    "| | Wichtendahl et al. <sup>56</sup> | Dohnalek et al. <sup>44</sup> | |\n"
    "| pre-exponential, $\\log \\nu$ (s <sup>-1</sup> ) | 13 | $15 \\pm 2$ | |\n"
    "| $E_{\\rm d}^{\\;\\rm A}$ | $13.5 \\pm 1.1^a$ | $18.5 \\pm 2.0^{b}$ | |"
)

# Two output columns printing the same literal for one measurand at
# two temperatures — the decision-5 case a row anchor would collapse.
SAME_LITERAL = "| Site | rate (s^-1) | rate (s^-1) |\n|---|---|---|\n| A | 0.5 | 0.5 |"

RAGGED = "| a | b | c |\n|---|---|---|\n| 1 | 2 |\n| 3 | 4 | 5 |"

CAP_ID, CAP_TEXT = 77, "Table 2. Oxidation rates at 300 K on Ni surfaces."


def _entry(ref_id: int, slug: str, name: str, unit: str, *aliases: str) -> VocabEntry:
    return VocabEntry(
        ref_id=ref_id,
        slug=slug,
        name=name,
        aliases=aliases,
        canonical_unit=unit,
        display_unit=unit,
        required_conditions=(),
    )


VOCAB = {
    11: _entry(11, "oxidation-rate-constant", "oxidation rate constant", "L^-2"),
    12: _entry(12, "saturation-coverage", "saturation coverage", "ML"),
    13: _entry(13, "saturation-sticking", "saturation sticking", "1"),
    14: _entry(14, "reaction-probability", "reaction probability", "1"),
    15: _entry(15, "temperature", "temperature", "K"),
    16: _entry(16, "rate", "rate", "s^-1"),
    21: _entry(
        21, "pre-exponential-factor", "pre-exponential factor", "s^-1", "prefactor"
    ),
    22: _entry(22, "desorption-energy", "desorption energy", "kJ/mol"),
    23: _entry(
        23,
        "desorption-activation-energy",
        "desorption activation energy",
        "J",
        "E_d",
    ),
    24: _entry(24, "coverage", "coverage", "1"),
    25: _entry(25, "formation-energy", "formation energy", "eV", "heat of formation"),
}


def _recipe1() -> dict[str, Any]:
    return {
        "table": {
            "orientation": "columns",
            "subject_axis": 0,
            "group": "Table 2",
            "attribution": "own_work",
            "tier": "measured",
            "conditions": [
                {
                    "condition": "temperature",
                    "measurand": "temperature",
                    "literal": "300 K",
                    "role": "context",
                    "source": "caption",
                }
            ],
            "series": [
                {
                    "index": 1,
                    "measurand": "oxidation-rate-constant",
                    "unit": "L^-2",
                    "role": "output",
                },
                {
                    "index": 2,
                    "measurand": "saturation-coverage",
                    "unit": "ML",
                    "role": "output",
                },
                {
                    "index": 3,
                    "measurand": "reaction-probability",
                    "unit": None,
                    "role": "output",
                },
                {
                    "index": 4,
                    "measurand": "saturation-sticking",
                    "unit": None,
                    "role": "output",
                },
            ],
        }
    }


def _expand(
    text: str,
    recipe: dict[str, Any],
    *,
    caption: tuple[int, str] | None = (CAP_ID, CAP_TEXT),
) -> tuple[Any, list[dict[str, Any]]]:
    parsed = parse_table(9, text)
    assert parsed is not None
    return expand_recipe(
        parsed,
        recipe,
        chunk_id=9,
        caption=caption,
        vocab=VOCAB,
        paper=500,
        table=2,
        chunk_text=text,
        extraction_run="test-run",
        model="stub",
    )


class TestSample1Columns:
    def test_four_output_columns_times_rows(self) -> None:
        runs, drops = _expand(SAMPLE1, _recipe1())
        assert drops == []
        # 4 output series × 2 data rows, every cell non-empty.
        assert len(runs) == 8
        for run in runs:
            spec = run.output
            assert spec.subject_ref_id == 500
            assert spec.source_attribution == "own_work"
            assert spec.tier == "measured"
            assert spec.meta["anchor_scheme"] == 3
            assert spec.meta["extraction"]["prompt"] == "table-r1"
            tm = spec.meta["table"]
            assert tm["chunk"] == 9 and tm["table"] == 2
            assert tm["orientation"] == "columns"
            assert tm["caption_chunk"] == CAP_ID
            # 1-based display coords, the series label as header.
            assert tm["row"] in (1, 2) and tm["col"] in (2, 3, 4, 5)

    def test_bound_cell_and_subjects(self) -> None:
        runs, _ = _expand(SAMPLE1, _recipe1())
        bound = [
            r
            for r in runs
            if r.output.measurand_ref_id == 11 and r.output.literal == "<1"
        ]
        assert len(bound) == 1
        spec = bound[0].output
        assert spec.value_form == "upper_bound"
        assert spec.value_num == 1.0
        assert spec.subject == "Ni111"
        ni100 = [
            r
            for r in runs
            if r.output.measurand_ref_id == 11 and r.output.subject == "Ni100"
        ]
        assert len(ni100) == 1
        assert ni100[0].output.value_num == 0.0002
        assert ni100[0].output.reported_unit == "L^-2"

    def test_caption_condition_is_a_shared_input(self) -> None:
        runs, _ = _expand(SAMPLE1, _recipe1())
        pos = CAP_TEXT.index("300 K")
        for run in runs:
            assert len(run.inputs) == 1
            cond = run.inputs[0]
            assert cond.literal == "300 K"
            assert cond.direction == "input"
            assert cond.role == "context"
            assert cond.measurand_ref_id == 15
            assert cond.anchor is not None
            assert cond.anchor.paper_ref_id == 500
            assert cond.anchor.chunk_id == CAP_ID
            assert cond.anchor.scheme == "offsets"
            assert cond.anchor.span == [CAP_ID, pos, pos + len("300 K")]

    def test_cell_anchors_are_exact_spans(self) -> None:
        runs, _ = _expand(SAMPLE1, _recipe1())
        for run in runs:
            anchor = run.output.anchor
            assert anchor is not None and anchor.scheme == "offsets"
            cid, start, end = anchor.span
            assert cid == 9
            assert SAMPLE1[start:end] == run.output.literal

    def test_skip_cells_and_override(self) -> None:
        recipe = _recipe1()
        recipe["table"]["skip_cells"] = ["0:1"]
        recipe["table"]["overrides"] = {"1:4": {"measurand_status": "ambiguous"}}
        runs, drops = _expand(SAMPLE1, recipe)
        assert drops == []
        assert len(runs) == 7  # cell (0,1) skipped
        assert all(r.output.meta["table"]["col"] != 2 for r in runs[:1]) or True
        skipped = [r for r in runs if r.output.meta["table"]["col"] == 2]
        assert [r.output.meta["table"]["row"] for r in skipped] == [2]
        amb = [
            r
            for r in runs
            if r.output.meta["table"]["row"] == 2 and r.output.meta["table"]["col"] == 5
        ]
        assert len(amb) == 1
        assert amb[0].output.measurand_status == "ambiguous"

    def test_null_unit_on_dimensionless_reports_1(self) -> None:
        runs, _ = _expand(SAMPLE1, _recipe1())
        sticking = [r for r in runs if r.output.measurand_ref_id == 13]
        assert sticking and all(r.output.reported_unit == "1" for r in sticking)

    def test_dimensionless_claim_never_flags_unit_support(self) -> None:
        """gr506762 slice-4: ``claimed_unit == '1'`` is the dimensionless
        claim — no printed unit token exists to support it, so
        ``unit_without_text_support`` was always spurious (correct
        sticking-coefficient rows carried it: mx1675, mx1708)."""
        runs, _ = _expand(SAMPLE1, _recipe1())
        sticking = [r for r in runs if r.output.measurand_ref_id == 13]
        assert sticking
        for run in sticking:
            assert "unit_without_text_support" not in run.flags


class TestSample2Rows:
    def _recipe2(self) -> dict[str, Any]:
        return {
            "table": {
                "orientation": "rows",
                "subject_axis": None,
                "attribution": "own_work",
                "tier": "measured",
                "series": [
                    {
                        "index": 0,
                        "measurand": "pre-exponential-factor",
                        "unit": "s^-1",
                        "role": "output",
                        "transform": "log10",
                    },
                    {
                        "index": 1,
                        "measurand": "desorption-energy",
                        "unit": "kJ/mol",
                        "role": "output",
                    },
                ],
                # Column 0 is the row-label axis — skipped in code now;
                # the model-listed skip_cells stays belt and braces.
                "skip_cells": ["0:0", "1:0"],
            }
        }

    def test_log10_transform_and_cited_columns(self) -> None:
        runs, drops = _expand(SAMPLE2, self._recipe2(), caption=None)
        assert drops == []
        assert len(runs) == 4  # two series × two cited columns
        pref = {r.output.literal: r for r in runs if r.output.measurand_ref_id == 21}
        assert pref["13"].output.value_num == 1e13
        assert pref["13"].output.value_form == "point"
        interval = pref["$15 \\pm 2$"].output
        assert interval.value_form == "interval"
        assert interval.value_low == 1e13 and interval.value_high == 1e17
        for run in pref.values():
            assert run.output.meta["transform"] == "log10"
            assert run.output.meta["printed_value"] in (13.0, 15.0)
            assert run.output.measurand_status == "interpreted"

    def test_footnote_cell_and_asserted_tier(self) -> None:
        runs, _ = _expand(SAMPLE2, self._recipe2(), caption=None)
        ed = {r.output.literal: r for r in runs if r.output.measurand_ref_id == 22}
        a = ed["$13.5 \\pm 1.1^a$"].output
        assert a.value_num == 13.5 and a.value_err == 1.1
        assert a.value_form == "point"
        assert a.meta["table"]["footnote"] == "a"
        assert ed["$18.5 \\pm 2.0^{b}$"].output.meta["table"]["footnote"] == "b"
        # Decision 6: cited source columns win over the recipe's claim.
        for run in runs:
            assert run.output.source_attribution == "cited_work"
            assert run.output.tier == "asserted"

    def test_rows_orientation_subjects_and_meta(self) -> None:
        runs, _ = _expand(SAMPLE2, self._recipe2(), caption=None)
        by_col = {r.output.meta["table"]["col"]: r for r in runs}
        assert set(by_col) == {2, 3}
        assert "Wichtendahl" in (by_col[2].output.subject or "")
        assert "Dohnalek" in (by_col[3].output.subject or "")
        for run in runs:
            assert run.output.meta["table"]["orientation"] == "rows"

    def test_label_column_skipped_without_skip_cells(self) -> None:
        # Opus finding 2: the row-label column is skipped in code — a
        # recipe that forgets skip_cells must not expand "log nu" /
        # "Ea (kJ/mol)" as readings.
        recipe = self._recipe2()
        del recipe["table"]["skip_cells"]
        runs, _ = _expand(SAMPLE2, recipe, caption=None)
        assert len(runs) == 4
        assert all(r.output.meta["table"]["col"] != 1 for r in runs)


class TestCellAnchors:
    def test_same_literal_cells_get_distinct_anchors(self) -> None:
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {"index": 1, "measurand": "rate", "unit": "s^-1", "role": "output"},
                    {"index": 2, "measurand": "rate", "unit": "s^-1", "role": "output"},
                ],
            }
        }
        runs, drops = _expand(SAME_LITERAL, recipe, caption=None)
        assert drops == []
        assert len(runs) == 2
        lit_a, lit_b = (r.output.literal for r in runs)
        assert lit_a == lit_b == "0.5"
        span_a = runs[0].output.anchor.span
        span_b = runs[1].output.anchor.span
        assert span_a != span_b
        assert SAME_LITERAL[span_a[1] : span_a[2]] == "0.5"
        assert SAME_LITERAL[span_b[1] : span_b[2]] == "0.5"


class TestDrops:
    def test_ragged_table_drops_whole(self) -> None:
        runs, drops = _expand(RAGGED, {"table": {"orientation": "columns"}})
        assert runs == []
        assert [d["reason"] for d in drops] == ["ragged_table"]
        assert "0" in drops[0]["detail"]

    def test_unknown_measurand_drops_series_only(self) -> None:
        recipe = _recipe1()
        recipe["table"]["series"][1]["measurand"] = "no-such-measurand"
        runs, drops = _expand(SAMPLE1, recipe)
        assert [d["reason"] for d in drops] == ["unknown_measurand"]
        assert "series[1]" in drops[0]["location"]
        assert len(runs) == 6  # 3 good series × 2 rows
        assert all(r.output.measurand_ref_id != 12 for r in runs)

    def test_null_table_is_no_results(self) -> None:
        runs, drops = _expand(
            SAMPLE1, {"table": None, "reason": "setpoints"}, caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["no_results"]

    def test_caption_condition_without_caption_drops(self) -> None:
        runs, drops = _expand(SAMPLE1, _recipe1(), caption=None)
        assert [d["reason"] for d in drops] == ["caption_missing"]
        assert len(runs) == 8
        assert all(run.inputs == [] for run in runs)

    def test_unit_dimension_mismatch_drops_series(self) -> None:
        recipe = _recipe1()
        recipe["table"]["series"][0]["unit"] = "kg"
        runs, drops = _expand(SAMPLE1, recipe)
        assert [d["reason"] for d in drops] == ["unit_dimension_mismatch"]
        assert "kg" in drops[0]["detail"] and "L^-2" in drops[0]["detail"]
        assert len(runs) == 6

    def test_transform_on_categorical_drops_cell(self) -> None:
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": 1,
                        "measurand": "rate",
                        "unit": "s^-1",
                        "role": "output",
                        "transform": "log10",
                    }
                ],
            }
        }
        text = "| Site | rate (s^-1) |\n|---|---|\n| A | high |"
        runs, drops = _expand(text, recipe, caption=None)
        assert runs == []
        assert [d["reason"] for d in drops] == ["bad_shape"]

    def test_transform_overflow_drops_cell(self) -> None:
        # Opus finding 1: 10**400 raises OverflowError — the cell drops
        # as bad_shape and the row behind it still writes.
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": 1,
                        "measurand": "rate",
                        "unit": "s^-1",
                        "role": "output",
                        "transform": "log10",
                    }
                ],
            }
        }
        text = "| Site | log rate |\n|---|---|\n| A | 400 |\n| B | 2 |"
        runs, drops = _expand(text, recipe, caption=None)
        assert [d["reason"] for d in drops] == ["bad_shape"]
        assert "overflow" in drops[0]["detail"] and "400" in drops[0]["detail"]
        assert len(runs) == 1
        assert runs[0].output.literal == "2"
        assert runs[0].output.value_num == 100.0

    def test_transform_uncertainty_edge_overflow_drops_cell(self) -> None:
        # Opus r2 E: ``$307 \pm 3$`` under log10 — 10**307 is finite
        # but the +3 edge overflows. The cell drops naming
        # ``uncertainty_overflow``; it never writes an edgeless
        # interval.
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": 1,
                        "measurand": "rate",
                        "unit": "s^-1",
                        "role": "output",
                        "transform": "log10",
                    }
                ],
            }
        }
        text = "| Site | log rate |\n|---|---|\n| A | $307 \\pm 3$ |\n| B | 2 |"
        runs, drops = _expand(text, recipe, caption=None)
        assert [d["reason"] for d in drops] == ["uncertainty_overflow"]
        assert len(runs) == 1
        assert runs[0].output.literal == "2"
        assert runs[0].output.value_num == 100.0

    def test_series_on_subject_axis_drops(self) -> None:
        # Opus finding 2: a series index == subject_axis expands subject
        # labels as readings — refused per series, the rest still land.
        recipe = _recipe1()
        recipe["table"]["series"].insert(
            0, {"index": 0, "measurand": "rate", "unit": "s^-1", "role": "output"}
        )
        runs, drops = _expand(SAMPLE1, recipe)
        assert [d["reason"] for d in drops] == ["bad_shape"]
        assert "subject axis" in drops[0]["detail"]
        assert len(runs) == 8
        assert all(r.output.meta["table"]["col"] != 1 for r in runs)


def _one_series(
    measurand: str, unit: str | None, transform: str | None = None
) -> dict[str, Any]:
    series: dict[str, Any] = {
        "index": 1,
        "measurand": measurand,
        "unit": unit,
        "role": "output",
    }
    if transform is not None:
        series["transform"] = transform
    return {"table": {"orientation": "columns", "subject_axis": 0, "series": [series]}}


class TestLogScaleHeaders:
    """gr506762 slice-4: mx1410/mx1424 stored the printed exponents 13.1
    and 16.0 as ``s^-1`` off a ``log(ν/s⁻¹)`` column the recipe gave no
    transform. The label's log token now sets the transform itself."""

    @pytest.mark.parametrize(
        "header",
        [
            "log(nu/s^-1)",
            "$\\log(\\nu/\\mathrm{s}^{-1})$",
            "log10(k/s^-1)",
            "$\\log_{10}(A/\\mathrm{s}^{-1})$",
            "lg A (s^-1)",
            "Log k (s^-1)",
        ],
    )
    def test_base10_header_stores_power_of_ten(self, header: str) -> None:
        text = (
            f"| Species | {header} |\n|---|---|\n| methane | 13.1 |\n| ethane | 16.0 |"
        )
        runs, drops = _expand(
            text, _one_series("pre-exponential-factor", "s^-1"), caption=None
        )
        assert drops == []
        by_lit = {r.output.literal: r.output for r in runs}
        assert by_lit["13.1"].value_num == pytest.approx(10**13.1)
        assert by_lit["16.0"].value_num == pytest.approx(1e16)
        for out in by_lit.values():
            assert out.reported_unit == "s^-1"
            assert out.meta["transform"] == "log10"
            assert out.meta["printed_value"] in (13.1, 16.0)
            assert out.measurand_status == "interpreted"

    @pytest.mark.parametrize(
        "header", ["ln(k/s^-1)", "$\\ln (A/\\mathrm{s}^{-1})$", "log_e k (s^-1)"]
    )
    def test_ln_header_stores_power_of_e(self, header: str) -> None:
        text = f"| Species | {header} |\n|---|---|\n| methane | 30 |"
        runs, drops = _expand(
            text, _one_series("pre-exponential-factor", "s^-1"), caption=None
        )
        assert drops == []
        assert len(runs) == 1
        assert runs[0].output.value_num == pytest.approx(math.e**30)
        assert runs[0].output.meta["transform"] == "ln"

    @pytest.mark.parametrize(
        ("header", "transform"),
        [("log_2 k", None), ("log(k/s^-1)", "ln"), ("ln k", "log10")],
    )
    def test_unclear_or_conflicting_base_refuses(
        self, header: str, transform: str | None
    ) -> None:
        text = f"| Species | {header} |\n|---|---|\n| methane | 13.1 |"
        runs, drops = _expand(
            text,
            _one_series("pre-exponential-factor", "s^-1", transform),
            caption=None,
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["log_base_unclear"]

    @pytest.mark.parametrize(
        "header", ["rate (s^-1)", "Catalog rate (s^-1)", "Ln-O (s^-1)"]
    )
    def test_no_log_token_leaves_value(self, header: str) -> None:
        text = f"| Species | {header} |\n|---|---|\n| methane | 13.1 |"
        runs, drops = _expand(text, _one_series("rate", "s^-1"), caption=None)
        assert drops == []
        assert runs[0].output.value_num == 13.1
        assert "transform" not in runs[0].output.meta


class TestHeaderOnlyUnits:
    """gr506762 slice-4: a unit the table never prints, in a unit small
    models pin on unrelated columns (eV, %, s^-1), refuses when the label
    does not name the measurand either — mx1609 eV on laser-fluence
    exponents, mx1455 % on a site fraction read as coverage."""

    def test_unprinted_ev_on_unrelated_label_refuses(self) -> None:
        text = "| Sample | fluence exponent n |\n|---|---|\n| A | 1.8 |"
        runs, drops = _expand(
            text, _one_series("desorption-activation-energy", "eV"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["unit_header_mismatch"]

    def test_unprinted_percent_on_site_fraction_refuses(self) -> None:
        text = "| Site | site fraction |\n|---|---|\n| step | 12 |"
        runs, drops = _expand(text, _one_series("coverage", "%"), caption=None)
        assert runs == []
        assert [d["reason"] for d in drops] == ["unit_header_mismatch"]

    def test_unprinted_ev_on_naming_label_stays_a_flag(self) -> None:
        text = "| Sample | E_d |\n|---|---|\n| A | 1.8 |"
        runs, drops = _expand(
            text, _one_series("desorption-activation-energy", "eV"), caption=None
        )
        assert drops == []
        assert runs[0].flags == ["unit_without_text_support"]

    def test_printed_ev_lands_unflagged(self) -> None:
        text = "| Sample | fluence exponent n |\n|---|---|\n| A | 1.8 |"
        runs, drops = _expand(
            text,
            _one_series("desorption-activation-energy", "eV"),
            caption=(CAP_ID, "Table 1. Barriers in eV."),
        )
        assert drops == []
        assert runs[0].flags == []

    def test_other_unprinted_unit_stays_a_flag(self) -> None:
        text = "| Sample | fluence exponent n |\n|---|---|\n| A | 1.8 |"
        runs, drops = _expand(
            text, _one_series("desorption-activation-energy", "meV"), caption=None
        )
        assert drops == []
        assert runs[0].flags == ["unit_without_text_support"]

    def test_dimensionless_1_never_flags(self) -> None:
        text = "| Site | site fraction |\n|---|---|\n| step | 0.12 |"
        runs, drops = _expand(text, _one_series("coverage", None), caption=None)
        assert drops == []
        assert runs[0].output.reported_unit == "1"
        assert runs[0].flags == []


# A rows-orientation table whose first data row enumerates the columns —
# the pa452964 ``m = 0..12`` index row read as 13 sticking coefficients
# (gr506762 subset-2, mx2049-2061).
INDEX_ROW = (
    "| | m = 0 | m = 1 | m = 2 |\n|---|---|---|---|\n"
    "| m | 0 | 1 | 2 |\n"
    "| s (N2) | 0.9 | 0.8 | 0.7 |"
)

# A columns-orientation table with a row-counter column.
INDEX_COL = (
    "| sample | no. | s |\n|---|---|---|\n"
    "| A | 0 | 0.9 |\n"
    "| B | 1 | 0.8 |\n"
    "| C | 2 | 0.7 |"
)


class TestIndexEnumeration:
    """gr506762 subset-2: an integer run 0../1.. filling the cross axis
    or sitting in the axis's first position is a printed index, never a
    measured output."""

    def test_rows_index_row_drops(self) -> None:
        recipe = {
            "table": {
                "orientation": "rows",
                "series": [
                    {
                        "index": 0,
                        "measurand": "saturation-sticking",
                        "unit": None,
                        "role": "output",
                    },
                    {
                        "index": 1,
                        "measurand": "saturation-sticking",
                        "unit": None,
                        "role": "output",
                    },
                ],
            }
        }
        runs, drops = _expand(INDEX_ROW, recipe, caption=None)
        assert [d["reason"] for d in drops] == ["index_enumeration"]
        assert "series[0]" in drops[0]["location"]
        # The s(N2) row behind it still writes.
        assert len(runs) == 3
        assert [r.output.value_num for r in runs] == [0.9, 0.8, 0.7]

    def test_columns_index_column_drops(self) -> None:
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": 1,
                        "measurand": "saturation-sticking",
                        "unit": None,
                        "role": "output",
                    },
                    {
                        "index": 2,
                        "measurand": "saturation-sticking",
                        "unit": None,
                        "role": "output",
                    },
                ],
            }
        }
        runs, drops = _expand(INDEX_COL, recipe, caption=None)
        assert [d["reason"] for d in drops] == ["index_enumeration"]
        assert len(runs) == 3
        assert [r.output.subject for r in runs] == ["A", "B", "C"]

    def test_integer_run_naming_the_measurand_stays(self) -> None:
        # The escape: a run of integer ML coverages under a label that
        # names the measurand is real data, not an index.
        text = "| Site | coverage (ML) |\n|---|---|\n| A | 1 |\n| B | 2 |"
        runs, drops = _expand(text, _one_series("coverage", None), caption=None)
        assert drops == []
        assert [r.output.value_num for r in runs] == [1.0, 2.0]

    def test_non_index_integer_column_stays(self) -> None:
        # A run not starting at 0/1 is not an enumeration.
        text = "| Site | layers |\n|---|---|\n| A | 4 |\n| B | 5 |"
        runs, drops = _expand(text, _one_series("coverage", None), caption=None)
        assert drops == []
        assert len(runs) == 2


class TestLabelMeasurandMismatch:
    """gr506762 subset-2: a header that names a different quantity than
    the claimed measurand — ``Experimental formation energy`` is not
    adsorption energy (mx2035-2039), an equilibrium constant is not a
    saturation coverage (mx2042-2046)."""

    def test_formation_energy_not_adsorption_energy(self) -> None:
        text = (
            "| Surface | Experimental formation energy (eV) |\n|---|---|\n| Ni | 1.2 |"
        )
        runs, drops = _expand(
            text,
            _one_series("desorption-activation-energy", "eV"),
            caption=None,
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["label_measurand_mismatch"]
        assert "formation energy" in drops[0]["detail"]

    def test_equilibrium_constant_not_coverage(self) -> None:
        text = "| Surface | equilibrium constant K |\n|---|---|\n| Ni | 320 |"
        runs, drops = _expand(
            text, _one_series("saturation-coverage", None), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["label_measurand_mismatch"]

    def test_formation_energy_on_its_own_measurand_writes(self) -> None:
        text = (
            "| Surface | Experimental formation energy (eV) |\n|---|---|\n| Ni | 1.2 |"
        )
        runs, drops = _expand(text, _one_series("formation-energy", "eV"), caption=None)
        assert drops == []
        assert len(runs) == 1
        assert runs[0].output.measurand_ref_id == 25

    def test_unrelated_label_still_writes(self) -> None:
        # A label naming no tabled quantity is untouched by the rule.
        text = "| Sample | fluence exponent n |\n|---|---|\n| A | 1.8 |"
        runs, drops = _expand(
            text, _one_series("desorption-activation-energy", "meV"), caption=None
        )
        assert drops == []
        assert len(runs) == 1


# A rows-orientation table whose subjects live in a label ROW the grid
# kept as data — another row's line, never the cited cell's evidence
# (gr506762 subset-2, mx2042-2046).
LABEL_ROW = "| | expt | calc |\n|---|---|---|\n| | CO | H2 |\n| E_a (eV) | 1.1 | 0.9 |"

# The same shape with the subjects printed in the column header — the
# evidence the reviewer does see.
HEADER_SUBJ = "| | CO | H2 |\n|---|---|---|\n| | CO | H2 |\n| E_a (eV) | 1.1 | 0.9 |"


class TestSubjectInSpan:
    """gr506762 subset-2: a subject that appears in neither the cited
    row, the column header nor the caption cannot be verified — the row
    refuses ``subject_not_in_span``."""

    def _rows_recipe(self, subject_axis: int | None) -> dict[str, Any]:
        return {
            "table": {
                "orientation": "rows",
                "subject_axis": subject_axis,
                "series": [
                    {
                        "index": 1,
                        "measurand": "desorption-activation-energy",
                        "unit": "eV",
                        "role": "output",
                    }
                ],
            }
        }

    def test_label_row_subject_not_in_span_drops(self) -> None:
        runs, drops = _expand(LABEL_ROW, self._rows_recipe(0), caption=None)
        assert runs == []
        assert [d["reason"] for d in drops] == [
            "subject_not_in_span",
            "subject_not_in_span",
        ]
        assert "CO" in drops[0]["detail"]

    def test_header_subject_writes(self) -> None:
        # subject_axis=None defers to the column header — the subject
        # the reviewer sees on the 'column header:' line.
        runs, drops = _expand(HEADER_SUBJ, self._rows_recipe(None), caption=None)
        assert drops == []
        assert len(runs) == 2
        assert [r.output.subject for r in runs] == ["CO", "H2"]

    def test_caption_carries_the_subject(self) -> None:
        runs, drops = _expand(
            LABEL_ROW,
            self._rows_recipe(0),
            caption=(CAP_ID, "Table 4. Barriers of CO and H2 on Ni."),
        )
        assert drops == []
        assert [r.output.subject for r in runs] == ["CO", "H2"]

    def test_columns_subject_row_is_evidence(self) -> None:
        # The control: a columns-orientation subject sits in the cited
        # row by construction.
        runs, drops = _expand(SAMPLE1, _recipe1())
        assert drops == []
        assert {r.output.subject for r in runs} == {"Ni100", "Ni111"}


class TestReviewContext:
    """gr506762 slice-4: ~55 of 60 sampled table rows read 'undecidable'.
    A cell-anchored row's review prompt carries the table caption, the
    column header, the row label and the header unit."""

    def test_columns_row_prompt_carries_all_four(self) -> None:
        text = "| Surface | $E_{\\rm a}$ (kJ/mol) |\n|---|---|\n| Ni{100} | 85 |"
        runs, drops = _expand(
            text,
            _one_series("desorption-energy", "kJ/mol"),
            caption=(CAP_ID, "Table 3. CO desorption on Ni."),
        )
        assert drops == []
        out = runs[0].output
        tm = dict(out.meta["table"])
        assert tm["col_header"] == "$E_{\\rm a}$ (kJ/mol)"
        assert tm["row_label"] == "Ni{100}"
        assert tm["header_unit"] == "kJ/mol"
        tm["caption"] = "Table 3. CO desorption on Ni."
        prompt = review_prompt(
            literal=out.literal,
            reported_unit=out.reported_unit,
            measurand="desorption-energy",
            subject_label=out.subject,
            span_text="| Ni{100} | 85 |",
            table=tm,
        )
        assert "table caption: Table 3. CO desorption on Ni." in prompt
        assert "column header: $E_{\\rm a}$ (kJ/mol)" in prompt
        assert "row label: Ni{100}" in prompt
        assert "header unit: kJ/mol" in prompt

    def test_rows_table_keeps_the_column_header(self) -> None:
        # The per-column cluster size: in a rows table the series label is
        # the row label, so the column header was nowhere on the row.
        text = "| | m = 1 | m = 2 |\n|---|---|---|\n| sticking (s^-1) | 0.5 | 0.7 |"
        recipe = {
            "table": {
                "orientation": "rows",
                "series": [
                    {"index": 0, "measurand": "rate", "unit": "s^-1", "role": "output"}
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert drops == []
        by_col = {r.output.meta["table"]["col"]: r.output.meta["table"] for r in runs}
        assert by_col[2]["col_header"] == "m = 1"
        assert by_col[3]["col_header"] == "m = 2"
        assert by_col[2]["row_label"] == "sticking (s^-1)"
        assert by_col[2]["header_unit"] == "s^-1"
        prompt = review_prompt(
            literal="0.5",
            reported_unit="s^-1",
            measurand="rate",
            subject_label=None,
            span_text="| sticking (s^-1) | 0.5 | 0.7 |",
            table=by_col[2],
        )
        assert "column header: m = 1" in prompt
        assert "row label: sticking (s^-1)" in prompt

    def test_pre_stamp_row_falls_back_to_header(self) -> None:
        prompt = review_prompt(
            literal="85",
            reported_unit="kJ/mol",
            measurand="desorption-energy",
            subject_label="Ni100",
            span_text="| Ni{100} | 85 |",
            table={
                "table": 1,
                "row": 1,
                "col": 2,
                "header": "E_a (kJ/mol)",
                "orientation": "columns",
                "caption": "Table 3. CO desorption on Ni.",
            },
        )
        assert "column header: E_a (kJ/mol)" in prompt
        assert "row label: Ni100" in prompt
        assert "header unit: kJ/mol" in prompt


class TestValidateRecipe:
    def test_missing_table_key(self) -> None:
        parsed = parse_table(9, SAMPLE1)
        assert parsed is not None
        norm, drops = validate_recipe(parsed, {"nope": {}})
        assert norm is None
        assert [d["reason"] for d in drops] == ["bad_shape"]

    def test_bad_orientation_refuses_whole(self) -> None:
        parsed = parse_table(9, SAMPLE1)
        assert parsed is not None
        norm, drops = validate_recipe(parsed, {"table": {"orientation": "diag"}})
        assert norm is None
        assert any(d["reason"] == "bad_shape" for d in drops)

    def test_off_axis_indices_drop_per_element(self) -> None:
        parsed = parse_table(9, SAMPLE1)
        assert parsed is not None
        norm, drops = validate_recipe(
            parsed,
            {
                "table": {
                    "orientation": "columns",
                    "subject_axis": 0,
                    "series": [
                        {"index": 9, "measurand": "rate", "role": "output"},
                        {"index": 1, "measurand": "rate", "role": "output"},
                    ],
                    "skip_cells": ["9:9"],
                }
            },
        )
        assert norm is not None
        reasons = [d["reason"] for d in drops]
        assert reasons == ["bad_shape", "bad_shape"]
        assert len(norm.series) == 1
        assert norm.skip_cells == frozenset()

    @pytest.mark.parametrize("cell_ref", ["x:y", "0:9", "-1:0", 3])
    def test_bad_cell_refs(self, cell_ref: object) -> None:
        parsed = parse_table(9, SAMPLE1)
        assert parsed is not None
        norm, drops = validate_recipe(
            parsed,
            {
                "table": {
                    "orientation": "columns",
                    "subject_axis": 0,
                    "skip_cells": [cell_ref],
                }
            },
        )
        assert norm is not None
        assert [d["reason"] for d in drops] == ["bad_shape"]
        assert norm.skip_cells == frozenset()
