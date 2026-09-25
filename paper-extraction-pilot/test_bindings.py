import copy
import json
import unittest
from pathlib import Path

import bindings
import compact
import pilot

BASE = Path(__file__).resolve().parent

PROSE = (
    "Sample A reached 98.0% Faradaic efficiency at -0.4 V vs RHE. "
    "Sample B reached 91.5% under the same conditions. "
    "All measurements were performed at 298 K in 1 M KOH."
)
METHOD = (
    "The films were annealed at 1123 K for 2 h before use. "
    "Electrochemical tests used a three-electrode cell."
)
TABLE = (
    "**Table 1.** Measured efficiencies.\n"
    "| Sample | FE (%) | Overpotential (mV) |\n"
    "| --- | --- | --- |\n"
    "| A | 98.0 | 310 |\n"
    "| B | 91.5 | 380 |\n"
)


def synthetic_source():
    return pilot.prepare_source(
        {
            "ref_id": 10,
            "title": "Synthetic source, not a scientific observation",
            "chunks": [
                {
                    "chunk_id": 20,
                    "ord": 0,
                    "chunk_kind": "paragraph",
                    "text": PROSE,
                    "section_path": ["Results"],
                },
                {
                    "chunk_id": 21,
                    "ord": 1,
                    "chunk_kind": "paragraph",
                    "text": METHOD,
                    "section_path": ["Experimental"],
                },
                {
                    "chunk_id": 22,
                    "ord": 2,
                    "chunk_kind": "table",
                    "text": TABLE,
                    "section_path": ["Results"],
                },
            ],
            "bibliography": [],
            "citations": [],
        }
    )


def base_document(source):
    return {
        "schema_version": bindings.SCHEMA_VERSION,
        "anchor_scheme": bindings.ANCHOR_SCHEME,
        "source_ref_id": 10,
        "source_fingerprint": source["fingerprint"],
        "round": 6,
        "producer": "synthetic binding test",
        "coverage": {"inspected_chunk_ids": [[20, 22]], "limitations": []},
        "spans": {
            "e1": "20.0",
            "e2": "20.1",
            "e3": "20.2",
            "e4": "21.0",
        },
        "conditions": {
            "c1": ["Electrode potential versus RHE", "-0.4", "V", ["e1"]],
            "c2": ["Measurement temperature", "298", "K", ["e3"]],
        },
        "condition_items": {
            "k1": ["c1", "explicit", ["e1"]],
            "k2": ["c2", "explicit", ["e1", "e3"]],
        },
        "condition_sets": {"S1": ["k1", "k2"]},
        "defaults": {
            "results": {
                "u": "%",
                "unc": None,
                "em": "experimental",
                "vg": "measurement",
                "sa": "own_work",
                "vf": "point_estimate",
                "nb": None,
                "ns": "not_applicable",
                "ne": [],
                "ms": "explicit",
                "lim": [],
            }
        },
        "objects": [
            ["a", "sample", "Sample A", ["e1"]],
            ["b", "sample", "Sample B", ["e2"]],
        ],
        "groups": [],
        "assertions": [],
        "results": [
            {
                "id": "r1",
                "s": "a",
                "x": None,
                "m": "Faradaic efficiency of Sample A",
                "v": "98.0",
                "ev": ["e1"],
                "cs": "S1",
            },
            {
                "id": "r2",
                "s": "b",
                "x": None,
                "m": "Faradaic efficiency of Sample B",
                "v": "91.5",
                "ev": ["e2"],
                "cs": "S1",
                "ms": "interpreted",
            },
        ],
        "gaps": [],
    }


