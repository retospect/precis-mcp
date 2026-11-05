"""Fabrication and re-import must agree with the source DRC boundary."""

from __future__ import annotations

import hashlib
import math
import xml.etree.ElementTree as ET
from typing import Any

import pytest
from shapely.geometry import LineString, Point, Polygon

from precis.pcb import (
    DEFAULT_STACKUP,
    drc,
    epro,
    epro_write,
    gerber,
    padplace,
    realize,
    svg,
)
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import from_graph
from precis.pcb.session import apply_real_pin_offsets
from tests.test_pcb_export_round9 import _art_geometry
from tests.test_pcb_geometry_round12 import _placed


def _raw(shape="obround", offset=(0.7483, 0.0146)):
    return dict(
        number="1",
        x=0.0,
        y=0.0,
        shape=shape,
        w=2.4,
        h=2.4 if shape == "circle" else 1.2,
        rot=37.0,
        drill=0.6,
        drill_offset=dict(zip(("x", "y"), offset, strict=True)),
    )


def _model(pad, inst):
    return dict(
        layers=["F.Cu", "B.Cu"],
        pads=[{**pad, "net": ""}],
        outline=[[0, 0], [20, 0], [20, 20], [0, 20]],
        drills=padplace.drills_for_pads([pad]),
        copper=[],
        instances=[inst],
        silkscreen={"top": [], "bottom": []},
    )


@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("angle", [0.0, 23.7, 90.0])
@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("offset", [(0.7483, 0.0146), (0.752, 0.0146)])
def test_native_archive_preserves_ring_verdict(shape, angle, bottom, offset):
    pad, inst = _placed(_raw(shape, offset), angle, bottom, True)
    model = _model(pad, inst)
    archive = epro_write.epro_files(model, slug="ring-parity")
    assert not archive.warnings
    design, _ = epro.build_design(epro.read_archive(epro_write.zip_epro(archive.files)))
    component = design.components[0]
    footprint = next(
        f for f in design.footprints if f["name"] == component["footprint"]
    )
    recovered, _ = padplace.place_footprint_pads(
        footprint["pads"], component, layers=model["layers"]
    )
    cap = capability_for("4layer")
    source = drc.check_annular_ring(model, cap)
    imported = drc.check_annular_ring({"pads": recovered}, cap)
    assert [(f.rule, f.severity) for f in imported] == [
        (f.rule, f.severity) for f in source
    ]
    assert drc._round_pad_ring_mm(recovered[0], 0.6) == pytest.approx(
        drc._round_pad_ring_mm(pad, 0.6), abs=0.00015
    )
    assert pad["shape"] == recovered[0]["shape"] == shape


@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("film", ["copper", "mask", "paste"])
def test_ir_films_emit_the_analytic_boundary(shape, bottom, film):
    raw = _raw(shape)
    # Paste is defined only on undrilled pads.
    if film == "paste":
        raw.pop("drill")
    pad, inst = _placed(raw, 23.7, bottom, True)
    model = _model(pad, inst)
    side, layer = ("bottom", "B.Cu") if bottom else ("top", "F.Cu")
    text = (
        gerber.copper_gerber(model, layer)
        if film == "copper"
        else gerber.soldermask_gerber(model, side)
        if film == "mask"
        else gerber.solderpaste_gerber(model, side)
    )
    actual, _ = _art_geometry(text)
    margin = 0.05 if film == "mask" else 0.0
    w, h = 2.4 + 2 * margin, 1.2 + 2 * margin
    a = math.radians(23.7 - 37 if bottom else 23.7 + 37)

    def world(x, y):
        return 10 + x * math.cos(a) + y * math.sin(a), 10 - x * math.sin(
            a
        ) + y * math.cos(a)

    if shape == "obround":
        expected = LineString([world(-0.6, 0), world(0.6, 0)]).buffer(
            h / 2, quad_segs=256
        )
    else:
        expected = Polygon(
            [
                world(x, y)
                for x, y in [
                    (-w / 2, -h / 2),
                    (w / 2, -h / 2),
                    (w / 2, h / 2),
                    (-w / 2, h / 2),
                ]
            ]
        )
    assert actual.hausdorff_distance(expected) < 0.00005
    if film == "copper" and shape == "obround":
        hole = Point(pad["drill_x"], pad["drill_y"])
        assert actual.boundary.distance(hole) - 0.3 > 0.15


