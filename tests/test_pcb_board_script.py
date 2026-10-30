"""pcb-epro-writer (gr514048/gr514049): ``precis.pcb.board_script`` emits a
workspace ``main.py`` whose ``build().to_batch()`` is byte-equal to the
batch it came from — a tiny fixture through :func:`emit`, and the dev-DB
round trip ``pcb_apply`` -> :func:`board_batch` -> script -> ``to_batch``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from precis.ingest import pcb_epro
from precis.pcb.board_script import BoardScriptError, board_batch, emit
from tests.workspaces._script import load_module

_FIXTURE = Path(__file__).parent / "fixtures" / "pcb_epro_tiny"


def _exec(text: str) -> ModuleType:
    spec = importlib.util.spec_from_loader("_emitted", loader=None)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    exec(compile(text, "<emit>", "exec"), mod.__dict__)
    return mod


def _roundtrip(batch: dict[str, Any], **kw: Any) -> str:
    text = emit(batch, "t", "test", **kw)
    assert _exec(text).build().to_batch() == batch
    return text


def _tiny_batch() -> dict[str, Any]:
    return {
        "components": [
            {
                "refdes": "J1",
                "label": "HDR-2",
                "footprint": "HDR-2.54-1x2",
                "pins": [
                    {
                        "name": "1",
                        "pad": "1",
                        "etype": "power_in",
                        "meta": {
                            "max_current_a": 0.5,
                            "max_voltage_v": 5.0,
                            "assume": "gnd pin",
                        },
                    },
                    {
                        "name": "2",
                        "pad": "2",
                        "etype": "bidi",
                        "meta": {"fmax_hz": 0.0, "assume": "sig pin"},
                    },
                ],
                "x": 1.0,
                "y": 2.0,
                "rot": 90.0,
                "layer": "top",
                "fixed": "both",
            },
            {
                "refdes": "R1",
                "part": "C25804",
                "pins": [
                    {
                        "name": "1",
                        "etype": "passive",
                        "meta": {"fmax_hz": 0.0, "assume": "resistor"},
                    },
                    {
                        "name": "2",
                        "etype": "passive",
                        "meta": {"fmax_hz": 0.0, "assume": "resistor"},
                    },
                ],
                "value": "10k",
            },
        ],
        "nets": [
            {"name": "GND", "class": "ground", "current": 0.5},
            {"name": "SIG", "note": "one net's note"},
        ],
        "connections": [
            {"net": "GND", "refdes": "J1", "pin": "1"},
            {"net": "GND", "refdes": "R1", "pin": "2"},
            {"net": "SIG", "refdes": "J1", "pin": "2", "note": "shell"},
            {"net": "SIG", "refdes": "R1", "pin": "1"},
        ],
        "footprints": [
            {
                "name": "HDR-2.54-1x2",
                "pads": [
                    {
                        "pin": "1",
                        "shape": "circle",
                        "x": -1.27,
                        "y": 0.0,
                        "w": 1.7,
                        "drill": 0.9,
                    },
                    {
                        "pin": "2",
                        "shape": "circle",
                        "x": 1.27,
                        "y": 0.0,
                        "w": 1.7,
                        "drill": 0.9,
                    },
                ],
                "pin_map": {"1": "1", "2": "2"},
            },
            {
                "name": "HDR-2.54-1x2-alt",
                "pads": [
                    {
                        "pin": "1",
                        "shape": "circle",
                        "x": -1.27,
                        "y": 0.0,
                        "w": 1.7,
                        "drill": 0.9,
                    },
                    {
                        "pin": "2",
                        "shape": "circle",
                        "x": 1.27,
                        "y": 0.0,
                        "w": 1.7,
                        "drill": 0.9,
                    },
                ],
            },
        ],
        "features": [
            {
                "ftype": "outline",
                "geom": {"path": [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]},
            },
            {
                "ftype": "mounting_hole",
                "x": 3.0,
                "y": 3.0,
                "geom": {"diameter": 3.2, "ring_dia_mm": 6.0, "plated": True},
            },
        ],
        "net_classes": {"power": {"width_mm": 0.5}},
        "meta": {"spec": "fixture"},
        "profile": "prototype",
    }


def test_tiny_batch_roundtrips_byte_equal() -> None:
    _roundtrip(_tiny_batch())


def test_footprints_share_one_pads_binding() -> None:
    text = _roundtrip(_tiny_batch())
    pads_vars = {
        line.split(" = ")[0]
        for line in text.splitlines()
        if line.startswith("    _pads_")
    }
    assert len(pads_vars) == 1
    assert text.count("_pads_hdr_2_54_1x2") >= 2


def test_raw_pads_keep_rot_layer_slot() -> None:
    batch = _tiny_batch()
    batch["footprints"] = [
        {
            "name": "SLOT-FP",
            "pads": [
                {
                    "pin": "1",
                    "shape": "rect",
                    "x": 0.0,
                    "y": 0.0,
                    "w": 2.0,
                    "h": 1.0,
                    "rot": 45.0,
                    "layer": "F.Cu",
                    "role": "solderable",
                    "mask": "open",
                    "paste": "full",
                    "drill": 0.5,
                    "slot": {"w": 1.2, "h": 0.5},
                },
            ],
        }
    ]
    text = _roundtrip(batch)
    assert ('"rot": 45.0' in text and "'rot': 45.0" not in text) or "45.0" in text


def test_named_nets_once_no_line_over_100() -> None:
    text = _roundtrip(_tiny_batch())
    for line in text.splitlines():
        assert len(line) <= 100, line
    assert text.count("b.net('GND'") == 1
    assert text.count("b.net('SIG'") == 1
    # ties grouped per consecutive run — no pin-by-pin dump
    assert ".tie(" in text


def test_module_blocks_for_prefixed_refdes() -> None:
    batch = {
        "components": [
            {
                "refdes": "P.U1",
                "part": "C1",
                "pins": [
                    {
                        "name": "GND",
                        "etype": "power_in",
                        "meta": {
                            "max_current_a": 0.1,
                            "max_voltage_v": 3.6,
                            "assume": "ic gnd",
                        },
                    },
                    {
                        "name": "FB",
                        "etype": "input",
                        "meta": {"fmax_hz": 0.0, "assume": "feedback"},
                    },
                ],
            },
            {
                "refdes": "P.C1",
                "pins": [
                    {
                        "name": "1",
                        "etype": "passive",
                        "meta": {"fmax_hz": 0.0, "assume": "cap"},
                    },
                    {
                        "name": "2",
                        "etype": "passive",
                        "meta": {"fmax_hz": 0.0, "assume": "cap"},
                    },
                ],
            },
            {
                "refdes": "J1",
                "pins": [
                    {
                        "name": "1",
                        "etype": "power_in",
                        "meta": {
                            "max_current_a": 0.1,
                            "max_voltage_v": 5.0,
                            "assume": "gnd pin",
                        },
                    }
                ],
            },
        ],
        "nets": [{"name": "GND"}, {"name": "P/FB"}],
        "connections": [
            {"net": "GND", "refdes": "P.U1", "pin": "GND"},
            {"net": "P/FB", "refdes": "P.U1", "pin": "FB"},
            {"net": "P/FB", "refdes": "P.C1", "pin": "1"},
            {"net": "GND", "refdes": "P.C1", "pin": "2"},
            {"net": "GND", "refdes": "J1", "pin": "1"},
        ],
    }
    text = _roundtrip(batch)
    assert "@pcb.module" in text
    assert "b.use(" in text and "prefix='P'" in text


def test_group_and_edge_and_leash() -> None:
    batch = _tiny_batch()
    batch["components"] = [
        {
            "refdes": "J1",
            "pins": [
                {
                    "name": "1",
                    "etype": "power_in",
                    "meta": {
                        "max_current_a": 0.1,
                        "max_voltage_v": 5.0,
                        "assume": "gnd pin",
                    },
                }
            ],
            "x": 0.0,
            "y": 0.0,
            "rot": 0.0,
            "layer": "top",
            "edge_mate": {"face": "S", "require": "required"},
        },
        {
            "refdes": "J2",
            "pins": [
                {
                    "name": "1",
                    "etype": "power_in",
                    "meta": {
                        "max_current_a": 0.1,
                        "max_voltage_v": 5.0,
                        "assume": "gnd pin",
                    },
                }
            ],
            "x": 5.0,
            "y": 0.0,
            "rot": 90.0,
            "layer": "top",
            "max_travel_mm": 1.5,
        },
        {
            "refdes": "J3",
            "pins": [],
            "group": "hdr",
            "group_offset": {"x": 0.0, "y": 0.0, "rot": 0.0},
        },
        {
            "refdes": "J4",
            "pins": [],
            "group": "hdr",
            "group_offset": {"x": 5.0, "y": 0.0, "rot": 90.0},
        },
    ]
    batch["connections"] = [
        {"net": "GND", "refdes": "J1", "pin": "1"},
        {"net": "GND", "refdes": "J2", "pin": "1"},
    ]
    text = _roundtrip(batch)
    assert "b.group('hdr'" in text or 'b.group("hdr"' in text


def test_annotation_handle_and_lcsc_fallback() -> None:
    batch = _tiny_batch()
    text = _roundtrip(
        batch,
        annotations={
            "parts": {"R1": "pn777 — thick film"},
            "footprints": {"HDR-2.54-1x2": "fp42"},
        },
    )
    assert "pn777 — thick film" in text
    assert "fp42" in text
    # J1 has no handle: its fallback label must not invent one.
    j1_lines = [l for l in text.splitlines() if "'J1'" in l]
    assert j1_lines and all("#" not in l for l in j1_lines)
    assert any(
        "j1 = b.part(" in line and "synthesized footprint" in line
        for line in text.splitlines()
    )
    # without a handle the lcsc fallback fires instead
    text2 = _roundtrip(batch)
    assert "lcsc:C25804" in text2
    assert "pn777" not in text2


def test_import_keys_roundtrip() -> None:
    """gr514214: the epro-import keys emit verbatim — footprint holes,
    component part_meta/supplies/pin_swap_groups, net width_mm/meta."""
    batch = _tiny_batch()
    batch["footprints"][0]["holes"] = [{"x": 0.0, "y": 0.0, "dia_mm": 3.0}]
    batch["components"][0]["part_meta"] = {"mpn": "X", "supplier": "LCSC"}
    batch["components"][0]["supplies"] = [
        {"role": "VCC", "blocks": [["1"]], "voltage_v": 5.0}
    ]
    batch["components"][1]["pin_swap_groups"] = [["1", "2"]]
    batch["nets"][0]["width_mm"] = 0.4
    batch["nets"][0]["meta"] = {"src": "import"}
    _roundtrip(batch)


def test_footprint_graphics_roundtrip() -> None:
    """gr520150: footprint ``graphics`` used to be dropped at export —
    they now ride the ``Footprint`` kwarg and come back byte-equal."""
    batch = _tiny_batch()
    batch["footprints"][0]["graphics"] = [
        {
            "kind": "polygon",
            "layer": "F.SilkS",
            "width_mm": 0.1,
            "points": [[-3, -2], [4, -2], [4, 2], [-3, -2]],
        },
        {
            "kind": "text",
            "layer": "F.SilkS",
            "x": 0,
            "y": 2.5,
            "text": "NO COPPER",
            "height_mm": 1.0,
        },
        {
            "kind": "region",
            "layer": "F.Cu",
            "keepout": True,
            "points": [[1, 1], [3, 1], [3, 2]],
        },
    ]
    text = _roundtrip(batch)
    assert "graphics=" in text


def test_unexpressible_keys_refuse_or_report() -> None:
    batch = _tiny_batch()
    batch["components"][0]["pin_decls"] = {"claims": [{"pin": "1", "name": "A"}]}
    batch["components"][0]["courtyard"] = [[0, 0], [1, 1]]
    with pytest.raises(BoardScriptError, match="pin_decls"):
        emit(batch, "t", "test")
    text = emit(batch, "t", "test", allow_loss=True)
    assert "dropped keys" in text


def test_readback_roundtrip_store(store) -> None:
    """Slice 4's chain minus the epro front end: ``pcb_apply`` ->
    ``board_batch`` -> ``emit`` -> ``to_batch`` equal."""
    batch = _tiny_batch()
    store.pcb_apply(
        slug="bs-tiny",
        title="Tiny",
        components=batch["components"],
        nets=batch["nets"],
        connections=batch["connections"],
        features=batch["features"],
        footprints=batch["footprints"],
        meta=batch["meta"],
    )
    got, _annotations = board_batch(store, "bs-tiny")
    text = emit(got, "bs-tiny", "store:bs-tiny")
    assert _exec(text).build().to_batch() == got


def test_readback_epro_fixture(store) -> None:
    """``tests/test_pcb_epro_import.py::_zip`` -> import -> ``board_batch``
    -> ``emit`` -> ``to_batch`` byte-equal — footprint ``holes`` and pad
    ``rot``/``layer`` included (gr514214 closed the geom gap: pads emit
    raw, holes ride the Footprint kwarg)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project2.json", (_FIXTURE / "project2.json").read_bytes())
        zf.writestr("board.epru", (_FIXTURE / "board.epru").read_bytes())
    imported = pcb_epro.import_epro(store, buf.getvalue(), slug="epro-bs", title="Tiny")
    # gr520810: an imported board's pins arrive unclassified and the
    # emitted script's ``tie()`` refuses them — ``op='pins'`` is the
    # classify-first door for a legacy board.
    store.pcb_patch_pins(
        imported.ref_id,
        "R1",
        [
            {
                "name": "1",
                "etype": "passive",
                "fmax_hz": 0.0,
                "assume": "epro import",
            }
        ],
    )
    store.pcb_patch_pins(
        imported.ref_id,
        "U1",
        [
            {
                "name": "VDD",
                "etype": "power_in",
                "max_current_a": 0.1,
                "max_voltage_v": 5.0,
                "assume": "epro import",
            },
            {
                "name": "GND",
                "etype": "power_in",
                "max_current_a": 0.1,
                "max_voltage_v": 5.0,
                "assume": "epro import",
            },
        ],
    )
    batch, _annotations = board_batch(store, "epro-bs")
    assert _exec(emit(batch, "epro-bs", "epro")).build().to_batch() == batch