class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.source = synthetic_source()

    def test_sentence_spans_are_source_exact(self):
        for chunk in self.source["chunks"]:
            text = chunk["text"]
            for start, end in bindings.sentence_spans(text):
                self.assertEqual(text[start:end], text[start:end].strip())
                self.assertTrue(text[start:end])

    def test_anchored_text_only_adds_markers(self):
        for chunk in self.source["chunks"]:
            rendered = bindings.anchored_text(chunk["text"])
            stripped = bindings.re.sub(r"\[\d+\]", "", rendered)
            self.assertEqual(stripped, chunk["text"])

    def test_anchor_index_resolves_ids_and_runs(self):
        index = bindings.anchor_index(self.source)
        self.assertEqual(index["20.0"][0], 20)
        run = bindings.resolve_anchor(index, "20.0-2")
        self.assertEqual(PROSE[run[1] : run[2]], PROSE)
        with self.assertRaises(ValueError):
            bindings.resolve_anchor(index, "20.9")
        with self.assertRaises(ValueError):
            bindings.resolve_anchor(index, "20.2-0")

    def test_numeric_anchor_covers_sign_and_exponent(self):
        index = bindings.anchor_index(self.source)
        literals = {
            PROSE[start:end]
            for ident, (cid, start, end) in index.items()
            if cid == 20 and "#" in ident
        }
        self.assertIn("-0.4", literals)

    def test_table_rows_report_exact_cell_spans(self):
        rows = bindings.table_rows(TABLE)
        self.assertEqual(len(rows), 3)
        header = [TABLE[a:b] for a, b in rows[0]["cells"]]
        self.assertEqual(header, ["Sample", "FE (%)", "Overpotential (mV)"])
        self.assertEqual(
            [TABLE[a:b] for a, b in rows[1]["cells"]], ["A", "98.0", "310"]
        )

    def test_map_rows_are_navigable_and_marked_non_evidence(self):
        document = bindings.build_map(self.source)
        self.assertEqual(
            document["map_fingerprint"],
            pilot.digest({k: v for k, v in document.items() if k != "map_fingerprint"}),
        )
        self.assertIn("not evidence", document["policy"])
        shapes = [row[5] for row in document["chunks"] if row[0] == 22]
        self.assertEqual(shapes, [[3, 3]])


class RangeAndPoolTests(unittest.TestCase):
    def test_ranges_round_trip(self):
        values = [1, 2, 3, 7, 9, 10]
        self.assertEqual(bindings.encode_ranges(values), [[1, 3], 7, [9, 10]])
        self.assertEqual(bindings.decode_ranges(bindings.encode_ranges(values)), values)

    def test_unsorted_lists_are_left_alone(self):
        values = [5, 1, 2]
        self.assertEqual(bindings.encode_ranges(values), values)
        self.assertEqual(bindings.decode_ranges(values), values)

    def test_pools_round_trip_strings_and_lists(self):
        pools = bindings.Pools(
            {"p1": "a long shared limitation string"}, {"L1": ["e1"]}
        )
        self.assertEqual(pools.encode_string("a long shared limitation string"), ["p1"])
        self.assertEqual(pools.decode_string(["p1"]), "a long shared limitation string")
        self.assertEqual(pools.encode_list(["e1"]), "L1")
        self.assertEqual(pools.decode_list("L1"), ["e1"])
        self.assertEqual(pools.decode_list(["e2"]), ["e2"])


