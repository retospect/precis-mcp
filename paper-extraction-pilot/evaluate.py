from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pilot


def condition_values(row, term):
    return {
        pilot.literal_normalize(condition["value"])
        for condition in row.get("conditions", [])
        if term.lower() in condition["name"].lower()
    }


def guardrails(rows):
    indexed = {(row["source_ref_id"], row["id"]): row for row in rows}
    checks = []

    def add(name, keys, predicate, reason):
        available = all(key in indexed for key in keys)
        checks.append(
            {
                "check": name,
                "scope": "curated pilot regression, not a general scientific validator",
                "input_results": [
                    {"source_ref_id": ref, "result_id": ident} for ref, ident in keys
                ],
                "exercised": available,
                "passed": bool(predicate(*[indexed[key] for key in keys]))
                if available
                else None,
                "reason": reason,
            }
        )

    add(
        "separate_palladium_performance_maxima",
        [(196807, "nh3_fe_pdce"), (196807, "nh3_yield_pdce")],
        lambda efficiency, rate: (
            efficiency["reported_value"] == "98.0"
            and rate["reported_value"] == "4.17"
            and condition_values(efficiency, "potential") == {"-0.4"}
            and condition_values(rate, "potential") == {"-0.6"}
        ),
        "The two maxima belong to different potentials and must not form a fictional joint operating point.",
    )
    add(
        "preserve_active_metal_mass_basis",
        [(196807, "nh3_yield_pdce")],
        lambda row: (
            "Pd mass" in (row.get("normalization_basis") or "")
            and row.get("normalization_status") == "explicit"
        ),
        "The reported denominator is Pd mass, not total catalyst mass or geometric area.",
    )
    add(
        "absolute_current_is_not_current_density",
        [(5524, "result-limiting-current")],
        lambda row: (
            row["reported_value"] == "3.58"
            and row["reported_unit"] == "mA"
            and "density" not in row["measurand"].lower()
        ),
        "No current-density or exchange-current value is manufactured from an absolute limiting current.",
    )
    add(
        "reaction_energy_is_not_activation_barrier",
        [(3334, "tafel-reaction-energy-1ml"), (3334, "tafel-barrier-1ml")],
        lambda energy, barrier: (
            energy["reported_value"] == "0.17 \\pm 0.03"
            and barrier["reported_value"] == "0.53 \\pm 0.02"
            and "reaction" in energy["measurand"].lower()
            and energy["id"] != barrier["id"]
        ),
        "Different table columns and quantity definitions remain distinct despite sharing eV units.",
    )
    add(
        "secondary_reports_are_not_own_experiments",
        [(965, "cited-n2o-onset-range"), (3334, "he-cited-activation-range")],
        lambda first, second: (
            first["source_attribution"] == second["source_attribution"] == "cited_work"
        ),
        "These observations have not been verified in their originating papers and are not independent own-work support.",
    )
    add(
        "sacrificial_medium_remains_distinct",
        [(1708, "r-au-water"), (1708, "r-au-methanol-first")],
        lambda water, methanol: (
            water["context_id"] != methanol["context_id"]
            and methanol["measurand_status"] == "interpreted"
        ),
        "Nominal pure-water and methanol-assisted conditions remain separate; first-cycle attribution is exposed as interpretation.",
    )
    add(
        "nitrogen_removal_and_ammonia_rate_not_collapsed",
        [(3651, "r-nitrate-capacity"), (3651, "r-ammonia-maximum-rate")],
        lambda capacity, rate: (
            "1,008.0" in capacity["reported_value"]
            and "1,505.9" in rate["reported_value"]
            and "mg N" in capacity["reported_unit"]
            and capacity["context_id"] != rate["context_id"]
        ),
        "Cumulative nitrogen-mass capacity and time/area-normalized ammonia production remain separate quantities and contexts.",
    )
    return checks


def evidence_chunk_ids(value):
    if isinstance(value, dict):
        found = {value["chunk_id"]} if isinstance(value.get("chunk_id"), int) else set()
        for item in value.values():
            found |= evidence_chunk_ids(item)
        return found
    if isinstance(value, list):
        return (
            set().union(*(evidence_chunk_ids(item) for item in value))
            if value
            else set()
        )
    return set()


