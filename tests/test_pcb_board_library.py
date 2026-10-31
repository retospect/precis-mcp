"""Offline library references require exact geometry and a pinned cache row."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from precis.pcb.board_library import catalog_matches
from precis.pcb.board_script import classified_batch, emit
from precis.pcb.rows import normalize_footprint
from precis_geom.pcb import PcbBuilderError
from tests.workspaces._script import load_module


def _batch() -> dict[str, Any]:
    return {
        "components": [
            {
                "refdes": "J1",
                "part": "C123",
                "footprint": "Connector-2",
                "pins": [
                    {
                        "name": "1",
                        "etype": "power_in",
                        "meta": {
                            "max_current_a": 0.1,
                            "max_voltage_v": 5.0,
                            "assume": "gnd pin",
                        },
                    },
                    {"name": "2"},
                ],
            }
        ],
        "nets": [{"name": "GND"}],
        "connections": [{"refdes": "J1", "pin": "1", "net": "GND"}],
        "footprints": [
            {
                "name": "Connector-2",
                "pads": [
                    {
                        "pin": "1",
                        "shape": "rect",
                        "x": -1.0,
                        "y": 0.0,
                        "w": 1.5,
                        "h": 2.0,
                        "drill": 0.6,
                        "slot": {"w": 0.6, "h": 1.0},
                        "mask": "open",
                    },
                    {"pin": "2", "shape": "circle", "x": 1.0, "y": 0.0, "w": 1.5},
                ],
                "pin_map": {"1": "1", "2": "2"},
                "courtyard": [[-2, -2], [2, -2], [2, 2], [-2, 2]],
                "centroid": [0.0, 0.0],
                "holes": [{"x": 0.0, "y": 1.5, "dia_mm": 0.5}],
            }
        ],
    }


def _write_cache(tmp_path: Path, rows: dict[str, Any]) -> Path:
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"footprints": rows}), encoding="utf-8")
    return cache


def _emit_and_load(
    tmp_path: Path, batch: dict[str, Any], cache: Path
) -> tuple[str, Any]:
    text = emit(batch, "library", "fixture", catalog_cache=cache)
    script = tmp_path / "main.py"
    script.write_text(text, encoding="utf-8")
    return text, load_module(script)


def _assert_local_fallback(text: str) -> None:
    assert "pcb.catalog_footprint(" not in text
    assert "pcb.Footprint(" in text
    part_lines = [line for line in text.splitlines() if "b.part(" in line]
    assert part_lines
    assert all("synthesized footprint" in line for line in part_lines)


@pytest.mark.parametrize("row", [None, [], 1])
def test_malformed_catalog_row_keeps_source_pads(tmp_path: Path, row: Any) -> None:
    batch = _batch()
    cache = _write_cache(tmp_path, {"C123": row})
    text, module = _emit_and_load(tmp_path, batch, cache)
    _assert_local_fallback(text)
    assert module.build().to_batch() == classified_batch(batch)


def test_matching_normalized_catalog_row_replaces_inline_pads(tmp_path: Path) -> None:
    batch = _batch()
    original = deepcopy(batch)
    _, row = normalize_footprint(batch["footprints"][0])
    cache = _write_cache(tmp_path, {"C123": row})
    text, module = _emit_and_load(tmp_path, batch, cache)
    digest = hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert "pcb.catalog_footprint(" in text
    assert f"sha256={digest!r}" in text
    assert "pcb.pad(" not in text
    assert "_pads_" not in text
    assert "synthesized footprint" not in text
    assert "footprint='Connector-2'" in text
    rebuilt = module.build().to_batch()
    assert normalize_footprint(rebuilt["footprints"][0]) == normalize_footprint(
        batch["footprints"][0]
    )
    assert rebuilt["components"] == classified_batch(batch)["components"]
    assert rebuilt["nets"] == batch["nets"]
    assert rebuilt["connections"] == batch["connections"]
    assert batch == original


@pytest.mark.parametrize("mismatch", ["dimensions", "holes", "pin_map", "mask", "slot"])
def test_catalog_geometry_mismatch_keeps_source_pads(
    tmp_path: Path, mismatch: str
) -> None:
    batch = _batch()
    _, row = normalize_footprint(batch["footprints"][0])
    if mismatch == "dimensions":
        row["pads"][0]["w"] += 0.2
    elif mismatch == "holes":
        row["holes"][0]["dia_mm"] += 0.2
    elif mismatch == "pin_map":
        row["pin_map"]["1"]["name"] = "OTHER"
    elif mismatch == "mask":
        row["pads"][0]["mask"] = "covered"
    else:
        row["pads"][0]["slot"]["h"] += 0.2
    cache = _write_cache(tmp_path, {"C123": row})
    text, module = _emit_and_load(tmp_path, batch, cache)
    _assert_local_fallback(text)
    assert module.build().to_batch() == classified_batch(batch)


def test_missing_catalog_row_keeps_source_pads(tmp_path: Path) -> None:
    batch = _batch()
    cache = _write_cache(tmp_path, {})
    text, module = _emit_and_load(tmp_path, batch, cache)
    _assert_local_fallback(text)
    assert module.build().to_batch() == classified_batch(batch)


@pytest.mark.parametrize("invalid", ["source", "cache"])
def test_geometry_error_keeps_pads_and_reports_on_each_part(
    tmp_path: Path, invalid: str
) -> None:
    batch = _batch()
    batch["components"].append({**deepcopy(batch["components"][0]), "refdes": "J2"})
    _, row = normalize_footprint(batch["footprints"][0])
    broken = batch["footprints"][0] if invalid == "source" else row
    broken["pads"][0]["shape"] = "unsupported-shape"
    cache = _write_cache(tmp_path, {"C123": row})
    text, module = _emit_and_load(tmp_path, batch, cache)
    _assert_local_fallback(text)
    part_lines = [line for line in text.splitlines() if "b.part(" in line]
    assert len(part_lines) == 2
    context = "source footprint" if invalid == "source" else "catalog row 'C123'"
    for line in part_lines:
        assert f"catalog geometry error ({context}): ValueError:" in line
        assert "unsupported-shape" in line
    assert module.build().to_batch() == classified_batch(batch)


def test_shared_footprint_needs_catalog_match_for_every_part(tmp_path: Path) -> None:
    batch = _batch()
    batch["components"].append(
        {**deepcopy(batch["components"][0]), "refdes": "J2", "part": "C456"}
    )
    _, row = normalize_footprint(batch["footprints"][0])
    cache = _write_cache(tmp_path, {"C123": row})
    text, module = _emit_and_load(tmp_path, batch, cache)
    _assert_local_fallback(text)
    assert module.build().to_batch() == classified_batch(batch)


def test_cache_edit_after_emission_refuses_rebuild(tmp_path: Path) -> None:
    batch = _batch()
    _, row = normalize_footprint(batch["footprints"][0])
    cache = _write_cache(tmp_path, {"C123": row})
    text, module = _emit_and_load(tmp_path, batch, cache)
    assert "pcb.catalog_footprint(" in text
    row["pads"][0]["w"] += 0.2
    _write_cache(tmp_path, {"C123": row})
    with pytest.raises(PcbBuilderError, match="SHA256 mismatch"):
        module.build()


@pytest.mark.parametrize("field", ["x", "w"])
@pytest.mark.parametrize("delta", [0.000002, 0.00002])
def test_sub_storage_quantum_geometry_difference_refuses_match(
    tmp_path: Path, field: str, delta: float
) -> None:
    batch = _batch()
    _, row = normalize_footprint(batch["footprints"][0])
    row["pads"][0][field] += delta
    cache = _write_cache(tmp_path, {"C123": row})
    matches, _, errors = catalog_matches(batch, cache)
    assert matches == {}
    assert errors == {}
    text, module = _emit_and_load(tmp_path, batch, cache)
    _assert_local_fallback(text)
    assert module.build().to_batch()["footprints"] == batch["footprints"]
