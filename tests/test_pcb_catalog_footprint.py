"""Pinned local catalog geometry for readable workspace board scripts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from precis_geom import pcb


def _row() -> dict[str, Any]:
    return {
        "pads": [
            {
                "number": "1",
                "shape": "POLYGON",
                "x": 1.25,
                "y": -0.5,
                "rot": 90.0,
                "layer": "F.Cu",
                "drill": 0.4,
                "slot": {"w": 0.8, "h": 0.4},
                "poly": [[0.0, 0.0], [1.0, 0.0], [0.5, 1.0]],
                "paste": None,
            },
            {
                "number": "2",
                "pin": "EP",
                "shape": "RECT",
                "x": 0.0,
                "y": 0.0,
                "w": 1.0,
                "h": 1.0,
            },
        ],
        "pin_map": {"1": "catalog pin"},
        "note": "catalog metadata is not source metadata",
    }


def _digest(row: dict[str, Any]) -> str:
    return hashlib.sha256(pcb.canonical_json(row).encode()).hexdigest()


@pytest.mark.parametrize("envelope", [False, True])
def test_pinned_catalog_pads_preserve_geometry(
    tmp_path: Path, monkeypatch, envelope: bool
) -> None:
    def refuse_catalog_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("pinned local footprint must not query the catalog")

    monkeypatch.setattr(pcb, "_catalog_read", refuse_catalog_read)
    row = _row()
    doc: dict[str, Any] = {"C123": row}
    if envelope:
        doc = {"footprints": doc, "missing": []}
    cache = tmp_path / "footprints.json"
    original = json.dumps(doc)
    cache.write_text(original, encoding="utf-8")

    footprint = pcb.catalog_footprint(
        "local-package", "C123", cache, sha256=_digest(row)
    )

    expected = deepcopy(row["pads"])
    expected[0].pop("number")
    expected[0].pop("paste")
    expected[0].update(pin="1", shape="polygon")
    expected[1].pop("number")
    expected[1]["shape"] = "rect"
    assert footprint.to_dict() == {"name": "local-package", "pads": expected}
    assert cache.read_text(encoding="utf-8") == original
    footprint.pads[0]["poly"][0][0] = 42.0
    assert row["pads"][0]["poly"][0][0] == 0.0
    assert cache.read_text(encoding="utf-8") == original


def test_catalog_footprint_keeps_explicit_source_metadata(tmp_path: Path) -> None:
    row = _row()
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"C123": row}), encoding="utf-8")
    fields = {
        "pin_map": {"1": "source pin"},
        "courtyard": {"bbox": [-2.0, -2.0, 2.0, 2.0]},
        "centroid": {"x": 0.5, "y": -0.5},
        "note": "source note",
        "holes": [{"x": 0.0, "y": 0.0, "dia_mm": 1.0}],
        "graphics": [{"kind": "line", "layer": "F.SilkS", "x1": 1.0}],
    }
    original = deepcopy(fields)

    footprint = pcb.catalog_footprint(
        "local-package", "C123", str(cache), sha256=_digest(row), **fields
    )

    output = footprint.to_dict()
    assert {key: output[key] for key in fields} == original
    assert fields == original
    footprint.courtyard["bbox"][0] = -100.0
    assert fields == original


def test_catalog_footprint_refuses_missing_cache(tmp_path: Path) -> None:
    with pytest.raises(pcb.PcbBuilderError, match="unreadable cache"):
        pcb.catalog_footprint(
            "local-package", "C123", tmp_path / "missing.json", sha256="0" * 64
        )


def test_catalog_footprint_refuses_missing_row(tmp_path: Path) -> None:
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"C456": _row()}), encoding="utf-8")
    with pytest.raises(pcb.PcbBuilderError, match="missing footprint row"):
        pcb.catalog_footprint("local-package", "C123", cache, sha256=_digest(_row()))


@pytest.mark.parametrize("changed", ["geometry", "metadata"])
def test_catalog_footprint_refuses_changed_raw_row(
    tmp_path: Path, changed: str
) -> None:
    row = _row()
    expected_sha = _digest(row)
    if changed == "geometry":
        row["pads"][0]["x"] += 0.001
    else:
        row["note"] = "changed catalog metadata"
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"C123": row}), encoding="utf-8")
    with pytest.raises(pcb.PcbBuilderError, match="SHA256 mismatch"):
        pcb.catalog_footprint("local-package", "C123", cache, sha256=expected_sha)


@pytest.mark.parametrize("document", ["{", "[]", '{"footprints": []}'])
def test_catalog_footprint_refuses_malformed_cache(
    tmp_path: Path, document: str
) -> None:
    cache = tmp_path / "footprints.json"
    cache.write_text(document, encoding="utf-8")
    with pytest.raises(pcb.PcbBuilderError):
        pcb.catalog_footprint("local-package", "C123", cache, sha256="0" * 64)


@pytest.mark.parametrize("pads", [None, {}, [None], [{"shape": 2}]])
def test_catalog_footprint_refuses_malformed_pads(tmp_path: Path, pads: Any) -> None:
    row = {"pads": pads}
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"C123": row}), encoding="utf-8")
    with pytest.raises(pcb.PcbBuilderError):
        pcb.catalog_footprint("local-package", "C123", cache, sha256=_digest(row))


@pytest.mark.parametrize("envelope", [False, True])
def test_catalog_part_keeps_component_fields_and_raw_geometry(
    tmp_path: Path, monkeypatch, envelope: bool
) -> None:
    def refuse_catalog_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("pinned catalog part must not query the catalog")

    monkeypatch.setattr(pcb, "_catalog_read", refuse_catalog_read)
    row = _row()
    row.update(
        package="catalog-package-not-authored-on-component",
        height_mm=2.0,
        courtyard={"bbox": [-2.0, -2.0, 2.0, 2.0]},
        centroid={"x": 0.5, "y": -0.5},
        holes=[{"x": 0.0, "y": 0.0, "dia_mm": 1.0}],
        part_id="pn123",
    )
    cache = tmp_path / "footprints.json"
    doc = {"footprints": {"C123": row}} if envelope else {"C123": row}
    original = json.dumps(doc)
    cache.write_text(original, encoding="utf-8")

    reference = pcb.catalog_part("C123", cache, sha256=_digest(row))
    board = pcb.Board("pinned")
    authored_pins = [{"name": "authored pin", "pad": "1"}]
    part = board.part("U1", part=reference, pins=authored_pins)

    assert board.to_batch()["components"] == [
        {"refdes": "U1", "part": "C123", "pins": authored_pins}
    ]
    assert part._catalog is reference
    assert reference.resolved
    assert reference.package is None
    assert reference.height_mm is None
    assert reference.part_id == "pn123"
    assert reference.footprint_sha == _digest(row)
    for field in ("pads", "pin_map", "courtyard", "centroid", "holes"):
        assert getattr(reference, field) == row[field]
    assert reference.pins == ["catalog pin"]
    reference.pads[0]["poly"][0][0] = 100.0
    reference.courtyard["bbox"][0] = -100.0
    again = pcb.catalog_part("C123", cache, sha256=_digest(row))
    assert again.pads == row["pads"]
    assert again.courtyard == row["courtyard"]
    assert cache.read_text(encoding="utf-8") == original


def test_catalog_part_falls_back_to_pad_names_without_pin_map(tmp_path: Path) -> None:
    row = _row()
    row.pop("pin_map")
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"C123": row}), encoding="utf-8")
    reference = pcb.catalog_part("C123", str(cache), sha256=_digest(row))
    assert reference.pins == ["1", "EP"]
    board = pcb.Board("pinned")
    board.part("U1", part=reference)
    assert board.to_batch()["components"] == [
        {"refdes": "U1", "part": "C123", "pins": []}
    ]


def test_catalog_part_refuses_changed_cache(tmp_path: Path) -> None:
    row = _row()
    expected_sha = _digest(row)
    row["pads"][0]["x"] += 0.001
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"C123": row}), encoding="utf-8")
    with pytest.raises(pcb.PcbBuilderError, match="SHA256 mismatch"):
        pcb.catalog_part("C123", cache, sha256=expected_sha)


def test_catalog_part_refuses_missing_cache(tmp_path: Path) -> None:
    with pytest.raises(pcb.PcbBuilderError, match="unreadable cache"):
        pcb.catalog_part("C123", tmp_path / "missing.json", sha256="0" * 64)


def _pinned_board(tmp_path: Path) -> tuple[pcb.Board, dict[str, Any]]:
    row = _row()
    row["pin_map"] = {"1": "SIG", "2": "GND"}
    cache = tmp_path / "nondefault-pinned-catalog.json"
    cache.write_text(json.dumps({"C123": row}), encoding="utf-8")
    reference = pcb.catalog_part("C123", cache, sha256=_digest(row))
    board = pcb.Board("pinned", outline=pcb.rect(20.0, 20.0))
    part = board.part("U1", part=reference, pins=["SIG", "GND"]).at(5.0, 5.0)
    part["SIG"].etype("bidi", fmax_hz=0.0, assume="sig pin")
    part["GND"].etype(
        "power_in", max_current_a=0.1, max_voltage_v=5.0, assume="gnd pin"
    )
    board.net("SIG").tie(part["SIG"])
    board.net("GND").tie(part["GND"])
    return board, row


def test_local_design_keeps_bound_geometry_without_default_cache(
    tmp_path: Path, monkeypatch
) -> None:
    from precis.pcb import local

    board, row = _pinned_board(tmp_path)
    monkeypatch.setattr(local, "_footprint_cache_path", lambda: None)
    # Embedded metadata is lower precedence than the verified bound reference.
    stale = deepcopy(row)
    stale["pads"][0]["x"] = 999.0
    board.meta["catalog_footprints"] = {"C123": stale}
    design = local._design(board, None)
    assert design.by_lcsc["C123"]["pads"] == row["pads"]
    assert design.by_lcsc["C123"]["pin_map"] == row["pin_map"]
    assert (
        local._uncached_lcscs(design.graph, design.by_lcsc, design.local_by_name) == []
    )


def test_local_design_explicit_catalog_still_overrides_bound_geometry(
    tmp_path: Path, monkeypatch
) -> None:
    from precis.pcb import local

    board, row = _pinned_board(tmp_path)
    monkeypatch.setattr(local, "_footprint_cache_path", lambda: None)
    override = deepcopy(row)
    override["pads"][0]["x"] = 2.5
    for source in (board, board.to_batch()):
        design = local._design(source, {"C123": override})
        assert design.by_lcsc["C123"]["pads"] == override["pads"]
    assert board.catalog_footprints()["C123"]["pads"] == row["pads"]


def test_board_catalog_footprints_returns_independent_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    def refuse_catalog_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("catalog snapshot must not fetch unresolved references")

    board, row = _pinned_board(tmp_path)
    board.part("U2", part=pcb.CatalogPart("C456"))
    monkeypatch.setattr(pcb, "_catalog_read", refuse_catalog_read)
    snapshot = board.catalog_footprints()
    assert set(snapshot) == {"C123"}
    assert set(snapshot["C123"]) == {
        "pads",
        "pin_map",
        "courtyard",
        "centroid",
        "holes",
    }
    snapshot["C123"]["pads"][0]["poly"][0][0] = 100.0
    snapshot["C123"]["pin_map"]["1"] = "changed"
    fresh = board.catalog_footprints()["C123"]
    assert fresh["pads"] == row["pads"]
    assert fresh["pin_map"] == row["pin_map"]


def test_board_catalog_footprints_refuses_conflicting_bound_rows() -> None:
    first = pcb.CatalogPart("C123", resolved=True, pads=_row()["pads"])
    second = deepcopy(first)
    board = pcb.Board("conflict")
    board.part("U1", part=first)
    board.part("U2", part=second)
    assert board.catalog_footprints()["C123"]["pads"] == first.pads
    second.pads[0]["x"] = 999.0
    with pytest.raises(pcb.PcbBuilderError, match="conflicting catalog geometry"):
        board.catalog_footprints()