def operating_context(row):
    temperatures = []
    for index, condition in enumerate(row.get("conditions", [])):
        name = condition.get("name", "")
        if not re.search(r"temperature|\btemp\b", name, re.I):
            continue
        if re.search(
            r"prepar|anneal|calcination|pyrolysis|synthes|heat.treat", name, re.I
        ):
            role = "preparation"
        elif row.get("evidence_mode") == "computational" or re.search(
            r"reference|correction|smearing|model", name, re.I
        ):
            role = "model_or_reference"
        else:
            role = "operating_or_unspecified"
        temperatures.append(
            {
                "condition_index": index,
                "role": role,
                "name": name,
                "reported_value": condition.get("value"),
                "reported_unit": condition.get("unit"),
                "applicability_basis": condition.get("basis"),
            }
        )
    return {
        "temperature_conditions": temperatures,
        "operating_temperature_status": "reported_condition_present"
        if any(item["role"] == "operating_or_unspecified" for item in temperatures)
        else "not_located_in_extracted_conditions",
        "role_assignment": "condition-name/evidence-mode heuristic; inspect the original condition evidence",
        "applicability": "not_assessed_without_application_profile",
        "necessity": "reported conditions do not by themselves establish necessary conditions",
    }


def extraction_paths(base, complete=False):
    folders = ["round2", "round3"]
    if complete:
        folders += ["round4/compact", "round4/remaining", "round4/recovered"]
    selected = {}
    for folder in folders:
        for path in (base / folder).glob("*.json"):
            if path.stem.isdecimal():
                selected[int(path.stem)] = path
    return [selected[ref_id] for ref_id in sorted(selected)]


def paired_payload_report(base):
    full = {
        item["source_ref_id"]: item
        for item in pilot.load_json(base / "round4/audits/full-initial/summary.json")
    }
    compact = {
        item["source_ref_id"]: item
        for item in pilot.load_json(base / "round4/audits/compact-initial/summary.json")
    }
    pairs = []
    for ref_id in sorted(full.keys() & compact.keys()):
        baseline, reduced = full[ref_id], compact[ref_id]
        before = baseline["payload_costs"]["total_payload_tokens"]
        after = reduced["payload_costs"]["total_payload_tokens"]
        left = pilot.load_json(
            base / "round4/audits/full-initial/expanded" / f"{ref_id}.json"
        )
        right = pilot.load_json(
            base / "round4/audits/compact-initial/expanded" / f"{ref_id}.json"
        )
        overlaps = []
        for a in left["results"]:
            for b in right["results"]:
                if pilot.literal_normalize(
                    a["reported_value"]
                ) == pilot.literal_normalize(
                    b["reported_value"]
                ) and evidence_chunk_ids(a.get("evidence", [])) & evidence_chunk_ids(
                    b.get("evidence", [])
                ):
                    overlaps.append(
                        {
                            "full_id": a["id"],
                            "compact_id": b["id"],
                            "reported_value": a["reported_value"],
                            "full_measurand": a["measurand"],
                            "compact_measurand": b["measurand"],
                        }
                    )
        pairs.append(
            {
                "source_ref_id": ref_id,
                "full_input_tokens": before,
                "compact_input_tokens_including_expansions": after,
                "input_reduction_fraction": 1 - after / before,
                "full_result_count": baseline["counts"]["results"],
                "compact_result_count": reduced["counts"]["results"],
                "full_output_tokens": baseline["payload_costs"][
                    "encoded_output_tokens"
                ],
                "compact_output_tokens": reduced["payload_costs"][
                    "encoded_output_tokens"
                ],
                "expanded_compact_output_tokens": reduced["payload_costs"][
                    "expanded_output_tokens"
                ],
                "shared_value_source_candidates": overlaps,
            }
        )
    before = sum(pair["full_input_tokens"] for pair in pairs)
    after = sum(pair["compact_input_tokens_including_expansions"] for pair in pairs)
    encoded = sum(pair["compact_output_tokens"] for pair in pairs)
    expanded = sum(pair["expanded_compact_output_tokens"] for pair in pairs)
    return {
        "pairs": pairs,
        "tokenizer": "tiktoken 0.12.0 / o200k_base",
        "full_input_tokens": before,
        "compact_input_tokens_including_expansions": after,
        "input_reduction_fraction": 1 - after / before if before else None,
        "encoded_compact_output_tokens": encoded,
        "expanded_same_content_output_tokens": expanded,
        "output_representation_reduction_fraction": 1 - encoded / expanded
        if expanded
        else None,
        "scope": "Unique file-payload reference tokens, not billing. The output comparison is lossless representation size, not a measured alternative model run. Value/source overlaps are candidates, not a scientific-equivalence or recall score. Arms were free to choose representative results.",
    }