def _row_set(rows: list[dict[str, Any]]) -> set[str]:
    """Compare unordered batch rows with sub-micrometre float noise removed."""

    def normalize(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 6)
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return {json.dumps(normalize(row), sort_keys=True) for row in rows}


def _assert_board_rows_equal(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    for key in ("components", "nets", "connections"):
        assert _row_set(actual[key]) == _row_set(expected[key]), key
    assert _row_set(actual.get("planes", [])) == _row_set(expected.get("planes", []))
    expected_measures = []
    for source in expected.get("measures", []):
        measure = {
            key: value
            for key, value in source.items()
            if key not in {"reason", "strength"}
        }
        measure["operands"] = [
            operand["instance"] if isinstance(operand, dict) else operand
            for operand in source["operands"]
        ]
        measure["hard"] = (
            source["strength"] == "hard"
            if "strength" in source
            else source.get("hard", False)
        )
        measure.setdefault("weight", 1.0)
        expected_measures.append(measure)
    assert _row_set(actual.get("measures", [])) == _row_set(expected_measures)
    actual_footprints = {fp["name"]: fp for fp in actual.get("footprints", [])}
    expected_footprints = {fp["name"]: fp for fp in expected.get("footprints", [])}
    assert actual_footprints.keys() == expected_footprints.keys()
    for name, footprint in expected_footprints.items():
        assert _row_set(actual_footprints[name]["pads"]) == _row_set(footprint["pads"])


@pytest.mark.parametrize(
    "fixture",
    [
        "esp32c3_reference.json",
        "nano_usb_pd_testboard.py",
        "motor_power_reference.json",
        "nano_oc_switch.json",
    ],
)
def test_reference_batch_to_workspace(tmp_path: Path, fixture: str) -> None:
    source = Path(__file__).parent / "fixtures" / "pcb" / fixture
    batch = (
        load_module(source, name="board_fixture").build_design()
        if source.suffix == ".py"
        else json.loads(source.read_text(encoding="utf-8"))
    )
    script = tmp_path / "main.py"
    script.write_text(emit(batch, source.stem, str(source)), encoding="utf-8")
    rebuilt = load_module(script).build().to_batch()
    _assert_board_rows_equal(rebuilt, batch)
    assert rebuilt.get("features", []) == batch.get("features", [])
    assert str(source) in script.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "strength,hard", [("hard", True), ("soft", False), (None, False)]
)
def test_legacy_measure_strength_and_instance_operands(
    strength: str | None, hard: bool
) -> None:
    batch = _tiny_batch()
    measure: dict[str, Any] = {
        "metric": "proximity",
        "operands": [{"instance": "J1"}, {"instance": "R1"}],
        "bound": 10.0,
        "goal": 5.0,
        "weight": 2.5,
        "reason": "keep the connector close\npreserve short leads",
    }
    if strength is not None:
        measure["strength"] = strength
    batch["measures"] = [measure]
    original = deepcopy(batch)
    text = emit(batch, "tiny", "measure fixture")
    rebuilt = _exec(text).build().to_batch()
    assert rebuilt["measures"] == [
        {
            "metric": "proximity",
            "operands": ["J1", "R1"],
            "bound": 10.0,
            "goal": 5.0,
            "hard": hard,
            "weight": 2.5,
        }
    ]
    comments = [
        line for line in text.splitlines() if "# keep the connector close" in line
    ]
    assert len(comments) == 1
    assert "b.measure(" in comments[0]
    assert comments[0].endswith("# keep the connector close preserve short leads")
    assert batch == original


