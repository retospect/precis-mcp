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
