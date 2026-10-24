"""pcb-epro-writer (gr514048/gr514049): ``precis.pcb.board_script`` emits a
workspace ``main.py`` whose ``build().to_batch()`` is byte-equal to the
batch it came from — a tiny fixture through :func:`emit`, and the dev-DB
round trip ``pcb_apply`` -> :func:`board_batch` -> script -> ``to_batch``.
"""

from __future__ import annotations

import importlib.util
import io
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from precis.ingest import pcb_epro
from precis.pcb.board_script import BoardScriptError, board_batch, emit

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
                "pins": [{"name": "1", "pad": "1"}, {"name": "2", "pad": "2"}],
                "x": 1.0,
                "y": 2.0,
                "rot": 90.0,
                "layer": "top",
                "fixed": "both",
            },
            {
                "refdes": "R1",
                "part": "C25804",
                "pins": [{"name": "1"}, {"name": "2"}],
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
            {"refdes": "P.U1", "part": "C1", "pins": []},
            {"refdes": "P.C1", "pins": [{"name": "1"}, {"name": "2"}]},
            {"refdes": "J1", "pins": [{"name": "1"}]},
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
            "pins": [],
            "x": 0.0,
            "y": 0.0,
            "rot": 0.0,
            "layer": "top",
            "edge_mate": {"face": "S", "require": "required"},
        },
        {
            "refdes": "J2",
            "pins": [],
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
    # J1 has no handle and no lcsc -> no invented comment on its own line
    j1_lines = [l for l in text.splitlines() if "'J1'" in l]
    assert j1_lines and all("#" not in l for l in j1_lines)
    # without a handle the lcsc fallback fires instead
    text2 = _roundtrip(batch)
    assert "lcsc:C25804" in text2
    assert "pn777" not in text2


def test_unexpressible_keys_refuse_or_report() -> None:
    batch = _tiny_batch()
    batch["footprints"][0]["holes"] = [{"x": 0.0, "y": 0.0, "dia_mm": 3.0}]
    batch["components"][0]["part_meta"] = {"mpn": "X"}
    with pytest.raises(BoardScriptError, match="holes"):
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


def test_readback_epro_fixture_names_the_geom_gap(store) -> None:
    """``tests/test_pcb_epro_import.py::_zip`` -> import -> ``board_batch``
    -> ``emit``. The R0603 footprint's ``holes`` and pad ``rot``/``layer``
    fields are beyond ``precis_geom.pcb`` today: pads emit raw, ``holes``
    is the named geom gap (reported to the coordinator)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project2.json", (_FIXTURE / "project2.json").read_bytes())
        zf.writestr("board.epru", (_FIXTURE / "board.epru").read_bytes())
    pcb_epro.import_epro(store, buf.getvalue(), slug="epro-bs", title="Tiny")
    batch, _annotations = board_batch(store, "epro-bs")
    with pytest.raises(BoardScriptError, match="holes"):
        emit(batch, "epro-bs", "epro")
    # minus the holes the rest of the import round-trips byte-equal
    for fp in batch["footprints"]:
        fp.pop("holes", None)
    assert _exec(emit(batch, "epro-bs", "epro")).build().to_batch() == batch
