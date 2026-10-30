"""Offline nano board round trips preserve cached/local pads and locked poses.

The fixture's catalog is explicitly hand-authored stand-in geometry, not a
vendor footprint fetch. Its meta.catalog_footprints strings are provenance
notes; geometry comes from the separate CATALOG_FOOTPRINTS rows.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

import precis_geom.pcb as pcb
from precis.pcb.board_script import emit
from precis.pcb.padplace import is_bottom_instance, oblique_world_rot, place_pad_point
from precis.pcb.rows import normalize_pad
from tests.workspaces._script import load_module

_FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "nano_usb_pd_testboard.py"


def _canonical(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def _row(row: dict[str, Any]) -> str:
    return json.dumps(_canonical(row), sort_keys=True)


def _complete_pad(raw: dict[str, Any]) -> dict[str, Any]:
    # Normalize aliases/defaults, retain every extra field, and restore source
    # geometry so storage's coarser quantization cannot hide writer drift.
    normalized = {key: value for key, value in raw.items() if value is not None}
    normalized.pop("pin", None)
    normalized.pop("rot_deg", None)
    normalized.update(normalize_pad(raw))
    for key in ("x", "y", "w", "h", "poly", "drill", "slot"):
        if raw.get(key) is not None:
            normalized[key] = raw[key]
    if raw.get("h") is None and raw.get("w") is not None:
        normalized["h"] = raw["w"]
    return normalized


def _resolved_pads(
    batch: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    local = {footprint["name"]: footprint for footprint in batch.get("footprints", [])}
    resolved = {}
    for component in batch["components"]:
        footprint = local.get(component.get("footprint"))
        if footprint is None:
            footprint = catalog.get(component.get("part") or component.get("part_lcsc"))
        resolved[component["refdes"]] = [
            _complete_pad(pad) for pad in (footprint or {}).get("pads", [])
        ]
    return resolved


def _world_pads(
    batch: dict[str, Any], pads: dict[str, list[dict[str, Any]]]
) -> dict[str, list[str]]:
    placed = {}
    for component in batch["components"]:
        assert component["fixed"] == "both"
        rows = []
        for pad in pads[component["refdes"]]:
            x, y = place_pad_point(pad, component)
            rows.append(
                _row(
                    {
                        **pad,
                        "x": x,
                        "y": y,
                        "world_rotation": oblique_world_rot(
                            component["rot"], pad["rot"], is_bottom_instance(component)
                        ),
                        "instance_side": component["layer"],
                    }
                )
            )
        placed[component["refdes"]] = sorted(rows)
    return placed


@pytest.mark.parametrize("placement", ["keep", "omit"])
def test_nano_board_script_roundtrip_with_offline_catalog(
    tmp_path: Path, monkeypatch, placement: str
) -> None:
    def refuse_catalog_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail(
            "nano round trip must use only the supplied offline footprint cache"
        )

    monkeypatch.setattr(pcb, "_catalog_read", refuse_catalog_read)
    fixture = load_module(_FIXTURE, name="nano_fixture")
    catalog = deepcopy(fixture.CATALOG_FOOTPRINTS)
    fixture_batch = fixture.build_design()
    assert all(
        isinstance(value, str)
        for value in fixture_batch["meta"]["catalog_footprints"].values()
    )
    initial = tmp_path / "source.py"
    initial.write_text(emit(fixture_batch, "nano", str(_FIXTURE)), encoding="utf-8")
    source_board = load_module(initial, name="nano_source").build()

    # The builder has no public part lookup; pose each existing Part through
    # its public at() method, including non-axis-aligned bottom-side poses.
    for index, part in enumerate(source_board._parts.values()):
        part.at(
            5.25 + index * 1.5,
            7.125 + index * 0.75,
            rot=(index * 37.0 + 13.0) % 360.0,
            side="bottom" if index % 2 else "top",
            fixed="both",
        )

    # Materialize a matching source-local footprint so emission must replace
    # real inline pads with a pinned library reference, not merely pass lcsc=.
    matching = next(
        part for part in source_board._parts.values() if part.lcsc in catalog
    )
    cached = catalog[matching.lcsc]
    matching_name = "nano-cached-source-footprint"
    source_board.footprint(
        pcb.Footprint(
            matching_name,
            deepcopy(cached["pads"]),
            pin_map=deepcopy(cached["pin_map"]),
        )
    )
    matching.footprint = matching_name
    source = source_board.to_batch()
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps(catalog), encoding="utf-8")
    text = emit(
        source, "nano-rebuilt", "source.py", placement=placement, catalog_cache=cache
    )
    assert "pcb.catalog_footprint(" in text
    assert "pcb.catalog_part(" in text
    assert matching_name in text
    output = tmp_path / "rebuilt.py"
    output.write_text(text, encoding="utf-8")
    rebuilt_board = load_module(output, name="nano_rebuilt").build()
    rebuilt = rebuilt_board.to_batch()

    local_names = {footprint["name"] for footprint in source["footprints"]}
    cached_only = [
        component
        for component in source["components"]
        if component.get("part") in catalog
        and component.get("footprint") not in local_names
    ]
    assert cached_only
    bound_catalog = {}
    for component in cached_only:
        bound = rebuilt_board._parts[component["refdes"]]._catalog
        assert bound is not None and bound.resolved
        assert sorted(_row(_complete_pad(pad)) for pad in bound.pads) == sorted(
            _row(_complete_pad(pad)) for pad in catalog[component["part"]]["pads"]
        )
        bound_catalog[component["part"]] = {"pads": bound.pads}

    expected = deepcopy(source)
    if placement == "omit":
        for component in expected["components"]:
            for key in (
                "x",
                "y",
                "rot",
                "layer",
                "fixed",
                "max_travel_mm",
                "group_offset",
            ):
                component.pop(key, None)
    for key in ("components", "nets", "connections"):
        assert {_row(row) for row in rebuilt[key]} == {
            _row(row) for row in expected[key]
        }
    before_pads = _resolved_pads(source, catalog)
    after_pads = _resolved_pads(rebuilt, bound_catalog)
    assert sum(len(pads) for pads in before_pads.values()) == 226
    # Sorted lists retain repeated physical lands; a set would hide pad loss.
    assert {name: sorted(map(_row, pads)) for name, pads in after_pads.items()} == {
        name: sorted(map(_row, pads)) for name, pads in before_pads.items()
    }
    if placement == "keep":
        assert _world_pads(rebuilt, after_pads) == _world_pads(source, before_pads)
    else:
        assert all(
            "x" not in component and "fixed" not in component
            for component in rebuilt["components"]
        )
