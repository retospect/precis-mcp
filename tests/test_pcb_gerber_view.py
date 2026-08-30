"""`precis.pcb.gerber_view` — read back what we hand the fab.

The valuable test here is a ROUND TRIP: geometry the writer emitted, read
by an independent parser, compared to what went in. A reader tested only
against hand-written gerber strings would agree with the writer's bugs;
a writer tested only against its own reader would too. Going out and back
and checking against the ORIGINAL model is what catches either.
"""

from __future__ import annotations

import math
import re
from typing import Any

import pytest

from precis.pcb import gerber, gerber_view

_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def _model(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "layers": _LAYERS,
        "outline": [[0, 0], [30, 0], [30, 20], [0, 20]],
        "copper": [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "N",
                "width_mm": 0.25,
                "segments": [
                    {"shape": "line", "start": [2.0, 2.0], "end": [10.0, 2.0]},
                    {"shape": "line", "start": [10.0, 2.0], "end": [10.0, 9.0]},
                ],
            },
            {
                "ctype": "via",
                "net": "N",
                "x": 10.0,
                "y": 9.0,
                "dia_mm": 0.6,
                "drill_mm": 0.3,
                "span": ["F.Cu", "B.Cu"],
            },
        ],
        "pads": [
            {
                "layer": "F.Cu",
                "net": "N",
                "shape": "circle",
                "x": 2.0,
                "y": 2.0,
                "w": 0.9,
                "h": 0.9,
            }
        ],
    }
    base.update(over)
    return base


def _art(model: dict[str, Any], layer_file: str) -> gerber_view.LayerArt:
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    return gerber_view.parse_gerber(files[f"t-{layer_file}.gbr"])


def test_a_track_survives_the_round_trip_at_its_own_width() -> None:
    art = _art(_model(), "F_Cu")
    tracks = [s for s in art.strokes if math.isclose(s.width, 0.25, abs_tol=1e-6)]
    assert len(tracks) == 1
    assert tracks[0].points[0] == pytest.approx((2.0, 2.0))
    assert tracks[0].points[-1] == pytest.approx((10.0, 9.0))


def test_pads_reach_the_copper_and_the_mask() -> None:
    """The fab-output defect, from the far side. The gerbers used to carry
    no pad at all — mask files were header-only, ten lines and zero
    apertures — so the traces connected to nothing and the board was
    unsolderable. The mask opening is deliberately LARGER than the pad
    (``SOLDERMASK_EXPANSION_MM`` of swell per side)."""
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    copper = gerber_view.parse_gerber(files["t-F_Cu.gbr"])
    mask = gerber_view.parse_gerber(files["t-F_Mask.gbr"])

    pad = [f for f in copper.flashes if math.isclose(f.aperture.sizes[0], 0.9)]
    assert len(pad) == 1
    assert (pad[0].x, pad[0].y) == pytest.approx((2.0, 2.0))

    assert len(mask.flashes) == 1
    assert mask.flashes[0].aperture.sizes[0] == pytest.approx(
        0.9 + 2 * gerber.SOLDERMASK_EXPANSION_MM
    )


def test_a_via_is_flashed_on_both_spanned_layers() -> None:
    top = _art(_model(), "F_Cu")
    bottom = _art(_model(), "B_Cu")
    for art in (top, bottom):
        assert any(
            math.isclose(f.aperture.sizes[0], 0.6) and (f.x, f.y) == (10.0, 9.0)
            for f in art.flashes
        )


def test_a_pour_hole_reads_back_as_clear_polarity() -> None:
    """A plane's antipads are the difference between a plane and a short.
    They ride out as ``%LPC*%`` regions and have to come back distinguished
    from copper — a reader that lost the polarity would render, and a
    viewer would show, a solid sheet over every foreign via."""
    pour = {
        "ctype": "pour",
        "layer": "In1.Cu",
        "net": "GND",
        "polygon": [[1, 1], [29, 1], [29, 19], [1, 19]],
        "holes": [[[10, 10], [12, 10], [12, 12], [10, 12]]],
    }
    model = _model(copper=[pour])
    art = _art(model, "In1_Cu")
    assert [r.solid for r in art.regions] == [True, False]
    assert len(art.regions[1].ring) >= 4


