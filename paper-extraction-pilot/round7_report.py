"""Round-7 measurement: what a reader actually produced from a semantic map.

Rounds 1-5 measured re-encodings of documents readers wrote by copying
quotations.  This round measures documents a reader wrote *as bindings*, which
is the only way to learn whether the contract in format-v4.json is usable.

Two things are reported and must not be conflated:

* feasibility — did the reader cite anchors rather than character offsets, did
  it reach for series recipes, did it correct the selector;
* representation cost — how large its binding document is, and how large the
  materialized record is.

Paper 537 also has a round-4 extraction produced the old way. The comparison
against it is descriptive overlap, NOT an accuracy score: the two runs had
different producers, prompts and scope declarations, and 537 was inspected in
an earlier round, so it is not held-out test data.
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
PAIRED = {537: Path("round4/remaining/537.json")}


def count(value):
    return len(
        ENCODER.encode(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            disallowed_special=(),
        )
    )


def span_styles(document):
    """How the reader chose to address the source."""
    styles = collections.Counter()
    for span in document.get("spans", {}).values():
        if isinstance(span, str):
            styles[
                "sentence_run"
                if "-" in span
                else "numeric"
                if "#" in span
                else "sentence"
            ] += 1
        else:
            styles["character_offsets"] += 1
    return dict(styles)


def canonical(document):
    if document.get("schema_version") == 2:
        return compact.intern_extraction(document)
    return document


def paired_overlap(fresh, prior, source):
    """Descriptive overlap between the two extractions of one paper."""

    def chunks(document):
        return {anchor["chunk_id"] for anchor in document["evidence_pool"].values()}

    fresh_chunks, prior_chunks = chunks(fresh), chunks(prior)
    return {
        "fresh_results": len(fresh["results"]),
        "prior_results": len(prior["results"]),
        "fresh_evidence_chunks": len(fresh_chunks),
        "prior_evidence_chunks": len(prior_chunks),
        "shared_evidence_chunks": len(fresh_chunks & prior_chunks),
        "fresh_only_evidence_chunks": sorted(fresh_chunks - prior_chunks),
        "prior_only_evidence_chunks": sorted(prior_chunks - fresh_chunks),
        "fresh_measurands": [row["measurand"] for row in fresh["results"]],
        "prior_measurands": [row["measurand"] for row in prior["results"]],
        "source_chunks": len(source["chunks"]),
        "interpretation": (
            "Overlap is descriptive. Different producers, prompts and scope "
            "declarations; neither run is a gold standard and 537 was already "
            "inspected in round 4."
        ),
    }


def main():
    directory = BASE / "round7" / "readers"
    rows, totals = [], collections.Counter()
    escalations = collections.Counter()
    paired = {}
    for path in sorted(directory.glob("*.json")):
        if not path.stem.isdecimal():
            continue
        ref_id = int(path.stem)
        document = pilot.load_json(path)
        source = pilot.load_json(BASE / "sources" / f"{ref_id}.json")
        report = bindings.check(document, source)
        for item in report["escalations"]:
            escalations[item["type"]] += 1
        expanded = (
            bindings.materialize(document, source)
            if not report["binding_errors"]
            else None
        )
        semantic_map = pilot.load_json(BASE / "maps" / "norr-pd" / f"{ref_id}.json")
        row = {
            "source_ref_id": ref_id,
            "map_tokens": count(semantic_map),
            "binding_tokens": count(document),
            "materialized_schema3_tokens": count(expanded) if expanded else None,
            "materialized_schema2_tokens": (
                count(compact.expand_extraction(expanded)) if expanded else None
            ),
            "results_written_by_reader": len(document.get("results", [])),
            "results_after_expansion": (len(expanded["results"]) if expanded else None),
            "series_recipes": len(document.get("series", [])),
            "conditions": len(document.get("conditions", {})),
            "spans": len(document.get("spans", {})),
            "span_styles": span_styles(document),
            #  expanded_chunk_ids is range-encoded, so count decoded ids.
            "expanded_blocks": len(
                bindings.decode_ranges(
                    document.get("access", {}).get("expanded_chunk_ids", [])
                )
            ),
            "retirements": len(document.get("retire", [])),
            "gaps": len(document.get("gaps", [])),
            "mechanical_errors": report["errors"],
            "binding_errors": report["binding_errors"],
            "escalations": len(report["escalations"]),
        }
        rows.append(row)
        for key in (
            "map_tokens",
            "binding_tokens",
            "results_written_by_reader",
            "series_recipes",
            "spans",
        ):
            totals[key] += row[key] or 0
        if expanded is not None:
            totals["materialized_schema3_tokens"] += row["materialized_schema3_tokens"]
            totals["results_after_expansion"] += row["results_after_expansion"]
        if ref_id in PAIRED and (BASE / PAIRED[ref_id]).exists() and expanded:
            paired[str(ref_id)] = paired_overlap(
                expanded, canonical(pilot.load_json(BASE / PAIRED[ref_id])), source
            )

    styles = collections.Counter()
    for row in rows:
        styles.update(row["span_styles"])
    offsets = styles.get("character_offsets", 0)
    total_spans = sum(styles.values()) or 1
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "round": 7,
        "scope": (
            "First extractions written as bindings from a semantic map alone. "
            "Four Pd papers behind two subgroups of draft 348633. Feasibility "
            "check, not an accuracy measurement: no human adjudication, no "
            "held-out set, no precision or recall estimate."
        ),
        "tokenizer": "tiktoken==0.12.0 / o200k_base",
        "provider_usage": "Not observable from this pilot; payload proxy only.",
        "documents": len(rows),
        "feasibility": {
            "span_styles": dict(styles),
            "anchor_share_of_spans": round(1 - offsets / total_spans, 4),
            "series_recipes_used": totals["series_recipes"],
            "results_written_by_hand": totals["results_written_by_reader"],
            "results_after_recipe_expansion": totals["results_after_expansion"],
            "note": (
                "anchor_share is the headline feasibility number: the contract "
                "asks the reader to cite anchors, and character offsets are the "
                "fallback it was not supposed to need."
            ),
        },
        "totals": dict(totals),
        "escalations": {
            "total": sum(escalations.values()),
            "by_type": dict(sorted(escalations.items())),
        },
        "per_paper": rows,
        "paired_with_round4": paired,
        "not_done": [
            "Human scientific adjudication of any proposal",
            "Held-out evaluation set or precision/recall estimate",
            "Supplement acquisition, figure digitisation, OCR",
            "Cross-paper identity, measurand ontology or comparability",
            "Any production write, signing or publication",
        ],
    }
    pilot.save_json(BASE / "reports" / "round7-evaluation.json", report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("documents", "feasibility", "totals", "escalations")
            },
            indent=2,
        )
    )
    for row in rows:
        if row["mechanical_errors"] or row["binding_errors"]:
            print(
                json.dumps(
                    {
                        "source_ref_id": row["source_ref_id"],
                        "binding_errors": row["binding_errors"],
                        "mechanical_errors": row["mechanical_errors"][:5],
                    },
                    indent=2,
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
