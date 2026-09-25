from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path

import pilot

END_MATTER = re.compile(
    r"\b(references|bibliography|acknowledg\w*|author contributions|conflicts? of interest|funding information)\b",
    re.I,
)
METHOD_SECTION = re.compile(
    r"\b(methods?|experimental|methodology|computational details|calculation details|characterization|synthesis|preparation|measurements?)\b",
    re.I,
)
RESULT_SECTION = re.compile(r"\b(results?|discussion|conclusions?)\b", re.I)
METHOD_CUE = re.compile(
    r"\b(all potentials|unless otherwise|reference electrode|normaliz\w*|calibrat\w*|geometric area|confidence|uncertaint\w*|error bars|standard deviation|electrolyte|pH|working electrode|functional|k.points?|temperature|light source|irradiat\w*|concentration|scan rate)\b",
    re.I,
)
RESULT_CUE = re.compile(
    r"\b(rate|yield|efficien\w*|current|potential|barrier|activation|energy|capacity|selectiv\w*|conversion|reached|achieved|obtained|measured|calculated|rmse|accuracy|diameter|surface area|performance)\b",
    re.I,
)
CAPTION = re.compile(
    r"^\s*(?:\*\*)?(Fig(?:ure)?\.?|Table|Scheme)\s+([Ss]?\d+[a-z]?)", re.I
)


def render_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


QUANTITY_UNIT = re.compile(
    r"\d\s*(?:%|[µmkn]?A|[µmk]?V|[µmk]?F|[µmk]?Ω|ohms?|eV|kJ|[µm]?mol|mL|ML|L|nm|µm|mg|g|K|°C)(?![A-Za-z])"
)
RESTRICTION = re.compile(
    r"\b(can only|not necessarily|not measured|not monitored|unless|whereas|in contrast|assum\w*|limitations?|uncertain\w*)\b",
    re.I,
)


def looks_bibliographic(text):
    if re.search(r"\b(we|our|these|therefore|however|whereas)\b", text, re.I):
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    marked = [
        line
        for line in lines
        if re.match(r"^(?:-\s*)?(?:\[\d+[a-z]?\]|\(?\d+\)?[.)])\s+", line)
    ]
    return bool(marked) and sum(
        bool(re.search(r"\b(?:18|19|20)\d{2}\b|10\.\d{4,}/", line)) for line in marked
    ) >= max(1, len(lines) / 2)


def source_reasons(source, selection_version=4):
    reasons = {}
    title = pilot.unit_surface(source.get("title", "")).casefold().strip()
    for chunk in source["chunks"]:
        text = chunk["text"]
        section_parts = [
            part
            for part in chunk.get("section_path", [])
            if pilot.unit_surface(part).casefold().strip() != title
        ]
        section = " / ".join(section_parts)
        reference_label = chunk["chunk_kind"] == "references" or bool(
            re.search(r"\b(references|bibliography)\b", section, re.I)
        )
        other_end_matter = bool(END_MATTER.search(section)) and not reference_label
        reference_content = reference_label and (
            selection_version == 4 or looks_bibliographic(text)
        )
        if reference_content or other_end_matter:
            reasons[chunk["chunk_id"]] = ["end_matter"]
            continue
        flags = []
        plain = pilot.unit_surface(text)
        if selection_version >= 5:
            if reference_label:
                flags.append("reference_label_requires_review")
            if QUANTITY_UNIT.search(plain):
                flags.append("unit_bearing_quantitative_text")
            if RESTRICTION.search(plain):
                flags.append("qualitative_restriction")
            if re.search(
                r"\b(tests?|theoretical|calculations?|simulations?)\b", section, re.I
            ):
                flags.append("method_family")
        numeric = bool(pilot.numeric_regions(text))
        if METHOD_SECTION.search(section) or METHOD_CUE.search(plain):
            flags.append("method_or_qualifier")
        if numeric and (RESULT_SECTION.search(section) or RESULT_CUE.search(plain)):
            flags.append("quantitative_context")
        if chunk["chunk_kind"] in {"table", "figure"} or text.lstrip().startswith("|"):
            flags.append("table_or_figure")
        if CAPTION.match(text):
            flags.append("caption_candidate")
        if re.match(r"\s*(?:where\b|Here,?\b|In this (?:work|study))", plain, re.I):
            flags.append("definition_or_scope")
        reasons[chunk["chunk_id"]] = flags
    eligible = [
        chunk
        for chunk in source["chunks"]
        if "end_matter" not in reasons[chunk["chunk_id"]]
    ]
    for chunk in eligible[:3]:
        reasons[chunk["chunk_id"]].append("orientation")
    return reasons