def test_drill_holes_round_trip_with_their_diameters() -> None:
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    holes = gerber_view.parse_excellon(files["t-PTH.drl"])
    assert len(holes) == 1
    x, y, dia = holes[0]
    assert (x, y) == pytest.approx((10.0, 9.0))
    assert dia == pytest.approx(0.3)


def test_the_svg_carries_one_toggleable_group_per_layer() -> None:
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, title="t")
    for key in ("F_Cu", "B_Cu", "In1_Cu", "Edge_Cuts", "F_Mask", "PTH"):
        assert f'id="layer-{key}"' in svg
        assert f'data-layer="{key}"' in svg
    assert svg.startswith("<svg")
    assert "</svg>" in svg
    # Gerber's origin is bottom-left and SVG's is top-left. Getting this
    # wrong renders a mirrored board that looks entirely plausible — which
    # is why it is asserted rather than eyeballed. The y factor must be the
    # NEGATIVE of the x factor: equal magnitudes (an anisotropic scale
    # would distort the board) and opposite signs (the flip).
    m = re.search(r"scale\((\d+(?:\.\d+)?),-(\d+(?:\.\d+)?)\)", svg)
    assert m is not None, svg[:400]
    assert m.group(1) == m.group(2)


def _hex_brightness(hexcolour: str) -> float:
    hexcolour = hexcolour.lstrip("#")
    r, g, b = (int(hexcolour[i : i + 2], 16) for i in (0, 2, 4))
    return (r + g + b) / 3.0


def test_drill_holes_render_as_a_visible_void_not_a_dark_fill() -> None:
    """The bug, from the user's side: the PTH group is present (a `<circle>`
    for every via drill) but was painted ``#101010`` on a ``#12141a``
    document background -- a hole that "renders" and is still invisible. A
    presence check does not catch that; this compares the rendered fill
    against the document background's own brightness and requires a real
    gap, not merely a distinct hex string."""
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, title="t")

    bg_m = re.search(r'<rect[^>]*fill="(#[0-9a-fA-F]{6})"/>', svg)
    assert bg_m is not None, svg[:400]
    bg_brightness = _hex_brightness(bg_m.group(1))

    layer_m = re.search(r'<g id="layer-PTH"[^>]*>(.*?)</g>', svg, re.DOTALL)
    assert layer_m is not None, svg[:400]
    circle_m = re.search(r'<circle[^>]*fill="(#[0-9a-fA-F]{6})"', layer_m.group(1))
    assert circle_m is not None, layer_m.group(1)
    hole_brightness = _hex_brightness(circle_m.group(1))

    # Not just "a different colour" -- a real, eyeball-visible gap. The old
    # PTH fill (#101010, brightness ~16) sat within a few percent of the
    # #12141a background (~19); this asserts an order-of-magnitude gap.
    assert abs(hole_brightness - bg_brightness) > 100


def test_npth_drill_is_dashed_pth_is_not() -> None:
    """Plated and unplated holes are drilled on separate passes and mean
    different things -- the fill alone (both render as the same void
    colour) cannot tell them apart, so the stroke must: solid for a plated
    via/pad hole, dashed for an unplated mechanical hole, the same cue
    `precis.pcb.svg._drill_el` already uses."""
    model = _model(drills=[{"x": 5.0, "y": 5.0, "dia_mm": 3.2, "plated": False}])
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    assert "t-NPTH.drl" in files
    svg = gerber_view.render_fab_svg(files, title="t")

    pth = re.search(r'<g id="layer-PTH"[^>]*>(.*?)</g>', svg, re.DOTALL)
    npth = re.search(r'<g id="layer-NPTH"[^>]*>(.*?)</g>', svg, re.DOTALL)
    assert pth is not None and npth is not None
    assert "stroke-dasharray" not in pth.group(1)
    assert "stroke-dasharray" in npth.group(1)


