"""The survey/review extraction library (docs/backlog/
measures-substrate.md, "Next slice"): the ported anchor machinery, the
deterministic checks, the reader-reply contract, and the Wilson
acceptance rule — pure functions; the worker glue is tested in
``test_survey_extract_job.py``.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.extract import anchors, verify
from precis.extract.accept import acceptance, stratified_sample, wilson_interval
from precis.extract.prompt import reader_prompt
from precis.extract.proposals import (
    READER_PROMPT_VERSION,
    build_runs,
    resolve_measurand,
    vocab_entry,
)
from precis.store import Store
from precis.taxonomy.measure_units import (
    make_converter,
    parse_literal,
    pint_unit_text,
)
from tests.extract_helpers import (
    CHUNK1,
    fe_reply,
    make_taxon,
    ref_meta,
    survey_world,
)


@pytest.fixture
def world(store: Store) -> dict:
    return survey_world(store)


# ── anchors ──────────────────────────────────────────────────────────


class TestAnchors:
    def test_abbreviations_do_not_break_sentences(self) -> None:
        text = (
            "The loading was ca. 5 mg as shown in Fig. 2. "
            "Cathodes were satd. at 90% RH, cf. Smith et al. (2020)."
        )
        pieces = [text[a:b] for a, b in anchors.sentence_spans(text)]
        assert len(pieces) == 2
        assert "ca. 5 mg" in pieces[0] and "Fig. 2." in pieces[0]
        assert "satd." in pieces[1]

    def test_real_breaks_still_split(self) -> None:
        text = "Yield reached 91.5%. The potential was held at -0.4 V."
        assert len(anchors.sentence_spans(text)) == 2

    def test_anchored_text_only_adds_markers(self) -> None:
        rendered = anchors.anchored_text(CHUNK1)
        assert anchors.re.sub(r"\[\d+\]", "", rendered) == CHUNK1

    def test_index_resolves_sentences_runs_atoms_and_offsets(self) -> None:
        index = anchors.anchor_index([(7, CHUNK1)])
        cid, s, e = index["7.0"]
        assert CHUNK1[s:e].startswith("The Cu NWA")
        cid, s, e = anchors.resolve_anchor(index, "7.0-1")
        assert CHUNK1[s:e].endswith("conditions.")
        atom = next(k for k in index if k.startswith("7#"))
        assert "%" in CHUNK1[index[atom][1] : index[atom][2]]
        assert anchors.resolve_anchor(index, [7, 0, 10]) == (7, 0, 10)
        with pytest.raises(ValueError):
            anchors.resolve_anchor(index, "7.99")

    def test_anchor_scheme_is_versioned(self) -> None:
        # 3: a lone-sign table cell merges onto the next number (gr511461)
        assert anchors.ANCHOR_SCHEME == 3


# ── verify ───────────────────────────────────────────────────────────


class TestVerify:
    def test_literal_supported_in_span(self) -> None:
        span = (0, CHUNK1.index(" at -0.5"))
        assert verify.literal_supported("95", CHUNK1[: span[1]], CHUNK1, span)
        assert not verify.literal_supported("96", CHUNK1[: span[1]], CHUNK1, span)

    def test_cropped_numeric_prefix_is_rejected(self) -> None:
        text = "Mass 1,505.9 mg."
        span = (0, len(text))
        assert not verify.literal_supported("505.9", text, text, span)
        assert verify.literal_supported("1,505.9", text, text, span)

    def test_categorical_literal_verbatim_only(self) -> None:
        text = "Sample A produced NH3 at -0.4 V."
        span = (0, len(text))
        assert verify.literal_supported("NH3", text, text, span, numeric_required=False)
        assert not verify.literal_supported(
            "N2", text, text, span, numeric_required=False
        )

    def test_unit_support_percent_glued_and_eV_inside_word(self) -> None:
        assert verify.unit_has_text_support("%", "gave 95%NO3 selectivity")
        assert not verify.unit_has_text_support("eV", "a 95 meV barrier")
        assert verify.unit_has_text_support("eV", "a 1.2 eV barrier")
        assert verify.unit_has_text_support(None, "anything")

    def test_numeric_crop_flags_a_cut_number(self) -> None:
        text = "Mass 1,505.9 mg."
        assert verify.numeric_crop_errors(text, [(0, 7)])
        assert not verify.numeric_crop_errors(text, [(0, len(text))])


# ── proposals ────────────────────────────────────────────────────────


class TestProposals:
    def test_a_run_builds_output_and_inputs(self, world: dict) -> None:
        cid = world["c1"]
        out = build_runs(
            fe_reply(),
            chunk_id=cid,
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="test-run",
            model="m",
        )
        assert not out.dropped
        (prop,) = out.proposals
        spec = prop.output
        assert spec.measurand_ref_id == world["fe"]
        assert spec.tier == "measured"
        assert spec.source_attribution == "own_work"
        assert spec.anchor is not None and spec.anchor.scheme == "offsets"
        assert [i.meta.get("condition") for i in prop.inputs] == [
            "product",
            "applied-potential",
        ]
        assert spec.anchor.span[0] == cid
        assert "95%" in CHUNK1[spec.anchor.span[1] : spec.anchor.span[2]]

    def test_cited_work_result_drops_as_context(self, world: dict) -> None:
        """gr511459: a result the model itself attributes to another work
        is context, not a measure — the review door rejected those rows
        whatever their tier, so they never land."""
        out = build_runs(
            fe_reply(attribution="cited_work"),
            chunk_id=world["c1"],
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "cited_context"

    def test_cited_work_still_lands_as_an_input(self, world: dict) -> None:
        """The drop is on results only: a cited number that conditions the
        paper's own result stays an input row with attribution kept."""
        reply = fe_reply()
        reply["results"][0]["inputs"].append(
            {
                "condition": "cited-baseline",
                "measurand": "applied-electrode-potential",
                "literal": "-0.5",
                "unit": "V",
                "attribution": "cited_work",
            }
        )
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.inputs[-1].source_attribution == "cited_work"

    def test_unknown_measurand_drops_the_result(self, world: dict) -> None:
        out = build_runs(
            fe_reply(measurand="made-up-measurand"),
            chunk_id=world["c1"],
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "unknown_measurand"

    def test_null_measurand_drops_as_no_fitting(self, world: dict) -> None:
        """gr511568: when the allow-list has no taxon for a number the
        chunk states, 'measurand': null is the reader's honest answer —
        filing it under the nearest slug is what filled 'fraction
        attributable to climate change' with unrelated shares."""
        out = build_runs(
            fe_reply(measurand=None),
            chunk_id=world["c1"],
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "no_fitting_measurand"

    def test_written_runs_carry_the_prompt_version(self, world: dict) -> None:
        out = build_runs(
            fe_reply(),
            chunk_id=world["c1"],
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model="m",
        )
        assert (
            out.proposals[0].output.meta["extraction"]["prompt"]
            == READER_PROMPT_VERSION
        )

    def test_reader_prompt_offers_the_no_fitting_door(self, world: dict) -> None:
        text = reader_prompt(vocab=list(world["vocab"].values()), chunk_text=CHUNK1)
        assert '"measurand": null' in text

    def test_bad_anchor_drops_the_result(self, world: dict) -> None:
        out = build_runs(
            fe_reply(anchor="99"),
            chunk_id=world["c1"],
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "unresolvable_anchor"

    def test_literal_outside_the_span_drops(self, world: dict) -> None:
        out = build_runs(
            fe_reply(literal="1000"),  # printed in sentence 1, not anchored 0
            chunk_id=world["c1"],
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "literal_not_in_span"

    def test_measurand_resolution_uses_alias_and_slug(self, world: dict) -> None:
        assert resolve_measurand("Faradaic Efficiency", world["vocab"]) is not None
        assert resolve_measurand("faradaic-efficiency", world["vocab"]) is not None
        assert resolve_measurand("nonsense", world["vocab"]) is None

    def test_bound_claim_lands_as_a_bound_not_a_point(self, world: dict) -> None:
        """gr511459: 'at most 0.2' stores upper_bound 0.2, not a point
        reading — the cue before the literal backs the claim."""
        text = "The activation barrier stayed at most 0.2 eV in every run."
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "0.2",
                    "unit": "eV",
                    "anchor": "0",
                    "attribution": "own_work",
                    "bound": "upper",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.value_form == "upper_bound"
        assert prop.output.value_num == 0.2

    def test_lower_bound_with_a_comma_literal(self, world: dict) -> None:
        text = "The stored charge kept exceeding 100,000 counts."
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "100,000",
                    "unit": "eV",
                    "anchor": "0",
                    "bound": "lower",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.value_form == "lower_bound"
        assert prop.output.value_num == 100000.0

    def test_bound_claim_without_a_cue_drops(self, world: dict) -> None:
        text = "The activation barrier was 0.2 eV."
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "0.2",
                    "unit": "eV",
                    "anchor": "0",
                    "bound": "upper",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "unsupported_bound"

    def test_bound_against_the_opposite_cue_drops(self, world: dict) -> None:
        text = "The barrier was at least 0.2 eV in each trial."
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "0.2",
                    "unit": "eV",
                    "anchor": "0",
                    "bound": "upper",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "bound_direction_mismatch"

    def test_an_unclaimed_bound_cue_flags_the_point(self, world: dict) -> None:
        """The worded bound stays a flag, not silent: a point literal on
        'at most 0.2' lands but the sampler sees bound_cue_ignored."""
        text = "The activation barrier stayed at most 0.2 eV in every run."
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "0.2",
                    "unit": "eV",
                    "anchor": "0",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.value_form is None
        assert "bound_cue_ignored" in prop.flags

    def test_lone_sign_cell_signs_the_next_cell(self, world: dict) -> None:
        """gr511461: a PDF table that puts '-' in its own cell signs the
        number after it — '-1.93' verifies against '| - | 1.93 |'."""
        text = "| Model | dT |\n| mod-a | − | 1.93 |"
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "-1.93",
                    "unit": "eV",
                    "anchor": "1",
                    "attribution": "own_work",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.literal == "-1.93"

    def test_unsigned_literal_on_a_signed_cell_drops(self, world: dict) -> None:
        """The merge also arms the region check: '1.93' claimed against
        '| - | 1.93 |' is no longer a complete numeric region."""
        text = "| Model | dT |\n| mod-a | − | 1.93 |"
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "1.93",
                    "unit": "eV",
                    "anchor": "1",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "literal_not_in_span"

    def test_header_anchor_reresolves_to_the_literal_row(self, world: dict) -> None:
        """gr514627: a '| header |\\n| row |' chunk splits one sentence
        span per line, so '0' names the header. A literal quoted from
        the data row re-resolves against the row it was quoted from
        instead of dropping literal_not_in_span (the canned reply that
        cost the gf-survey run)."""
        text = "| Model | dT (eV) |\n| mod-a | -1.93 |"
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "-1.93",
                    "unit": "eV",
                    "anchor": "0",
                    "attribution": "own_work",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.literal == "-1.93"
        anchor = prop.output.anchor
        assert anchor is not None
        assert text[anchor.span[1] : anchor.span[2]] == "| mod-a | -1.93 |"

    def test_wrong_data_row_anchor_still_drops(self, world: dict) -> None:
        """Only the header retries: a reader that anchors mod-a's row
        while quoting mod-b's literal must not re-anchor — the literal
        cannot move evidence between rows it didn't come from."""
        text = "| Model | dT |\n| mod-a | -1.90 |\n| mod-b | -1.93 |"
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "-1.93",
                    "unit": "eV",
                    "anchor": "1",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "literal_not_in_span"

    def test_table_literal_in_two_rows_still_drops(self, world: dict) -> None:
        """The re-resolution needs a unique row: the same literal in two
        data rows stays a drop — picking one would store the wrong
        evidence span."""
        text = "| Model | dT |\n| mod-a | -1.93 |\n| mod-b | -1.93 |"
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "-1.93",
                    "unit": "eV",
                    "anchor": "0",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "literal_not_in_span"

    def test_prose_anchor_does_not_reresolve_into_a_table(self, world: dict) -> None:
        """The retry is scoped to an anchored table line: a prose span
        anchored next to a table quoting a table literal stays a drop —
        the anchor was genuinely wrong, not a header/row miscount."""
        text = "Run A reports no figures.\n\n| Model | dT |\n| mod-a | -1.93 |"
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "-1.93",
                    "unit": "eV",
                    "anchor": "0",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "literal_not_in_span"

    def test_decrease_cue_signs_the_value(self, world: dict) -> None:
        text = "Run B cools the cell by 0.9 eV relative to the base case."
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "0.9",
                    "unit": "eV",
                    "anchor": "0",
                    "sign": -1,
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.value_num == -0.9

    def test_sign_claim_without_a_cue_drops(self, world: dict) -> None:
        text = "Run B shifted the cell by 0.9 eV."
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "0.9",
                    "unit": "eV",
                    "anchor": "0",
                    "sign": -1,
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.proposals
        assert out.dropped[0]["reason"] == "unsupported_sign"

    def test_bracketed_range_lands_and_fixes_the_sign(self, world: dict) -> None:
        """The bracket is verbatim evidence: a point that cannot sit
        inside it has its sign corrected by the range, flagged."""
        text = "Run C cools the bath with a median of 0.96 [-0.2 to -1.3] eV."
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "0.96",
                    "unit": "eV",
                    "anchor": "0",
                    "range": "[-0.2 to -1.3]",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.value_num == -0.96
        assert prop.output.value_low == -1.3
        assert prop.output.value_high == -0.2
        assert "sign_from_range" in prop.flags

    def test_range_with_consistent_sign_stays_quiet(self, world: dict) -> None:
        text = "Run C cools the bath with a median of 0.96 [-0.2 to -1.3] eV."
        reply = {
            "results": [
                {
                    "measurand": "adsorption-energy",
                    "literal": "0.96",
                    "unit": "eV",
                    "anchor": "0",
                    "sign": -1,
                    "range": "[-0.2 to -1.3]",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.value_num == -0.96
        assert (prop.output.value_low, prop.output.value_high) == (-1.3, -0.2)
        assert "sign_from_range" not in prop.flags

    def test_below_baseline_is_negative_with_conditions(self, world: dict) -> None:
        """'15% below the base-2 level' stores -15 and keeps the baseline
        on the row's conditions."""
        text = "Cell D emits 15% below the base-2 level in the 2030 window."
        reply = {
            "results": [
                {
                    "measurand": "faradaic-efficiency",
                    "literal": "15",
                    "unit": "%",
                    "anchor": "0",
                    "sign": -1,
                    "conditions": {"baseline": "base-2", "period": "2030"},
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.value_num == -15.0
        assert prop.output.conditions == {"baseline": "base-2", "period": "2030"}

    def test_condition_not_printed_is_flagged_off(self, world: dict) -> None:
        text = "Cell D emits 15% below the base-2 level."
        reply = {
            "results": [
                {
                    "measurand": "faradaic-efficiency",
                    "literal": "15",
                    "unit": "%",
                    "anchor": "0",
                    "sign": -1,
                    "conditions": {"baseline": "base-9"},
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.conditions is None
        assert "condition_not_in_chunk" in prop.flags

    def test_output_conditions_count_as_required(
        self, world: dict, store: Store
    ) -> None:
        """A required condition answered via the conditions dict is not
        escalated as missing at insert."""
        tid = make_taxon(
            world["store"],
            "cooling-power",
            unit="%",
            required=["baseline"],
            parent=world["root"],
        )
        text = "Cell D emits 15% below the base-2 level."
        reply = {
            "results": [
                {
                    "measurand": "cooling-power",
                    "literal": "15",
                    "unit": "%",
                    "anchor": "0",
                    "sign": -1,
                    "conditions": {"baseline": "base-2"},
                }
            ]
        }
        vocab = dict(world["vocab"])
        vocab[tid] = vocab_entry(tid, ref_meta(world["store"], tid))
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=vocab,
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        run = world["store"].insert_measure(prop.output, prop.inputs, actor="t")
        rows = world["store"].measures_for(world["paper"], include_superseded=True)
        row = next(r for r in rows if r["id"] == run.output_id)
        esc = row["meta"].get("escalation") or []
        assert not [e for e in esc if e["rule"] == "required_condition_missing"]

    def test_categorical_result_builds(self, world: dict) -> None:
        text = "Sample A produced NH3 at -0.4 V."
        reply = {
            "results": [
                {
                    "measurand": "reaction-product",
                    "literal": "NH3",
                    "value_form": "categorical",
                    "anchor": "0",
                    "attribution": "own_work",
                }
            ]
        }
        out = build_runs(
            reply,
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        (prop,) = out.proposals
        assert prop.output.value_form == "categorical"
        assert prop.output.measurand_ref_id == world["product"]


# ── Wilson acceptance ────────────────────────────────────────────────


class TestWilson:
    def test_zero_errors_at_n_52_passes_the_bar(self) -> None:
        lo, hi = wilson_interval(0, 52)
        assert lo == 0.0 and hi < 0.05
        assert acceptance(0, 52).accepted

    def test_zero_errors_at_small_n_fails(self) -> None:
        # n=40: z²/(n+z²) ≈ 6.3% > 5% — the interval is honest about the
        # small sample instead of passing on the point estimate.
        _lo, hi = wilson_interval(0, 40)
        assert hi > 0.05
        assert not acceptance(0, 40).accepted

    def test_one_error_in_60_fails(self) -> None:
        _lo, hi = wilson_interval(1, 60)
        assert hi > 0.05
        assert not acceptance(1, 60).accepted

    def test_empty_sample_never_passes(self) -> None:
        lo, hi = wilson_interval(0, 0)
        assert (lo, hi) == (0.0, 1.0)
        assert not acceptance(0, 0).accepted

    def test_stratified_sample_is_deterministic_and_covers(self) -> None:
        strata = {"a": [1, 2, 3, 4, 5], "b": [6, 7], "c": [8]}
        one = stratified_sample(strata, 5, seed="run")
        two = stratified_sample(strata, 5, seed="run")
        assert one == two
        assert sum(len(v) for v in one.values()) == 5
        assert all(len(v) >= 1 for v in one.values())
        assert one["c"] == [8]
        big = stratified_sample(strata, 99, seed="run")
        assert sum(len(v) for v in big.values()) == 8


class TestValueFormNormalised:
    @pytest.mark.parametrize(
        ("given", "built"),
        [
            # gr508162: every reader form except "categorical" is dropped —
            # parse_literal infers the numeric form from the literal, so a
            # reader-invented spelling can't reach the DB CHECK nor
            # override the literal's own parse.
            ("upper", None),
            ("lower", None),
            ("upper_bound", None),
            ("point", None),
            ("sideways", None),
            ("categorical", "categorical"),
        ],
    )
    def test_form_spelling(self, world: dict, given: str, built: str | None) -> None:
        out = build_runs(
            fe_reply(value_form=given),
            chunk_id=world["c1"],
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="test-run",
            model="m",
        )
        (prop,) = out.proposals
        assert prop.output.value_form == built


# ── markup folds (gr511458) ──────────────────────────────────────────
#
# Each markup form the checks now read through — LaTeX, HTML sup/sub,
# glyph minus, split table cells — with synthetic strings only (ids and
# numbers, never paper text).


class TestMarkupFolds:
    def test_latex_pm_and_text_brace_exponent(self) -> None:
        text = "the run gave ERFs of  $-0.7 \\pm 0.1 \\text{ W m}^{-2}$  here."
        span = (0, len(text))
        assert verify.literal_supported("-0.7 ± 0.1", text, text, span)
        assert verify.literal_supported("-0.7 ± 0.1 W m-2", text, text, span)
        assert verify.literal_supported(
            "$-0.7 \\pm 0.1 \\text{ W m}^{-2}$", text, text, span
        )
        assert not verify.literal_supported("-0.8 ± 0.1", text, text, span)

    def test_html_sup_split_sign(self) -> None:
        text = "the median is <sup>−</sup>1.9 W m<sup>−</sup><sup>2</sup> here"
        span = (0, len(text))
        assert verify.literal_supported("-1.9", text, text, span)
        assert verify.literal_supported("−1.9", text, text, span)  # U+2212

    def test_html_sup_interval(self) -> None:
        text = (
            "a spread of <sup>−</sup>0.6 to <sup>−</sup>2.5 W m<sup>−</sup><sup>2</sup>"
        )
        span = (0, len(text))
        assert verify.literal_supported("-0.6 to -2.5", text, text, span)
        assert not verify.literal_supported("-0.6 to -2.7", text, text, span)

    def test_latex_times10_sci_form(self) -> None:
        text = "loads of  $2 \\times 10^{-3}$  and  $3 \\times 10^{2}$  ran."
        span = (0, len(text))
        assert verify.literal_supported("2×10^-3", text, text, span)
        assert verify.literal_supported("2e-3", text, text, span)
        assert verify.literal_supported("3×10^2", text, text, span)
        assert not verify.literal_supported("4e-3", text, text, span)

    def test_split_table_cells(self) -> None:
        text = "| SEP | -0.7 |\n| ± | 0.1 |\n| unit | W m-2 |"
        span = (0, len(text))
        assert verify.literal_supported("-0.7 ± 0.1", text, text, span)
        assert verify.unit_has_text_support("W m-2", text)

    def test_minus_glyphs_fold(self) -> None:
        text = "a forcing of −1.9 W m−2 is seen"  # U+2212 minus
        span = (0, len(text))
        assert verify.literal_supported("-1.9", text, text, span)

    def test_markup_does_not_admit_a_fabricated_number(self) -> None:
        text = "ERFs of  $-0.7 \\pm 0.1 \\text{ W m}^{-2}$  were found."
        span = (0, len(text))
        assert not verify.literal_supported("-0.9", text, text, span)
        assert not verify.literal_supported("0.7", text, text, span)

    def test_unit_surface_markup_forms(self) -> None:
        text = "ERFs of  $-0.7 \\pm 0.1 \\text{ W m}^{-2}$  were found."
        assert verify.unit_has_text_support("W m-2", text)
        assert verify.unit_has_text_support("W m^{-2}", text)
        assert verify.unit_has_text_support(
            "W m^-2", "median −1.9 W m<sup>−</sup><sup>2</sup>"
        )
        assert not verify.unit_has_text_support("W m-3", text)


class TestMarkupUnitsToPint:
    def test_latex_brace_exponent_units_parse(self) -> None:
        conv = make_converter("W m^{-2}", "W/m^2")
        assert conv.value(-1.8) == pytest.approx(-1.8)
        conv = make_converter("cm^{-3}", "m^-3")
        assert conv.value(600.0) == pytest.approx(6e8)

    def test_sup_tag_and_unicode_sup_units_parse(self) -> None:
        conv = make_converter("W m<sup>−</sup><sup>2</sup>", "W/m^2")
        assert conv.value(3.7) == pytest.approx(3.7)
        conv = make_converter("µg h⁻¹ cm⁻²", "kg s^-1 m^-2")
        assert conv.value(1.0) == pytest.approx(1e-9 / 0.36)

    def test_text_command_unit_parses(self) -> None:
        assert pint_unit_text("\\text{W m}^{-2}") == ("W m^-2", None)

    def test_sub_tag_keeps_the_basis_label(self) -> None:
        assert pint_unit_text("mg<sub>cat</sub>⁻¹") == ("mg^-1", "cat")

    def test_glyph_damaged_unit_still_refused(self) -> None:
        with pytest.raises(BadInput, match="no dimension match"):
            make_converter("W mK^2", "W/m^2")

    def test_markup_literal_still_parses_numeric(self) -> None:
        parsed = parse_literal("$-1.8 \\pm 0.1$")
        assert parsed.form == "point"
        assert parsed.num == pytest.approx(-1.8)
        assert parsed.err == pytest.approx(0.1)


class TestMarkupBuildRuns:
    def test_latex_span_proposal_builds(self, world: dict) -> None:
        text = "ERFs of  $-0.7 \\pm 0.1 \\text{ W m}^{-2}$  were found for SEP."
        out = build_runs(
            {
                "results": [
                    {
                        "measurand": "faradaic-efficiency",
                        "literal": "-0.7 ± 0.1",
                        "unit": "%",
                        "anchor": "0",
                        "attribution": "own_work",
                    }
                ]
            },
            chunk_id=world["c1"],
            chunk_text=text,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert not out.dropped
        (prop,) = out.proposals
        assert prop.output.meta["extraction"]["prompt"] == "r3"
