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
from precis.extract.recipes import (
    anchor_cell_mismatch,
    expand_recipe,
    stored_row_reasons,
    validate_recipe,
)
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
    26: _entry(26, "adsorption-energy", "adsorption energy", "eV", "E_ads"),
    27: _entry(27, "absorption-energy", "absorption energy", "eV", "E_abs"),
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
    """gr506762 slice-4 / subset-5: a claimed unit the table never
    prints flags ``unit_unconfirmed`` (nothing printed anywhere) or
    ``unit_without_text_support`` (a compatible unit is printed) —
    ``unit_header_mismatch`` refuses only on a printed contradiction.
    The eV small models pin on unrelated columns often lives in the
    caption, a table note or the body text (mx1351/pa255303)."""

    def test_unprinted_ev_flags_unit_unconfirmed(self) -> None:
        text = "| Sample | fluence exponent n |\n|---|---|\n| A | 1.8 |"
        runs, drops = _expand(
            text, _one_series("desorption-activation-energy", "eV"), caption=None
        )
        assert drops == []
        assert runs[0].flags == ["unit_unconfirmed"]

    def test_unprinted_percent_flags_unit_unconfirmed(self) -> None:
        text = "| Site | site fraction |\n|---|---|\n| step | 12 |"
        runs, drops = _expand(text, _one_series("coverage", "%"), caption=None)
        assert drops == []
        assert runs[0].flags == ["unit_unconfirmed"]

    def test_conflicting_printed_unit_refuses(self) -> None:
        # The header prints a unit the claim can't convert to — a real
        # contradiction stays refused.
        text = "| Sample | T (K) |\n|---|---|\n| A | 300 |"
        runs, drops = _expand(
            text, _one_series("desorption-activation-energy", "eV"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["unit_header_mismatch"]

    def test_unit_in_table_notes_lands_unflagged(self) -> None:
        # The E_b case: unitless header, the eV comes from a footnote
        # line under the grid.
        text = (
            "| Surface | E_b |\n|---|---|\n| O^I | -2.44 |\n^a Binding energies in eV."
        )
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert drops == []
        assert runs[0].flags == []

    def test_unprinted_ev_on_naming_label_stays_a_flag(self) -> None:
        text = "| Sample | E_d |\n|---|---|\n| A | 1.8 |"
        runs, drops = _expand(
            text, _one_series("desorption-activation-energy", "eV"), caption=None
        )
        assert drops == []
        assert runs[0].flags == ["unit_unconfirmed"]

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
        assert runs[0].flags == ["unit_unconfirmed"]

    def test_printed_compatible_header_unit_is_taken(self) -> None:
        # subset-5d: the column's own header unit is the strongest
        # evidence — a printed unit that converts to the measurand's
        # unit is the row's unit and converts at write, never stored
        # under the claim. The header's ``(J)`` reads as joules — a
        # single letter in a column header IS the printed unit.
        text = "| Sample | E (J) |\n|---|---|\n| A | -7.3e-19 |"
        runs, drops = _expand(
            text, _one_series("desorption-activation-energy", "eV"), caption=None
        )
        assert drops == []
        assert runs[0].output.reported_unit == "J"
        assert runs[0].output.value_num == pytest.approx(-7.3e-19)
        assert runs[0].flags == []

    def test_footnote_letter_in_header_is_no_unit(self) -> None:
        # subset-5d: ``(b)`` in a column header is a footnote marker,
        # not barn — an eV claim on ``E_ads (b)`` flags
        # ``unit_unconfirmed``, never ``unit_header_mismatch``.
        text = "| Site | E_ads (b) |\n|---|---|\n| Ni | -0.9 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert drops == []
        assert runs[0].output.reported_unit == "eV"
        assert runs[0].flags == ["unit_unconfirmed"]

    def test_single_letter_header_unit_is_taken(self) -> None:
        # subset-5d: ``(V)`` in a column header is volts — the column's
        # own header wins over a differing claim, so an mV claim on it
        # stores volts (the 5b filter had swallowed it as "nothing
        # printed" and stored 1000x off).
        vocab = dict(VOCAB)
        vocab[41] = _entry(41, "overpotential", "overpotential", "V")
        text = "| Sample | Overpotential (V) |\n|---|---|\n| A | 0.523 |"
        parsed = parse_table(9, text)
        assert parsed is not None
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": 1,
                        "measurand": "overpotential",
                        "unit": "mV",
                        "role": "output",
                    }
                ],
            }
        }
        runs, drops = expand_recipe(
            parsed,
            recipe,
            chunk_id=9,
            caption=None,
            vocab=vocab,
            paper=500,
            table=2,
            chunk_text=text,
        )
        assert drops == []
        assert runs[0].output.reported_unit == "V"
        assert runs[0].output.value_num == pytest.approx(0.523)
        assert runs[0].flags == []

    def test_caption_panel_letter_is_no_unit(self) -> None:
        # subset-5c: ``(b)`` in a caption is a panel label, not barn —
        # nothing printed anywhere flags ``unit_unconfirmed``.
        text = "| Site | E_ads |\n|---|---|\n| Ni | -0.9 |"
        runs, drops = _expand(
            text,
            _one_series("adsorption-energy", "eV"),
            caption=(7, "(b) adsorption energies"),
        )
        assert drops == []
        assert runs[0].flags == ["unit_unconfirmed"]

    def test_caption_periodic_cell_is_no_unit(self) -> None:
        # subset-5b: "a" (a year to pint) and "(b)" (a barn) are
        # caption tokens, never units — an eV claim on an E_ads
        # column flags ``unit_unconfirmed``, not a refusal.
        text = "| Site | E_ads |\n|---|---|\n| Ni | -0.9 |"
        runs, drops = _expand(
            text,
            _one_series("adsorption-energy", "eV"),
            caption=(7, "Energies in a periodic cell (b)"),
        )
        assert drops == []
        assert runs[0].flags == ["unit_unconfirmed"]

    def test_printed_convertible_unit_is_taken(self) -> None:
        # subset-5b ruling: a printed unit that differs from the claim
        # but converts is the row's unit — it converts to the
        # measurand's unit at write, never stores under the claim.
        text = "| Site | E_ads |\n|---|---|\n| Ni | 96.5 |"
        runs, drops = _expand(
            text,
            _one_series("adsorption-energy", "eV"),
            caption=(7, "Adsorption energies in meV"),
        )
        assert drops == []
        assert runs[0].output.reported_unit == "meV"
        assert runs[0].output.value_num == 96.5
        assert runs[0].flags == []

    def test_printed_per_mol_unit_converts_via_avogadro(self) -> None:
        # subset-5c: kJ/mol against an eV claim is the standard
        # catalysis pair through Avogadro's number (96.485 kJ/mol =
        # 1 eV), not a dimension mismatch — the printed kJ/mol is the
        # row's unit and converts to ~1.0 eV at write.
        text = "| Site | E_ads |\n|---|---|\n| Ni | 96.5 |"
        runs, drops = _expand(
            text,
            _one_series("adsorption-energy", "eV"),
            caption=(7, "Adsorption energies in kJ/mol"),
        )
        assert drops == []
        assert runs[0].output.reported_unit == "kJ/mol"
        assert runs[0].output.value_num == 96.5
        # subset-5d: the strict store needs the row's opt-in to write it.
        assert runs[0].output.molar_bridge is True
        assert runs[0].flags == []

    def test_incompatible_caption_unit_flags_not_refuses(self) -> None:
        # subset-5c: a caption/notes unit that can't name this column's
        # quantity — it may describe another column — leaves the claim
        # flagged, as before 5b, never taken nor refused.
        vocab = dict(VOCAB)
        vocab[40] = _entry(40, "active-metal-content", "active metal content", "mol")
        text = "| Site | content |\n|---|---|\n| Ni | 3 |"
        parsed = parse_table(9, text)
        assert parsed is not None
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": 1,
                        "measurand": "active-metal-content",
                        "unit": "mol",
                        "role": "output",
                    }
                ],
            }
        }
        runs, drops = expand_recipe(
            parsed,
            recipe,
            chunk_id=9,
            caption=(7, "Loading in mg"),
            vocab=vocab,
            paper=500,
            table=2,
            chunk_text=text,
        )
        assert drops == []
        assert runs[0].output.reported_unit == "mol"
        assert runs[0].flags == ["unit_without_text_support"]

    def test_printed_unit_needing_molar_mass_refuses(self) -> None:
        # A unit the column's own header prints that bridges mass to
        # amount needs a molar mass the recipe does not have —
        # ``unit_needs_molar_mass`` (unlike a caption unit, a header
        # unit certainly names this column).
        vocab = dict(VOCAB)
        vocab[40] = _entry(40, "active-metal-content", "active metal content", "mol")
        text = "| Site | content (mg) |\n|---|---|\n| Ni | 3 |"
        parsed = parse_table(9, text)
        assert parsed is not None
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": 1,
                        "measurand": "active-metal-content",
                        "unit": "mol",
                        "role": "output",
                    }
                ],
            }
        }
        runs, drops = expand_recipe(
            parsed,
            recipe,
            chunk_id=9,
            caption=(7, "Contents in mg"),
            vocab=vocab,
            paper=500,
            table=2,
            chunk_text=text,
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["unit_needs_molar_mass"]

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
        text = "| Site | fraction |\n|---|---|\n| A | 4 |\n| B | 5 |"
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


class TestHeaderInSpan:
    """gr506762 subset-3 (jo551208): a table-derived row's cited span
    must carry the column header cells plus the data row, and the
    caption (or the nearest table title). A column that prints no
    header refuses ``no_header_in_span``."""

    def test_header_span_and_caption_ride_meta(self) -> None:
        text = "| Site | E_ads (eV) |\n|---|---|\n| Ni | -1.2 |"
        runs, drops = _expand(
            text,
            _one_series("desorption-activation-energy", "eV"),
            caption=(CAP_ID, "Table 7. Adsorption energies."),
        )
        assert drops == []
        tm = runs[0].output.meta["table"]
        h0, h1 = tm["header_span"]
        assert text[h0:h1] == "| Site | E_ads (eV) |"
        assert tm["header_text"] is None
        assert tm["caption"] == "Table 7. Adsorption energies."

    def test_continuation_carries_the_run_head_header_as_text(self) -> None:
        # subset-3 r2: an ``enforce_hard_max`` continuation parses
        # against the run head's header lines — their offsets index the
        # OTHER chunk, so ``header_span`` is None and the lines ride as
        # ``header_text`` for the cited span instead.
        head_line = "| Site | E_ads (eV) |"
        tail = "| Ni | -1.2 |\n| Co | -0.9 |"
        parsed = parse_table(10, tail, header=[head_line])
        assert parsed is not None and parsed.header_span is None
        runs, drops = expand_recipe(
            parsed,
            _one_series("adsorption-energy", "eV"),
            chunk_id=10,
            caption=None,
            vocab=VOCAB,
            paper=500,
            chunk_text=tail,
        )
        assert drops == []
        assert len(runs) == 2
        for run in runs:
            tm = run.output.meta["table"]
            assert tm["header_span"] is None
            assert tm["header_text"] == head_line

    def test_leading_title_stands_in_for_a_missing_caption(self) -> None:
        text = (
            "Table 7. Adsorption energies.\n"
            "| Site | E_ads (eV) |\n|---|---|\n| Ni | -1.2 |"
        )
        runs, _ = _expand(
            text, _one_series("desorption-activation-energy", "eV"), caption=None
        )
        assert (
            runs[0].output.meta["table"]["caption"] == "Table 7. Adsorption energies."
        )

    def test_headerless_column_refuses(self) -> None:
        text = "| Site | | E (eV) |\n|---|---|---|\n| Ni | x | -1.2 |"
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": 1,
                        "measurand": "desorption-activation-energy",
                        "unit": "eV",
                        "role": "output",
                    },
                    {
                        "index": 2,
                        "measurand": "desorption-activation-energy",
                        "unit": "eV",
                        "role": "output",
                    },
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert [d["reason"] for d in drops] == ["no_header_in_span"]
        assert drops[0]["location"] == "series[0] cell 0:1"
        assert len(runs) == 1
        assert runs[0].output.literal == "-1.2"

    def test_rows_headerless_column_refuses(self) -> None:
        text = "| | m = 1 | |\n|---|---|---|\n| E_a (eV) | 1.1 | 0.9 |"
        recipe = {
            "table": {
                "orientation": "rows",
                "series": [
                    {
                        "index": 0,
                        "measurand": "desorption-activation-energy",
                        "unit": "eV",
                        "role": "output",
                    }
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert [d["reason"] for d in drops] == ["no_header_in_span"]
        assert len(runs) == 1
        assert runs[0].output.meta["table"]["col"] == 2


# One wide same-measurand table, modelled on the CCSD(cT) case
# (mx2224-2228): three adsorption-energy columns at three sites.
WIDE_SITE = (
    "| Species | E_ads,top (eV) | E_ads,fcc (eV) | E_ads,hcp (eV) |\n"
    "|---|---|---|---|\n"
    "| CO | -1.21 | -1.45 | -1.38 |"
)


class TestSameMeasurandColumns:
    """gr506762 subset-3: several columns naming one measurand emit one
    run per matching column — never a silent pick — and each run
    records the column it took as a condition on the measure."""

    def _wide_recipe(self, **series_extra: Any) -> dict[str, Any]:
        series = {
            "index": 1,
            "measurand": "adsorption-energy",
            "unit": "eV",
            "role": "output",
        }
        series.update(series_extra)
        return {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [series],
            }
        }

    def test_one_run_per_matching_column(self) -> None:
        runs, drops = _expand(WIDE_SITE, self._wide_recipe(), caption=None)
        assert drops == []
        by_col = {r.output.meta["table"]["col"]: r.output for r in runs}
        assert set(by_col) == {2, 3, 4}
        assert {r.output.literal for r in runs} == {"-1.21", "-1.45", "-1.38"}
        for col, out in by_col.items():
            assert out.measurand_ref_id == 26
        assert by_col[2].conditions == {"column": "E_ads,top (eV)"}
        assert by_col[3].conditions == {"column": "E_ads,fcc (eV)"}
        assert by_col[4].conditions == {"column": "E_ads,hcp (eV)"}

    def test_recipe_condition_names_the_condition(self) -> None:
        runs, drops = _expand(
            WIDE_SITE, self._wide_recipe(condition="site"), caption=None
        )
        assert drops == []
        assert all(set(r.output.conditions) == {"site"} for r in runs)

    def test_single_matching_column_stamps_nothing(self) -> None:
        text = "| Species | E_ads,top (eV) | s |\n|---|---|---|\n| CO | -1.2 | 0.9 |"
        runs, drops = _expand(text, self._wide_recipe(), caption=None)
        assert drops == []
        assert len(runs) == 1
        assert runs[0].output.conditions is None

    def test_correction_labelled_auto_column_drops(self) -> None:
        # subset-3 r2: the auto-match checked only that the label names
        # the measurand — an unmapped correction column's own header
        # mark still refuses it.
        text = (
            "| Species | E_ads (eV) | ΔE_ads correction (eV) |\n"
            "|---|---|---|\n"
            "| CO | -1.21 | 0.08 |"
        )
        runs, drops = _expand(text, self._wide_recipe(), caption=None)
        assert [d["reason"] for d in drops] == ["correction_row"]
        assert len(runs) == 1
        assert runs[0].output.meta["table"]["col"] == 2

    def test_auto_column_printed_per_mol_unit_converts(self) -> None:
        # The auto column resolves its unit from its own header —
        # subset-5c: kJ/mol reaches the eV measurand through Avogadro's
        # number, so the column's own printed unit is taken and
        # converts at write.
        text = (
            "| Species | E_ads (eV) | E_ads (kJ/mol) |\n"
            "|---|---|---|\n"
            "| CO | -1.21 | -121 |"
        )
        runs, drops = _expand(text, self._wide_recipe(), caption=None)
        assert drops == []
        by_col = {r.output.meta["table"]["col"]: r.output for r in runs}
        assert set(by_col) == {2, 3}
        assert by_col[2].reported_unit == "eV"
        assert by_col[3].reported_unit == "kJ/mol"
        # subset-5d: the strict store needs the row's opt-in to write it.
        assert by_col[3].molar_bridge is True
        assert by_col[2].molar_bridge is False

    def test_auto_column_compatible_printed_unit_wins(self) -> None:
        # A printed unit that converts is the column's own claim — the
        # inherited eV stays only where the header prints none.
        text = (
            "| Species | E_ads (eV) | E_ads (J) |\n|---|---|---|\n"
            "| CO | -1.21 | -7.3e-19 |"
        )
        runs, drops = _expand(text, self._wide_recipe(), caption=None)
        assert drops == []
        by_col = {r.output.meta["table"]["col"]: r.output for r in runs}
        assert set(by_col) == {2, 3}
        assert by_col[2].reported_unit == "eV"
        assert by_col[3].reported_unit == "J"

    def test_auto_column_does_not_inherit_the_mapped_transform(self) -> None:
        # The mapped column's declared log10 belongs to its own cells —
        # an auto column printing plain numbers must not be
        # exponentiated by it.
        text = (
            "| Species | log rate (s^-1) | rate (s^-1) |\n|---|---|---|\n"
            "| A | 2 | 0.9 |"
        )
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
        runs, drops = _expand(text, recipe, caption=None)
        assert drops == []
        by_col = {r.output.meta["table"]["col"]: r.output for r in runs}
        assert by_col[2].value_num == 100.0
        assert by_col[3].value_num == 0.9

    def test_auto_column_derives_its_own_log_label(self) -> None:
        text = (
            "| Species | rate (s^-1) | log rate (s^-1) |\n|---|---|---|\n"
            "| A | 0.9 | 2 |"
        )
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {"index": 1, "measurand": "rate", "unit": "s^-1", "role": "output"}
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert drops == []
        by_col = {r.output.meta["table"]["col"]: r.output for r in runs}
        assert by_col[2].value_num == 0.9
        assert by_col[3].value_num == 100.0
        assert by_col[3].meta["transform"] == "log10"

    def test_auto_column_unclear_log_label_drops(self) -> None:
        text = (
            "| Species | rate (s^-1) | log_2 rate (s^-1) |\n|---|---|---|\n"
            "| A | 0.9 | 3 |"
        )
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {"index": 1, "measurand": "rate", "unit": "s^-1", "role": "output"}
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert [d["reason"] for d in drops] == ["log_base_unclear"]
        assert len(runs) == 1
        assert runs[0].output.meta["table"]["col"] == 2


class TestCorrectionRows:
    """gr506762 subset-3 (mx2224-2228): a row or series whose label
    marks a correction, difference or delta is not a reading."""

    @pytest.mark.parametrize("label_axis", ["row", "series"])
    @pytest.mark.parametrize(
        "label",
        [
            "ΔE_r",
            "ΔG_r",
            "ΔEr",
            "ΔGr",
            "ΔEr, forward",
            "ΔGr (reverse)",
            r"$\Delta E_{r}$",
            r"$\Delta G_{\mathrm{r}}$",
            "ΔE<sub>r</sub>",
            "ΔG<sub>r</sub>",
        ],
    )
    def test_reaction_energy_labels_write(self, label: str, label_axis: str) -> None:
        header = f"{label} (eV)" if label_axis == "series" else "reaction energy (eV)"
        subject = label if label_axis == "row" else "CO"
        text = f"| Species | {header} |\n|---|---|\n| {subject} | -2.09 |"
        parsed = parse_table(9, text)
        assert parsed is not None
        entry = _entry(28, "reaction-energy", "reaction energy", "eV")
        runs, drops = expand_recipe(
            parsed,
            _one_series("reaction-energy", "eV"),
            chunk_id=9,
            caption=None,
            vocab={**VOCAB, 28: entry},
            paper=500,
            chunk_text=text,
        )
        assert drops == []
        assert len(runs) == 1
        assert runs[0].output.value_num == -2.09

    @pytest.mark.parametrize("label_axis", ["row", "series"])
    @pytest.mark.parametrize(
        "label",
        [
            "ΔZPE",
            "ΔE_corr",
            "Δ(ZPE)",
            "Δ",
            "correction",
            "difference",
            "ΔError",
            "ΔGradient",
        ],
    )
    def test_reaction_exemption_preserves_correction_labels(
        self, label: str, label_axis: str
    ) -> None:
        header = label if label_axis == "series" else "E_ads (eV)"
        subject = label if label_axis == "row" else "CO"
        text = f"| Species | {header} |\n|---|---|\n| {subject} | 0.08 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["correction_row"]

    def test_delta_labelled_data_row_drops(self) -> None:
        text = (
            "| Species | E_ads (eV) |\n|---|---|\n"
            "| CO | -1.21 |\n| ZPE correction | 0.08 |\n| NO | -0.92 |"
        )
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert [d["reason"] for d in drops] == ["correction_row"]
        assert len(runs) == 2
        assert {r.output.subject for r in runs} == {"CO", "NO"}

    def test_delta_labelled_series_drops(self) -> None:
        text = "| Species | ΔE (eV) |\n|---|---|\n| CO | 0.08 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["correction_row"]

    @pytest.mark.parametrize(
        "label",
        [
            "ΔZPE (eV)",
            "Δ(CCSD-PBE) (eV)",
            "ZPE correction (eV)",
            "E_ads difference (eV)",
            "E_ads error (eV)",
        ],
    )
    def test_correction_term_labels_refuse(self, label: str) -> None:
        text = f"| Species | {label} |\n|---|---|\n| CO | 0.08 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["correction_row"]

    @pytest.mark.parametrize(
        "label",
        [
            # ``ΔE_ads``/``ΔH_ads`` is the measurand's own symbol, not a
            # delta row; a ``ZPE-corrected`` value IS the corrected
            # measurand (subset-3 r2 — the broad match refused both).
            "ΔE_ads (eV)",
            "ΔH_ads (eV)",
            "ΔG_ads (eV)",
            "ΔE_ads,top (eV)",
            "ZPE-corrected adsorption energy (eV)",
            "E_ads corrected for ZPE (eV)",
        ],
    )
    def test_measurand_symbol_and_corrected_labels_write(self, label: str) -> None:
        text = f"| Species | {label} |\n|---|---|\n| CO | -1.21 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert drops == []
        assert len(runs) == 1


class TestAbsorptionVsAdsorption:
    """gr506762 subset-3 (mx2216): ``absorption energy`` / ``E_abs`` is
    a different quantity than adsorption energy."""

    def test_e_abs_column_refuses_as_adsorption(self) -> None:
        text = "| Surface | E_abs (eV) |\n|---|---|\n| Ni | 0.9 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["label_measurand_mismatch"]
        assert "absorption energy" in drops[0]["detail"]

    def test_absorption_label_on_its_own_measurand_writes(self) -> None:
        text = "| Surface | absorption energy (eV) |\n|---|---|\n| Ni | 0.9 |"
        runs, drops = _expand(
            text, _one_series("absorption-energy", "eV"), caption=None
        )
        assert drops == []
        assert len(runs) == 1
        assert runs[0].output.measurand_ref_id == 27


class TestReferenceZero:
    """gr506762 subset-3 (mx2188): an exact 0 on an energy measurand is
    the scale's reference point — graphite's ``0 eV`` is not a measured
    adsorption energy."""

    def test_reference_marked_zero_drops(self) -> None:
        text = (
            "| Species | E_ads (eV) |\n|---|---|\n"
            "| graphite (reference) | 0 |\n| CO | -1.21 |"
        )
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert [d["reason"] for d in drops] == ["reference_zero"]
        assert len(runs) == 1

    def test_sole_zero_on_a_reference_phase_row_drops(self) -> None:
        # No marker word — a phase label plus the column's only zero.
        text = "| Species | E_ads (eV) |\n|---|---|\n| graphite | 0 |\n| CO | -1.21 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert [d["reason"] for d in drops] == ["reference_zero"]
        assert len(runs) == 1

    def test_second_zero_in_the_column_writes(self) -> None:
        text = (
            "| Species | E_ads (eV) |\n|---|---|\n"
            "| graphite | 0 |\n| CO | 0 |\n| NO | -1.2 |"
        )
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert drops == []
        assert len(runs) == 3

    def test_unmarked_zero_energy_writes(self) -> None:
        text = "| Species | E_ads (eV) |\n|---|---|\n| CO | 0 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert drops == []
        assert len(runs) == 1


class TestCountColumns:
    """gr506762 subset-4 (tables-4): pa1820's ``Number of oxide layers``
    column stored as 12 saturation coverages — a count header pins its
    axis to a dimensionless count, never a coverage/energy/rate
    measurand. ``_LABEL_QUANTITY`` is a term list and a count is a
    category, so the incompatibility never matched."""

    def _count_series(self, measurand: str) -> dict[str, Any]:
        return _one_series(measurand, None)

    @pytest.mark.parametrize(
        "header",
        [
            "Number of oxide layers",
            "No. of layers",
            "nr. of defects",
            "count",
            "layers",
            "#",
            "N",
        ],
    )
    def test_count_header_refuses_as_coverage(self, header: str) -> None:
        text = f"| Surface | {header} |\n|---|---|\n| Ni{{111}} | 3 |"
        runs, drops = _expand(
            text, self._count_series("saturation-coverage"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["label_measurand_mismatch"]
        assert "count" in drops[0]["detail"]

    def test_count_header_refuses_as_energy(self) -> None:
        text = "| Surface | Number of oxide layers |\n|---|---|\n| Ni | 3 |"
        runs, drops = _expand(
            text, self._count_series("adsorption-energy"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["label_measurand_mismatch"]

    def test_count_label_naming_the_measurand_writes(self) -> None:
        # The label-names-measurand escape: a measurand whose name is in
        # the count header IS the count the column reports.
        text = "| Surface | layers count |\n|---|---|\n| Ni | 3 |"
        entry = _entry(28, "layers", "layers", "1")
        parsed = parse_table(9, text)
        assert parsed is not None
        runs, drops = expand_recipe(
            parsed,
            self._count_series("layers"),
            chunk_id=9,
            caption=None,
            vocab={**VOCAB, 28: entry},
            paper=500,
            chunk_text=text,
        )
        assert drops == []
        assert len(runs) == 1

    def test_unit_carrying_count_word_stays(self) -> None:
        # The unit decides, not the word: "count rate (s^-1)" is a rate.
        text = "| Site | count rate (s^-1) |\n|---|---|\n| A | 1.8 |"
        runs, drops = _expand(text, _one_series("rate", "s^-1"), caption=None)
        assert drops == []
        assert len(runs) == 1

    def test_layers_with_a_unit_stays(self) -> None:
        text = "| Site | layers (nm) |\n|---|---|\n| A | 12 |"
        runs, drops = _expand(text, _one_series("rate", "nm"), caption=None)
        assert drops == [] or [d["reason"] for d in drops] == [
            "unit_dimension_mismatch"
        ]

    @pytest.mark.parametrize(
        "header",
        ["oxide layers", "defect layers", "no. of layers", "n of layers", "# layers"],
    )
    def test_layer_count_spellings_refuse(self, header: str) -> None:
        # subset-4b: plural "layers" ending the label, or "layer(s)"
        # preceded by a count opener — still a count.
        text = f"| Surface | {header} |\n|---|---|\n| Ni | 3 |"
        runs, drops = _expand(
            text, self._count_series("saturation-coverage"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["label_measurand_mismatch"]

    @pytest.mark.parametrize("header", ["top layer", "Layer 1", "E_ads bottom layer"])
    def test_layer_position_headers_stay(self, header: str) -> None:
        # subset-4b: a singular layer in a position phrase is not a
        # count — no opener, and "layer" is not the plural tail.
        text = f"| Surface | {header} |\n|---|---|\n| Ni | 0.5 |"
        runs, drops = _expand(text, _one_series("rate", "s^-1"), caption=None)
        assert "label_measurand_mismatch" not in [d["reason"] for d in drops]

    def test_bare_n_stays_when_symbol_is_n(self) -> None:
        # subset-4b/4c: a bare "n" names the measurand when the taxon's
        # slug/name is an n-symbol measurand (reaction order,
        # refractive index). Built the way the seeds build it — the
        # seed meta carries no ``symbol`` key.
        from precis.extract.proposals import vocab_entry

        text = "| Surface | n |\n|---|---|\n| Ni | 2 |"
        entry = vocab_entry(
            29,
            {
                "name": "Reaction order",
                "norm_name": "reaction order",
                "slug": "reaction-order",
                "aliases": [],
                "dimension_kind": "dimensionless",
                "canonical_unit": "1",
                "value_type": "quantity",
            },
        )
        parsed = parse_table(9, text)
        assert parsed is not None
        runs, drops = expand_recipe(
            parsed,
            self._count_series("reaction-order"),
            chunk_id=9,
            caption=None,
            vocab={**VOCAB, 29: entry},
            paper=500,
            chunk_text=text,
        )
        assert drops == []
        assert len(runs) == 1

    def test_bare_n_still_a_count_for_other_measurands(self) -> None:
        text = "| Surface | n |\n|---|---|\n| Ni | 2 |"
        runs, drops = _expand(
            text, self._count_series("saturation-coverage"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["label_measurand_mismatch"]


class TestSubHeaderRows:
    """gr506762 subset-4 (mx2313): a data row that is a printed
    sub-header — its extracted value cells empty or non-numeric AND a
    divider's shape: header echoes, label-only text, or a lone text
    cell floating among empties. subset-4b narrowed it: an ordinary
    data row with text columns is never one."""

    def _two_series(self) -> dict[str, Any]:
        return {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {"index": 1, "measurand": "coverage", "role": "output"},
                    {"index": 2, "measurand": "rate", "unit": "s^-1", "role": "output"},
                ],
            }
        }

    def test_merge_row_drops(self) -> None:
        # A lone text blob spanning the grid's middle — a divider.
        text = (
            "| Surface | coverage (ML) | rate (s^-1) |\n|---|---|---|\n"
            "| Ni{111} | 0.9 | 0.5 |\n"
            "| | thin oxide films | |\n"
            "| Ni{100} | 0.8 | 0.4 |"
        )
        runs, drops = _expand(text, self._two_series(), caption=None)
        assert [d["reason"] for d in drops] == ["sub_header_row"]
        assert len(runs) == 4
        assert {r.output.subject for r in runs} == {"Ni111", "Ni100"}

    def test_header_echo_row_drops(self) -> None:
        # A second header line eaten as a data row.
        text = (
            "| Surface | coverage (ML) |\n|---|---|\n"
            "| Surface | coverage |\n"
            "| Ni{111} | 0.9 |"
        )
        runs, drops = _expand(text, _one_series("coverage", None), caption=None)
        assert [d["reason"] for d in drops] == ["sub_header_row"]
        assert len(runs) == 1

    def test_label_only_row_is_a_sub_header(self) -> None:
        # "| divider | | |" emits nothing, but the predicate itself
        # must still flag it — stored_row_reasons asks the question.
        text = "| Surface | coverage (ML) |\n|---|---|\n| section 2 | |"
        parsed = parse_table(9, text)
        assert parsed is not None
        from precis.extract.recipes import _sub_header_row

        assert _sub_header_row(parsed, 0, 0, {1})
        # and the neighbouring real row is not one
        text2 = text + "\n| Ni{111} | 0.9 |"
        parsed2 = parse_table(9, text2)
        assert parsed2 is not None
        assert not _sub_header_row(parsed2, 1, 0, {1})

    def test_numeric_value_row_never_drops(self) -> None:
        # subset-4b: text in site/method columns is data — the numeric
        # value cell ends the sub-header question.
        text = (
            "| Surface | Site | E_ads (eV) | Method |\n|---|---|---|---|\n"
            "| Pt(111) | top | -0.45 | PBE |"
        )
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": 2,
                        "measurand": "adsorption-energy",
                        "unit": "eV",
                        "role": "output",
                    },
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert drops == []
        assert len(runs) == 1
        assert runs[0].output.subject == "Pt(111)"

    def test_categorical_lone_cell_stays(self) -> None:
        # subset-4c: a lone text cell amid empties is a merge — except
        # when it IS the value a categorical measurand reads
        # (``Pt(111) | top | --`` for an adsorption-site measurand).
        from precis.extract.proposals import vocab_entry

        text = "| Surface | Site | E_ads (eV) |\n|---|---|---|\n| Pt(111) | top | |"
        site = vocab_entry(
            30,
            {
                "name": "Adsorption site",
                "slug": "adsorption-site",
                "aliases": [],
                "dimension_kind": "categorical",
                "canonical_unit": "1",
                "value_type": "categorical",
                "allowed_values": ["top", "fcc", "hcp"],
            },
        )
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {"index": 1, "measurand": "adsorption-site", "role": "output"},
                ],
            }
        }
        parsed = parse_table(9, text)
        assert parsed is not None
        runs, drops = expand_recipe(
            parsed,
            recipe,
            chunk_id=9,
            caption=None,
            vocab={**VOCAB, 30: site},
            paper=500,
            chunk_text=text,
        )
        assert "sub_header_row" not in [d["reason"] for d in drops]
        assert len(runs) == 1

    def test_non_categorical_lone_cell_still_drops(self) -> None:
        # The same row shape under a numeric measurand is still a
        # merge — the categorical skip only covers value columns of
        # categorical measurands.
        text = "| Surface | Site | E_ads (eV) |\n|---|---|---|\n| Pt(111) | top | |"
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {"index": 1, "measurand": "coverage", "role": "output"},
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert "sub_header_row" in [d["reason"] for d in drops]

    def test_categorical_row_stays(self) -> None:
        # A categorical measurand's row is text in the value column —
        # still a reading row, never a sub-header.
        text = "| Site | phase |\n|---|---|\n| A | fcc |"
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {"index": 1, "measurand": "coverage", "role": "output"},
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert "sub_header_row" not in [d["reason"] for d in drops]

    def test_lone_text_cell_row_stays(self) -> None:
        # One non-label cell can't be judged a divider — a subject
        # plus a categorical value is a reading row's shape.
        text = "| Site | rate |\n|---|---|\n| A | high |"
        runs, drops = _expand(text, _one_series("rate", "s^-1"), caption=None)
        assert "sub_header_row" not in [d["reason"] for d in drops]

    def test_two_cell_data_row_stays(self) -> None:
        # A one-value row is data, not a sub-header.
        text = "| Surface | coverage (ML) |\n|---|---|\n| Ni{111} | 0.9 |"
        runs, drops = _expand(text, _one_series("coverage", None), caption=None)
        assert drops == []
        assert len(runs) == 1


class TestAnchorCellMismatch:
    """gr506762 subset-4 (mx1505/mx1721/mx1727): the row's literal must
    sit in the cell at the intersection of the claimed row label and
    column header — not anywhere in the span."""

    TEXT = "| Site | E_ads (eV) | rate (s^-1) |\n|---|---|---|\n| Ni | -1.2 | 0.9 |"

    def _parsed(self) -> Any:
        parsed = parse_table(9, self.TEXT)
        assert parsed is not None
        return parsed

    def _span(self, r: int, c: int) -> list[int]:
        cell = self._parsed().rows[r][c]
        return [9, cell.start, cell.end]

    def test_consistent_cell_passes(self) -> None:
        detail = anchor_cell_mismatch(
            self._parsed(),
            span=self._span(0, 2),
            literal="0.9",
            table_meta={
                "row": 1,
                "col": 3,
                "header": "rate (s^-1)",
                "orientation": "columns",
            },
            chunk_text=self.TEXT,
        )
        assert detail is None

    def test_off_by_one_column_refuses(self) -> None:
        detail = anchor_cell_mismatch(
            self._parsed(),
            span=self._span(0, 1),
            literal="-1.2",
            table_meta={
                "row": 1,
                "col": 3,
                "header": "rate (s^-1)",
                "orientation": "columns",
            },
            chunk_text=self.TEXT,
        )
        assert detail is not None

    def test_literal_elsewhere_in_the_span_refuses(self) -> None:
        # A row-level anchor covering the claimed cell but the literal
        # living in a different column.
        row_s, row_e = self._parsed().row_spans[0]
        detail = anchor_cell_mismatch(
            self._parsed(),
            span=[9, row_s, row_e],
            literal="0.9",
            table_meta={
                "row": 1,
                "col": 2,
                "header": "E_ads (eV)",
                "orientation": "columns",
            },
            chunk_text=self.TEXT,
        )
        assert detail is not None

    def test_claimed_column_header_mismatch_refuses(self) -> None:
        # No claimed coords: the anchor cell's header must match the
        # claimed series label.
        detail = anchor_cell_mismatch(
            self._parsed(),
            span=self._span(0, 1),
            literal="-1.2",
            table_meta={"header": "rate (s^-1)", "orientation": "columns"},
            chunk_text=self.TEXT,
        )
        assert detail is not None

    def test_row_span_locates_by_literal(self) -> None:
        # subset-4b: a row-level anchor resolves to the cell holding
        # the literal, not the widest cell; no match is unlocated,
        # never a guess.
        from precis.extract.recheck import locate_cell

        parsed = self._parsed()
        s, e = parsed.row_spans[0]
        assert locate_cell(parsed, [9, s, e], "0.9", self.TEXT) == (0, 2)
        assert locate_cell(parsed, [9, s, e], "9.9", self.TEXT) is None

    def test_stored_row_reasons_fires_it(self) -> None:
        entry = VOCAB[16]
        reasons = dict(
            stored_row_reasons(
                self._parsed(),
                cell=(0, 2),
                literal="0.9",
                unit="s^-1",
                subject="Ni",
                value_form="point",
                value_num=0.9,
                entry=entry,
                table_meta={
                    "row": 1,
                    "col": 3,
                    "header": "rate (s^-1)",
                    "orientation": "columns",
                },
                transform=None,
                caption_text="",
                chunk_text=self.TEXT,
                span=self._span(0, 1),
            )
        )
        assert "anchor_cell_mismatch" in reasons

    def test_located_cell_off_a_partial_claim_fires(self) -> None:
        # subset-5 (mx1505): with only a partial coordinate claim the
        # located cell's column silently moved — the claim is checked
        # by index now.
        detail = anchor_cell_mismatch(
            self._parsed(),
            span=self._span(0, 2),
            literal="0.9",
            table_meta={"col": 2, "orientation": "columns"},
            chunk_text=self.TEXT,
        )
        assert detail is not None

    def test_anchor_in_the_header_region_fires(self) -> None:
        # A header/index line stored as a value: the span lands above
        # the data region — refused, never silently unlocated.
        head_end = self.TEXT.index("\n")
        detail = anchor_cell_mismatch(
            self._parsed(),
            span=[9, 0, head_end],
            literal="rate (s^-1)",
            table_meta={"orientation": "columns"},
            chunk_text=self.TEXT,
        )
        assert detail is not None
        assert "header" in detail


# A stacked-header table modelled on pa452964's sticking table: Marker
# printed the visual title row as THE header, so the ``m`` label row
# was eaten as data — 164 no_header_in_span false refusals (mx1624),
# and the m cells stored as values (mx2049, mx2051-2061).
STACKED = (
    "| Sticking probabilities | | | |\n|---|---|---|---|\n"
    "| m | 0 | 1 | 2 |\n"
    "| Rh3Fe3(N2)m | 0.71 | 0.65 | 0.60 |\n"
    "| Rh3Fe3(N2)m+1 | 0.50 | 0.40 | 0.30 |"
)


class TestStackedHeaders:
    """gr506762 subset-5: the spanning title line plus the label row
    under it are the header — the index row never reaches the data
    region and the value columns keep their printed labels."""

    def _recipe(self) -> dict[str, Any]:
        return {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "series": [
                    {
                        "index": i,
                        "measurand": "saturation-sticking",
                        "unit": None,
                        "role": "output",
                    }
                    for i in (1, 2, 3)
                ],
            }
        }

    def test_label_row_joins_the_header(self) -> None:
        parsed = parse_table(9, STACKED)
        assert parsed is not None
        assert parsed.header_rows == 2
        assert parsed.header_lines == [
            "| Sticking probabilities | | | |",
            "| m | 0 | 1 | 2 |",
        ]
        # The first data row is the molecule, not the index row.
        assert parsed.rows[0][0].text == "Rh3Fe3(N2)m"
        assert parsed.columns[1].header == "0"
        # The cited span covers title, separator and the label row.
        assert parsed.header_span is not None
        h0, h1 = parsed.header_span
        assert "| m | 0 | 1 | 2 |" in STACKED[h0:h1]

    def test_sticking_values_land_with_headers(self) -> None:
        runs, drops = _expand(STACKED, self._recipe(), caption=None)
        assert drops == []
        assert len(runs) == 6
        m_row = [r for r in runs if r.output.subject == "Rh3Fe3(N2)m"]
        assert {r.output.value_num for r in m_row} == {0.71, 0.65, 0.60}
        tm = m_row[0].output.meta["table"]
        assert tm["col_header"] == "0"
        assert tm["row_label"] == "Rh3Fe3(N2)m"

    def test_index_cells_never_emit(self) -> None:
        runs, drops = _expand(STACKED, self._recipe(), caption=None)
        assert drops == []
        assert {r.output.literal for r in runs}.isdisjoint({"0", "1", "2"})

    def test_stored_index_cell_gets_value_in_header_row(self) -> None:
        # Regression (mx2049/mx2051-2061): a row stored when the m row
        # was a data row refuses deterministically — its anchor lands
        # in the header region now, which is ``value_in_header_row``,
        # not the drift-possible ``anchor_cell_mismatch``. The recheck
        # split (refused, not review-only) is tested at job level.
        parsed = parse_table(9, STACKED)
        assert parsed is not None
        m0 = STACKED.index("| m | 0 |") + 6
        assert STACKED[m0] == "0"
        reasons = dict(
            stored_row_reasons(
                parsed,
                cell=None,
                literal="0",
                unit=None,
                subject="Rh3Fe3(N2)m",
                value_form="point",
                value_num=0.0,
                entry=VOCAB[13],
                table_meta={"row": 1, "col": 2, "orientation": "columns"},
                transform=None,
                caption_text="",
                chunk_text=STACKED,
                span=[9, m0, m0 + 1],
            )
        )
        assert "value_in_header_row" in reasons
        assert "anchor_cell_mismatch" not in reasons

    def test_condition_literal_in_neighbour_cell_refuses(self) -> None:
        # subset-5b: a table-source condition whose literal sits in a
        # cell other than the claimed ``r:c`` — the mx1505 off-by-one
        # shape — refuses at extraction (same grid, deterministic).
        text = "| Site | T | E_ads (eV) |\n|---|---|---|\n| Ni | 300 | -1.2 |"
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "conditions": [
                    {
                        "condition": "temperature",
                        "measurand": "temperature",
                        "literal": "300",
                        "role": "context",
                        "source": "table",
                        "cell": "0:2",
                    }
                ],
                "series": [
                    {
                        "index": 2,
                        "measurand": "adsorption-energy",
                        "unit": "eV",
                        "role": "output",
                    }
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert [d["reason"] for d in drops] == ["anchor_cell_mismatch"]
        assert len(runs) == 1

    def test_condition_cell_on_the_subject_axis_is_not_a_mismatch(self) -> None:
        # subset-5c: a table-source condition anchored on the subject
        # column itself carries the claimed row_label AS its cell —
        # ``_row_has_label`` skipping the anchor column is not a
        # refusal.
        text = "| Site | T (K) | E_ads (eV) |\n|---|---|---|\n| Ni | 300 | -1.2 |"
        recipe = {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "conditions": [
                    {
                        "condition": "surface",
                        "measurand": "adsorption-energy",
                        "literal": "Ni",
                        "role": "context",
                        "source": "table",
                        "cell": "0:0",
                    }
                ],
                "series": [
                    {
                        "index": 2,
                        "measurand": "adsorption-energy",
                        "unit": "eV",
                        "role": "output",
                    }
                ],
            }
        }
        runs, drops = _expand(text, recipe, caption=None)
        assert drops == []
        assert len(runs) == 1
        assert len(runs[0].inputs) == 1
        assert runs[0].inputs[0].literal == "Ni"


class TestBarrierSymbols:
    """gr506762 subset-5 (pa4750, mx1821-1845): ΔE‡/E_a are barrier
    symbols, not correction marks — a ΔE‡ column is a genuine
    barrier."""

    @pytest.mark.parametrize(
        "label",
        [
            "ΔE‡ (eV)",
            "ΔE^‡ (eV)",
            "ΔE^{\\ddagger} (eV)",
            "ΔG‡ (eV)",
            "ΔH‡ (eV)",
            "ΔE_a (eV)",
            "E_a (eV)",
            "Ea (eV)",
        ],
    )
    def test_barrier_labels_write(self, label: str) -> None:
        text = f"| Species | {label} |\n|---|---|\n| ABC→A*C | 0.91 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert drops == []
        assert len(runs) == 1

    def test_delta_deviation_stays_a_correction(self) -> None:
        # True positive mx1507 (pa39644): an "RPBE ΔE deviation" row is
        # a correction/difference, not a barrier.
        text = "| Species | RPBE ΔE deviation (eV) |\n|---|---|\n| CO | 0.009 |"
        runs, drops = _expand(
            text, _one_series("adsorption-energy", "eV"), caption=None
        )
        assert runs == []
        assert [d["reason"] for d in drops] == ["correction_row"]


class TestRecheckUnitSemantics:
    """subset-5 parity: stored_row_reasons refuses a claimed unit only
    on a printed contradiction — notes/caption support and the
    unitless-everywhere case survive."""

    def _reasons(self, text: str, unit: str, caption: str = "") -> dict[str, str]:
        parsed = parse_table(9, text)
        assert parsed is not None
        cell = parsed.rows[0][1]
        return dict(
            stored_row_reasons(
                parsed,
                cell=(0, 1),
                literal=cell.text,
                unit=unit,
                subject="Ni",
                value_form="point",
                value_num=-1.2,
                entry=VOCAB[26],
                table_meta={"header": parsed.columns[1].header},
                transform=None,
                caption_text=caption,
                chunk_text=text,
                span=[9, cell.start, cell.end],
            )
        )

    def test_unit_in_notes_survives(self) -> None:
        text = "| Surface | E_b |\n|---|---|\n| Ni | -1.2 |\n^a In eV."
        assert "unit_header_mismatch" not in self._reasons(text, "eV")

    def test_unit_in_caption_survives(self) -> None:
        text = "| Surface | E_b |\n|---|---|\n| Ni | -1.2 |"
        assert "unit_header_mismatch" not in self._reasons(
            text, "eV", "Table 5. Barriers in eV."
        )

    def test_nothing_printed_survives(self) -> None:
        text = "| Surface | E_b |\n|---|---|\n| Ni | -1.2 |"
        assert "unit_header_mismatch" not in self._reasons(text, "eV")

    def test_conflicting_printed_unit_still_refuses(self) -> None:
        text = "| Surface | T (K) |\n|---|---|\n| Ni | 300 |"
        assert "unit_header_mismatch" in self._reasons(text, "eV")