def test_legend_labels_the_drill_layers_as_drills_not_gerbers() -> None:
    """`i know its not a gerber but it'd be neat to have it all in one
    place` -- the legend row must say so, tersely, not just repeat the
    bare gerber-style layer key."""
    model = _model(drills=[{"x": 5.0, "y": 5.0, "dia_mm": 3.2, "plated": False}])
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, title="t")
    # ">PTH (drill)<" (not merely "PTH (drill)" as a substring) so this
    # doesn't pass on "N" + "PTH (drill)" alone.
    assert ">PTH (drill)<" in svg
    assert ">NPTH (drill)<" in svg
    # and the un-glossed bare key never appears as its own legend label
    assert ">PTH<" not in svg
    assert ">NPTH<" not in svg


def test_an_unreadable_construct_raises_instead_of_rendering_a_gap() -> None:
    """A viewer that silently drops what it cannot read shows a clean board
    with a missing feature — the exact failure this module exists to
    avoid."""
    with pytest.raises(gerber_view.UnsupportedGerber):
        gerber_view.parse_gerber("%FSLAX46Y46*%\n%MOMM*%\n%AMDONUT*\n1,1,1,0,0*%\nM02*")


def test_omitted_coordinates_repeat_rather_than_reset_to_zero() -> None:
    """Modal coordinates are legal and common. A reader that assumed zero
    would fold the board onto its own axes — and would do it quietly."""
    art = gerber_view.parse_gerber(
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.2000*%\nG01*\nD10*\n"
        "X1000000Y2000000D02*\nX5000000D01*\nY7000000D01*\nM02*\n"
    )
    assert len(art.strokes) == 1
    assert art.strokes[0].points == [
        pytest.approx((1.0, 2.0)),
        pytest.approx((5.0, 2.0)),
        pytest.approx((5.0, 7.0)),
    ]


# ── X2 object attributes: identity lands on the RIGHT primitive ──────
def test_track_net_survives_the_round_trip_on_its_own_stroke() -> None:
    art = _art(_model(), "F_Cu")
    tracks = [s for s in art.strokes if math.isclose(s.width, 0.25, abs_tol=1e-6)]
    assert len(tracks) == 1
    assert tracks[0].net == "N"


def test_via_net_survives_on_both_spanned_layers() -> None:
    top = _art(_model(), "F_Cu")
    bottom = _art(_model(), "B_Cu")
    for art in (top, bottom):
        via = [f for f in art.flashes if math.isclose(f.aperture.sizes[0], 0.6)]
        assert len(via) == 1
        assert via[0].net == "N"


def test_pad_pin_identity_survives_round_trip_when_the_model_supplies_it() -> None:
    model = _model()
    model["pads"][0]["refdes"] = "U1"
    model["pads"][0]["pin"] = "3"
    art = _art(model, "F_Cu")
    pad = [f for f in art.flashes if (f.x, f.y) == pytest.approx((2.0, 2.0))]
    assert len(pad) == 1
    assert pad[0].net == "N"
    assert pad[0].refdes == "U1"
    assert pad[0].pin == "3"


def test_pad_with_no_refdes_gets_net_identity_and_no_pin() -> None:
    art = _art(_model(), "F_Cu")
    pad = [f for f in art.flashes if (f.x, f.y) == pytest.approx((2.0, 2.0))]
    assert len(pad) == 1
    assert pad[0].net == "N"
    assert pad[0].refdes is None and pad[0].pin is None