def test_normalized_measure_roundtrips_with_existing_hard_flag() -> None:
    batch = _tiny_batch()
    batch["measures"] = [
        {
            "metric": "proximity",
            "operands": ["J1", "R1"],
            "hard": True,
            "weight": 1.0,
            "goal": 8.0,
        }
    ]
    _roundtrip(batch)


@pytest.mark.parametrize(
    "overrides",
    [
        {"strength": "required"},
        {"strength": "hard", "hard": False},
        {"strength": "soft", "hard": True},
        {"operands": [{"instance": "J1", "pin": "1"}]},
        {"operands": [{"instance": "MISSING"}]},
    ],
)
def test_unexpressible_legacy_measure_refused(overrides: dict[str, Any]) -> None:
    batch = _tiny_batch()
    batch["measures"] = [{"metric": "proximity", "operands": ["J1", "R1"], **overrides}]
    with pytest.raises(BoardScriptError):
        emit(batch, "tiny", "measure fixture")


def test_plane_assignments_are_rows_without_pour_features() -> None:
    import precis_geom.pcb as pcb

    board = pcb.Board("assignments")
    ground = board.net("GND")
    supply = board.net("VBAT")
    assert "planes" not in board.to_batch()
    board.plane_assignment(ground, "F.Cu")
    board.plane_assignment(supply, "In2.Cu")
    expected = [
        {"net": "GND", "layer": "F.Cu"},
        {"net": "VBAT", "layer": "In2.Cu"},
    ]
    batch = board.to_batch()
    assert batch["planes"] == expected
    assert "features" not in batch
    batch["planes"][0]["net"] = "mutated snapshot"
    assert board.to_batch()["planes"] == expected


