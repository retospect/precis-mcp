import copy
import unittest

import compact
import pilot


class CompactTests(unittest.TestCase):
    def source(self):
        chunks = [
            (1, "Introduction", "General background without measurements."),
            (
                2,
                "Experimental methods",
                "All potentials are versus RHE. Sample A uses the stated electrolyte.",
            ),
            (3, "Results", "Sample A reached 98.0% at −0.4 V. See Figure 1b."),
            (
                4,
                "Results",
                "Figure 1. Performance at different potentials; error bars are standard deviations.",
            ),
            (5, "References", "1. Author, Journal 2020, 10, 5."),
            (6, "Acknowledgments", "We thank the funding agency."),
        ]
        return pilot.prepare_source(
            {
                "ref_id": 10,
                "title": "Synthetic test",
                "chunks": [
                    {
                        "chunk_id": cid,
                        "ord": index,
                        "chunk_kind": "references"
                        if section == "References"
                        else "paragraph",
                        "section_path": [section],
                        "text": text,
                    }
                    for index, (cid, section, text) in enumerate(chunks)
                ],
                "bibliography": [],
                "citations": [],
            }
        )

    def document(self):
        anchor = {"chunk_id": 3, "quote": "Sample A reached 98.0% at −0.4 V."}
        condition = {
            "name": "potential",
            "value": "−0.4",
            "unit": "V",
            "basis": "explicit",
            "evidence": [anchor],
        }
        return {
            "schema_version": 2,
            "source_ref_id": 10,
            "source_fingerprint": self.source()["fingerprint"],
            "objects": [
                {"id": "a", "type": "sample", "label": "A", "evidence": [anchor]}
            ],
            "groups": [],
            "assertions": [],
            "results": [
                {
                    "id": "r1",
                    "evidence": [anchor],
                    "normalization_evidence": [anchor],
                    "conditions": [condition],
                },
                {
                    "id": "r2",
                    "evidence": [anchor],
                    "normalization_evidence": [],
                    "conditions": [dict(condition, basis="inferred")],
                },
            ],
            "gaps": [],
            "coverage": {"inspected_chunk_ids": [3], "limitations": []},
            "round": 4,
            "producer": "synthetic test",
        }

    def test_codec_is_lossless(self):
        original = self.document()
        encoded = compact.intern_extraction(original)
        self.assertEqual(compact.expand_extraction(encoded), original)
        self.assertEqual(len(encoded["evidence_pool"]), 1)
        self.assertEqual(len(encoded["condition_pool"]), 1)

    def test_condition_applicability_is_not_inherited_from_pool(self):
        encoded = compact.intern_extraction(self.document())
        refs = [row["condition_refs"][0] for row in encoded["results"]]
        self.assertEqual(refs[0]["id"], refs[1]["id"])
        self.assertEqual([ref["basis"] for ref in refs], ["explicit", "inferred"])

    def test_unknown_evidence_reference_is_rejected(self):
        encoded = compact.intern_extraction(self.document())
        encoded["results"][0]["evidence"] = ["does-not-exist"]
        with self.assertRaises(ValueError):
            compact.expand_extraction(encoded)

    def test_missing_applicability_basis_is_rejected(self):
        encoded = compact.intern_extraction(self.document())
        del encoded["results"][0]["condition_refs"][0]["basis"]
        with self.assertRaises(ValueError):
            compact.expand_extraction(encoded)

    def test_packet_preserves_selected_text_exactly(self):
        source = self.source()
        packet = compact.build_packet(source, mode="compact")
        original = {chunk["chunk_id"]: chunk["text"] for chunk in source["chunks"]}
        for row in packet["chunks"]:
            self.assertEqual("".join(row[3]), original[row[0]])

    def test_methods_and_referenced_caption_survive(self):
        packet = compact.build_packet(self.source(), mode="compact")
        selected = {row[0] for row in packet["chunks"]}
        self.assertTrue({2, 3, 4} <= selected)
        self.assertNotIn(5, selected)
        self.assertNotIn(6, selected)

    def test_full_packet_contains_all_source_chunks(self):
        packet = compact.build_packet(self.source(), mode="full")
        self.assertEqual({row[0] for row in packet["chunks"]}, {1, 2, 3, 4, 5, 6})
        self.assertFalse(packet["omitted_index"])

    def test_packet_does_not_depend_on_previous_extractions(self):
        source = self.source()
        packet = compact.build_packet(source, mode="compact")
        source["irrelevant_previous_extractions"] = ["not input to selector"]
        self.assertEqual(compact.build_packet(source, mode="compact"), packet)

    def test_expansion_cost_is_counted_and_unknown_chunk_refused(self):
        source = self.source()
        packet = compact.build_packet(source, mode="compact")
        result = compact.access_metrics(source, packet, [5], lambda text: len(text))
        self.assertGreater(
            result["total_payload_tokens"], result["initial_payload_tokens"]
        )
        with self.assertRaises(ValueError):
            compact.access_metrics(source, packet, [999], len)

    def test_v5_does_not_trust_false_reference_label(self):
        source = self.source()
        source["chunks"][4]["text"] = (
            "[13] These barriers do not necessarily yield quantitative reaction rates; see the SI."
        )
        selected = {
            row[0]
            for row in compact.build_packet(
                source, mode="compact", selection_version=5
            )["chunks"]
        }
        self.assertIn(5, selected)

    def test_v5_keeps_unit_bearing_result_without_keyword_match(self):
        source = self.source()
        source["chunks"][4].update(
            chunk_kind="paragraph",
            section_path=["Activity"],
            text="The Nyquist fit gives Rct of 4.02 Ω.",
        )
        selected = {
            row[0]
            for row in compact.build_packet(
                source, mode="compact", selection_version=5
            )["chunks"]
        }
        self.assertIn(5, selected)

    def test_v5_keeps_qualitative_restriction(self):
        source = self.source()
        source["chunks"][4].update(
            chunk_kind="paragraph",
            section_path=["Mechanism"],
            text="The reaction can only occur when the oxygen end points down; the other orientation is unlikely.",
        )
        selected = {
            row[0]
            for row in compact.build_packet(
                source, mode="compact", selection_version=5
            )["chunks"]
        }
        self.assertIn(5, selected)

    def access_document(self):
        source = self.source()
        packet = compact.build_packet(source)
        document = compact.intern_extraction(self.document())
        document["input_packet_fingerprint"] = packet["packet_fingerprint"]
        document["access"] = {"packet_read_in_full": True, "expanded_chunk_ids": []}
        return source, packet, document

    def test_access_audit_checks_packet_identity(self):
        source, packet, document = self.access_document()
        document["input_packet_fingerprint"] = "wrong"
        self.assertTrue(compact.access_errors(source, packet, document))

    def test_preview_cannot_be_cited_without_declared_expansion(self):
        source, packet, document = self.access_document()
        document["evidence_pool"]["e2"] = {
            "chunk_id": 5,
            "quote": "Author, Journal 2020",
        }
        document["coverage"]["inspected_chunk_ids"].append(5)
        self.assertTrue(compact.access_errors(source, packet, document))
        document["access"]["expanded_chunk_ids"] = [5]
        self.assertEqual(compact.access_errors(source, packet, document), [])

    def test_unused_evidence_is_also_checked(self):
        source, packet, document = self.access_document()
        document["evidence_pool"]["unused"] = {
            "chunk_id": 3,
            "quote": "not actually present",
        }
        self.assertTrue(compact.access_errors(source, packet, document))

    def test_codec_does_not_mutate_input(self):
        original = self.document()
        saved = copy.deepcopy(original)
        compact.intern_extraction(original)
        self.assertEqual(original, saved)


if __name__ == "__main__":
    unittest.main()