def test_object_attribute_does_not_leak_onto_the_next_object() -> None:
    """Two pads, different nets/identity, back to back on the same layer:
    the SECOND one must carry its OWN attributes, not the first's -- the
    failure mode worth catching is an attribute surviving past its own
    %TD*% and mislabelling copper it was never on."""
    model = _model(
        pads=[
            {
                "layer": "F.Cu",
                "net": "N",
                "shape": "circle",
                "x": 2.0,
                "y": 2.0,
                "w": 0.9,
                "h": 0.9,
                "refdes": "U1",
                "pin": "1",
            },
            {
                "layer": "F.Cu",
                "net": "M",
                "shape": "circle",
                "x": 6.0,
                "y": 6.0,
                "w": 0.9,
                "h": 0.9,
            },
        ]
    )
    art = _art(model, "F_Cu")
    first = [f for f in art.flashes if (f.x, f.y) == pytest.approx((2.0, 2.0))][0]
    second = [f for f in art.flashes if (f.x, f.y) == pytest.approx((6.0, 6.0))][0]
    assert (first.net, first.refdes, first.pin) == ("N", "U1", "1")
    assert (second.net, second.refdes, second.pin) == ("M", None, None)


def test_pour_net_survives_holes_carry_no_net() -> None:
    pour = {
        "ctype": "pour",
        "layer": "In1.Cu",
        "net": "GND",
        "polygon": [[1, 1], [29, 1], [29, 19], [1, 19]],
        "holes": [[[10, 10], [12, 10], [12, 12], [10, 12]]],
    }
    art = _art(_model(copper=[pour]), "In1_Cu")
    assert [r.solid for r in art.regions] == [True, False]
    assert art.regions[0].net == "GND"
    assert art.regions[1].net is None


def test_parse_gerber_td_clears_attrs_before_the_next_flash() -> None:
    """The reader side of the same guarantee, at the parser level rather
    than through the writer: a %TD*% between two flashes must leave the
    second one with no identity at all."""
    text = (
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.5000*%\nG01*\nD10*\n"
        "%TO.N,GND*%\nX1000000Y1000000D03*\n%TD*%\n"
        "X2000000Y2000000D03*\nM02*\n"
    )
    art = gerber_view.parse_gerber(text)
    assert len(art.flashes) == 2
    assert art.flashes[0].net == "GND"
    assert art.flashes[1].net is None


def test_malformed_net_attribute_raises_not_silently_dropped() -> None:
    with pytest.raises(gerber_view.UnsupportedGerber):
        gerber_view.parse_gerber("%FSLAX46Y46*%\n%MOMM*%\n%TO.N*%\nM02*\n")


def test_malformed_pin_attribute_raises_not_silently_dropped() -> None:
    with pytest.raises(gerber_view.UnsupportedGerber):
        gerber_view.parse_gerber("%FSLAX46Y46*%\n%MOMM*%\n%TO.P,U1*%\nM02*\n")


# ── SVG hover tooltips: every rendered element has a <title>, and every
#    title carries coordinates -- present whether or not the object also
#    carries X2 identity ────────────────────────────────────────────────
def test_every_board_element_has_a_title_with_coordinates() -> None:
    pour = {
        "ctype": "pour",
        "layer": "In1.Cu",
        "net": "GND",
        "polygon": [[1, 1], [29, 1], [29, 19], [1, 19]],
        "holes": [[[10, 10], [12, 10], [12, 12], [10, 12]]],
    }
    model = _model(copper=[*_model()["copper"], pour])
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, title="t")

    start = svg.index('<g transform="translate')
    end = svg.index('<g class="legend">')
    board_svg = svg[start:end]
    elements = re.findall(r"<(?:circle|rect|path)\b", board_svg)
    titles = re.findall(r"<title>([^<]*)</title>", board_svg)
    assert elements, "fixture must actually render something"
    assert len(titles) == len(elements)
    for t in titles:
        assert "mm" in t  # coordinates in mm, on every hoverable object