def select_chunks(source, selection_version=4):
    chunks = source["chunks"]
    reasons = source_reasons(source, selection_version)
    selected = {
        cid for cid, flags in reasons.items() if flags and "end_matter" not in flags
    }
    caption_targets = {}
    for chunk in chunks:
        match = CAPTION.match(chunk["text"])
        if match:
            family = (
                "figure"
                if match[1].lower().startswith("fig")
                else "table"
                if match[1].lower().startswith("tab")
                else "scheme"
            )
            caption_targets.setdefault((family, match[2].lower()), []).append(
                chunk["chunk_id"]
            )
    for chunk in chunks:
        if chunk["chunk_id"] not in selected:
            continue
        for match in pilot.INTERNAL.finditer(chunk["text"]):
            family = (
                "figure"
                if match[1].lower().startswith("fig")
                else "table"
                if match[1].lower().startswith("tab")
                else "equation"
            )
            label = match[2].lower()
            for cid in caption_targets.get((family, label), []) + caption_targets.get(
                (family, re.sub(r"[a-z]$", "", label)), []
            ):
                if "end_matter" not in reasons[cid]:
                    selected.add(cid)
                    reasons[cid].append("referenced_caption")
    for index, chunk in enumerate(chunks):
        if chunk["chunk_id"] not in selected:
            continue
        for neighbor_index in (index - 1, index + 1):
            if not 0 <= neighbor_index < len(chunks):
                continue
            neighbor = chunks[neighbor_index]
            if (
                neighbor["chunk_id"] in selected
                or "end_matter" in reasons[neighbor["chunk_id"]]
            ):
                continue
            if neighbor.get("section_path") != chunk.get("section_path"):
                continue
            first, second = (
                (neighbor, chunk) if neighbor_index < index else (chunk, neighbor)
            )
            begins_continuation = bool(
                re.match(r"\s*(?:[a-z]|<sup>|[),;])", second["text"])
            )
            ends_continuation = not bool(re.search(r"[.!?][\s*$]*$", first["text"]))
            overlap = (
                first["text"][-50:] in second["text"][:160]
                if len(first["text"]) >= 50
                else False
            )
            if begins_continuation or ends_continuation or overlap:
                selected.add(neighbor["chunk_id"])
                reasons[neighbor["chunk_id"]].append("local_continuation")
    return selected, reasons


def block_packet(source, chunk):
    return {
        "source_ref_id": source["ref_id"],
        "source_fingerprint": source["fingerprint"],
        "chunk_id": chunk["chunk_id"],
        "section": chunk.get("section_path", []),
        "kind": chunk["chunk_kind"],
        "text_segments": [
            chunk["text"][i : i + 700] for i in range(0, len(chunk["text"]), 700)
        ],
    }


