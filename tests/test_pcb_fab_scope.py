"""Region-scoped fab renders (gr540838).

``view='svg' args={'level':'fab', ...}`` clips the gerber-rendered SVG to
a ``bbox`` or to the union of named ``refdes``/``nets`` extents plus a
margin, drops elements wholly outside so the file is small rather than
cropped, and highlights the named nets. The unscoped render must stay
byte-identical — existing goldens depend on it.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import gerber, gerber_view

_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def _model(**over: Any) -> dict[str, Any]:
    """A spread-out board: two pads at opposite corners plus a track and a
    pour, so a small scope keeps little of it."""
    base: dict[str, Any] = {
        "layers": _LAYERS,
        "outline": [[0, 0], [200, 0], [200, 100], [0, 100]],
        "copper": [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "N",
                "width_mm": 0.25,
                "segments": [
                    {"shape": "line", "start": [2.0, 2.0], "end": [30.0, 2.0]},
                    {"shape": "line", "start": [30.0, 2.0], "end": [30.0, 9.0]},
                ],
            },
            {
                "ctype": "via",
                "net": "N",
                "x": 30.0,
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
                "shape": "rect",
                "x": 2.0,
                "y": 2.0,
                "w": 0.9,
                "h": 0.9,
                "refdes": "R1",
                "pin": "1",
            },
            {
                "layer": "F.Cu",
                "net": "FAR",
                "shape": "rect",
                "x": 190.0,
                "y": 90.0,
                "w": 0.9,
                "h": 0.9,
                "refdes": "R2",
                "pin": "1",
            },
            # A field of far pads so the whole-board file is big enough
            # that dropping it measurably beats the fixed legend/style
            # floor (~7 kB) — the same mechanism a 340 kB real render has.
            *[
                {
                    "layer": "F.Cu",
                    "net": "FAR",
                    "shape": "rect",
                    "x": 40.0 + (i % 10) * 15.0,
                    "y": 20.0 + (i // 10) * 10.0,
                    "w": 0.8,
                    "h": 0.8,
                    "refdes": f"F{i}",
                    "pin": "1",
                }
                for i in range(100)
            ],
        ],
    }
    base.update(over)
    return base


def _files(model: dict[str, Any]) -> dict[str, str]:
    return gerber.export_fab(model, name="t", allow_synthesized=True)


def test_bbox_scope_drops_far_elements_and_clips_the_viewbox() -> None:
    files = _files(_model())
    full = gerber_view.render_fab_svg(files, title="t")
    scoped = gerber_view.render_fab_svg(
        files, title="t", scope={"bbox": [0, 0, 10, 10]}
    )
    assert len(scoped.encode()) < 0.1 * len(full.encode())
    assert "pad 1 of R1 ·" in scoped
    assert "pad 1 of R2" not in scoped
    assert "pad 1 of F50" not in scoped
    # The resolved clip is stamped on the root for an agent to read back.
    assert 'data-scope="0.0000 0.0000 10.0000 10.0000"' in scoped


def test_refdes_scope_unions_extent_and_margin() -> None:
    files = _files(_model())
    scoped = gerber_view.render_fab_svg(
        files, title="t", scope={"refdes": ["R1"], "margin": 2}
    )
    # R1's own objects sit around (2, 2); with a 2mm margin the clip must
    # contain the pad but stay nowhere near R2 at (190, 90).
    m = re.search(r'data-scope="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', scoped)
    assert m is not None
    x0, y0, x1, y1 = (float(v) for v in m.groups())
    assert x0 < 1.55 and y0 < 1.55 and x1 > 2.45 and y1 > 2.45
    assert x1 < 50 and y1 < 50
    assert "pad 1 of R1 ·" in scoped
    assert "pad 1 of R2" not in scoped


def test_nets_scope_highlights_and_keeps_only_the_net() -> None:
    files = _files(_model())
    scoped = gerber_view.render_fab_svg(
        files, title="t", scope={"nets": ["N"], "margin": 1}
    )
    assert 'class="pcb-hl"' in scoped
    assert gerber_view._HL_COLOUR in scoped
    # N's extent: pads/track/via 2..30mm x, 2..9mm y + 1mm margin.
    assert "pad 1 of R2" not in scoped  # FAR net's pad is far away
    assert "pad 1 of R1 ·" in scoped


def test_unknown_names_raise_scope_error_with_the_roster() -> None:
    files = _files(_model())
    with pytest.raises(gerber_view.ScopeError) as ei:
        gerber_view.render_fab_svg(files, scope={"refdes": ["R99"]})
    assert ei.value.kind == "refdes" and "R1" in ei.value.known
    with pytest.raises(gerber_view.ScopeError) as ei2:
        gerber_view.render_fab_svg(files, scope={"nets": ["VCC"]})
    assert ei2.value.kind == "net" and "N" in ei2.value.known


#: An Edge_Cuts file that draws the 200x100 board edge as FOUR strokes
#: (a D02 move before each edge) — the multi-stroke outline a scoped
#: clip can half-drop. FS4.6 mm: 200mm -> 200000000.
_EDGE_CUTS_FOUR_STROKES = (
    "%FSLAX46Y46*%\n"
    "%MOMM*%\n"
    "%ADD10C,0.100000*%\n"
    "D10*\n"
    "X0Y0D02*\nX200000000Y0D01*\n"
    "X200000000Y0D02*\nX200000000Y100000000D01*\n"
    "X200000000Y100000000D02*\nX0Y100000000D01*\n"
    "X0Y100000000D02*\nX0Y0D01*\n"
    "M02*\n"
)


def test_scoped_mask_film_keeps_the_full_outline_shape() -> None:
    """A scope crossing the board edge must not turn the mask film into a
    chord polygon: the film's shape is the UNCLIPPED Edge_Cuts outline,
    bounded to the view — not the few strokes the clip happened to keep."""
    files = _files(_model())
    files["t-Edge_Cuts.gbr"] = _EDGE_CUTS_FOUR_STROKES
    scoped = gerber_view.render_fab_svg(
        files, title="t", scope={"bbox": [190, 0, 210, 20]}
    )
    group = scoped.split('id="layer-F_Mask"', 1)[1].split("</g>", 1)[0]
    films = re.findall(r'd="([^"]+)"[^>]*fill-opacity="0\.55"', group)
    assert films
    for d in films:
        pts = [
            (float(x), float(y)) for x, y in re.findall(r"[ML] ([-\d.]+) ([-\d.]+)", d)
        ]
        # Board ∩ view = [190,0]-[200,20]: the clipped outline's corners.
        assert {(round(x, 3), round(y, 3)) for x, y in pts} == {
            (190.0, 0.0),
            (200.0, 0.0),
            (200.0, 20.0),
            (190.0, 20.0),
        }


def test_fab_scope_rejects_a_bad_bbox() -> None:
    for args, match in (
        ({"bbox": [0, 0, 10]}, "needs \\[x0, y0, x1, y1\\]"),
        ({"bbox": [10, 0, 0, 10]}, "x0<x1 and y0<y1"),
        ({"bbox": [0, 0, "ten", 10]}, "must be \\[x0, y0, x1, y1\\]"),
    ):
        with pytest.raises(BadInput, match=match):
            PcbHandler._fab_scope(args)


def test_unscoped_render_is_byte_identical_to_before() -> None:
    files = _files(_model())
    a = gerber_view.render_fab_svg(files, title="t")
    b = gerber_view.render_fab_svg(files, title="t", scope=None)
    c = gerber_view.render_fab_svg(files, title="t", scope={})
    assert a == b == c


def test_empty_scope_clips_nothing() -> None:
    files = _files(_model())
    scoped = gerber_view.render_fab_svg(files, title="t", scope={"margin": 5})
    assert "pad 1 of R2" in scoped


def _seed_board(store: Any, n: int = 60) -> tuple[Hub, Any]:
    """A board with ``n`` parts strung far apart plus two nets — enough
    silk and pads that a one-part scope is well under 10% of the bytes."""
    hub = Hub(store=store)
    PcbHandler(hub=hub).put(
        id="scope_fixture",
        args={
            "components": [
                {
                    "refdes": f"R{i}",
                    "label": "resistor",
                    "footprint": "local-R",
                    "pins": [{"name": "1", "pad": "1"}],
                    "x": i * 10.0,
                    "y": (i % 4) * 10.0,
                }
                for i in range(1, n + 1)
            ],
            "nets": [{"name": "SIG"}, {"name": "GND"}],
            "connections": [
                {"net": "SIG", "refdes": "R1", "pin": "1"},
                *[
                    {"net": "GND", "refdes": f"R{i}", "pin": "1"}
                    for i in range(2, n + 1)
                ],
            ],
            "footprints": [
                {
                    "name": "local-R",
                    "pads": [
                        {
                            "number": "1",
                            "x": 0,
                            "y": 0,
                            "w": 0.6,
                            "h": 0.6,
                            "shape": "rect",
                        }
                    ],
                }
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id="scope_fixture")
    return hub, ref


def test_handler_scoped_render_small_and_unscoped_unchanged(store) -> None:
    hub, ref = _seed_board(store)
    pcb = PcbHandler(hub=hub)
    full = pcb._render_fab_svg(ref.id, ref.slug).body
    again = pcb._render_fab_svg(ref.id, ref.slug).body
    assert full == again

    scoped = pcb.get(
        id=ref.slug,
        view="svg",
        args={"level": "fab", "refdes": ["R1"]},
    ).body
    assert len(scoped.encode()) < 0.1 * len(full.encode())
    assert "pad 1 of R1 ·" in scoped
    assert "pad 1 of R60" not in scoped
    # The clip box is stamped on the root for an agent to read back.
    assert 'data-scope="' in scoped
    # Byte-identical unscoped render afterwards too — scoping mutates nothing.
    assert pcb._render_fab_svg(ref.id, ref.slug).body == full


def test_handler_net_scope_highlights_and_unknown_name_names_nearest(store) -> None:
    hub, ref = _seed_board(store)
    pcb = PcbHandler(hub=hub)
    scoped = pcb.get(
        id=ref.slug, view="svg", args={"level": "fab", "nets": ["GND"]}
    ).body
    assert 'class="pcb-hl"' in scoped
    assert "pad 1 of R1 ·" not in scoped  # SIG-only pad far from the GND union
    with pytest.raises(BadInput, match="nearest"):
        pcb.get(id=ref.slug, view="svg", args={"level": "fab", "refdes": ["R999"]})
    with pytest.raises(BadInput):
        pcb.get(
            id=ref.slug,
            view="svg",
            args={"level": "fab", "bbox": [0, 0, 10, 10], "refdes": ["R1"]},
        )
    with pytest.raises(BadInput):
        pcb.get(id=ref.slug, view="svg", args={"level": "fab", "bbox": [10, 0, 0, 10]})


def test_scope_error_with_empty_roster_keeps_options(store, monkeypatch) -> None:
    """An unknown refdes on a render with NO attributed objects keeps a
    useful refusal: no dangling "nearest:" clause, and options= names the
    scope arg forms instead of an empty list."""
    hub, ref = _seed_board(store)
    monkeypatch.setattr(gerber_view, "_identity_boxes", lambda arts: ({}, {}))
    with pytest.raises(BadInput) as ei:
        PcbHandler(hub=hub).get(
            id=ref.slug, view="svg", args={"level": "fab", "refdes": ["R9"]}
        )
    assert "nearest" not in str(ei.value)
    assert ei.value.options
    assert "bbox=[x0,y0,x1,y1]" in ei.value.options


def test_scoped_svg_is_well_formed_xml(store) -> None:
    import xml.etree.ElementTree as ET

    hub, ref = _seed_board(store)
    scoped = (
        PcbHandler(hub=hub)
        .get(
            id=ref.slug,
            view="svg",
            args={"level": "fab", "refdes": ["R1"], "nets": ["GND"]},
        )
        .body
    )
    ET.fromstring(scoped)
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', scoped)
    assert m is not None