def test_flash_title_names_a_pad_only_when_the_gerber_said_so() -> None:
    """A round flash with just a net (a via barrel, or a pad the model gave
    no pin) must not be called a "pad" -- that is exactly the kind of
    inference this view exists to avoid; it must read "flash" instead."""
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, title="t")
    assert "· flash ·" in svg  # the via, and the pin-less pad
    model = _model()
    model["pads"][0]["refdes"] = "U1"
    model["pads"][0]["pin"] = "3"
    files2 = gerber.export_fab(model, name="t", allow_synthesized=True)
    svg2 = gerber_view.render_fab_svg(files2, title="t")
    assert "· pad ·" in svg2
    assert "pin U1.3" in svg2


def test_drill_title_has_no_identity_only_layer_diameter_and_coords() -> None:
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, title="t")
    pth_m = re.search(r'<g id="layer-PTH"[^>]*>(.*?)</g>', svg, re.DOTALL)
    assert pth_m is not None
    title_m = re.search(r"<title>([^<]*)</title>", pth_m.group(1))
    assert title_m is not None
    assert "drill" in title_m.group(1)
    assert "mm" in title_m.group(1)
    assert "net" not in title_m.group(1)


# ── deterministic across calls ────────────────────────────────────────
def test_render_fab_svg_is_byte_identical_across_calls() -> None:
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    a = gerber_view.render_fab_svg(files, title="t")
    b = gerber_view.render_fab_svg(files, title="t")
    assert a == b


# ── copper-layer translucency, without breaking antipad rendering ────
def test_copper_layer_groups_get_opacity_other_layers_stay_opaque() -> None:
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, title="t")
    for key in ("F_Cu", "B_Cu"):
        m = re.search(rf'<g id="layer-{key}"[^>]*>', svg)
        assert m is not None, svg[:400]
        assert f'opacity="{gerber_view._COPPER_LAYER_OPACITY}"' in m.group(0)
    for key in ("PTH", "Edge_Cuts", "F_Mask"):
        m = re.search(rf'<g id="layer-{key}"[^>]*>', svg)
        assert m is not None, svg[:400]
        assert "opacity=" not in m.group(0)


def test_pour_and_its_hole_render_as_one_evenodd_cutout() -> None:
    """A real geometric cutout, not the old "paint the hole in the board
    background colour" trick -- with the layer now translucent, an opaque
    background-coloured patch would itself read as a smear over whatever
    is beneath rather than a transparent hole letting it show through. A
    plane rendered without a REAL hole is indistinguishable from a short."""
    pour = {
        "ctype": "pour",
        "layer": "In1.Cu",
        "net": "GND",
        "polygon": [[1, 1], [29, 1], [29, 19], [1, 19]],
        "holes": [[[10, 10], [12, 10], [12, 12], [10, 12]]],
    }
    files = gerber.export_fab(_model(copper=[pour]), name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, title="t")

    layer_m = re.search(r'<g id="layer-In1_Cu"[^>]*>(.*?)</g>', svg, re.DOTALL)
    assert layer_m is not None, svg[:400]
    layer_body = layer_m.group(1)

    paths = re.findall(r"<path\b[^>]*>", layer_body)
    assert len(paths) == 1, "pour + its hole must be ONE compound path"
    assert 'fill-rule="evenodd"' in paths[0]
    d_m = re.search(r'd="([^"]*)"', paths[0])
    assert d_m is not None
    assert d_m.group(1).count("M ") == 2  # outer ring + the one hole ring

    # translucent via the GROUP, not the path itself painting the board
    # background over the hole
    group_m = re.search(r'<g id="layer-In1_Cu"[^>]*>', svg)
    assert group_m is not None
    assert f'opacity="{gerber_view._COPPER_LAYER_OPACITY}"' in group_m.group(0)


# ── silk knockout (S/N patch): a clear-polarity STROKE, not a region,
# cutting a hole in a solid silk fill -- gerber.py::silkscreen_gerber's
# two new draw shapes, read back through gerber_view ──────────────────
def _sn_patch_silk_model() -> dict[str, Any]:
    return {
        "top": [
            {
                "shape": "region",
                "polygon": [[2.0, 2.0], [10.0, 2.0], [10.0, 6.0], [2.0, 6.0]],
            },
            {
                "width_mm": 0.3,
                "polarity": "clear",
                "segments": [{"shape": "line", "start": [3.0, 3.0], "end": [5.0, 5.0]}],
            },
        ],
        "bottom": [],
    }