def build_packet(source, mode="compact", selection_version=4):
    if mode not in {"compact", "full"} or selection_version not in {4, 5}:
        raise ValueError("Invalid packet mode or selector version")
    selected, _reasons = select_chunks(source, selection_version)
    if mode == "full":
        selected = {chunk["chunk_id"] for chunk in source["chunks"]}
    sections = []
    rows, omitted = [], []
    for chunk in source["chunks"]:
        section = chunk.get("section_path", [])
        if section not in sections:
            sections.append(section)
        section_id = sections.index(section)
        if chunk["chunk_id"] in selected:
            rows.append(
                [
                    chunk["chunk_id"],
                    section_id,
                    chunk["chunk_kind"],
                    [
                        chunk["text"][i : i + 700]
                        for i in range(0, len(chunk["text"]), 700)
                    ],
                ]
            )
        else:
            omitted.append(
                [
                    chunk["chunk_id"],
                    section_id,
                    chunk["chunk_kind"],
                    chunk["text"][:100].replace("\n", " "),
                ]
            )
    result = {
        "packet_version": 4,
        "mode": mode,
        "source_ref_id": source["ref_id"],
        "source_fingerprint": source["fingerprint"],
        "title": source["title"],
        "chunk_columns": [
            "chunk_id",
            "section_index",
            "kind",
            "verbatim_text_segments",
        ],
        "sections": sections,
        "chunks": rows,
        "omitted_columns": [
            "chunk_id",
            "section_index",
            "kind",
            "navigation_preview_NOT_evidence",
        ],
        "omitted_index": omitted,
        "expansion_directory": f"compact/v4/blocks/{source['ref_id']}",
        "policy": "Selection is heuristic, not scope verification. Text segments concatenate exactly. Omitted previews are NOT evidence: read the full block before citing it. No PDF, figure-image or supplement verification is implied.",
    }
    if selection_version != 4:
        result["selection_version"] = selection_version
    result["packet_fingerprint"] = pilot.digest(result)
    return result


def access_metrics(source, packet, expanded_chunk_ids, counter):
    included = {row[0] for row in packet["chunks"]}
    chunks = {chunk["chunk_id"]: chunk for chunk in source["chunks"]}
    expanded = set(expanded_chunk_ids)
    if not expanded <= chunks.keys():
        raise ValueError("Expansion refers to an unknown source chunk")
    actual = sorted(expanded - included)
    initial = counter(render_json(packet))
    extra = sum(
        counter(render_json(block_packet(source, chunks[cid]))) for cid in actual
    )
    return {
        "initial_payload_tokens": initial,
        "expansion_payload_tokens": extra,
        "total_payload_tokens": initial + extra,
        "expanded_chunk_ids": actual,
        "measurement_scope": "Reference-tokenizer file payload, excluding tool wrappers, system prompts, reasoning and hidden/billed usage",
    }


def access_errors(source, packet, document):
    errors = []
    if document.get("input_packet_fingerprint") != packet.get("packet_fingerprint"):
        errors.append("input packet fingerprint mismatch")
    if pilot.digest(
        {key: value for key, value in packet.items() if key != "packet_fingerprint"}
    ) != packet.get("packet_fingerprint"):
        errors.append("packet contents do not match its fingerprint")
    if (
        packet.get("source_ref_id") != source["ref_id"]
        or packet.get("source_fingerprint") != source["fingerprint"]
    ):
        errors.append("packet source identity mismatch")
    chunks = {chunk["chunk_id"]: chunk for chunk in source["chunks"]}
    included = {row[0] for row in packet["chunks"]}
    for row in packet["chunks"]:
        if row[0] not in chunks or "".join(row[3]) != chunks[row[0]]["text"]:
            errors.append("packet contains an unknown or modified source chunk")
    access = document.get("access", {})
    if not isinstance(access, dict) or access.get("packet_read_in_full") is not True:
        errors.append(
            "trial requires an explicit declaration that the initial packet was read"
        )
        access = {}
    expanded = access.get("expanded_chunk_ids", [])
    if not isinstance(expanded, list) or any(
        type(cid) is not int or cid not in chunks for cid in expanded
    ):
        errors.append("invalid expansion declaration")
        expanded = []
    permitted = included | set(expanded)
    inspected = document.get("coverage", {}).get("inspected_chunk_ids", [])
    if not isinstance(inspected, list) or any(
        type(cid) is not int or cid not in permitted for cid in inspected
    ):
        errors.append(
            "inspection ledger includes a chunk not supplied or declared expanded"
        )
        inspected = []
    pool = document.get("evidence_pool", {})
    if not isinstance(pool, dict):
        return errors + ["evidence_pool must be an object"]
    for ident, anchor in pool.items():
        if (
            not isinstance(anchor, dict)
            or type(anchor.get("chunk_id")) is not int
            or not isinstance(anchor.get("quote"), str)
            or not anchor["quote"]
        ):
            errors.append(f"{ident}: malformed evidence anchor")
            continue
        cid = anchor["chunk_id"]
        if cid not in permitted or cid not in inspected:
            errors.append(
                f"{ident}: evidence cites an unavailable or uninspected block"
            )
            continue
        matches = list(re.finditer(re.escape(anchor["quote"]), chunks[cid]["text"]))
        occurrence = anchor.get("occurrence", 0 if len(matches) == 1 else None)
        if (
            not matches
            or type(occurrence) is not int
            or not 0 <= occurrence < len(matches)
        ):
            errors.append(f"{ident}: quote is absent or its occurrence is ambiguous")
    return errors


