"""Gerber X2 / Excellon writer — round-trip a small synthetic copper model
to text and check the RS-274X/Excellon shape (headers, aperture dedup,
flashes, arcs-as-arcs, pour regions, tool tables), never a real board file.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO

from precis.pcb import gerber

# ── a small synthetic 2-layer board: two tracks sharing a width (dedup),
# one arc segment, a pour on B.Cu, a via, pads on both outer layers, an
# outline, and a bit of top silkscreen ─────────────────────────────────
_MODEL = {
    "layers": ["F.Cu", "B.Cu"],
    "outline": [[0.0, 0.0], [20.0, 0.0], [20.0, 15.0], [0.0, 15.0]],
    "copper": [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "GND",
            "width_mm": 0.25,
            "segments": [
                {"shape": "line", "start": [1.0, 1.0], "end": [5.0, 1.0]},
            ],
        },
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "VCC",
            "width_mm": 0.25,  # same width as the GND track above -> 1 aperture
            "segments": [
                {"shape": "line", "start": [1.0, 3.0], "end": [4.0, 3.0]},
                {
                    "shape": "arc",
                    "start": [4.0, 3.0],
                    "end": [6.0, 5.0],
                    "center": [4.0, 5.0],
                    "cw": True,
                },
            ],
        },
        {
            "ctype": "pour",
            "layer": "B.Cu",
            "net": "GND",
            "polygon": [[0.5, 0.5], [10.0, 0.5], [10.0, 10.0], [0.5, 10.0]],
        },
        {
            "ctype": "via",
            "net": "GND",
            "x": 8.0,
            "y": 8.0,
            "dia_mm": 0.6,
            "drill_mm": 0.3,
        },
    ],
    "pads": [
        {
            "layer": "F.Cu",
            "net": "VCC",
            "shape": "circle",
            "x": 2.0,
            "y": 2.0,
            "w": 0.8,
        },
        {
            "layer": "B.Cu",
            "net": "GND",
            "shape": "rect",
            "x": 3.0,
            "y": 3.0,
            "w": 1.0,
            "h": 0.6,
        },
    ],
    "silkscreen": {
        "top": [
            {
                "width_mm": 0.15,
                "segments": [
                    {"shape": "line", "start": [0.0, 14.0], "end": [5.0, 14.0]}
                ],
            }
        ],
        "bottom": [],
    },
    "drills": [
        {"x": 1.0, "y": 1.0, "dia_mm": 3.2, "plated": False},  # mounting hole
    ],
}


# ── format header / units ───────────────────────────────────────────
def test_every_gerber_has_format_and_units():
    for content in gerber.export_gerbers(_MODEL, name="brd").values():
        assert "%FSLAX46Y46*%" in content
        assert "%MOMM*%" in content
        assert content.strip().endswith("M02*")


def test_x2_file_function_identifies_layers():
    files = gerber.export_gerbers(_MODEL, name="brd")
    assert "%TF.FileFunction,Copper,L1,Top*%" in files["brd-F_Cu.gbr"]
    assert "%TF.FileFunction,Copper,L2,Bot*%" in files["brd-B_Cu.gbr"]
    assert "%TF.FileFunction,Soldermask,Top*%" in files["brd-F_Mask.gbr"]
    assert "%TF.FileFunction,Soldermask,Bot*%" in files["brd-B_Mask.gbr"]
    assert "%TF.FileFunction,Legend,Top*%" in files["brd-F_Silkscreen.gbr"]
    assert "%TF.FileFunction,Profile,NP*%" in files["brd-Edge_Cuts.gbr"]


# ── aperture dedup ──────────────────────────────────────────────────
def test_aperture_dedup_same_width_one_definition():
    top = gerber.copper_gerber(_MODEL, "F.Cu")
    # both F.Cu tracks are width_mm=0.25 -> exactly one 0.2500 circle aperture
    defs = re.findall(r"%ADD\d+C,0\.2500\*%", top)
    assert len(defs) == 1


# ── flash for a pad ─────────────────────────────────────────────────
def test_pad_emits_a_flash():
    top = gerber.copper_gerber(_MODEL, "F.Cu")
    assert "D03*" in top
    # the VCC pad flash sits at (2.0, 2.0) mm = 2000000 units (1e6/mm)
    assert "X2000000Y2000000D03*" in top


def test_soldermask_opening_is_expanded_pad():
    mask = gerber.soldermask_gerber(_MODEL, "top")
    # 0.8mm pad + 2*0.05mm expansion = 0.9mm aperture
    assert "%ADD10C,0.9000*%" in mask
    assert "D03*" in mask


# ── arcs are real arcs, not polyline approximations ─────────────────
def test_arc_emits_g02_or_g03_not_many_segments():
    top = gerber.copper_gerber(_MODEL, "F.Cu")
    arc_ops = re.findall(r"X\d+Y\d+I-?\d+J-?\d+D01\*", top)
    assert len(arc_ops) == 1  # exactly one arc interpolation command
    assert "G02*" in top  # cw=True -> clockwise


def test_arc_offsets_point_from_start_to_center():
    top = gerber.copper_gerber(_MODEL, "F.Cu")
    # arc start (4,3), center (4,5) -> I=0, J=2mm -> J2000000, I0
    assert "I0J2000000D01*" in top


# ── pour region ──────────────────────────────────────────────────────
def test_pour_emits_g36_g37_region():
    bottom = gerber.copper_gerber(_MODEL, "B.Cu")
    assert "G36*" in bottom and "G37*" in bottom
    g36 = bottom.index("G36*")
    g37 = bottom.index("G37*")
    assert g36 < g37


def test_via_flashes_on_every_span_layer_by_default():
    top = gerber.copper_gerber(_MODEL, "F.Cu")
    bottom = gerber.copper_gerber(_MODEL, "B.Cu")
    # the via has no explicit span/layers -> through via -> flashed on both
    assert "X8000000Y8000000D03*" in top
    assert "X8000000Y8000000D03*" in bottom


def test_via_span_restricts_flash_layers():
    model = {**_MODEL, "layers": ["F.Cu", "In1.Cu", "B.Cu"]}
    copper = _MODEL["copper"]
    assert isinstance(copper, list)
    model = {
        **model,
        "copper": [
            *copper[:3],
            {
                "ctype": "via",
                "net": "GND",
                "x": 8.0,
                "y": 8.0,
                "dia_mm": 0.6,
                "drill_mm": 0.3,
                "span": ["F.Cu", "In1.Cu"],
            },
        ],
    }
    bottom = gerber.copper_gerber(model, "B.Cu")
    assert "X8000000Y8000000D03*" not in bottom  # blind via never reaches B.Cu


# ── outline ──────────────────────────────────────────────────────────
def test_outline_traces_closed_polygon():
    edge = gerber.outline_gerber(_MODEL)
    assert "X0Y0D02*" in edge  # starts at the first outline point
    # closes back to the start
    assert edge.count("X0Y0") >= 2


# ── silkscreen ───────────────────────────────────────────────────────
def test_silkscreen_top_has_stroke_bottom_is_empty():
    top = gerber.silkscreen_gerber(_MODEL, "top")
    bottom = gerber.silkscreen_gerber(_MODEL, "bottom")
    assert "D01*" in top
    assert "D01*" not in bottom and "D02*" not in bottom


# ── Excellon ─────────────────────────────────────────────────────────
def test_excellon_tool_table_and_plated_split():
    files = gerber.excellon_files(_MODEL, name="brd")
    assert set(files) == {"brd-PTH.drl", "brd-NPTH.drl"}
    pth = files["brd-PTH.drl"]
    npth = files["brd-NPTH.drl"]
    assert "M48" in pth and "METRIC" in pth
    # the one via (0.3mm drill) is the sole plated tool
    assert "T01C0.3000" in pth
    assert "X8.0000Y8.0000" in pth
    # the one drills[] entry (plated=False, 3.2mm) lands in NPTH only
    assert "T01C3.2000" in npth
    assert "X1.0000Y1.0000" in npth
    assert "3.2000" not in pth


def test_excellon_coordinates_match_gerber_units():
    files = gerber.excellon_files(_MODEL, name="brd")
    top = gerber.copper_gerber(_MODEL, "F.Cu")
    # same via position, 8.0mm, in both the drill file (decimal mm) and the
    # gerber (mm * 1e6 units) — same underlying coordinate frame.
    assert "X8.0000Y8.0000" in files["brd-PTH.drl"]
    assert "X8000000Y8000000" in top


# ── bundle + zip ─────────────────────────────────────────────────────
def test_export_fab_bundles_gerbers_and_drills():
    files = gerber.export_fab(_MODEL, name="brd")
    assert "brd-F_Cu.gbr" in files
    assert "brd-PTH.drl" in files
    assert "brd-NPTH.drl" in files


def test_zip_fab_round_trips_all_files():
    files = gerber.export_fab(_MODEL, name="brd")
    blob = gerber.zip_fab(files)
    with zipfile.ZipFile(BytesIO(blob)) as zf:
        names = set(zf.namelist())
        assert names == set(files)
        for fname, content in files.items():
            assert zf.read(fname).decode("utf-8") == content
