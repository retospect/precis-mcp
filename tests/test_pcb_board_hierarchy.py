"""Explicit schematic rooms group code without changing board identities."""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from precis.pcb.board_script import emit
from tests.workspaces._script import load_module


def _batch() -> dict[str, Any]:
    components = []
    for refdes, room in (
        ("A1", {"id": "sheet-a", "name": "Analog"}),
        ("B1", {"id": "sheet-b", "name": "Power"}),
        ("J1", None),
        ("A2", {"id": "sheet-a", "name": "Analog"}),
        ("B2", {"id": "sheet-b", "name": "Power"}),
    ):
        component: dict[str, Any] = {
            "refdes": refdes,
            "pins": [
                {
                    "name": "1",
                    "etype": "bidi",
                    "meta": {"fmax_hz": 0.0, "assume": "signal pin"},
                },
                {
                    "name": "2",
                    "etype": "power_in",
                    "meta": {
                        "max_current_a": 0.1,
                        "max_voltage_v": 5.0,
                        "assume": "gnd pin",
                    },
                },
                {
                    "name": "3",
                    "etype": "bidi",
                    "meta": {"fmax_hz": 0.0, "assume": "signal pin"},
                },
            ],
            "part_meta": {"source_document": "fixture"},
            "note": f"source part {refdes}",
        }
        if room is not None:
            component["part_meta"]["source_sheet"] = room
        components.append(component)
    return {
        "components": components,
        "nets": [
            {"name": "ANALOG_SIGNAL", "note": "internal analog connection"},
            {"name": "POWER_SIGNAL", "class": "power"},
            {"name": "GND", "class": "ground"},
            {"name": "MIXED"},
            {"name": "ORPHAN"},
        ],
        "connections": [
            {"net": "ANALOG_SIGNAL", "refdes": "A1", "pin": "1"},
            {"net": "POWER_SIGNAL", "refdes": "B1", "pin": "1"},
            {"net": "ANALOG_SIGNAL", "refdes": "A2", "pin": "1"},
            {"net": "POWER_SIGNAL", "refdes": "B2", "pin": "1"},
            *[
                {"net": "GND", "refdes": refdes, "pin": "2"}
                for refdes in ("A1", "A2", "B1", "B2", "J1")
            ],
            {"net": "MIXED", "refdes": "A1", "pin": "3"},
            {"net": "MIXED", "refdes": "J1", "pin": "3"},
        ],
        "features": [{"ftype": "outline", "geom": {"rect": [0, 0, 20, 20]}}],
        "planes": [{"net": "GND", "layer": "B.Cu"}],
        "meta": {"source": "schematic-fixture"},
    }


def _assert_same_payload(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        if isinstance(value, list):
            assert {json.dumps(row, sort_keys=True) for row in actual[key]} == {
                json.dumps(row, sort_keys=True) for row in value
            }, key
        else:
            assert actual[key] == value, key


def _creation_sites(
    text: str, kind: str
) -> dict[str, tuple[ast.FunctionDef, ast.Call]]:
    sites = {}
    for function in ast.parse(text).body:
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == kind
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                name = node.args[0].value
                assert name not in sites, f"duplicate {kind} creation: {name}"
                sites[name] = (function, node)
    return sites


def _assert_room_structure(text: str) -> None:
    parts = _creation_sites(text, "part")
    nets = _creation_sites(text, "net")
    analog = parts["A1"][0]
    power = parts["B1"][0]
    assert analog.name != power.name
    assert analog.name != "build"
    assert power.name != "build"
    assert parts["A2"][0].name == analog.name
    assert parts["B2"][0].name == power.name
    assert nets["ANALOG_SIGNAL"][0].name == analog.name
    assert nets["POWER_SIGNAL"][0].name == power.name
    assert parts["J1"][0].name == "build"
    for name in ("GND", "MIXED", "ORPHAN"):
        assert nets[name][0].name == "build"
    assert not analog.decorator_list
    assert not power.decorator_list
    assert "@pcb.module" not in text
    assert "b.use(" not in text


def _rebuild(tmp_path: Path, text: str) -> dict[str, Any]:
    script = tmp_path / "main.py"
    script.write_text(text, encoding="utf-8")
    return load_module(script).build().to_batch()


def test_source_sheets_group_internal_nets_and_parts_without_renaming(
    tmp_path: Path,
) -> None:
    batch = _batch()
    original = deepcopy(batch)
    text = emit(batch, "hierarchy", "source schematic")
    _assert_room_structure(text)
    _assert_same_payload(_rebuild(tmp_path, text), batch)
    assert "Hierarchy:" in text
    assert "grouped" in text
    assert batch == original


def test_same_sheet_label_does_not_merge_distinct_sheet_ids(tmp_path: Path) -> None:
    batch = _batch()
    for component in batch["components"]:
        sheet = component["part_meta"].get("source_sheet")
        if sheet is not None:
            sheet["name"] = "Repeated label"
    text = emit(batch, "hierarchy", "source schematic")
    _assert_room_structure(text)
    _assert_same_payload(_rebuild(tmp_path, text), batch)


@pytest.mark.parametrize("block_shape", ["string", "dict"])
def test_source_blocks_group_parts_and_internal_nets(
    tmp_path: Path, block_shape: str
) -> None:
    batch = _batch()
    for component in batch["components"]:
        metadata = component["part_meta"]
        sheet = metadata.pop("source_sheet", None)
        if sheet is not None:
            metadata["source_block"] = sheet["id"] if block_shape == "string" else sheet
    text = emit(batch, "hierarchy", "source schematic")
    _assert_room_structure(text)
    _assert_same_payload(_rebuild(tmp_path, text), batch)


def test_flat_source_stays_flat_and_explains_missing_hierarchy(tmp_path: Path) -> None:
    batch = _batch()
    for component in batch["components"]:
        component["part_meta"].pop("source_sheet", None)
    text = emit(batch, "flat", "source schematic")
    assert "Hierarchy: flat; source carries no schematic sheets or blocks." in text
    for kind in ("part", "net"):
        assert all(
            function.name == "build"
            for function, _ in _creation_sites(text, kind).values()
        )
    _assert_same_payload(_rebuild(tmp_path, text), batch)


def test_grouped_parts_keep_provenance_and_locked_positions(tmp_path: Path) -> None:
    batch = _batch()
    for index, component in enumerate(batch["components"]):
        component.update(x=float(index), y=2.0, rot=90.0, layer="bottom", fixed="rot")
    original = deepcopy(batch)
    annotations: dict[str, Any] = {
        "board": "pb123",
        "instance_ids": {
            component["refdes"]: 100 + index
            for index, component in enumerate(batch["components"])
        },
    }
    text = emit(
        batch,
        "hierarchy",
        "source schematic",
        annotations=annotations,
        placement="keep",
    )
    _assert_room_structure(text)
    for refdes, (function, call) in _creation_sites(text, "part").items():
        assert function.name == "build" or not function.decorator_list
        line = text.splitlines()[call.lineno - 1]
        assert "#" in line
        assert "pb123" in line
        assert f"instance_id={annotations['instance_ids'][refdes]}" in line
    expected = deepcopy(batch)
    for component in expected["components"]:
        component["fixed"] = "both"
    _assert_same_payload(_rebuild(tmp_path, text), expected)
    assert batch == original
