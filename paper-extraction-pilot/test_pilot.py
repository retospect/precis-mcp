import copy
import importlib.util
import unittest
from pathlib import Path

import evaluate

SPEC = importlib.util.spec_from_file_location(
    "pilot", Path(__file__).with_name("pilot.py")
)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.source = pilot.prepare_source(
            {
                "ref_id": 10,
                "title": "Synthetic source, not a scientific observation",
                "chunks": [
                    {
                        "chunk_id": 20,
                        "ord": 0,
                        "chunk_kind": "paragraph",
                        "text": "Sample A reached 98.0% at −0.4 V vs RHE. Yield peaked at −0.6 V. [4] [4]",
                        "section_path": ["Results"],
                    }
                ],
                "bibliography": [
                    {"id": 30, "marker": 4, "doi": "synthetic", "held_ref_id": None}
                ],
                "citations": [],
            }
        )
        self.document = {
            "schema_version": 1,
            "source_ref_id": 10,
            "source_fingerprint": self.source["fingerprint"],
            "round": 1,
            "producer": "synthetic test",
            "coverage": {"inspected_chunk_ids": [20], "limitations": []},
            "objects": [
                {
                    "id": "sample-a",
                    "type": "sample",
                    "label": "Sample A",
                    "evidence": [{"chunk_id": 20, "quote": "Sample A"}],
                }
            ],
            "groups": [],
            "assertions": [],
            "results": [
                {
                    "id": "r1",
                    "subject_id": "sample-a",
                    "context_id": None,
                    "measurand": "Faradaic efficiency",
                    "reported_value": "98.0",
                    "reported_unit": "%",
                    "normalization_basis": None,
                    "origin": "reported_experiment",
                    "bound": "unspecified",
                    "uncertainty": None,
                    "evidence": [
                        {
                            "chunk_id": 20,
                            "quote": "Sample A reached 98.0% at −0.4 V vs RHE.",
                        }
                    ],
                    "conditions": [
                        {
                            "name": "potential",
                            "value": "−0.4",
                            "unit": "V vs RHE",
                            "basis": "explicit",
                            "evidence": [
                                {"chunk_id": 20, "quote": "98.0% at −0.4 V vs RHE."}
                            ],
                        }
                    ],
                    "limitations": [],
                }
            ],
            "gaps": [],
        }

    def test_structurally_grounded_result(self):
        report = pilot.validate(self.source, self.document)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["assessment"], "mechanical_checks_only")

    def test_rejects_wrong_document(self):
        self.document["source_ref_id"] = 11
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_rejects_stale_fingerprint(self):
        self.document["source_fingerprint"] = "stale"
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_rejects_invented_quote(self):
        self.document["results"][0]["evidence"][0]["quote"] = "Invented measurement."
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_rejects_unavailable_chunk(self):
        self.document["results"][0]["evidence"][0]["chunk_id"] = 999
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_rejects_number_not_in_own_evidence(self):
        self.document["results"][0]["reported_value"] = "99.0"
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_rejects_dangling_subject(self):
        self.document["results"][0]["subject_id"] = "missing"
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_rejects_duplicate_ids(self):
        self.document["objects"].append(copy.deepcopy(self.document["objects"][0]))
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_repeated_quote_requires_occurrence(self):
        self.document["objects"][0]["evidence"][0]["quote"] = "[4]"
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])
        self.document["objects"][0]["evidence"][0]["occurrence"] = 1
        self.assertEqual(pilot.validate(self.source, self.document)["errors"], [])

    def test_citation_occurrences_are_not_collapsed(self):
        hits = [
            x
            for x in pilot.candidates(self.source, 1)["citations"]
            if x["literal"] == "[4]"
        ]
        self.assertEqual(len(hits), 2)
        self.assertNotEqual(hits[0]["start"], hits[1]["start"])

    def test_number_candidates_preserve_unicode_minus(self):
        literals = [x["literal"] for x in pilot.candidates(self.source, 2)["numbers"]]
        self.assertIn("−0.4", literals)
        self.assertIn("−0.6", literals)

    def test_quote_binding_preserves_source_text(self):
        for anchor in pilot.validate(self.source, self.document)["anchors"]:
            text = self.source["chunks"][0]["text"]
            self.assertEqual(text[anchor["start"] : anchor["end"]], anchor["quote"])

    def test_context_alignment_is_not_claimed_by_validator(self):
        condition = self.document["results"][0]["conditions"][0]
        condition["value"] = "−0.6"
        condition["evidence"] = [{"chunk_id": 20, "quote": "Yield peaked at −0.6 V."}]
        report = pilot.validate(self.source, self.document)
        self.assertEqual(report["errors"], [])
        self.assertFalse(report["semantic_validity_established"])

    def test_superscript_citations_exclude_scientific_notation(self):
        self.source["chunks"][0]["text"] = (
            "Prior report.<sup>4</sup> m<sup>2</sup> 10<sup>3</sup> <sup>13</sup>C"
        )
        hits = pilot.candidates(self.source, 2)["citations"]
        self.assertEqual([x["literal"] for x in hits], ["<sup>4</sup>"])
        self.assertEqual(hits[0]["bibliography_candidates"][0]["entry_id"], 30)

    def test_author_year_proposes_local_bibliography_match(self):
        self.source["chunks"][0]["text"] = (
            "Earlier results (Miller et al., 2020) differ."
        )
        self.source["bibliography"][0].update(
            authors="Miller and colleagues", year=2020
        )
        hits = pilot.candidates(self.source, 2)["citations"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["bibliography_candidates"][0]["entry_id"], 30)

    def test_text_condition_before_sentence_period(self):
        condition = self.document["results"][0]["conditions"][0]
        condition["value"] = "Yield peaked at −0.6 V"
        condition["evidence"] = [{"chunk_id": 20, "quote": "Yield peaked at −0.6 V."}]
        self.assertEqual(pilot.validate(self.source, self.document)["errors"], [])

    def test_integer_condition_before_sentence_period(self):
        raw = {
            key: self.source[key]
            for key in ("ref_id", "title", "chunks", "bibliography", "citations")
        }
        raw["chunks"] = [
            dict(raw["chunks"][0], text=raw["chunks"][0]["text"] + " pH 14.")
        ]
        self.source = pilot.prepare_source(raw)
        self.document["source_fingerprint"] = self.source["fingerprint"]
        condition = self.document["results"][0]["conditions"][0]
        condition["value"] = "14"
        condition["evidence"] = [{"chunk_id": 20, "quote": "pH 14."}]
        self.assertEqual(pilot.validate(self.source, self.document)["errors"], [])

    def test_numeric_prefix_is_not_a_literal_match(self):
        self.document["results"][0]["reported_value"] = "98"
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_tampered_source_is_rejected(self):
        self.source["chunks"][0]["text"] += " Modified after export."
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def v2_document(self):
        document = copy.deepcopy(self.document)
        document["schema_version"] = 2
        result = document["results"][0]
        result.pop("origin")
        result.pop("bound")
        result.update(
            evidence_mode="experimental",
            value_generation="measurement",
            source_attribution="own_work",
            value_form="point_estimate",
            normalization_status="not_applicable",
            normalization_evidence=[],
            measurand_status="explicit",
        )
        return document

    def test_v2_separates_mode_and_generation(self):
        self.assertEqual(pilot.validate(self.source, self.v2_document())["errors"], [])

    def test_v2_normalization_requires_attached_evidence(self):
        document = self.v2_document()
        document["results"][0]["normalization_status"] = "explicit"
        document["results"][0]["normalization_basis"] = "charge fraction"
        errors = pilot.validate(self.source, document)["errors"]
        self.assertTrue(any("normalization" in error for error in errors))

    def test_v2_rejects_exact_as_a_value_form(self):
        document = self.v2_document()
        document["results"][0]["value_form"] = "exact"
        errors = pilot.validate(self.source, document)["errors"]
        self.assertTrue(any("value_form" in error for error in errors))

    def test_v3_preserves_thousands_and_adjacent_units(self):
        self.source["chunks"][0]["text"] = "A rate of 1,505.9 mg at -0.95V."
        values = [
            item["literal"] for item in pilot.candidates(self.source, 3)["numbers"]
        ]
        self.assertIn("1,505.9", values)
        self.assertIn("-0.95", values)
        self.assertNotIn("505.9", values)

    def test_v3_parenthetical_numeric_citation(self):
        self.source["chunks"][0]["text"] = "Earlier work (4) supports this."
        hits = pilot.candidates(self.source, 3)["citations"]
        self.assertEqual(hits[0]["bibliography_candidates"][0]["entry_id"], 30)

    def test_v3_panel_reference_can_propose_parent_caption(self):
        self.source["chunks"][0]["text"] = "Results are shown in Fig. 3b."
        self.source["chunks"].append(
            {
                "chunk_id": 21,
                "chunk_kind": "paragraph",
                "text": "Figure 3. Sample comparison.",
            }
        )
        hit = pilot.candidates(self.source, 3)["internal_references"][0]
        self.assertEqual(hit["candidate_target_chunks"], [21])
        self.assertEqual(hit["target_match_basis"], "parent_label_only_unverified")

    def test_numeric_suffix_of_thousands_value_is_rejected(self):
        raw = {
            key: self.source[key]
            for key in ("ref_id", "title", "chunks", "bibliography", "citations")
        }
        raw["chunks"] = [
            dict(raw["chunks"][0], text=raw["chunks"][0]["text"] + " Mass 1,505.9 mg.")
        ]
        self.source = pilot.prepare_source(raw)
        self.document["source_fingerprint"] = self.source["fingerprint"]
        self.document["results"][0]["reported_value"] = "505.9"
        self.document["results"][0]["evidence"] = [
            {"chunk_id": 20, "quote": "Mass 1,505.9 mg."}
        ]
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_comparison_guardrail_rejects_fictional_joint_maximum(self):
        rows = [
            {
                "source_ref_id": 196807,
                "id": "nh3_fe_pdce",
                "reported_value": "98.0",
                "conditions": [{"name": "potential", "value": "-0.4"}],
            },
            {
                "source_ref_id": 196807,
                "id": "nh3_yield_pdce",
                "reported_value": "4.17",
                "conditions": [{"name": "potential", "value": "-0.6"}],
            },
        ]
        check = evaluate.guardrails(rows)[0]
        self.assertTrue(check["passed"])
        rows[1]["conditions"][0]["value"] = "-0.4"
        self.assertFalse(evaluate.guardrails(rows)[0]["passed"])

    def test_cited_observations_are_not_own_work(self):
        rows = [
            {
                "source_ref_id": 965,
                "id": "cited-n2o-onset-range",
                "source_attribution": "own_work",
            },
            {
                "source_ref_id": 3334,
                "id": "he-cited-activation-range",
                "source_attribution": "cited_work",
            },
        ]
        check = next(
            item
            for item in evaluate.guardrails(rows)
            if item["check"] == "secondary_reports_are_not_own_experiments"
        )
        self.assertFalse(check["passed"])

    def test_potential_dependency_links_use_evidence_chunks_only(self):
        value = {
            "source_ref_id": 123,
            "evidence": [{"chunk_id": 20}],
            "conditions": [{"value": "42", "evidence": [{"chunk_id": 21}]}],
        }
        self.assertEqual(evaluate.evidence_chunk_ids(value), {20, 21})

    def append_source_text(self, text):
        raw = {
            key: self.source[key]
            for key in ("ref_id", "title", "chunks", "bibliography", "citations")
        }
        raw["chunks"] = [dict(raw["chunks"][0], text=raw["chunks"][0]["text"] + text)]
        self.source = pilot.prepare_source(raw)
        self.document["source_fingerprint"] = self.source["fingerprint"]

    def test_cropped_quote_cannot_hide_negative_sign(self):
        condition = self.document["results"][0]["conditions"][0]
        condition["value"] = "0.4"
        condition["evidence"] = [{"chunk_id": 20, "quote": "0.4"}]
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_cropped_quote_cannot_hide_thousands_prefix(self):
        self.append_source_text(" Mass 1,505.9 mg.")
        self.document["results"][0]["reported_value"] = "505.9"
        self.document["results"][0]["evidence"] = [{"chunk_id": 20, "quote": "505.9"}]
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_scientific_notation_coefficient_is_not_whole_value(self):
        self.append_source_text(" Current 2 \\times 10^{-3} A.")
        self.document["results"][0]["reported_value"] = "2"
        self.document["results"][0]["evidence"] = [
            {"chunk_id": 20, "quote": "Current 2 \\times 10^{-3} A."}
        ]
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_fraction_numerator_is_not_whole_value(self):
        self.append_source_text(" Coverage 2/3 ML.")
        self.document["results"][0]["reported_value"] = "2"
        self.document["results"][0]["evidence"] = [{"chunk_id": 20, "quote": "2/3 ML"}]
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_whole_scientific_expression_remains_valid(self):
        self.append_source_text(" Current 2 \\times 10^{-3} A.")
        self.document["results"][0]["reported_value"] = "2 \\times 10^{-3}"
        self.document["results"][0]["evidence"] = [
            {"chunk_id": 20, "quote": "Current 2 \\times 10^{-3} A."}
        ]
        self.assertEqual(pilot.validate(self.source, self.document)["errors"], [])

    def test_malformed_record_returns_errors_instead_of_crashing(self):
        self.document["objects"].append(None)
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_unknown_gap_chunk_is_rejected(self):
        self.document["gaps"] = [
            {"type": "ocr_uncertainty", "description": "Check this", "chunk_ids": [999]}
        ]
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_protocol_link_cannot_target_a_sample(self):
        self.document["assertions"] = [
            {
                "id": "a1",
                "subject_id": "r1",
                "predicate": "uses-protocol",
                "object_id": "sample-a",
                "basis": "explicit",
                "evidence": [{"chunk_id": 20, "quote": "Sample A"}],
            }
        ]
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_assertion_cannot_reference_itself(self):
        self.document["assertions"] = [
            {
                "id": "a1",
                "subject_id": "a1",
                "predicate": "compares",
                "object_id": "a1",
                "basis": "explicit",
                "evidence": [{"chunk_id": 20, "quote": "Sample A"}],
            }
        ]
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_result_cannot_override_source_provenance(self):
        self.document["results"][0]["source_ref_id"] = 999
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_unanchored_unit_is_flagged_for_semantic_review(self):
        self.document["results"][0]["reported_unit"] = "kg"
        self.assertTrue(
            any(
                "unit" in warning
                for warning in pilot.validate(self.source, self.document)["warnings"]
            )
        )

    def test_en_dash_negative_literal_uses_original_source_boundaries(self):
        self.append_source_text(" Voltage –0.645 V.")
        condition = self.document["results"][0]["conditions"][0]
        condition.update(
            value="–0.645", evidence=[{"chunk_id": 20, "quote": "Voltage –0.645 V."}]
        )
        self.assertEqual(pilot.validate(self.source, self.document)["errors"], [])
        condition.update(value="0.645", evidence=[{"chunk_id": 20, "quote": "0.645"}])
        self.assertTrue(pilot.validate(self.source, self.document)["errors"])

    def test_missing_temperature_is_not_assumed_ambient(self):
        context = evaluate.operating_context(
            {"evidence_mode": "experimental", "conditions": []}
        )
        self.assertEqual(
            context["operating_temperature_status"],
            "not_located_in_extracted_conditions",
        )
        self.assertEqual(context["temperature_conditions"], [])

    def test_preparation_temperature_is_not_operating_temperature(self):
        context = evaluate.operating_context(
            {
                "evidence_mode": "experimental",
                "conditions": [
                    {
                        "name": "Catalyst calcination temperature",
                        "value": "900",
                        "unit": "C",
                    }
                ],
            }
        )
        self.assertEqual(context["temperature_conditions"][0]["role"], "preparation")
        self.assertEqual(
            context["operating_temperature_status"],
            "not_located_in_extracted_conditions",
        )

    def test_simulation_temperature_does_not_imply_physical_requirement(self):
        context = evaluate.operating_context(
            {
                "evidence_mode": "computational",
                "conditions": [{"name": "MD temperature", "value": "0", "unit": "K"}],
            }
        )
        self.assertEqual(
            context["temperature_conditions"][0]["role"], "model_or_reference"
        )
        self.assertEqual(
            context["applicability"], "not_assessed_without_application_profile"
        )

    def test_low_temperature_is_visible_but_not_globally_rejected(self):
        row = {
            "evidence_mode": "experimental",
            "conditions": [{"name": "Sample temperature", "value": "20", "unit": "mK"}],
        }
        context = evaluate.operating_context(row)
        self.assertEqual(context["temperature_conditions"][0]["condition_index"], 0)
        self.assertEqual(
            context["operating_temperature_status"], "reported_condition_present"
        )
        self.assertEqual(row["conditions"][0]["unit"], "mK")
        self.assertEqual(context["temperature_conditions"][0]["reported_unit"], "mK")
        self.assertEqual(context["temperature_conditions"][0]["reported_value"], "20")
        self.assertEqual(
            context["applicability"], "not_assessed_without_application_profile"
        )

    def test_complete_selection_excludes_duplicate_full_arm(self):
        paths = evaluate.extraction_paths(Path(__file__).parent, complete=True)
        self.assertEqual(len(paths), 20)
        self.assertTrue(all("round4/full/" not in str(path) for path in paths))
        self.assertEqual(len({path.stem for path in paths}), 20)

    def test_structured_packets_preserve_exact_source(self):
        packet = pilot.reader_packet(self.source)
        self.assertEqual(
            "".join(packet["chunks"][0]["text_segments"]),
            self.source["chunks"][0]["text"],
        )


if __name__ == "__main__":
    unittest.main()
