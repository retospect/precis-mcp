from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
REPO = BASE.parent
NUMERICS_SPEC = importlib.util.spec_from_file_location(
    "precis_pilot_numerics", REPO / "src/precis/utils/numerics.py"
)
NUMERICS = importlib.util.module_from_spec(NUMERICS_SPEC)
NUMERICS_SPEC.loader.exec_module(NUMERICS)
NUMBER = re.compile(
    r"(?<![\w.])[-+−]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+−-]?\d+)?(?![\w.])"
)
CITATION = re.compile(r"\[\s*\d{1,4}(?:\s*[,–-]\s*\d{1,4})*\s*\]")
AUTHOR_YEAR = r"\((?:e\.g\.,?\s*)?[A-Z][\w'’-]+(?:\s+(?:et al\.?|and\s+[A-Z][\w'’-]+|&\s*[A-Z][\w'’-]+))?,?\s+(?:1[789]|20)\d{2}[a-z]?\)"
SUPERSCRIPT = r"<sup>\s*\d+(?:\s*[,;–—-]\s*\d+)*\s*(?:</sup>|$)"
CITATION_EXTENDED = re.compile("|".join((CITATION.pattern, AUTHOR_YEAR, SUPERSCRIPT)))
NUMBER_V3 = re.compile(
    r"(?<![\w.])[-+−]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][+−-]?\d+)?(?!\d|\.\d)"
)
PAREN_CITATION = r"\(\s*\d{1,4}(?:\s*[,–-]\s*\d{1,4})*\s*\)"
CITATION_V3 = re.compile("|".join((CITATION_EXTENDED.pattern, PAREN_CITATION)))
INTERNAL = re.compile(
    r"\b(Fig(?:ure)?s?\.?|Tables?|Eq(?:uation)?s?\.?)\s*\(?([Ss]?\d+[a-z]?)\)?", re.I
)
LABEL = re.compile(
    r"\b(?:sample|electrode|catalyst|membrane)\s+[A-Z][A-Za-z0-9_.()-]{0,24}\b"
)
OBJECT_TYPES = {
    "sample",
    "experiment",
    "protocol",
    "series",
    "model",
    "material",
    "figure",
    "table",
}
ORIGINS = {
    "reported_experiment",
    "reported_simulation",
    "reported_fit",
    "cited_other_work",
    "unspecified",
}
BOUNDS = {"exact", "approximate", "lower", "upper", "range", "unspecified"}
V2_ENUMS = {
    "evidence_mode": {
        "experimental",
        "computational",
        "analytical",
        "mixed",
        "not_established",
    },
    "value_generation": {"measurement", "fit", "calculation", "not_established"},
    "source_attribution": {"own_work", "cited_work", "not_established"},
    "value_form": {
        "point_estimate",
        "approximate_point",
        "interval",
        "lower_bound",
        "upper_bound",
        "not_established",
    },
    "normalization_status": {"explicit", "inferred", "unresolved", "not_applicable"},
    "measurand_status": {"explicit", "interpreted", "ambiguous"},
}
ROLES = {
    "description",
    "method",
    "qualifier",
    "result",
    "caption",
    "comparison",
    "model",
}
PREDICATES = {
    "uses-protocol",
    "qualifies",
    "reports-result-for",
    "measured-under",
    "refers-to",
    "compares",
    "derived-from",
    "same-source-object",
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def query_rows(query: str) -> list[dict[str, Any]]:
    sql = (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL statement_timeout='30s';\n"
        + query
        + "\nROLLBACK;"
    )
    try:
        result = subprocess.run(
            [str(REPO / "scripts/prod-psql"), sql],
            cwd=REPO,
            env={**os.environ, "PRECIS_PROD_PSQL_OPTS": "-qAt"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Read-only export timed out; no completed snapshot") from exc
    if result.returncode:
        errors = [
            line for line in result.stderr.splitlines() if line.startswith("ERROR:")
        ]
        known = next(
            (
                reason
                for reason in (
                    "No route to host",
                    "Permission denied",
                    "Connection refused",
                    "Host key verification failed",
                )
                if reason.lower() in result.stderr.lower()
            ),
            None,
        )
        raise RuntimeError(
            "; ".join(errors)
            or known
            or "Database read failed; connection details withheld"
        )
    return [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]


def prepare_source(raw: dict[str, Any]) -> dict[str, Any]:
    source = dict(raw)
    source["chunks"] = [
        dict(
            chunk, text_sha256=hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
        )
        for chunk in raw["chunks"]
    ]
    source["fingerprint"] = digest(source)
    source["exported_at"] = datetime.now(UTC).isoformat()
    source["schema_version"] = 1
    source["limitations"] = [
        "Snapshot of held text, not an audit of PDF completeness or OCR fidelity",
        "Stored page fields are unverified locators",
        "No supplementary documents or figures were fetched by this export",
    ]
    return source


def discover(limit: int) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100:
        raise ValueError("Discovery limit must be 1..100")
    return query_rows(f"""
SELECT json_build_object('ref_id', p.ref_id, 'title', p.title, 'year', p.year,
'doi', (SELECT id_value FROM ref_identifiers WHERE ref_id=p.ref_id AND id_kind='doi' LIMIT 1))
FROM refs p
WHERE p.kind='paper' AND p.retired_at IS NULL
AND EXISTS (SELECT 1 FROM chunks c WHERE c.ref_id=p.ref_id AND c.ord>=0 AND c.retired_at IS NULL)
AND EXISTS (
 SELECT 1 FROM links l JOIN ref_tags rt ON rt.ref_id=l.dst_ref_id
 JOIN tags t ON t.tag_id=rt.tag_id
 WHERE l.src_ref_id=p.ref_id AND l.relation IN ('corroborates','establishes')
 AND t.namespace='OPEN' AND t.value='campaign:norr-her-survey')
ORDER BY p.ref_id LIMIT {limit};
""")


def export_sources(ids: list[int]) -> list[dict[str, Any]]:
    if (
        not ids
        or len(set(ids)) != len(ids)
        or any(x <= 0 for x in ids)
        or len(ids) > 30
    ):
        raise ValueError("Supply 1..30 distinct positive source IDs")
    selected = ",".join(str(x) for x in ids)
    rows = query_rows(f"""
SELECT json_build_object('ref_id', r.ref_id, 'title', r.title, 'authors', r.authors,
'year', r.year, 'pdf_sha256', r.pdf_sha256, 'pdf_role', r.pdf_role,
'identifiers', COALESCE((SELECT json_agg(json_build_object('kind', i.id_kind, 'value', i.id_value)) FROM ref_identifiers i WHERE i.ref_id=r.ref_id AND i.id_kind IN ('doi','arxiv','cite_key','s2')), '[]'::json),
'chunks', COALESCE((SELECT json_agg(json_build_object('chunk_id', c.chunk_id, 'ord', c.ord, 'chunk_kind', c.chunk_kind, 'text', c.text, 'section_path', c.section_path, 'page_first', c.page_first, 'page_last', c.page_last, 'stored_content_sha', c.content_sha, 'numerics', c.numerics, 'keywords', c.keywords) ORDER BY c.ord, c.chunk_id) FROM chunks c WHERE c.ref_id=r.ref_id AND c.ord>=0 AND c.retired_at IS NULL), '[]'::json),
'bibliography', COALESCE((SELECT json_agg(to_jsonb(b) ORDER BY b.marker) FROM paper_bib_entries b WHERE b.ref_id=r.ref_id), '[]'::json),
'citations', COALESCE((SELECT json_agg(to_jsonb(cc)) FROM chunk_citations cc JOIN chunks c ON c.chunk_id=cc.chunk_id WHERE c.ref_id=r.ref_id AND c.ord>=0 AND c.retired_at IS NULL), '[]'::json))
FROM refs r WHERE r.ref_id IN ({selected}) AND r.kind='paper' AND r.retired_at IS NULL
ORDER BY r.ref_id;
""")
    if {row["ref_id"] for row in rows} != set(ids):
        raise ValueError("Not all requested source IDs resolved to live papers")
    return [prepare_source(row) for row in rows]


def reader_packet(source: dict[str, Any]) -> dict[str, Any]:
    packet = {
        key: value
        for key, value in source.items()
        if key not in {"chunks", "citations", "bibliography"}
    }
    packet["chunk_count"] = len(source["chunks"])
    packet["chunks"] = []
    for chunk in source["chunks"]:
        text = chunk["text"]
        packet["chunks"].append(
            {
                "chunk_id": chunk["chunk_id"],
                "ord": chunk["ord"],
                "kind": chunk["chunk_kind"],
                "section": chunk.get("section_path", []),
                "stored_page": chunk.get("page_first"),
                "text_sha256": chunk["text_sha256"],
                "text_segments": [text[i : i + 700] for i in range(0, len(text), 700)],
            }
        )
    return packet


def candidates(source: dict[str, Any], revision: int) -> dict[str, Any]:
    result = {
        "source_ref_id": source["ref_id"],
        "source_fingerprint": source["fingerprint"],
        "generator_revision": revision,
        "status": "unverified_proposals",
        "numbers": [],
        "citations": [],
        "internal_references": [],
        "local_labels": [],
        "limitations": [
            "Proximity is not applicability",
            "Numerical candidates are not scientific observations",
            "Internal-reference targets require semantic or layout verification",
        ],
    }
    entries = {str(entry["marker"]): entry for entry in source.get("bibliography", [])}
    targets = {}
    for chunk in source["chunks"]:
        if chunk["chunk_kind"] == "references":
            continue
        text = chunk["text"]
        for match in INTERNAL.finditer(text):
            kind = (
                "figure"
                if match[1].lower().startswith("fig")
                else "table"
                if match[1].lower().startswith("tab")
                else "equation"
            )
            key = (kind, match[2].lower())
            if match.start() <= 4:
                targets.setdefault(key, []).append(chunk["chunk_id"])
    for chunk in source["chunks"]:
        if chunk["chunk_kind"] == "references":
            continue
        text = chunk["text"]
        numeric_regex = (
            NUMERICS._NUMERIC_RE
            if revision == 1
            else NUMBER
            if revision == 2
            else NUMBER_V3
        )
        citation_regex = (
            CITATION
            if revision == 1
            else CITATION_EXTENDED
            if revision == 2
            else CITATION_V3
        )
        for field, regex in (
            ("numbers", numeric_regex),
            ("citations", citation_regex),
            ("internal_references", INTERNAL),
            ("local_labels", LABEL),
        ):
            for match in regex.finditer(text):
                item = {
                    "chunk_id": chunk["chunk_id"],
                    "start": match.start(),
                    "end": match.end(),
                    "literal": match[0],
                    "context": text[max(0, match.start() - 100) : match.end() + 150],
                }
                if field == "citations":
                    literal = match[0]
                    if literal.startswith("<sup>"):
                        previous = text[match.start() - 1] if match.start() else ""
                        following = text[match.end()] if match.end() < len(text) else ""
                        nums = re.findall(r"\d+", literal)
                        if (
                            previous.isdigit()
                            or following.isalpha()
                            or (
                                previous.isalpha()
                                and len(nums) == 1
                                and nums[0] in {"2", "3", "4"}
                            )
                        ):
                            continue
                    markers = []
                    if re.fullmatch(AUTHOR_YEAR, literal):
                        surname = re.search(r"\b[A-Z][\w'’-]+", literal)
                        year = re.search(r"\b(?:1[789]|20)\d{2}", literal)
                        for marker, entry in entries.items():
                            byline = str(
                                entry.get("authors") or entry.get("raw_text") or ""
                            )
                            if (
                                surname
                                and year
                                and str(entry.get("year")) == year[0]
                                and re.search(
                                    r"\b" + re.escape(surname[0]) + r"\b", byline, re.I
                                )
                            ):
                                markers.append(marker)
                        item["match_basis"] = "author_year_local_candidate_only"
                    else:
                        content = re.sub(r"</?sup>", "", literal).strip("[]() ")
                        for part in re.split(r"[,;]", content):
                            ends = re.split("[-–—]", part.strip())
                            if len(ends) == 2 and all(
                                x.strip().isdigit() for x in ends
                            ):
                                lo, hi = map(int, ends)
                                if 0 <= hi - lo <= 100:
                                    markers.extend(str(x) for x in range(lo, hi + 1))
                            elif part.strip().isdigit():
                                markers.append(str(int(part)))
                        item["match_basis"] = "numeric_marker_local_candidate_only"
                    item["bibliography_candidates"] = [
                        {
                            "marker": marker,
                            "entry_id": entries[marker].get("id"),
                            "doi": entries[marker].get("doi"),
                            "target_ref_id": entries[marker].get("held_ref_id"),
                        }
                        for marker in markers
                        if marker in entries
                    ]
                    item["resolution"] = (
                        "bibliography_match_not_support_verdict"
                        if item["bibliography_candidates"]
                        else "unresolved_or_non_citation"
                    )
                elif field == "internal_references":
                    kind = (
                        "figure"
                        if match[1].lower().startswith("fig")
                        else "table"
                        if match[1].lower().startswith("tab")
                        else "equation"
                    )
                    item["object_type"] = kind
                    item["label"] = match[2]
                    item["candidate_target_chunks"] = targets.get(
                        (kind, match[2].lower()), []
                    )
                    if revision >= 3:
                        item["target_match_basis"] = (
                            "label_at_start_heuristic_unverified"
                        )
                        if not item["candidate_target_chunks"]:
                            parent_label = re.sub(r"[a-z]$", "", match[2].lower())
                            if parent_label != match[2].lower():
                                item["candidate_target_chunks"] = targets.get(
                                    (kind, parent_label), []
                                )
                                item["target_match_basis"] = (
                                    "parent_label_only_unverified"
                                )
                result[field].append(item)
    return result


def literal_normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("−", "-").replace("–", "-")).strip()


SCIENTIFIC_NUMBER = re.compile(
    r"(?<![\w.])(?:[-+−]?\d+(?:\.\d+)?\s*(?:\\times|\\cdot|×|·)\s*)?"
    r"10\s*(?:\^\s*\{?\s*[-+−]?\d+\s*\}?|<sup>[-+−]?\d+</sup>|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)"
)
FRACTION_NUMBER = re.compile(r"(?<![\w.])[-+−]?\d+\s*/\s*\d+(?![\w.])")
GAP_TYPES = {
    "missing_supplement",
    "unresolved_context",
    "figure_only",
    "not_located_in_inspected_text",
    "ocr_uncertainty",
    "schema_gap",
}


def numeric_regions(text: str) -> list[tuple[int, int, str]]:
    scan = text.replace("−", "-").replace("–", "-")
    compound = sorted(
        (match.start(), match.end(), text[match.start() : match.end()])
        for regex in (SCIENTIFIC_NUMBER, FRACTION_NUMBER)
        for match in regex.finditer(scan)
    )
    atoms = [
        (match.start(), match.end(), text[match.start() : match.end()])
        for match in NUMBER_V3.finditer(scan)
        if not any(start <= match.start() < end for start, end, _ in compound)
    ]
    return sorted(compound + atoms)


def document_shape_errors(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["extraction must be a JSON object"]
    errors = []
    coverage = document.get("coverage")
    if not isinstance(coverage, dict) or not isinstance(
        coverage.get("inspected_chunk_ids"), list
    ):
        errors.append("coverage.inspected_chunk_ids must be a list")
    elif any(type(value) is not int for value in coverage["inspected_chunk_ids"]):
        errors.append("coverage chunk IDs must be integers")
    for collection in ("objects", "groups", "assertions", "results", "gaps"):
        records = document.get(collection)
        if not isinstance(records, list):
            errors.append(f"{collection} must be a list")
            continue
        for index, record in enumerate(records):
            path = f"{collection}[{index}]"
            if not isinstance(record, dict):
                errors.append(f"{path} must be an object")
                continue
            if collection != "gaps" and (
                not isinstance(record.get("id"), str) or not record["id"]
            ):
                errors.append(f"{path} needs a nonempty string id")
            if collection == "results":
                reserved = {
                    "source_ref_id",
                    "source_fingerprint",
                    "source_title",
                    "extraction_path",
                    "extraction_fingerprint",
                    "status",
                    "human_approved",
                } & record.keys()
                if reserved:
                    errors.append(
                        f"{path}: reserved provenance/trust fields {sorted(reserved)}"
                    )
                for field in ("subject_id", "measurand", "reported_value"):
                    if not isinstance(record.get(field), str):
                        errors.append(f"{path}.{field} must be a string")
                if record.get("context_id") is not None and not isinstance(
                    record["context_id"], str
                ):
                    errors.append(f"{path}.context_id must be a string or null")
                if not isinstance(record.get("conditions"), list):
                    errors.append(f"{path}.conditions must be a list")
                elif any(not isinstance(item, dict) for item in record["conditions"]):
                    errors.append(f"{path}.conditions must contain objects")
            if collection == "groups" and (
                not isinstance(record.get("members"), list)
                or any(not isinstance(item, dict) for item in record.get("members", []))
            ):
                errors.append(f"{path}.members must be a list of objects")
            if collection == "assertions" and any(
                not isinstance(record.get(field), str)
                for field in ("subject_id", "object_id", "predicate")
            ):
                errors.append(f"{path}: assertion endpoints/predicate must be strings")

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"evidence", "normalization_evidence"}:
                    if not isinstance(item, list):
                        errors.append(f"{path}.{key} must be a list")
                    else:
                        for anchor in item:
                            if (
                                not isinstance(anchor, dict)
                                or type(anchor.get("chunk_id")) is not int
                                or not isinstance(anchor.get("quote"), str)
                            ):
                                errors.append(f"{path}.{key}: malformed anchor")
                            elif "occurrence" in anchor and (
                                type(anchor["occurrence"]) is not int
                                or anchor["occurrence"] < 0
                            ):
                                errors.append(f"{path}.{key}: invalid occurrence")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(document, "document")
    return errors


def unit_surface(value: str) -> str:
    value = html.unescape(value).replace("μ", "µ").replace("−", "-")
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\\mu", "µ").replace("\\Omega", "Ω").replace("\\%", "%")
    value = re.sub(
        r"\\(?:text|mathrm|textrm|mathbf|operatorname|rm|left|right|tiny)\b", "", value
    )
    value = re.sub(r"\\[,;! ]", " ", value)
    value = value.translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+"))
    return re.sub(r"[$^_{}]", "", value)


def unit_has_text_support(unit: Any, quotes: str) -> bool:
    if unit is None:
        return True
    if not isinstance(unit, str) or not unit.strip():
        return False
    literal = re.sub(r"\s+", "", unit_surface(unit))
    pattern = (
        r"(?<![A-Za-zµΩ])"
        + r"\s*".join(re.escape(char) for char in literal)
        + r"(?![A-Za-zµΩ])"
    )
    return bool(re.search(pattern, unit_surface(quotes)))


def validate(source: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    errors, warnings, anchors = document_shape_errors(document), [], []
    if errors:
        return {
            "schema_version": document.get("schema_version")
            if isinstance(document, dict)
            else None,
            "source_ref_id": source["ref_id"],
            "source_fingerprint": source["fingerprint"],
            "extraction_fingerprint": digest(document),
            "assessment": "mechanical_checks_only",
            "semantic_validity_established": False,
            "errors": errors,
            "warnings": [],
            "anchors": [],
            "counts": {},
            "inspected_chunks": 0,
            "available_chunks": len(source["chunks"]),
        }
    chunks = {chunk["chunk_id"]: chunk for chunk in source["chunks"]}
    source_payload = {
        key: value
        for key, value in source.items()
        if key not in {"fingerprint", "exported_at", "schema_version", "limitations"}
    }
    if digest(source_payload) != source["fingerprint"]:
        errors.append("source snapshot content does not match its fingerprint")
    for chunk in source["chunks"]:
        if (
            hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
            != chunk["text_sha256"]
        ):
            errors.append(f"chunk {chunk['chunk_id']} content hash mismatch")
    version = document.get("schema_version")
    if version not in {1, 2}:
        errors.append("schema_version must be 1 or 2")
    if document.get("source_ref_id") != source["ref_id"]:
        errors.append("source_ref_id mismatch")
    if document.get("source_fingerprint") != source["fingerprint"]:
        errors.append("source_fingerprint mismatch")
    inspected = document.get("coverage", {}).get("inspected_chunk_ids", [])
    if not isinstance(inspected, list) or any(x not in chunks for x in inspected):
        errors.append("coverage contains unknown chunks or is not a list")
        inspected = []
    if not document.get("producer"):
        errors.append("producer is required")
    collections = {}
    ids = set()
    for name in ("objects", "groups", "assertions", "results", "gaps"):
        records = document.get(name)
        if not isinstance(records, list):
            errors.append(f"{name} must be a list")
            records = []
        collections[name] = records
        if name == "gaps":
            continue
        for index, record in enumerate(records):
            if not isinstance(record, dict) or not record.get("id"):
                errors.append(f"{name}[{index}] needs an id")
                continue
            if record["id"] in ids:
                errors.append(f"duplicate id: {record['id']}")
            ids.add(record["id"])
    object_ids = {x.get("id") for x in collections["objects"] if isinstance(x, dict)}
    context_ids = object_ids | {
        x.get("id") for x in collections["groups"] if isinstance(x, dict)
    }

    def bind(evidence: Any, location: str) -> str:
        texts = []
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{location}: nonempty evidence required")
            return ""
        for item in evidence:
            if not isinstance(item, dict):
                errors.append(f"{location}: malformed anchor")
                continue
            chunk = chunks.get(item.get("chunk_id"))
            quote = item.get("quote")
            if chunk is None or not isinstance(quote, str) or not quote:
                errors.append(f"{location}: unknown chunk or empty quote")
                continue
            if chunk["chunk_id"] not in inspected:
                errors.append(f"{location}: evidence chunk is not declared inspected")
            starts = [
                match.start() for match in re.finditer(re.escape(quote), chunk["text"])
            ]
            occurrence = item.get("occurrence")
            if not starts:
                errors.append(
                    f"{location}: quote not verbatim in chunk {chunk['chunk_id']}: {quote[:85]!r}"
                )
                continue
            if occurrence is None and len(starts) != 1:
                errors.append(f"{location}: repeated quote needs zero-based occurrence")
                continue
            occurrence = 0 if occurrence is None else occurrence
            if not isinstance(occurrence, int) or not 0 <= occurrence < len(starts):
                errors.append(f"{location}: invalid occurrence")
                continue
            start = starts[occurrence]
            anchors.append(
                {
                    "location": location,
                    "chunk_id": chunk["chunk_id"],
                    "text_sha256": chunk["text_sha256"],
                    "start": start,
                    "end": start + len(quote),
                    "quote": quote,
                }
            )
            texts.append(quote)
        return "\n".join(texts)

    def check_value(
        value: Any,
        evidence_text: str,
        location: str,
        value_anchors: list[dict],
        numeric_required: bool = False,
    ) -> None:
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{location}: nonempty reported literal required")
            return
        literal = literal_normalize(value)
        text = literal_normalize(evidence_text)
        expected = {literal_normalize(part) for _, _, part in numeric_regions(literal)}
        available = {
            literal_normalize(part)
            for anchor in value_anchors
            for start, end, part in numeric_regions(chunks[anchor["chunk_id"]]["text"])
            if anchor["start"] <= start and end <= anchor["end"]
        }
        if (
            literal not in text
            or not expected <= available
            or (numeric_required and not expected)
        ):
            errors.append(
                f"{location}: literal {value!r} is not supported as a complete source numeric expression"
            )

    for record in collections["objects"]:
        if record.get("type") not in OBJECT_TYPES:
            errors.append(f"{record.get('id')}: unknown object type")
        bind(record.get("evidence"), str(record.get("id")))
    for group in collections["groups"]:
        bind(group.get("evidence"), str(group.get("id")))
        for member in group.get("members", []):
            if member.get("chunk_id") not in chunks or member.get("role") not in ROLES:
                errors.append(f"{group.get('id')}: invalid member chunk or role")
            if member.get("chunk_id") not in inspected:
                errors.append(
                    f"{group.get('id')}: member chunk is not declared inspected"
                )
            if not any(
                anchor.get("chunk_id") == member.get("chunk_id")
                for anchor in member.get("evidence", [])
            ):
                errors.append(
                    f"{group.get('id')}: member needs evidence from the member chunk itself"
                )
            bind(member.get("evidence"), f"{group.get('id')}.member")
    endpoint_types = {record["id"]: record["type"] for record in collections["objects"]}
    endpoint_types.update({record["id"]: "group" for record in collections["groups"]})
    endpoint_types.update({record["id"]: "result" for record in collections["results"]})
    for assertion in collections["assertions"]:
        subject = assertion.get("subject_id")
        target = assertion.get("object_id")
        predicate = assertion.get("predicate")
        if subject not in endpoint_types or target not in endpoint_types:
            errors.append(
                f"{assertion.get('id')}: dangling or assertion-valued endpoint"
            )
        if subject == target:
            errors.append(f"{assertion.get('id')}: self-referential assertion")
        if predicate == "uses-protocol" and endpoint_types.get(target) != "protocol":
            errors.append(
                f"{assertion.get('id')}: uses-protocol target must be a protocol"
            )
        if predicate == "measured-under" and endpoint_types.get(subject) != "result":
            errors.append(
                f"{assertion.get('id')}: measured-under subject must be a result"
            )
        if predicate == "same-source-object" and (
            subject not in object_ids
            or target not in object_ids
            or endpoint_types.get(subject) != endpoint_types.get(target)
        ):
            errors.append(
                f"{assertion.get('id')}: identity endpoints must be objects of the same type"
            )
        if assertion.get("predicate") not in PREDICATES:
            errors.append(f"{assertion.get('id')}: unknown predicate")
        if assertion.get("basis") not in {"explicit", "inferred"}:
            errors.append(f"{assertion.get('id')}: explicit/inferred basis required")
        bind(assertion.get("evidence"), str(assertion.get("id")))
    for result in collections["results"]:
        name = str(result.get("id"))
        if result.get("subject_id") not in object_ids:
            errors.append(f"{name}: dangling subject")
        if (
            result.get("context_id") is not None
            and result.get("context_id") not in context_ids
        ):
            errors.append(f"{name}: dangling context")
        if version == 1:
            if result.get("origin") not in ORIGINS or result.get("bound") not in BOUNDS:
                errors.append(f"{name}: invalid origin or bound")
        else:
            for field, choices in V2_ENUMS.items():
                if result.get(field) not in choices:
                    errors.append(f"{name}: missing or invalid {field}")
            normalization = result.get("normalization_status")
            if normalization in {"explicit", "inferred"}:
                if not result.get("normalization_basis"):
                    errors.append(f"{name}: normalization basis required")
                bind(result.get("normalization_evidence"), f"{name}.normalization")
            elif not isinstance(result.get("normalization_evidence"), list):
                errors.append(f"{name}: normalization_evidence must be a list")
            for field, states in (
                ("normalization_status", {"inferred", "unresolved"}),
                ("measurand_status", {"interpreted", "ambiguous"}),
                ("source_attribution", {"cited_work", "not_established"}),
            ):
                if result.get(field) in states:
                    warnings.append(
                        f"{name}: {field}={result[field]} remains a review/comparability qualification"
                    )
        if not result.get("measurand"):
            errors.append(f"{name}: measurand required")
        for field in (
            "reported_unit",
            "normalization_basis",
            "uncertainty",
            "conditions",
            "limitations",
        ):
            if field not in result:
                errors.append(
                    f"{name}: explicit {field} field required (null allowed where unknown)"
                )
        anchor_start = len(anchors)
        evidence_text = bind(result.get("evidence"), name)
        check_value(
            result.get("reported_value"),
            evidence_text,
            name,
            anchors[anchor_start:],
            numeric_required=True,
        )
        unit_quotes = (
            evidence_text
            + "\n"
            + "\n".join(
                anchor["quote"] for anchor in result.get("normalization_evidence", [])
            )
        )
        if not unit_has_text_support(result.get("reported_unit"), unit_quotes):
            warnings.append(
                f"{name}: reported unit not grounded by the current text/markup matcher; verify its scope or conversion"
            )
        for index, condition in enumerate(result.get("conditions") or []):
            location = f"{name}.condition[{index}]"
            anchor_start = len(anchors)
            context_text = bind(condition.get("evidence"), location)
            check_value(
                condition.get("value"), context_text, location, anchors[anchor_start:]
            )
            if not unit_has_text_support(condition.get("unit"), context_text):
                warnings.append(
                    f"{location}: condition unit requires source/markup verification"
                )
            if condition.get("basis") not in {"explicit", "inferred"}:
                errors.append(f"{location}: explicit/inferred basis required")
            if condition.get("basis") == "inferred":
                warnings.append(
                    f"{location}: inferred applicability requires semantic review"
                )
    for gap in collections["gaps"]:
        if gap.get("type") not in GAP_TYPES or not isinstance(
            gap.get("description"), str
        ):
            errors.append("invalid gap type or description")
        if not isinstance(gap.get("chunk_ids"), list) or any(
            type(cid) is not int or cid not in chunks
            for cid in gap.get("chunk_ids", [])
        ):
            errors.append("gap references an unknown source chunk")
    if not collections["results"]:
        warnings.append(
            "no results extracted; this does not establish absence of results in the paper"
        )
    return {
        "schema_version": version,
        "source_ref_id": source["ref_id"],
        "source_fingerprint": source["fingerprint"],
        "extraction_fingerprint": digest(document),
        "validator_fingerprint": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "assessment": "mechanical_checks_only",
        "semantic_validity_established": False,
        "errors": errors,
        "warnings": warnings,
        "anchors": anchors,
        "counts": {name: len(records) for name, records in collections.items()},
        "inspected_chunks": len(set(inspected)),
        "available_chunks": len(chunks),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    discovery = sub.add_parser("discover")
    discovery.add_argument("--limit", type=int, default=60)
    discovery.add_argument("--out", default="sample_candidates.json")
    exporter = sub.add_parser("export")
    exporter.add_argument("--ids", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--revision", type=int, choices=(1, 2, 3), required=True)
    checker = sub.add_parser("validate")
    checker.add_argument("--round", required=True)
    checker.add_argument("--label")
    args = parser.parse_args()
    if args.command == "discover":
        rows = discover(args.limit)
        if Path(args.out).name != args.out:
            raise ValueError(
                "Discovery output must be a filename inside the pilot directory"
            )
        save_json(BASE / args.out, rows)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.command == "export":
        ids = [int(value) for value in args.ids.split(",")]
        rows = export_sources(ids)
        for source in rows:
            path = BASE / "sources" / f"{source['ref_id']}.json"
            if path.exists():
                if load_json(path)["fingerprint"] != source["fingerprint"]:
                    raise ValueError(
                        f"Source {source['ref_id']} changed; preserve this snapshot and choose a new run directory"
                    )
            else:
                save_json(path, source)
            packet = BASE / "packets" / path.name
            if not packet.exists():
                save_json(packet, reader_packet(source))
        print(
            json.dumps(
                {
                    "exported": [
                        {
                            "ref_id": x["ref_id"],
                            "title": x["title"],
                            "chunks": len(x["chunks"]),
                        }
                        for x in rows
                    ]
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "prepare":
        counts = []
        for path in sorted((BASE / "sources").glob("*.json")):
            source = load_json(path)
            target = BASE / "candidates" / f"v{args.revision}" / path.name
            if target.exists():
                result = load_json(target)
                if result["source_fingerprint"] != source["fingerprint"]:
                    raise ValueError("Candidate snapshot refers to a different source")
            else:
                result = candidates(source, args.revision)
                save_json(target, result)
            counts.append(
                {
                    "ref_id": source["ref_id"],
                    **{
                        name: len(result[name])
                        for name in (
                            "numbers",
                            "citations",
                            "internal_references",
                            "local_labels",
                        )
                    },
                }
            )
        print(json.dumps(counts, indent=2))
    else:
        reports = []
        for path in sorted((BASE / args.round).glob("[0-9]*.json")):
            source = load_json(BASE / "sources" / path.name)
            reports.append(validate(source, load_json(path)))
        if not reports:
            raise ValueError("No extraction files found")
        label = args.label or f"{args.round}-validation"
        save_json(BASE / "reports" / f"{label}.json", reports)
        print(
            json.dumps(
                [
                    {
                        key: value
                        for key, value in report.items()
                        if key
                        not in {
                            "anchors",
                            "source_fingerprint",
                            "extraction_fingerprint",
                        }
                    }
                    for report in reports
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return int(any(report["errors"] for report in reports))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, FileExistsError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
