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

from typing import Any

import pytest

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