def test_legacy_plane_pour_stays_separate_from_assignments() -> None:
    import precis_geom.pcb as pcb

    board = pcb.Board("mixed")
    ground = board.net("GND")
    board.plane_assignment(ground, "B.Cu")
    board.plane(1, net=ground, priority=2)
    batch = board.to_batch()
    assert batch["planes"] == [{"net": "GND", "layer": "B.Cu"}]
    assert batch["features"] == [
        {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND", "priority": 2}
    ]
    text = _roundtrip(batch)
    assert "b.plane_assignment(gnd, 'B.Cu')" in text
    assert "b.plane(" not in text


def test_plane_assignment_refuses_foreign_net() -> None:
    import precis_geom.pcb as pcb

    board = pcb.Board("local")
    board.net("GND")
    foreign_ground = pcb.Board("other").net("GND")
    with pytest.raises(pcb.PcbBuilderError):
        board.plane_assignment(foreign_ground, "F.Cu")
    assert "planes" not in board.to_batch()


@pytest.mark.parametrize("layer", ["", 0, None])
def test_plane_assignment_refuses_invalid_layer(layer: Any) -> None:
    import precis_geom.pcb as pcb

    board = pcb.Board("local")
    ground = board.net("GND")
    with pytest.raises(pcb.PcbBuilderError):
        board.plane_assignment(ground, layer)
    assert "planes" not in board.to_batch()


@pytest.mark.parametrize(
    "plane",
    [
        {"net": "MISSING", "layer": "F.Cu"},
        {"net": "GND", "layer": "F.Cu", "priority": 2},
    ],
)
def test_unexpressible_plane_assignment_refused(plane: dict[str, Any]) -> None:
    batch = _tiny_batch()
    batch["planes"] = [plane]
    with pytest.raises(BoardScriptError):
        emit(batch, "tiny", "plane fixture")


def test_synthesized_pad_geometry_roundtrip(tmp_path: Path) -> None:
    # Exercise raw slot-pad geometry as well as the reference fixtures' pads.
    batch = _tiny_batch()
    batch["footprints"][0]["pads"][0].update(
        x=-1.2700000002, rot=45.0, layer="F.Cu", slot={"w": 1.2, "h": 0.5}
    )
    text = emit(batch, "tiny", "test fixture")
    assert "# synthesized footprint" in text
    script = tmp_path / "main.py"
    script.write_text(text, encoding="utf-8")
    rebuilt = load_module(script).build().to_batch()
    assert sum(len(fp["pads"]) for fp in rebuilt["footprints"]) == 4
    _assert_board_rows_equal(rebuilt, batch)


def test_library_reference_needs_no_inline_pads_or_catalog_read(monkeypatch) -> None:
    import precis_geom.pcb as pcb

    def refuse_catalog_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("rebuilding a footprint reference must not query the catalog")

    monkeypatch.setattr(pcb, "_catalog_read", refuse_catalog_read)
    batch = _tiny_batch()
    batch.pop("footprints")
    batch["components"][0]["footprint"] = "Connector:HDR-2.54-1x2"
    text = _roundtrip(batch)
    assert "footprint='Connector:HDR-2.54-1x2'" in text
    assert "pcb.Footprint(" not in text
    assert "# synthesized footprint" not in text


@pytest.mark.parametrize("placement", ["keep", "omit"])
@pytest.mark.parametrize("prefix", ["", "Power."])
def test_placement_modes_preserve_design_and_input(placement: str, prefix: str) -> None:
    batch = _tiny_batch()
    for index, component in enumerate(batch["components"]):
        component.update(
            refdes=prefix + component["refdes"],
            x=float(index + 1),
            y=float(index + 2),
            rot=90.0,
            layer="bottom",
            fixed="rot",
            max_travel_mm=2.5,
            group="supply",
            group_offset={"x": float(index), "y": 0.0, "rot": 90.0},
        )
    for connection in batch["connections"]:
        connection["refdes"] = prefix + connection["refdes"]
    original = deepcopy(batch)
    text = emit(batch, "tiny", "test fixture", placement=placement)
    rebuilt = _exec(text).build().to_batch()
    expected = deepcopy(original)
    for component in expected["components"]:
        if placement == "keep":
            component["fixed"] = "both"
        else:
            for key in (
                "x",
                "y",
                "rot",
                "layer",
                "fixed",
                "max_travel_mm",
                "group_offset",
            ):
                component.pop(key)
    _assert_board_rows_equal(rebuilt, expected)
    assert batch == original
    assert rebuilt["features"] == original["features"]
    assert all(component["group"] == "supply" for component in rebuilt["components"])
    if placement == "omit":
        assert ".at(" not in text
        assert ".leash(" not in text


def test_invalid_placement_refused() -> None:
    with pytest.raises(BoardScriptError, match="placement"):
        emit(_tiny_batch(), "tiny", "test fixture", placement="discard")


@pytest.mark.parametrize("missing", ["x", "y"])
def test_keep_placement_refuses_partial_pose(missing: str) -> None:
    batch = _tiny_batch()
    batch["components"][0].pop(missing)
    with pytest.raises(BoardScriptError, match="placement needs both x and y"):
        emit(batch, "tiny", "test fixture", placement="keep")


def test_omit_placement_preserves_edge_intent_without_position() -> None:
    batch = _tiny_batch()
    batch["components"][0]["edge_mate"] = {
        "face": "S",
        "require": "required",
        "at": 5.0,
    }
    original = deepcopy(batch)
    rebuilt = (
        _exec(emit(batch, "tiny", "test fixture", placement="omit")).build().to_batch()
    )
    assert rebuilt["components"][0]["edge_mate"] == {"face": "S", "require": "required"}
    assert batch == original


@pytest.mark.parametrize("placement", [None, "keep", "omit"])
def test_batch_to_script_cli(tmp_path: Path, placement: str | None) -> None:
    from precis.pcb.board_script import main

    batch = _tiny_batch()
    batch["components"][0]["fixed"] = "rot"
    source = tmp_path / "batch.json"
    original = json.dumps(batch)
    source.write_text(original, encoding="utf-8")
    output = tmp_path / "workspace.py"
    argv = [str(source), str(output)]
    if placement is not None:
        argv += ["--placement", placement]
    assert main(argv) == 0
    rebuilt = load_module(output).build().to_batch()
    connector = next(part for part in rebuilt["components"] if part["refdes"] == "J1")
    if placement == "omit":
        assert "x" not in connector
        assert "fixed" not in connector
    else:
        assert connector["x"] == 1.0
        assert connector["y"] == 2.0
        assert connector["fixed"] == "both"
    assert _row_set(rebuilt["nets"]) == _row_set(batch["nets"])
    assert _row_set(rebuilt["connections"]) == _row_set(batch["connections"])
    assert source.read_text(encoding="utf-8") == original


def test_batch_to_script_cli_refuses_overwriting_input(tmp_path: Path) -> None:
    from precis.pcb.board_script import main

    source = tmp_path / "batch.json"
    original = json.dumps(_tiny_batch())
    source.write_text(original, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main([str(source), str(source)])
    assert exc.value.code == 1
    assert source.read_text(encoding="utf-8") == original


def test_batch_to_script_cli_malformed_json_preserves_output(tmp_path: Path) -> None:
    from precis.pcb.board_script import main

    source = tmp_path / "batch.json"
    source.write_text("{", encoding="utf-8")
    output = tmp_path / "workspace.py"
    output.write_text("# authored workspace\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main([str(source), str(output)])
    assert exc.value.code == 1
    assert output.read_text(encoding="utf-8") == "# authored workspace\n"