def audit_extraction(base, input_path, packet_version, mode, counter=None):
    document = pilot.load_json(input_path)
    ref_id = int(input_path.stem)
    source = pilot.load_json(base / "sources" / f"{ref_id}.json")
    directory = "full" if mode == "full" else "selected"
    packet = pilot.load_json(
        base / "compact" / packet_version / directory / f"{ref_id}.json"
    )
    errors = access_errors(source, packet, document)
    expanded = expand_extraction(document)
    validation = pilot.validate(source, expanded)
    validation["errors"] = errors + validation["errors"]
    counter = counter or count_tokens
    costs = access_metrics(
        source,
        packet,
        document.get("access", {}).get("expanded_chunk_ids", []),
        counter,
    )
    costs.update(
        encoded_output_tokens=counter(render_json(document)),
        expanded_output_tokens=counter(render_json(expanded)),
    )
    validation.update(
        input_path=str(input_path.relative_to(base)),
        packet_version=packet_version,
        arm=mode,
        payload_costs=costs,
        original_schema_version=3,
    )
    return expanded, validation


def intern_extraction(document):
    if document.get("schema_version") != 2:
        raise ValueError("Only schema-v2 source extractions can be interned")
    evidence_pool, condition_pool = {}, {}
    evidence_ids, condition_ids = {}, {}

    def evidence_ref(anchor):
        key = pilot.digest(anchor)
        if key not in evidence_ids:
            ident = f"e{len(evidence_pool) + 1}"
            evidence_ids[key] = ident
            evidence_pool[ident] = copy.deepcopy(anchor)
        return evidence_ids[key]

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in {"evidence", "normalization_evidence"}:
                result[key] = [evidence_ref(anchor) for anchor in item]
            elif key == "conditions":
                refs = []
                for condition in item:
                    definition = walk(
                        {
                            name: entry
                            for name, entry in condition.items()
                            if name != "basis"
                        }
                    )
                    fingerprint = pilot.digest(definition)
                    if fingerprint not in condition_ids:
                        ident = f"c{len(condition_pool) + 1}"
                        condition_ids[fingerprint] = ident
                        condition_pool[ident] = definition
                    refs.append(
                        {
                            "id": condition_ids[fingerprint],
                            "basis": condition["basis"],
                            "applicability_evidence": list(definition["evidence"]),
                        }
                    )
                result["condition_refs"] = refs
            else:
                result[key] = walk(item)
        return result

    result = walk(document)
    result["schema_version"] = 3
    result["evidence_pool"] = evidence_pool
    result["condition_pool"] = condition_pool
    return result


