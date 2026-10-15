"""The survey/review extraction library (docs/backlog/
measures-substrate.md, "Next slice"): the ported anchor machinery, the
deterministic checks, the reader-reply contract, and the Wilson
acceptance rule — pure functions; the worker glue is tested in
``test_survey_extract_job.py``.
"""

from __future__ import annotations

import pytest

from precis.extract import anchors, verify
from precis.extract.accept import acceptance, stratified_sample, wilson_interval
from precis.extract.proposals import build_runs, resolve_measurand
from precis.store import Store
from tests.extract_helpers import CHUNK1, fe_reply, survey_world


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
        assert anchors.ANCHOR_SCHEME == 2


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

    def test_cited_work_downgrades_to_asserted(self, world: dict) -> None:
        out = build_runs(
            fe_reply(attribution="cited_work", tier="measured"),
            chunk_id=world["c1"],
            chunk_text=CHUNK1,
            paper_ref_id=world["paper"],
            vocab=world["vocab"],
            extraction_run="t",
            model=None,
        )
        assert out.proposals[0].output.tier == "asserted"
        assert out.proposals[0].output.source_attribution == "cited_work"

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
