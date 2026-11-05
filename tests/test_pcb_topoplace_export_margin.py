"""Export quantization must fit inside the route oracle's reserved margin."""

from __future__ import annotations

import dataclasses
import math
import re
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from precis.pcb import drc, epro, epro_write, gerber, padplace
from precis.pcb.capabilities import capability_for
from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.realise import RoutePlan, ViaSite, _guarded_nets

_LAYERS = ["F.Cu", "B.Cu"]
_CAP = capability_for("4layer")


def _pad(x: float, y: float, refdes: str = "U1") -> dict[str, Any]:
    return {
        "refdes": refdes,
        "pin": "1",
        "net": "N0",
        "layer": "F.Cu",
        "shape": "rect",
        "x": x,
        "y": y,
        "w": 1.0,
        "h": 1.0,
    }


def _via(x: float, y: float, dia: float = 0.45) -> ViaSite:
    return ViaSite(net=0, branch=0, edge=0, t=0.5, x=x, y=y, dia_mm=dia, drill_mm=0.2)


def _model(pads: list[dict[str, Any]], vias: tuple[ViaSite, ...]) -> dict[str, Any]:
    return {
        "layers": _LAYERS,
        "outline": [[0, 0], [30, 0], [30, 20], [0, 20]],
        "pads": pads,
        "drills": [],
        "instances": [],
        "copper": [
            {
                "ctype": "via",
                "net": "N0",
                "layer": "F.Cu",
                "x": v.x,
                "y": v.y,
                "dia_mm": v.dia_mm,
                "drill_mm": v.drill_mm,
                "span": _LAYERS,
            }
            for v in vias
        ],
        "silkscreen": {"top": [], "bottom": []},
        "mask_open_regions": [],
    }


def _guard(pads: list[dict[str, Any]], vias: tuple[ViaSite, ...]) -> set[int]:
    # One pad per independently placed part, exactly as the guarded pose sees it.
    d = TopoDesign(
        parts=tuple(p["refdes"] for p in pads),
        pin_xy=np.zeros((len(pads), 2)),
        pin_part=np.arange(len(pads)),
        pin_side=np.zeros(len(pads), dtype=np.int8),
        pin_label=np.asarray(["1"] * len(pads), dtype=object),
        pin_net=np.zeros(len(pads), dtype=np.int32),
        net_pins=(np.arange(len(pads)),),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(len(pads), dtype=bool),
        freedoms=np.full(len(pads), 3, dtype=np.uint8),
        outline=np.asarray([[0, 0], [30, 0], [30, 20], [0, 20]]),
        pose0=np.asarray([[p["x"], p["y"], 0, 0] for p in pads]),
        net_name=("N0",),
        pin_wh=np.ones((len(pads), 2)),
        via_mask_web_mm=0.2,
    )
    ir = SimpleNamespace(net_name=np.asarray(["N0"], dtype=object))
    return _guarded_nets(
        RoutePlan(pieces=(), vias=vias, layers=()), d, ir, 2, d.pose0, {}
    )


def test_fab_pad_rounding_cannot_consume_the_reserved_web() -> None:
    pads = [_pad(5.000079, 5.000079)]
    near = _via(5.8006, 5.8006)
    assert drc.check_via_mask_web(_model(pads, (near,)), _CAP) == []
    inst = {
        "refdes": "U1",
        "x": pads[0]["x"],
        "y": pads[0]["y"],
        "rot": 0,
        "layer": "top",
    }
    exported, _ = padplace.place_footprint_pads(
        [
            {
                "number": "1",
                "shape": "RECT",
                "x": 0,
                "y": 0,
                "w": 1,
                "h": 1,
                "layer": "F.Cu",
            }
        ],
        inst,
        layers=_LAYERS,
        pin_to_net={"1": "N0"},
    )
    assert drc.check_via_mask_web(_model(exported, (near,)), _CAP)
    assert _guard(pads, (near,)) == {0}
    step = FAB_QUANTUM_MM / math.sqrt(2.0)
    safe = dataclasses.replace(near, x=near.x + step, y=near.y + step)
    assert _guard(pads, (safe,)) == set()
    assert drc.check_via_mask_web(_model(exported, (safe,)), _CAP) == []