def expand_extraction(document):
    if document.get("schema_version") != 3:
        raise ValueError("Expected schema version 3")
    evidence_pool = document.get("evidence_pool")
    condition_pool = document.get("condition_pool")
    if not isinstance(evidence_pool, dict) or not isinstance(condition_pool, dict):
        raise ValueError("Evidence and condition pools must be objects")

    def evidence_refs(refs):
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or ref not in evidence_pool for ref in refs
        ):
            raise ValueError("Unknown or malformed evidence reference")
        return [copy.deepcopy(evidence_pool[ref]) for ref in refs]

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in {"evidence", "normalization_evidence"}:
                result[key] = evidence_refs(item)
            elif key == "condition_refs":
                conditions = []
                if not isinstance(item, list):
                    raise ValueError("condition_refs must be a list")
                for reference in item:
                    if (
                        not isinstance(reference, dict)
                        or reference.get("id") not in condition_pool
                        or reference.get("basis") not in {"explicit", "inferred"}
                    ):
                        raise ValueError(
                            "Unknown condition or missing per-result applicability basis"
                        )
                    applicability = evidence_refs(
                        reference.get("applicability_evidence")
                    )
                    if not applicability:
                        raise ValueError(
                            "Per-result condition applicability evidence is required"
                        )
                    condition = walk(condition_pool[reference["id"]])
                    condition["basis"] = reference["basis"]
                    union = condition["evidence"] + applicability
                    condition["evidence"] = list(
                        {pilot.digest(anchor): anchor for anchor in union}.values()
                    )
                    conditions.append(condition)
                result["conditions"] = conditions
            elif key not in {"evidence_pool", "condition_pool"}:
                result[key] = walk(item)
        return result

    result = walk(document)
    result["schema_version"] = 2
    return result


def count_tokens(text):
    import tiktoken

    return len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))


def build_files(base, label, selection_version=4):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        raise ValueError("Use a simple output label")
    output = base / "compact" / label
    if output.exists():
        raise FileExistsError("Packet version already exists; choose a fresh label")
    metrics = []
    for path in sorted((base / "sources").glob("*.json")):
        source = pilot.load_json(path)
        full = build_packet(source, "full", selection_version)
        packet = build_packet(source, "compact", selection_version)
        for current in (full, packet):
            current["expansion_directory"] = (
                f"compact/{label}/blocks/{source['ref_id']}"
            )
            current.pop("packet_fingerprint")
            current["packet_fingerprint"] = pilot.digest(current)
        pilot.save_json(output / "full" / path.name, full)
        pilot.save_json(output / "selected" / path.name, packet)
        for chunk in source["chunks"]:
            pilot.save_json(
                output / "blocks" / str(source["ref_id"]) / f"{chunk['chunk_id']}.json",
                block_packet(source, chunk),
            )
        legacy = (base / "packets" / path.name).read_text(encoding="utf-8")
        selected_ids = {row[0] for row in packet["chunks"]}
        development = base / "round3" / path.name
        if not development.exists():
            development = base / "round2" / path.name
        anchors = []
        output_tokens = None
        if development.exists():
            document = pilot.load_json(development)
            validation = pilot.validate(source, document)
            anchors = list(
                {
                    (anchor["chunk_id"], anchor["start"], anchor["end"])
                    for anchor in validation["anchors"]
                }
            )
            packed = intern_extraction(document)
            if expand_extraction(packed) != document:
                raise ValueError("Evidence-pool encoding was not lossless")
            pilot.save_json(output / "encoded_existing" / path.name, packed)
            output_tokens = {
                "expanded": count_tokens(render_json(document)),
                "interned": count_tokens(render_json(packed)),
            }
        metrics.append(
            {
                "source_ref_id": source["ref_id"],
                "source_fingerprint": source["fingerprint"],
                "legacy_packet_tokens": count_tokens(legacy),
                "full_indexed_tokens": count_tokens(render_json(full)),
                "selected_packet_tokens": count_tokens(render_json(packet)),
                "source_chunks": len(source["chunks"]),
                "selected_chunks": len(selected_ids),
                "known_unique_anchors": len(anchors),
                "known_anchors_retained": sum(
                    cid in selected_ids for cid, _, _ in anchors
                ),
                "missing_known_anchor_chunks": sorted(
                    {cid for cid, _, _ in anchors if cid not in selected_ids}
                ),
                "output_payload_tokens": output_tokens,
                "calibration_note": "Existing extracted anchors are a development diagnostic, not an exhaustive recall gold standard. Selector does not consume them.",
            }
        )
    report = {
        "tokenizer": "tiktoken==0.12.0 / o200k_base",
        "billing_tokens": "not observable",
        "metrics_scope": "Exact serialized file payloads, excluding tool/system/reasoning overhead",
        "policy": "No numerical or semantic decisions are copied from prior extractions into packet selection.",
        "documents": metrics,
    }
    pilot.save_json(output / "metrics.json", report)
    print(
        json.dumps(
            {
                "documents": len(metrics),
                "legacy_tokens": sum(row["legacy_packet_tokens"] for row in metrics),
                "full_indexed_tokens": sum(
                    row["full_indexed_tokens"] for row in metrics
                ),
                "selected_tokens": sum(
                    row["selected_packet_tokens"] for row in metrics
                ),
                "development_anchors": sum(
                    row["known_unique_anchors"] for row in metrics
                ),
                "retained_development_anchors": sum(
                    row["known_anchors_retained"] for row in metrics
                ),
            },
            indent=2,
        )
    )