def test_a_silk_knockout_stroke_reads_back_as_a_hole_not_ink() -> None:
    """The reader-side half of the polarity round trip: a clear-polarity
    STROKE (not just a region) must come back as a hole in the box, and
    must NEVER also survive as an ordinary dark stroke -- either bug
    would be invisible to a presence-only check."""
    model = _model(silkscreen=_sn_patch_silk_model())
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    art = gerber_view.parse_gerber(files["t-F_Silkscreen.gbr"])

    assert [r.solid for r in art.regions] == [True, False]
    assert len(art.regions[0].ring) >= 4  # the box itself, untouched
    assert len(art.regions[1].ring) >= 2  # a real knockout ring, not degenerate
    assert art.strokes == []  # never ALSO rendered as ink


def test_the_svg_renders_the_sn_box_and_its_knockout_as_one_cutout() -> None:
    """The class of bug this whole module exists to catch: a viewer that
    ignores %LPC*% on a stroke would show the box either fully solid
    (polarity silently dropped) or with the letter simply missing
    (silently un-drawn) -- both look plausible and both disagree with the
    gerber. This asserts the ACTUAL rendered shape: one real cutout path,
    same technique as a copper pour's antipad."""
    model = _model(silkscreen=_sn_patch_silk_model())
    files = gerber.export_fab(model, name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(files, title="t")

    layer_m = re.search(r'<g id="layer-F_Silkscreen"[^>]*>(.*?)</g>', svg, re.DOTALL)
    assert layer_m is not None, svg[:400]
    layer_body = layer_m.group(1)

    paths = re.findall(r"<path\b[^>]*>", layer_body)
    assert len(paths) == 1, "the box + its knockout must be ONE compound path"
    assert 'fill-rule="evenodd"' in paths[0]
    d_m = re.search(r'd="([^"]*)"', paths[0])
    assert d_m is not None
    assert d_m.group(1).count("M ") == 2  # the box ring + the one knockout ring

    # silk gets silk-appropriate hover vocabulary, not copper's
    # "pour"/"antipad" (the compound path's title reflects the SOLID
    # half of the pair -- see _region_title's own docstring).
    titles = re.findall(r"<title>([^<]*)</title>", layer_body)
    assert any("silk fill" in t for t in titles)
    assert not any("pour" in t for t in titles)

    # F_Silkscreen never gets the copper translucency group opacity
    group_m = re.search(r'<g id="layer-F_Silkscreen"[^>]*>', svg)
    assert group_m is not None
    assert "opacity=" not in group_m.group(0)


def test_region_title_names_a_silk_hole_a_knockout_not_an_antipad() -> None:
    """A direct check of the wording a real board's orphan/foreign-file
    case would hit (this project's own writer always pairs the hole with
    a preceding solid fill, so the compound-path title above never shows
    this branch) -- the vocabulary has to be right independent of how the
    region got there."""
    hole = gerber_view.Region([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)], solid=False)
    title = gerber_view._region_title("F_Silkscreen", hole)
    assert "silk knockout" in title
    assert "antipad" not in title


def test_export_fab_refuses_synthesized_pads() -> None:
    """`landpattern.py` is explicit that a synthesized pattern is a BOUND
    and "must never be exported to fabrication". A board built from a
    plausible guess at a part solders to nothing, and the discovery
    happens with the assembled board in someone's hand."""
    model = _model()
    model["pads"][0]["synthesized"] = True
    with pytest.raises(gerber.SynthesizedPadError):
        gerber.export_fab(model, name="t")
    assert gerber.export_fab(model, name="t", allow_synthesized=True)
