"""Offline catalog equivalence for the board-script writer.

Compare geometry, pin maps and mechanical metadata rather than package names.
The row normalizer supplies aliases/defaults; raw coordinates are restored
before comparison because its storage quantization is coarser than a script
round trip. Catalog rows are pinned so a later cache edit cannot change pads.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from precis.pcb.rows import normalize_footprint, normalize_pad


def _canonical(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def _geometry(row: dict[str, Any]) -> dict[str, Any]:
    fields = {
        k: row[k]
        for k in ("pads", "pin_map", "courtyard", "centroid", "holes", "graphics")
        if row.get(k) is not None
    }
    _, normalized = normalize_footprint({"name": "compare", **fields})
    pads = []
    for raw in row.get("pads") or []:
        pad = normalize_pad(raw)
        # Preserve sub-storage-quantum geometry, including polygon vertices.
        for key in ("x", "y", "w", "h", "poly", "drill", "slot"):
            if raw.get(key) is not None:
                pad[key] = raw[key]
        if raw.get("h") is None and raw.get("w") is not None:
            pad["h"] = raw["w"]
        pads.append(_canonical(pad))
    normalized["pads"] = sorted(pads, key=lambda p: json.dumps(p, sort_keys=True))
    normalized.pop("note", None)
    return _canonical(normalized)


def catalog_matches(
    batch: dict[str, Any], cache: Path
) -> tuple[dict[str, tuple[str, str]], dict[str, Any], dict[str, str]]:
    """Return matches, the local cache, and geometry errors by footprint name.

    An invalid geometry cannot justify substitution; callers retain its source
    pads and report the error on each affected part.
    """
    document = json.loads(cache.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("catalog cache must be a JSON object")
    catalog = document.get("footprints", document)
    if not isinstance(catalog, dict):
        raise ValueError("catalog cache must be an LCSC-keyed object")
    matches = {}
    errors = {}
    for fp in batch.get("footprints") or []:
        users = [c for c in batch["components"] if c.get("footprint") == fp["name"]]
        keys = [c.get("part") or c.get("part_lcsc") for c in users]
        if not keys or any(
            not key or not isinstance(catalog.get(key), dict) for key in keys
        ):
            continue
        context = "source footprint"
        try:
            geometry = _geometry(fp)
            candidates = []
            for key in keys:
                context = f"catalog row {key!r}"
                candidates.append(_geometry(catalog[key]))
        except (ValueError, TypeError, KeyError) as exc:
            # An uncomparable cache row cannot justify deleting source pads.
            errors[fp["name"]] = (
                f"catalog geometry error ({context}): {type(exc).__name__}: {exc}"
            )
            continue
        if any(candidate != geometry for candidate in candidates):
            continue
        key = keys[0]
        matches[fp["name"]] = (key, catalog_digest(catalog[key]))
    return matches, catalog, errors


def catalog_digest(row: dict[str, Any]) -> str:
    """Pin the exact offline row, including its non-geometry metadata."""
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()