def build_evaluation(base, complete=False):
    sources = {
        int(path.stem): pilot.load_json(path)
        for path in (base / "sources").glob("*.json")
    }
    selected, validations, rows, review_requests = [], [], [], []
    for chosen in extraction_paths(base, complete):
        original = pilot.load_json(chosen)
        source = sources[int(chosen.stem)]
        if original.get("schema_version") == 3:
            import compact

            packet_version = "v4" if chosen.parent.name == "compact" else "v5"
            document, validation = compact.audit_extraction(
                base, chosen, packet_version, "compact"
            )
        else:
            document = original
            validation = pilot.validate(source, document)
        extraction_fingerprint = pilot.digest(original)
        validations.append(validation)
        selected.append(
            {
                "source_ref_id": source["ref_id"],
                "path": str(chosen.relative_to(base)),
                "source_fingerprint": source["fingerprint"],
                "extraction_fingerprint": extraction_fingerprint,
                "round": document["round"],
                "status": "proposed_not_human_approved",
            }
        )
        for result in document["results"]:
            rows.append(
                {
                    **result,
                    "source_ref_id": source["ref_id"],
                    "source_title": source["title"],
                    "operating_context": operating_context(result),
                    "source_fingerprint": source["fingerprint"],
                    "extraction_path": str(chosen.relative_to(base)),
                    "extraction_fingerprint": extraction_fingerprint,
                    "status": "proposed_not_human_approved",
                }
            )
        for gap in document["gaps"]:
            chunks = set(gap.get("chunk_ids", []))
            affected = [
                result["id"]
                for result in document["results"]
                if chunks & evidence_chunk_ids(result)
            ]
            review_requests.append(
                {
                    "id": pilot.digest({"source": source["fingerprint"], "gap": gap})[
                        :20
                    ],
                    "source_ref_id": source["ref_id"],
                    "source_pdf_sha256": source.get("pdf_sha256"),
                    "source_fingerprint": source["fingerprint"],
                    "type": gap["type"],
                    "question": gap["description"],
                    "chunk_ids": gap.get("chunk_ids", []),
                    "potentially_affected_results": affected,
                    "impact_basis": "shared evidence chunks only; not a validated dependency graph",
                    "status": "local_request_only_not_queued_in_production",
                }
            )
    if not selected:
        raise ValueError("No completed extraction records are available")
    candidate_counts = {}
    for revision in (1, 2, 3):
        total = Counter()
        unresolved = 0
        documents = 0
        for path in (base / "candidates" / f"v{revision}").glob("*.json"):
            result = pilot.load_json(path)
            documents += 1
            total.update(
                {
                    key: len(result[key])
                    for key in (
                        "numbers",
                        "citations",
                        "internal_references",
                        "local_labels",
                    )
                }
            )
            unresolved += sum(
                not x.get("bibliography_candidates") for x in result["citations"]
            )
        candidate_counts[f"v{revision}"] = {
            "documents": documents,
            **dict(total),
            "citation_occurrences_without_local_bib_candidate": unresolved,
        }
    checks = guardrails(rows)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "Exploratory local pilot, purposive chemistry/electrochemistry sample; no statistical precision/recall estimate or human approval",
        "source_papers_exported": len(sources),
        "source_chunks_exported": sum(
            len(source["chunks"]) for source in sources.values()
        ),
        "sources_with_parsed_bibliography": sum(
            bool(source.get("bibliography")) for source in sources.values()
        ),
        "papers_with_agent_extraction": len(selected),
        "proposed_results": len(rows),
        "evidence_modes": dict(Counter(row["evidence_mode"] for row in rows)),
        "source_attribution": dict(Counter(row["source_attribution"] for row in rows)),
        "normalization_status": dict(
            Counter(row["normalization_status"] for row in rows)
        ),
        "value_generation": dict(Counter(row["value_generation"] for row in rows)),
        "self_reported_inspected_chunks": sum(
            report["inspected_chunks"] for report in validations
        ),
        "mechanical_errors": sum(len(report["errors"]) for report in validations),
        "mechanical_warnings": sum(len(report["warnings"]) for report in validations),
        "candidate_counts": candidate_counts,
        "curated_guardrails_exercised": sum(check["exercised"] for check in checks),
        "curated_guardrails_passed": sum(check["passed"] is True for check in checks),
        "review_requests": len(review_requests),
        "review_request_types": dict(
            Counter(request["type"] for request in review_requests)
        ),
        "rounds": [
            {
                "round": 1,
                "work": "Four-source extraction, 30 results, baseline mechanical candidates; preserve the initial checker false-positive report and corrected revalidation.",
            },
            {
                "round": 2,
                "work": "Separate evidence/generation/attribution and normalization status; revise four sources and extend to eight; expand superscript and author-year citation proposals.",
            },
            {
                "round": 3,
                "work": "Main-agent source review, corrected cycle-order interpretation flags, thousands/adjacent-unit/parenthetical-citation/panel-reference regressions, version-pinned evidence view and local review requests.",
            },
        ],
        "not_done": [
            "Complete twenty-paper hand-audited scientific benchmark",
            "Original PDF/figure/OCR fidelity verification",
            "Acquisition of missing supplements or cited primary papers",
            "Curve digitization or exchange-current refitting",
            "General cross-paper quantity ontology or comparability policy",
            "Database migration, deployment, signing, publication or production write",
        ],
        "reproducibility": "Source/extraction/candidate/review snapshots retained; final code snapshot included. Early script edits were made in place, not all historical code bytes were archived.",
        "interpretation": "Higher candidate counts measure proposals, not recall or correctness. Mechanical checks do not establish scientific validity. The held text may be incomplete or mistranscribed.",
    }
    if complete:
        report["completion_scope"] = (
            "Completed local extraction and mechanical/source-anchor audit; source scientific validity and exhaustive recall are not established"
        )
        report["missing_source_extractions"] = sorted(
            set(sources) - {item["source_ref_id"] for item in selected}
        )
        report["temperature_context_status"] = dict(
            Counter(
                row["operating_context"]["operating_temperature_status"] for row in rows
            )
        )
        report["paired_comparison"] = paired_payload_report(base)
        report["rounds"] += [
            {
                "round": 4,
                "work": "Stronger source-bound numeric checks, controlled full/compact reader comparison, pooled evidence/conditions, and completed source-text extraction for the twenty-paper pilot.",
            },
            {
                "round": 5,
                "work": "Conservative packet selection recovered all 388 development anchors; interrupted jobs recovered from files; final audit and one selected extraction per source consolidated without counting duplicate control runs.",
            },
        ]
        report["excluded_control_runs"] = (
            "round4/full contains the paired control outputs; they are retained but not counted as additional observations in the selected view."
        )
        report["ocr_scope"] = (
            "Re-OCR is intentionally deferred; consequential text ambiguities remain flagged."
        )
    return report, selected, validations, rows, checks, review_requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="final")
    parser.add_argument("--complete", action="store_true")
    args = parser.parse_args()
    if not args.label or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in args.label
    ):
        raise ValueError("Use a simple alphanumeric report label")
    base = Path(__file__).resolve().parent
    report, selected, validations, rows, checks, requests = build_evaluation(
        base, args.complete
    )
    output = base / ("round5" if args.complete else "round3")
    index = [
        {
            key: row.get(key)
            for key in (
                "source_ref_id",
                "id",
                "measurand",
                "reported_value",
                "reported_unit",
                "evidence_mode",
                "source_attribution",
                "normalization_status",
                "extraction_path",
                "operating_context",
            )
        }
        for row in rows
    ]
    artifacts = {
        base / "reports" / f"{args.label}-evaluation.json": report,
        base / "reports" / f"{args.label}-validation.json": validations,
        output / f"{args.label}-manifest.json": selected,
        output / f"{args.label}-evidence-view.json": rows,
        output / f"{args.label}-index.json": index,
        output / f"{args.label}-guardrails.json": checks,
        output / f"{args.label}-review-requests.json": requests,
    }
    code = {}
    for filename in (
        "pilot.py",
        "evaluate.py",
        "test_pilot.py",
        "format.json",
        "format-v2.json",
        "compact.py",
        "test_compact.py",
        "format-v3.json",
    ):
        text = (base / filename).read_text(encoding="utf-8")
        code[filename] = {
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "lines": text.splitlines(keepends=True),
        }
    artifacts[output / f"{args.label}-code-snapshot.json"] = code
    if any(path.exists() for path in artifacts):
        raise FileExistsError(
            "Report artifacts already exist; choose a new label to preserve history"
        )
    for path, value in artifacts.items():
        pilot.save_json(path, value)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(
        report["mechanical_errors"] > 0
        or any(check["exercised"] and not check["passed"] for check in checks)
    )


if __name__ == "__main__":
    raise SystemExit(main())