class MaterializeTests(unittest.TestCase):
    def setUp(self):
        self.source = synthetic_source()
        self.document = base_document(self.source)

    def test_defaults_are_written_out_with_exceptions_preserved(self):
        expanded = bindings.materialize(self.document, self.source)
        first, second = expanded["results"]
        self.assertEqual(first["measurand_status"], "explicit")
        self.assertEqual(second["measurand_status"], "interpreted")
        for result in expanded["results"]:
            for field, _short in bindings.RESULT_FIELDS:
                self.assertIn(field, result)

    def test_conditions_are_selected_per_result_with_applicability(self):
        expanded = bindings.materialize(self.document, self.source)
        references = expanded["results"][0]["condition_refs"]
        self.assertEqual([item["id"] for item in references], ["c1", "c2"])
        self.assertEqual(references[1]["applicability_evidence"], ["e1", "e3"])

    def test_evidence_is_sliced_from_the_snapshot(self):
        expanded = bindings.materialize(self.document, self.source)
        quote = expanded["evidence_pool"]["e1"]["quote"]
        self.assertIn(quote, PROSE)
        self.assertIn("98.0%", quote)

    def test_schema_two_expansion_and_validation(self):
        expanded = bindings.materialize(self.document, self.source)
        schema2 = compact.expand_extraction(expanded)
        report = pilot.validate(self.source, schema2)
        self.assertEqual(report["errors"], [])
        self.assertFalse(report["semantic_validity_established"])

    def test_source_mismatch_is_refused(self):
        other = copy.deepcopy(self.source)
        other["fingerprint"] = "0" * 64
        with self.assertRaises(ValueError):
            bindings.materialize(self.document, other)

    def test_missing_anchor_scheme_fails_closed(self):
        """Absence is not provenance.

        The dangerous document is the UNSTAMPED one, not the one carrying an
        old number: defaulting a missing field to the current scheme asserts
        exactly what the guard is supposed to check.
        """
        document = copy.deepcopy(self.document)
        document.pop("anchor_scheme", None)
        with self.assertRaises(ValueError) as caught:
            bindings.materialize(document, self.source)
        self.assertIn("no anchor_scheme", str(caught.exception))

    def test_stale_anchor_scheme_is_refused(self):
        """Positional anchors are only valid under the splitter that made them.

        A matching source fingerprint is not enough: if the sentence splitter
        changes, "c.n" points at different text while the source is untouched.
        The scheme guard has to catch that, because nothing else will.
        """
        document = copy.deepcopy(self.document)
        document["anchor_scheme"] = bindings.ANCHOR_SCHEME - 1
        with self.assertRaises(ValueError) as caught:
            bindings.materialize(document, self.source)
        self.assertIn("anchor scheme", str(caught.exception))

    def test_a_changed_splitter_would_repoint_anchors(self):
        """Demonstrates the failure the scheme guard exists to prevent."""
        document = copy.deepcopy(self.document)
        original = bindings.materialize(document, self.source)
        quote = original["evidence_pool"]["e1"]["quote"]
        saved = bindings.SENTENCE_BREAK
        try:
            #  Splitting after "%" too: the source is untouched and every id
            #  still resolves, but 20.0 now covers less text than it did.
            bindings.SENTENCE_BREAK = bindings.re.compile(
                r"(?<=[.!?%])[ \t]+(?=[^a-z])|\n{2,}"
            )
            moved = bindings.materialize(document, self.source)
        finally:
            bindings.SENTENCE_BREAK = saved
        self.assertNotEqual(moved["evidence_pool"]["e1"]["quote"], quote)
        self.assertEqual(
            bindings.materialize(document, self.source)["evidence_pool"]["e1"]["quote"],
            quote,
        )

    def test_span_outside_its_chunk_is_refused(self):
        document = copy.deepcopy(self.document)
        document["spans"]["e1"] = [20, 0, 10_000]
        with self.assertRaises(ValueError):
            bindings.materialize(document, self.source)

    def test_whitespace_only_span_is_refused(self):
        document = copy.deepcopy(self.document)
        document["spans"]["e1"] = [20, PROSE.index(" "), PROSE.index(" ") + 1]
        with self.assertRaises(ValueError):
            bindings.materialize(document, self.source)

    def test_missing_field_without_default_is_refused(self):
        document = copy.deepcopy(self.document)
        del document["defaults"]["results"]["u"]
        with self.assertRaises(ValueError):
            bindings.materialize(document, self.source)

    def test_repeated_quote_gets_an_occurrence_index(self):
        source = pilot.prepare_source(
            {
                "ref_id": 11,
                "title": "Synthetic repeat",
                "chunks": [
                    {
                        "chunk_id": 30,
                        "ord": 0,
                        "chunk_kind": "paragraph",
                        "text": "Yield was 5 mA. Yield was 5 mA.",
                        "section_path": ["Results"],
                    }
                ],
                "bibliography": [],
                "citations": [],
            }
        )
        document = {
            "schema_version": 4,
            "anchor_scheme": bindings.ANCHOR_SCHEME,
            "source_ref_id": 11,
            "source_fingerprint": source["fingerprint"],
            "round": 6,
            "producer": "synthetic",
            "coverage": {"inspected_chunk_ids": [30], "limitations": []},
            "spans": {"e1": "30.1"},
            "conditions": {},
            "objects": [],
            "groups": [],
            "assertions": [],
            "results": [],
            "gaps": [],
        }
        expanded = bindings.materialize(document, source)
        self.assertEqual(expanded["evidence_pool"]["e1"]["occurrence"], 1)


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.source = synthetic_source()
        self.document = base_document(self.source)
        self.document["series"] = [
            {
                "id": "t1",
                "kind": "table",
                "chunk_id": 22,
                "header_row": 0,
                "row_subjects": {"1": "a", "2": "b"},
                "columns": [
                    {
                        "column": 1,
                        "measurand": "Faradaic efficiency from Table 1",
                        "unit": "%",
                        "fields": {"cs": "S1"},
                    },
                    {
                        "column": 2,
                        "measurand": "Overpotential from Table 1",
                        "unit": "mV",
                        "fields": {"cs": "S1"},
                        "id_prefix": "t1eta",
                    },
                ],
            }
        ]

    def test_table_expansion_yields_one_result_per_cell(self):
        expanded = bindings.materialize(self.document, self.source)
        produced = [r for r in expanded["results"] if r["id"].startswith("t1")]
        self.assertEqual(len(produced), 4)
        values = {(r["id"], r["reported_value"], r["reported_unit"]) for r in produced}
        self.assertIn(("t1_1", "98.0", "%"), values)
        self.assertIn(("t1eta_2", "380", "mV"), values)

    def test_expanded_result_cites_cell_row_and_header(self):
        expanded = bindings.materialize(self.document, self.source)
        produced = next(r for r in expanded["results"] if r["id"] == "t1eta_1")
        quotes = [expanded["evidence_pool"][e]["quote"] for e in produced["evidence"]]
        self.assertEqual(quotes[0], "310")
        self.assertIn("| A | 98.0 | 310 |", quotes[1])
        self.assertEqual(quotes[2], "Overpotential (mV)")

    def test_expanded_results_inherit_defaults_and_conditions(self):
        expanded = bindings.materialize(self.document, self.source)
        produced = next(r for r in expanded["results"] if r["id"] == "t1_2")
        self.assertEqual(produced["subject_id"], "b")
        self.assertEqual(produced["evidence_mode"], "experimental")
        self.assertEqual([c["id"] for c in produced["condition_refs"]], ["c1", "c2"])

    def test_skipped_cells_are_not_expanded(self):
        document = copy.deepcopy(self.document)
        document["series"][0]["skip_cells"] = ["2:1"]
        expanded = bindings.materialize(document, self.source)
        self.assertNotIn("t1_2", {r["id"] for r in expanded["results"]})

    def test_unbound_row_subject_is_refused(self):
        document = copy.deepcopy(self.document)
        del document["series"][0]["row_subjects"]
        with self.assertRaises(ValueError):
            bindings.materialize(document, self.source)

    def test_listed_series_expands_anchored_values(self):
        document = copy.deepcopy(self.document)
        document.pop("series")
        index = bindings.anchor_index(self.source)
        numeric = [
            ident
            for ident, (cid, start, end) in index.items()
            if cid == 20 and "#" in ident and PROSE[start:end] == "98.0"
        ]
        document["series"] = [
            {
                "id": "ls",
                "kind": "listed",
                "measurand": "Faradaic efficiency, listed",
                "unit": "%",
                "fields": {"cs": "S1"},
                "items": [
                    {
                        "subject_id": "a",
                        "anchor": numeric[0],
                        "context_anchor": "20.0",
                    }
                ],
            }
        ]
        expanded = bindings.materialize(document, self.source)
        produced = next(r for r in expanded["results"] if r["id"] == "ls_0")
        self.assertEqual(produced["reported_value"], "98.0")
        self.assertEqual(len(produced["evidence"]), 2)

    def test_unknown_series_kind_is_refused(self):
        document = copy.deepcopy(self.document)
        document["series"][0]["kind"] = "guess"
        with self.assertRaises(ValueError):
            bindings.materialize(document, self.source)


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.source = synthetic_source()
        self.document = base_document(self.source)
        self.document["assertions"] = [
            ["a1", "a", "uses-protocol", "b", "inferred", ["e4"], []]
        ]

    def test_retiring_an_assertion_keeps_identity_objects(self):
        document = copy.deepcopy(self.document)
        document["retire"] = [
            {"id": "a1", "reason": "protocol link was wrong", "recheck": ["r1"]}
        ]
        expanded = bindings.materialize(document, self.source)
        self.assertEqual(expanded["assertions"], [])
        self.assertEqual({o["id"] for o in expanded["objects"]}, {"a", "b"})
        self.assertIn("Retired a1", expanded["gaps"][0]["description"])
        self.assertIn("r1", expanded["gaps"][0]["description"])

    def test_retiring_an_identity_object_is_refused(self):
        document = copy.deepcopy(self.document)
        document["retire"] = [{"id": "a", "reason": "wrong"}]
        with self.assertRaises(ValueError):
            bindings.materialize(document, self.source)

    def test_retiring_an_unknown_target_is_refused(self):
        document = copy.deepcopy(self.document)
        document["retire"] = [{"id": "nope", "reason": "wrong"}]
        with self.assertRaises(ValueError):
            bindings.materialize(document, self.source)


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.source = synthetic_source()
        self.document = base_document(self.source)

    def kinds(self, document):
        report = bindings.check(document, self.source)
        return report["escalation_counts"], report

    def test_clean_document_has_no_binding_or_schema_errors(self):
        counts, report = self.kinds(self.document)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["binding_errors"], [])
        self.assertNotIn("temperature_not_visible", counts)
        self.assertFalse(report["semantic_validity_established"])

    def test_cropped_numeric_region_is_escalated(self):
        document = copy.deepcopy(self.document)
        start = PROSE.index("-0.4")
        document["spans"]["e1"] = [20, 0, start + 2]
        counts, _report = self.kinds(document)
        self.assertIn("cropped_numeric_region", counts)

    def test_punctuation_only_boundary_is_not_a_crop(self):
        source = pilot.prepare_source(
            {
                "ref_id": 12,
                "title": "Synthetic citation marker",
                "chunks": [
                    {
                        "chunk_id": 40,
                        "ord": 0,
                        "chunk_kind": "paragraph",
                        "text": "We used the PBE functional.69,70 Then we relaxed it.",
                        "section_path": ["Methods"],
                    }
                ],
                "bibliography": [],
                "citations": [],
            }
        )
        document = {
            "schema_version": 4,
            "anchor_scheme": bindings.ANCHOR_SCHEME,
            "source_ref_id": 12,
            "source_fingerprint": source["fingerprint"],
            "round": 6,
            "producer": "synthetic",
            "coverage": {"inspected_chunk_ids": [40], "limitations": []},
            "spans": {"e1": [40, 0, len("We used the PBE functional.")]},
            "conditions": {},
            "objects": [],
            "groups": [],
            "assertions": [],
            "results": [],
            "gaps": [],
        }
        report = bindings.check(document, source)
        self.assertNotIn("cropped_numeric_region", report["escalation_counts"])

    def test_sub_sentence_span_is_escalated(self):
        document = copy.deepcopy(self.document)
        document["spans"]["e2"] = [
            20,
            PROSE.index("Sample B"),
            PROSE.index("Sample B") + 8,
        ]
        counts, _report = self.kinds(document)
        self.assertIn("sub_sentence_span", counts)

    def test_missing_temperature_condition_is_escalated(self):
        document = copy.deepcopy(self.document)
        document["condition_sets"]["S1"] = ["k1"]
        counts, _report = self.kinds(document)
        self.assertEqual(counts["temperature_not_visible"], 2)

    def test_preparation_temperature_is_flagged_as_not_operating(self):
        document = copy.deepcopy(self.document)
        document["conditions"]["c3"] = ["Annealing temperature", "1123", "K", ["e4"]]
        document["condition_items"]["k3"] = ["c3", "inferred", ["e4"]]
        document["condition_sets"]["S1"] = ["k1", "k3"]
        counts, _report = self.kinds(document)
        self.assertIn("preparation_temperature_as_operating_condition", counts)
        self.assertIn("inferred_condition_basis", counts)

    def test_applicability_repeating_the_definition_is_escalated(self):
        document = copy.deepcopy(self.document)
        document["condition_items"]["k1"] = ["c1", "explicit", ["e1"]]
        counts, _report = self.kinds(document)
        self.assertIn("applicability_not_shown_for_this_result", counts)

    def test_unsupported_unit_is_escalated(self):
        document = copy.deepcopy(self.document)
        document["defaults"]["results"]["u"] = "mol L-1"
        counts, _report = self.kinds(document)
        self.assertIn("unit_without_text_support", counts)

    def test_value_absent_from_evidence_is_escalated(self):
        document = copy.deepcopy(self.document)
        document["results"][0]["v"] = "77.7"
        counts, _report = self.kinds(document)
        self.assertIn("value_not_located_in_evidence", counts)

    def test_audit_sample_is_reproducible(self):
        first = bindings.check(self.document, self.source)["audit_sample"]
        second = bindings.check(self.document, self.source)["audit_sample"]
        self.assertEqual(first, second)
        self.assertTrue(set(first) <= {"r1", "r2"})

    def test_failed_materialization_is_reported_not_raised(self):
        document = copy.deepcopy(self.document)
        document["spans"]["e1"] = [99, 0, 5]
        report = bindings.check(document, self.source)
        self.assertTrue(report["binding_errors"])
        self.assertFalse(report["semantic_validity_established"])


