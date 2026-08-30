"""Gerber X2 / Excellon writer — round-trip a small synthetic copper model
to text and check the RS-274X/Excellon shape (headers, aperture dedup,
flashes, arcs-as-arcs, pour regions, tool tables), never a real board file.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from typing import Any

from precis.pcb import gerber

# ── a small synthetic 2-layer board: two tracks sharing a width (dedup),
# one arc segment, a pour on B.Cu, a via, pads on both outer layers, an
# outline, and a bit of top silkscreen ─────────────────────────────────
_MODEL: dict[str, Any] = {
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


# ── a second small model, purpose-built for the X2 object-attribute
# tests: two same-layer pads with DIFFERENT identity (one carries a
# refdes/pin, one doesn't) so leakage between them is actually checkable,
# plus a track and a through via for the net-only cases ───────────────
_X2_MODEL: dict[str, Any] = {
    "layers": ["F.Cu", "B.Cu"],
    "outline": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
    "copper": [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "GND",
            "width_mm": 0.2,
            "segments": [{"shape": "line", "start": [1.0, 1.0], "end": [3.0, 1.0]}],
        },
        {
            "ctype": "via",
            "net": "VCC",
            "x": 5.0,
            "y": 5.0,
            "dia_mm": 0.5,
            "drill_mm": 0.25,
        },
    ],
    "pads": [
        {
            "layer": "F.Cu",
            "net": "3V3",
            "shape": "circle",
            "x": 2.0,
            "y": 2.0,
            "w": 0.6,
            "refdes": "U1",
            "pin": "3",
        },
        {  # no refdes/pin -> net identity only, must not fabricate a %TO.P
            "layer": "F.Cu",
            "net": "GND",
            "shape": "circle",
            "x": 4.0,
            "y": 4.0,
            "w": 0.6,
        },
    ],
}


# ── X2 object attributes: %TO.N / %TO.P / %TD ───────────────────────
def test_track_carries_its_net_object_attribute():
    top = gerber.copper_gerber(_X2_MODEL, "F.Cu")
    assert "%TO.N,GND*%" in top


def test_via_carries_its_net_object_attribute():
    top = gerber.copper_gerber(_X2_MODEL, "F.Cu")
    assert "%TO.N,VCC*%" in top


def test_pad_with_refdes_and_pin_carries_component_pin_attribute():
    top = gerber.copper_gerber(_X2_MODEL, "F.Cu")
    assert "%TO.P,U1,3*%" in top


def test_pad_without_refdes_gets_no_fabricated_pin_attribute():
    top = gerber.copper_gerber(_X2_MODEL, "F.Cu")
    # exactly one pad in this model carries refdes/pin -- the other must
    # not silently inherit or invent one.
    assert top.count("%TO.P,") == 1


def test_td_clears_the_pad_pin_before_the_next_pads_flash():
    """The leak this whole mechanism exists to prevent: the U1.3 pad's
    %TO.P must be closed with %TD*% strictly BEFORE the second pad's own
    D03 flash, not merely appear somewhere in the file."""
    top = gerber.copper_gerber(_X2_MODEL, "F.Cu")
    to_p_idx = top.index("%TO.P,U1,3*%")
    td_idx = top.index("%TD*%", to_p_idx)
    second_pad_flash_idx = top.index("X4000000Y4000000D03*")  # the net=GND pad
    assert to_p_idx < td_idx < second_pad_flash_idx


def test_pour_hole_does_not_inherit_the_pours_net_attribute():
    """A pour's antipad is where its own net's copper explicitly ISN'T --
    tagging the hole ring with the pour's net would mislabel it."""
    pour = {
        "ctype": "pour",
        "layer": "F.Cu",
        "net": "GND",
        "polygon": [[0.5, 0.5], [9.5, 0.5], [9.5, 9.5], [0.5, 9.5]],
        "holes": [[[4.0, 4.0], [6.0, 4.0], [6.0, 6.0], [4.0, 6.0]]],
    }
    model = {**_X2_MODEL, "copper": [pour]}
    top = gerber.copper_gerber(model, "F.Cu")
    to_n_idx = top.index("%TO.N,GND*%")
    td_idx = top.index("%TD*%", to_n_idx)
    hole_start_idx = top.index("%LPC*%")
    assert to_n_idx < td_idx < hole_start_idx
    # and no SECOND %TO.N appears between the clear and the hole's own draw
    assert "%TO.N" not in top[td_idx:hole_start_idx]


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


def test_silkscreen_region_shape_emits_a_g36_g37_fill():
    """A silk draw carrying `"shape": "region"` must ride the SAME
    G36/G37 writer a copper pour uses -- silk.py::build_sn_patch's box,
    reused rather than a second region writer."""
    model = {
        **_MODEL,
        "silkscreen": {
            "top": [
                {
                    "shape": "region",
                    "polygon": [[0.0, 0.0], [5.0, 0.0], [5.0, 3.0], [0.0, 3.0]],
                }
            ],
            "bottom": [],
        },
    }
    top = gerber.silkscreen_gerber(model, "top")
    assert "G36*" in top and "G37*" in top
    assert top.index("G36*") < top.index("G37*")


def test_silkscreen_clear_polarity_stroke_is_wrapped_lpc_then_lpd():
    """The knockout idiom, at the writer level: a stroke carrying
    `"polarity": "clear"` must be wrapped in %LPC*% ... %LPD*%, and
    polarity must be back to DARK immediately after -- never left clear
    for whatever silk draws next. This is the exact assertion a knockout
    that is never actually clear-polarity would fail."""
    model = {
        **_MODEL,
        "silkscreen": {
            "top": [
                {
                    "shape": "region",
                    "polygon": [[0.0, 0.0], [5.0, 0.0], [5.0, 3.0], [0.0, 3.0]],
                },
                {
                    "width_mm": 0.3,
                    "polarity": "clear",
                    "segments": [
                        {"shape": "line", "start": [1.0, 1.0], "end": [2.0, 2.0]}
                    ],
                },
            ],
            "bottom": [],
        },
    }
    top = gerber.silkscreen_gerber(model, "top")
    g37_idx = top.index("G37*")
    lpc_idx = top.index("%LPC*%")
    draw_idx = top.index("X1000000Y1000000D02*")
    lpd_idx = top.index("%LPD*%", lpc_idx)
    assert g37_idx < lpc_idx < draw_idx < lpd_idx
    # exactly one clear draw -> exactly one polarity round trip, no
    # trailing clear state leaked onto whatever a caller appends next
    assert top.count("%LPC*%") == 1
    assert top.count("%LPD*%") == 1


def test_silkscreen_plain_stroke_never_gets_a_polarity_token():
    """A draw with no "polarity" key must render exactly as before this
    shape existed -- no stray %LPC*%/%LPD*% around ordinary silk ink."""
    top = gerber.silkscreen_gerber(_MODEL, "top")
    assert "%LPC*%" not in top and "%LPD*%" not in top


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