def _gerber_via(model: dict[str, Any]) -> dict[str, Any]:
    raw = gerber.copper_gerber(model, "F.Cu")
    aperture = re.search(r"%ADD(\d+)C,([\d.]+)\*%", raw)
    assert aperture is not None
    flash = re.search(rf"D{aperture[1]}\*\nX(-?\d+)Y(-?\d+)D03\*", raw)
    assert flash is not None
    return {
        **model["copper"][0],
        "dia_mm": float(aperture[2]),
        "x": int(flash[1]) / 1e6,
        "y": int(flash[2]) / 1e6,
    }


def test_gerber_aperture_rounding_cannot_consume_the_reserved_web() -> None:
    pads = [_pad(5.0, 5.0)]
    near = _via(5.800546, 5.800546, 0.450051)
    model = _model(pads, (near,))
    assert drc.check_via_mask_web(model, _CAP) == []
    assert drc.check_via_mask_web({**model, "copper": [_gerber_via(model)]}, _CAP)
    assert _guard(pads, (near,)) == {0}
    step = FAB_QUANTUM_MM / math.sqrt(2.0)
    safe = dataclasses.replace(near, x=near.x + step, y=near.y + step)
    safe_model = _model(pads, (safe,))
    assert _guard(pads, (safe,)) == set()
    assert (
        drc.check_via_mask_web(
            {**safe_model, "copper": [_gerber_via(safe_model)]}, _CAP
        )
        == []
    )


def _excellon_vias(model: dict[str, Any]) -> list[dict[str, Any]]:
    raw = gerber.excellon_files(model)["design-PTH.drl"]
    sites = re.findall(r"^X([\d.]+)Y([\d.]+)$", raw, re.MULTILINE)
    assert len(sites) == len(model["copper"])
    return [
        {**via, "x": float(x), "y": float(y)}
        for via, (x, y) in zip(model["copper"], sites, strict=True)
    ]


def test_excellon_sites_cannot_consume_the_reserved_drill_wall() -> None:
    pads = [_pad(1, 1)]
    near = (_via(5.800579, 5.800579), _via(6.083422, 6.083422))
    model = _model(pads, near)
    assert drc.check_via_via_keepout(model, _CAP) == []
    assert drc.check_via_via_keepout({**model, "copper": _excellon_vias(model)}, _CAP)
    assert _guard(pads, near) == {0}
    step = FAB_QUANTUM_MM / math.sqrt(2.0)
    safe = (
        near[0],
        dataclasses.replace(near[1], x=near[1].x + step, y=near[1].y + step),
    )
    safe_model = _model(pads, safe)
    assert _guard(pads, safe) == set()
    assert (
        drc.check_via_via_keepout(
            {**safe_model, "copper": _excellon_vias(safe_model)}, _CAP
        )
        == []
    )


def test_easyeda_dedup_keeps_distinct_submicron_pad_offsets() -> None:
    pads = [_pad(5.0104, 5, "U1"), _pad(15.0096, 5, "U2")]
    via = _via(pads[1]["x"] + 0.5 + 0.225 + 0.2004, 5)
    model = _model(pads, (via,))
    model["instances"] = [
        {"refdes": "U1", "x": 5, "y": 5, "rot": 0, "layer": "top", "footprint": "PAD"},
        {"refdes": "U2", "x": 15, "y": 5, "rot": 0, "layer": "top", "footprint": "PAD"},
    ]
    assert _guard(pads, (via,)) == set()
    assert drc.check_via_mask_web(model, _CAP) == []
    exported = epro_write.epro_files(model, slug="dedup-margin")
    assert exported.warnings == []
    project = epro.read_archive(epro_write.zip_epro(exported.files))
    design, _frame = epro.build_design(project)
    pcb_doc = project.pcb()
    _outline, frame = epro.board_outline(pcb_doc)
    read_copper, warnings = epro.measured_copper(pcb_doc, frame)
    assert warnings == []
    fps = {fp["name"]: fp for fp in design.footprints}
    read_pads: list[dict[str, Any]] = []
    for comp in design.components:
        fp = fps[comp["footprint"]]
        for pad in fp["pads"]:
            x, y = padplace.place_pad_point(pad, comp)
            read_pads.append(_pad(x, y, comp["refdes"]))
    got = {p["refdes"]: p for p in read_pads}
    assert got["U2"]["x"] == pytest.approx(pads[1]["x"], abs=1e-6)
    assert (
        drc.check_via_mask_web(
            {**model, "pads": read_pads, "copper": read_copper}, _CAP
        )
        == []
    )