@pytest.mark.parametrize("slot", [False, True])
def test_relative_drill_geometry_is_quantized_only_after_translation(slot):
    raw = _raw("circle", (0.75, 0.0))
    if slot:
        raw["slot"] = dict(w=1.2, h=0.6)
    inst: dict[str, Any] = dict(refdes="J1", x=10.00005, y=10.000049, rot=0.0)
    # Declare a pin without adding a second footprint or synthetic pad.
    ir = from_graph(
        dict(
            instances=[inst, dict(refdes="U2", x=15, y=15)],
            nets=[
                dict(
                    name="N",
                    members=[dict(refdes="J1", pin="1"), dict(refdes="U2", pin="1")],
                )
            ],
        ),
        stackup=DEFAULT_STACKUP,
    )
    footprints = {"J1": dict(pads=[raw], pin_map={"1": {"name": "1"}})}
    apply_real_pin_offsets(ir, footprints)
    pad = next(
        p
        for p in realize.pads_for_ir(ir, ["F.Cu", "B.Cu"], footprints)
        if p["refdes"] == "J1"
    )
    direct, _ = padplace.place_footprint_pads([raw], inst, layers=["F.Cu"])
    a = math.radians(37)
    expected = (
        round(inst["x"] + 0.75 * math.cos(a), 4),
        round(inst["y"] - 0.75 * math.sin(a), 4),
    )
    assert (pad["drill_x"], pad["drill_y"]) == expected
    assert (pad["drill_x"], pad["drill_y"]) == (
        direct[0]["drill_x"],
        direct[0]["drill_y"],
    )
    if slot:
        assert pad["slot"] == direct[0]["slot"]
    else:
        assert drc._round_pad_ring_mm(pad, 0.6) == pytest.approx(0.150029, abs=0.000001)
        assert not any(
            f.severity == "error"
            for f in drc.check_annular_ring({"pads": [pad]}, capability_for("4layer"))
        )


def test_zero_offsets_keep_the_legacy_integer_serialization():
    for shape in ("rect", "circle", "obround"):
        raw = _raw(shape)
        raw.pop("drill_offset")
        pad, inst = _placed(raw, 0.0, False, False)
        body = epro_write._pad_body(epro_write._local_pad(pad, inst), 0)
        assert type(body["padOffsetX"]) is int
        assert type(body["padOffsetY"]) is int
        assert body["padOffsetX"] == body["padOffsetY"] == 0


def _ordinary_66_pad_model():
    """Synthetic local-66-like ordinary quarter-turn components, no board data."""
    model = _model({}, {})
    model["pads"], model["instances"], model["drills"] = [], [], []
    for i in range(22):
        inst = dict(
            refdes=f"U{i}",
            x=2.0 + i % 5 * 3,
            y=2.0 + i // 5 * 3,
            rot=(i % 4) * 90,
            layer="bottom" if i % 2 else "top",
        )
        raw = [
            dict(number=str(j + 1), shape="rect", x=(j - 1) * 0.8, y=0.0, w=0.5, h=0.9)
            for j in range(3)
        ]
        pads, _ = padplace.place_footprint_pads(raw, inst, layers=model["layers"])
        model["instances"].append(inst)
        model["pads"].extend(pads)
    return model


def _ordinary_pad_rows():
    archive = epro_write.epro_files(_ordinary_66_pad_model(), slug="ordinary-66")
    return "\n".join(
        line
        for content in archive.files.values()
        for line in content.splitlines()
        if line.startswith('{"type":"PAD"')
    ).encode("utf-8")


def test_ordinary_66_pad_rows_are_byte_stable():
    # Captured from bdb8e072a with the same synthetic, unmodified model.
    assert (
        hashlib.sha256(_ordinary_pad_rows()).hexdigest()
        == "7d39cdedb6c580d767758fa09e35ea6fbe07cac31605755fef28b8a94b2979af"
    )


@pytest.mark.parametrize("bottom", [False, True])
def test_svg_native_land_and_offset_hole_use_board_geometry(bottom):
    pad, _ = _placed(_raw(), 23.7, bottom, True)
    root = ET.fromstring("<svg>" + svg._pad_el(pad, fill="black") + "</svg>")
    land, hole = root
    assert land.tag == "rect"
    angle = 23.7 - 37 if bottom else 23.7 + 37
    assert land.attrib["transform"] == f"rotate({svg._fmt(-angle % -360)} 10 10)"
    assert float(hole.attrib["cx"]) == pytest.approx(pad["drill_x"], abs=0.001)
    assert float(hole.attrib["cy"]) == pytest.approx(pad["drill_y"], abs=0.001)