def audit_files(base, directory, packet_version, mode, label):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label) or not re.fullmatch(
        r"v\d+", packet_version
    ):
        raise ValueError("Use simple audit and packet-version labels")
    directory = (base / directory).resolve()
    directory.relative_to(base.resolve())
    output = base / "round4" / "audits" / label
    if output.exists():
        raise FileExistsError("Audit label already exists; use a new label")
    reports = []
    for path in sorted(directory.glob("*.json")):
        if not path.stem.isdecimal():
            continue
        try:
            expanded, report = audit_extraction(base, path, packet_version, mode)
            pilot.save_json(output / "expanded" / path.name, expanded)
        except (ValueError, TypeError, KeyError) as error:
            report = {
                "source_ref_id": int(path.stem),
                "input_path": str(path.relative_to(base)),
                "errors": [f"conversion/access error: {error}"],
                "warnings": [],
                "anchors": [],
                "counts": {},
                "semantic_validity_established": False,
            }
        reports.append(report)
    if not reports:
        raise ValueError("No extraction files available for audit")
    pilot.save_json(output / "validation.json", reports)
    summary = [
        {
            key: value
            for key, value in report.items()
            if key
            not in {
                "anchors",
                "validator_fingerprint",
                "source_fingerprint",
                "extraction_fingerprint",
            }
        }
        for report in reports
    ]
    pilot.save_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(any(report["errors"] for report in reports))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--label", default="v4")
    build.add_argument("--selection-version", type=int, choices=(4, 5), default=4)
    expand = sub.add_parser("expand")
    expand.add_argument("--input", type=Path, required=True)
    expand.add_argument("--output", type=Path, required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--directory", type=Path, required=True)
    audit.add_argument("--packet-version", choices=("v4", "v5"), required=True)
    audit.add_argument("--mode", choices=("full", "compact"), required=True)
    audit.add_argument("--label", required=True)
    args = parser.parse_args()
    if args.command == "build":
        build_files(Path(__file__).resolve().parent, args.label, args.selection_version)
    elif args.command == "audit":
        return audit_files(
            Path(__file__).resolve().parent,
            args.directory,
            args.packet_version,
            args.mode,
            args.label,
        )
    else:
        result = expand_extraction(pilot.load_json(args.input))
        source = pilot.load_json(
            Path(__file__).resolve().parent
            / "sources"
            / f"{result['source_ref_id']}.json"
        )
        report = pilot.validate(source, result)
        pilot.save_json(args.output, result)
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "anchors"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return int(bool(report["errors"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
