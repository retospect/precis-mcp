"""Round-6 measurement: binding representation and workflow payload costs.

Every number here is a reference-tokenizer proxy over exact serialized file
payloads.  Provider billing is not observable from this pilot, so it is
reported as such rather than estimated.  The twenty papers were inspected in
earlier rounds; none of this is held-out test data.
"""

from __future__ import annotations

import collections
import json
from datetime import UTC, datetime
from pathlib import Path

import bindings
import compact
import pilot
import tiktoken

BASE = Path(__file__).resolve().parent
ENCODER = tiktoken.get_encoding("o200k_base")


def count(value):
    return len(
        ENCODER.encode(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            disallowed_special=(),
        )
    )


def canonical(document):
    if document.get("schema_version") == 2:
        return compact.intern_extraction(document)
    return document


def main():
    manifest = pilot.load_json(BASE / "round5" / "completed-manifest.json")
    totals = collections.Counter()
    per_paper = []
    escalations = collections.Counter()
    audited = 0
    for row in manifest:
        ref_id = row["source_ref_id"]
        source = pilot.load_json(BASE / "sources" / f"{ref_id}.json")
        document = canonical(pilot.load_json(BASE / row["path"]))
        bound = pilot.load_json(BASE / "round6" / "bindings" / f"{ref_id}.json")
        sentence = pilot.load_json(
            BASE / "round6" / "bindings-sentence" / f"{ref_id}.json"
        )
        semantic_map = pilot.load_json(BASE / "maps" / "v1" / f"{ref_id}.json")
        report = bindings.check(bound, source)
        for item in report["escalations"]:
            escalations[item["type"]] += 1
        audited += len(report["audit_sample"])
        measures = {
            "source_ref_id": ref_id,
            "full_packet_tokens": count(compact.build_packet(source, "full")),
            "selected_packet_tokens": count(compact.build_packet(source, "compact")),
            "semantic_map_tokens": count(semantic_map),
            "schema2_expanded_tokens": count(compact.expand_extraction(document)),
            "schema3_pooled_tokens": count(document),
            "schema4_binding_tokens": count(bound),
            "schema4_sentence_bound_tokens": count(sentence),
            "results": len(document["results"]),
            "evidence_anchors": len(document["evidence_pool"]),
            "escalations": len(report["escalations"]),
            "mechanical_errors": len(report["errors"]),
        }
        per_paper.append(measures)
        for key, value in measures.items():
            if key != "source_ref_id":
                totals[key] += value

    def fraction(new, old):
        return round(1 - totals[new] / totals[old], 4)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "round": 6,
        "scope": (
            "Representation and payload measurement over the twenty papers the "
            "pilot already inspected in rounds 1-5. Not held-out test data, not "
            "a recall or precision estimate, and not scientific adjudication."
        ),
        "tokenizer": "tiktoken==0.12.0 / o200k_base",
        "tokenizer_cache": "paper-extraction-pilot/tokenizer-cache",
        "measurement_scope": (
            "Exact serialized file payloads. Tool wrappers, system prompts, "
            "reasoning tokens and cache behaviour are excluded."
        ),
        "provider_usage": (
            "Not observable from this pilot. No billed input/output token counts "
            "are claimed. The proxy below bounds payload size only."
        ),
        "documents": len(per_paper),
        "totals": dict(totals),
        "representation_reduction": {
            "schema4_vs_schema3_output": fraction(
                "schema4_binding_tokens", "schema3_pooled_tokens"
            ),
            "schema4_vs_schema2_output": fraction(
                "schema4_binding_tokens", "schema2_expanded_tokens"
            ),
            "sentence_bound_vs_schema3_output": fraction(
                "schema4_sentence_bound_tokens", "schema3_pooled_tokens"
            ),
            "semantic_map_vs_full_packet_input": fraction(
                "semantic_map_tokens", "full_packet_tokens"
            ),
            "semantic_map_vs_selected_packet_input": fraction(
                "semantic_map_tokens", "selected_packet_tokens"
            ),
            "note": (
                "The lossless binding encoding reproduces every source extraction "
                "byte for byte. The sentence-bound variant widens each quotation "
                "to its covering sentence anchor, so its evidence is a superset "
                "of the original, not an identity."
            ),
        },
        "single_pass_workflow_payload": {
            "binding_arm_tokens": totals["semantic_map_tokens"]
            + totals["schema4_sentence_bound_tokens"],
            "selected_packet_arm_tokens": totals["selected_packet_tokens"]
            + totals["schema3_pooled_tokens"],
            "full_packet_arm_tokens": totals["full_packet_tokens"]
            + totals["schema2_expanded_tokens"],
            "reduction_vs_selected_packet_arm": round(
                1
                - (
                    totals["semantic_map_tokens"]
                    + totals["schema4_sentence_bound_tokens"]
                )
                / (totals["selected_packet_tokens"] + totals["schema3_pooled_tokens"]),
                4,
            ),
            "reduction_vs_full_packet_arm": round(
                1
                - (
                    totals["semantic_map_tokens"]
                    + totals["schema4_sentence_bound_tokens"]
                )
                / (totals["full_packet_tokens"] + totals["schema2_expanded_tokens"]),
                4,
            ),
            "caveat": (
                "One reader pass per paper in each arm. Expansion reads, "
                "escalation turns and audit turns are additional in both arms "
                "and are not included here."
            ),
        },
        "escalations": {
            "total": sum(escalations.values()),
            "by_type": dict(sorted(escalations.items())),
            "sampled_for_strong_model_audit": audited,
            "interpretation": (
                "Escalations are deterministic triage of the existing rounds' "
                "bindings, not defects proven present. sub_sentence_span counts "
                "quotations the earlier rounds cropped inside a sentence; under "
                "the binding contract a reader cites whole sentence anchors."
            ),
        },
        "per_paper": per_paper,
        "not_done": [
            "Fresh binding extraction by a reader working only from the map",
            "Human scientific approval of any proposal",
            "Cross-paper identity resolution, measurand ontology or comparability",
            "Supplement acquisition, figure digitisation, curve refitting or OCR",
            "Statistical precision or recall estimate",
            "Any production write, signing or publication",
        ],
    }
    pilot.save_json(BASE / "reports" / "round6-evaluation.json", report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "documents",
                    "representation_reduction",
                    "single_pass_workflow_payload",
                    "escalations",
                )
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
