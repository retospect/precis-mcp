"""SVG rendering — round-trip a small synthetic copper model (the same
shape ``test_pcb_gerber.py`` uses) and a synthetic IR through
:mod:`precis.pcb.svg`, checking well-formedness, true arcs, deterministic
ordering, subset rendering, the scale bar, and layer distinguishability
without colour. Never a real board file.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from precis.pcb import ir as pcb_ir
from precis.pcb import svg

# A small synthetic 2-layer board mirroring test_pcb_gerber.py's _MODEL:
# two tracks (one with an arc segment), a pour, a via, pads on both outer
# layers, an outline, and top silkscreen.
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
            "width_mm": 0.25,
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
            "ctype": "track",
            "layer": "B.Cu",
            "net": "SDA",
            "width_mm": 0.2,
            "segments": [
                {"shape": "line", "start": [2.0, 8.0], "end": [8.0, 8.0]},
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
    # An UNPLATED mounting hole: no pad, no via, no copper of any kind
    # around it. Nothing else in this model draws at (17, 12), so if the
    # renderer ignores `drills` the hole is simply absent from the figure.
    "drills": [{"x": 17.0, "y": 12.0, "dia_mm": 3.2, "plated": False}],
}


def _parse(svg_text: str) -> ET.Element:
    return ET.fromstring(svg_text)


# ── well-formedness ─────────────────────────────────────────────────
def test_render_board_is_well_formed_svg():
    text = svg.render_board(_MODEL)
    root = _parse(text)  # raises ET.ParseError on malformed XML
    assert root.tag.endswith("svg")
    assert "viewBox" in root.attrib


def test_render_sketch_is_well_formed_svg():
    ir = pcb_ir.from_graph(_GRAPH)
    text = svg.render_sketch(ir)
    root = _parse(text)
    assert root.tag.endswith("svg")


# ── arcs render as true SVG arcs, not a polyline approximation ──────
def test_arc_segment_emits_svg_arc_command():
    text = svg.render_board(_MODEL, include={"copper"})
    arcs = re.findall(r"A [\d.]+ [\d.]+ 0 [01] [01] [\d.,]+", text)
    assert len(arcs) == 1
    # a straight-line segment in the SAME model must still render as an
    # ordinary line command, not swept into the arc regex.
    assert " L " in text


# ── deterministic ordering: re-rendering is byte-identical ──────────
def test_render_board_is_byte_identical_across_calls():
    a = svg.render_board(_MODEL)
    b = svg.render_board(_MODEL)
    assert a == b


def test_render_board_order_independent_of_input_list_order():
    """Shuffling the model's copper list must not change the output —
    the renderer sorts internally rather than trusting caller order."""
    shuffled = {**_MODEL, "copper": list(reversed(_MODEL["copper"]))}
    assert svg.render_board(_MODEL) == svg.render_board(shuffled)


def test_render_sketch_is_byte_identical_across_calls():
    ir_a = pcb_ir.from_graph(_GRAPH)
    ir_b = pcb_ir.from_graph(_GRAPH)
    assert svg.render_sketch(ir_a) == svg.render_sketch(ir_b)


# ── subset rendering ─────────────────────────────────────────────────
def test_copper_only_excludes_silk_and_outline():
    text = svg.render_board(_MODEL, include={"copper", "pours", "vias", "pads"})
    assert 'stroke-width="0.15"' not in text  # the silk stroke's width
    assert "M 0,0 L 20,0" not in text  # the outline path's start
    both = svg.render_board(_MODEL)  # sanity: both DO appear when included
    assert 'stroke-width="0.15"' in both
    assert "M 0,0 L 20,0" in both


def test_an_unplated_mounting_hole_is_drawn_and_is_not_a_solid_disc():
    """A bare hole has no annulus to reveal it, so a renderer that ignores
    ``drills`` shows nothing there — and the failure looks like a clean
    board rather than a missing feature. Measured before this was wired:
    two solder-on nuts with 3.2mm holes rendered as solid discs."""
    text = svg.render_board(_MODEL)
    hole = re.search(r'<circle cx="17(?:\.0)?" cy="12(?:\.0)?" r="1\.6"[^/]*/>', text)
    assert hole is not None, "the 3.2mm mounting hole was not drawn at all"
    assert 'fill="#ffffff"' in hole.group(0), "a hole must not read as copper"
    assert "stroke" in hole.group(0), "a white hole on a white page needs an edge"

    without = svg.render_board(_MODEL, include=svg.DEFAULT_INCLUDE - {"drills"})
    assert re.search(r'cx="17(?:\.0)?" cy="12(?:\.0)?"', without) is None


def test_silk_only_excludes_copper():
    text = svg.render_board(_MODEL, include={"silk"})
    assert "0,14" in text or "0,14.0" in text  # the silk stroke's start point
    # none of the copper track colours (non-black layer hues) should appear
    for hue in svg._LAYER_PALETTE[:2]:
        assert hue not in text


def test_single_layer_excludes_other_layer_geometry():
    both = svg.render_board(_MODEL, include={"copper"})
    f_only = svg.render_board(_MODEL, include={"copper"}, layers=["F.Cu"])
    assert "8,8" in both  # the B.Cu SDA track endpoint
    assert "8,8" not in f_only


# ── scale bar ─────────────────────────────────────────────────────────
def test_scale_bar_present_and_can_be_disabled():
    with_bar = svg.render_board(_MODEL)
    without_bar = svg.render_board(_MODEL, scale_bar=False)
    assert 'class="scale-bar"' in with_bar
    assert 'class="scale-bar"' not in without_bar
    assert "mm</text>" in with_bar


# ── layer distinguishability without colour ─────────────────────────
def test_layers_distinguishable_without_colour():
    """F.Cu and B.Cu tracks must differ in dash pattern (or its absence),
    not only in hue — the greyscale/colourblind requirement."""
    text = svg.render_board(_MODEL, include={"copper"})
    paths = re.findall(r"<path [^>]*/>", text)
    f_cu = [p for p in paths if svg._LAYER_PALETTE[0] in p]
    b_cu = [p for p in paths if svg._LAYER_PALETTE[1] in p]
    assert f_cu and b_cu
    f_has_dash = any("stroke-dasharray" in p for p in f_cu)
    b_has_dash = any("stroke-dasharray" in p for p in b_cu)
    assert f_has_dash != b_has_dash  # one solid, one dashed at minimum


def test_pour_hatch_pattern_defined_per_layer():
    text = svg.render_board(_MODEL, include={"pours"})
    assert "<pattern" in text
    assert 'fill="url(#pcb-hatch-1)"' in text  # the pour sits on B.Cu (index 1)


# ── palette override ─────────────────────────────────────────────────
def test_custom_palette_applied():
    text = svg.render_board(_MODEL, include={"copper"}, palette={"F.Cu": "#123456"})
    assert "#123456" in text


# ── L3 sketch: positions + straight-line connections + layer colour ──
_GRAPH: dict[str, Any] = {
    "instances": [
        {"refdes": "U1", "x": 0.0, "y": 0.0},
        {"refdes": "R1", "x": 5.0, "y": 0.0},
        {"refdes": "C1", "x": 0.0, "y": 5.0},
    ],
    "nets": [
        {
            "name": "SIG",
            "members": [{"refdes": "U1", "pin": "A"}, {"refdes": "R1", "pin": "1"}],
        },
        {
            "name": "GND",
            "members": [{"refdes": "U1", "pin": "G"}, {"refdes": "C1", "pin": "1"}],
        },
    ],
}


def test_sketch_draws_a_line_per_segment_and_a_marker_per_placed_instance():
    ir = pcb_ir.from_graph(_GRAPH)
    text = svg.render_sketch(ir)
    lines = re.findall(r"<line ", text)
    # 2 nets -> 2 segments (one spoke each, star-decomposed off a 2-member
    # net) + the scale bar's 3 lines (shaft + 2 ticks).
    assert len(lines) == 5
    assert text.count("<rect ") == 3  # one marker per instance
    assert "U1" in text and "R1" in text and "C1" in text


def test_sketch_unassigned_layer_uses_the_unassigned_style():
    ir = pcb_ir.from_graph(_GRAPH)  # from_graph never sets seg_layer
    text = svg.render_sketch(ir)
    assert svg._UNASSIGNED_STROKE in text


def test_sketch_layer_assignment_changes_stroke_and_dash():
    ir = pcb_ir.from_graph(_GRAPH)
    ir.set_layer(0, 1)  # assign the first segment to layer index 1
    text = svg.render_sketch(ir)
    assert svg._LAYER_PALETTE[1] in text
    assert svg._UNASSIGNED_STROKE in text  # the OTHER segment is still unassigned


def test_sketch_skips_unplaced_instances():
    graph = {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "U2"},  # no x/y -> unplaced
        ],
        "nets": [],
    }
    ir = pcb_ir.from_graph(graph)
    text = svg.render_sketch(ir)
    assert "U1" in text
    assert "U2" not in text