class InternTests(unittest.TestCase):
    """Losslessness against the extractions the pilot already produced."""

    @classmethod
    def setUpClass(cls):
        manifest = BASE / "round5" / "completed-manifest.json"
        cls.rows = pilot.load_json(manifest) if manifest.exists() else []

    def canonical(self, document):
        if document.get("schema_version") == 2:
            return compact.intern_extraction(document)
        return document

    def test_every_completed_extraction_round_trips(self):
        if not self.rows:
            self.skipTest("no completed manifest in this checkout")
        for row in self.rows:
            with self.subTest(source=row["source_ref_id"]):
                source = pilot.load_json(
                    BASE / "sources" / f"{row['source_ref_id']}.json"
                )
                document = pilot.load_json(BASE / row["path"])
                bound = bindings.intern_document(document, source)
                self.assertEqual(
                    bindings.materialize(bound, source), self.canonical(document)
                )

    def test_sentence_bound_variant_only_widens_evidence(self):
        if not self.rows:
            self.skipTest("no completed manifest in this checkout")
        row = self.rows[0]
        source = pilot.load_json(BASE / "sources" / f"{row['source_ref_id']}.json")
        document = self.canonical(pilot.load_json(BASE / row["path"]))
        bound = bindings.intern_document(document, source, sentence_bound=True)
        restored = bindings.materialize(bound, source)
        for ident, anchor in document["evidence_pool"].items():
            widened = restored["evidence_pool"][ident]
            self.assertEqual(widened["chunk_id"], anchor["chunk_id"])
            self.assertIn(anchor["quote"], widened["quote"])

    def test_interned_document_is_smaller_by_construction(self):
        if not self.rows:
            self.skipTest("no completed manifest in this checkout")
        row = self.rows[0]
        source = pilot.load_json(BASE / "sources" / f"{row['source_ref_id']}.json")
        document = self.canonical(pilot.load_json(BASE / row["path"]))
        bound = bindings.intern_document(document, source)
        self.assertLess(
            len(json.dumps(bound, ensure_ascii=False)),
            len(json.dumps(document, ensure_ascii=False)),
        )


if __name__ == "__main__":
    unittest.main()
