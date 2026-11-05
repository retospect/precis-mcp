"""Native land consumers must use the same physical boundary as the films."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

import pytest
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from precis.pcb import gerber, gerber_view, mech2d, padplace, selectables
from tests.test_pcb_export_round9 import _art_geometry


def _placed(shape, bottom=False, angle=37.0, w=2.4, h=1.2):
    raw = dict(number="1", shape=shape, x=0.0, y=0.0, w=w, h=h, rot=angle)
    inst = dict(
        instance_id=1,
        component_id=1,
        refdes="J1",
        x=10.0,
        y=10.0,
        rot=0.0,
        layer="bottom" if bottom else "top",
    )
    pads, _ = padplace.place_footprint_pads(
        [raw], inst, layers=["F.Cu", "B.Cu"], pin_to_net={"1": "SIG"}
    )
    return pads[0], inst


def _model(pad):
    return dict(
        layers=["F.Cu", "B.Cu"],
        pads=[pad],
        copper=[],
        outline=[[0, 0], [30, 0], [30, 25], [0, 25]],
        silkscreen={"top": [], "bottom": []},
    )


def _land(shape, angle, w=2.4, h=1.2):
    # Independent authored-shape oracle; no production shape/rotation helpers.
    theta = math.radians(angle)

    def world(x, y):
        return 10 + x * math.cos(theta) + y * math.sin(theta), 10 - x * math.sin(
            theta
        ) + y * math.cos(theta)

    if shape == "rect":
        return Polygon(
            [
                world(-w / 2, -h / 2),
                world(w / 2, -h / 2),
                world(w / 2, h / 2),
                world(-w / 2, h / 2),
            ]
        )
    dx, dy = ((w - h) / 2, 0) if w >= h else (0, (h - w) / 2)
    return LineString([world(-dx, -dy), world(dx, dy)]).buffer(
        min(w, h) / 2, quad_segs=512
    )


def _svg_shape(el):
    """Interpret the actual served SVG primitives, independent of hit metadata."""
    tag = el.tag.rsplit("}", 1)[-1]
    if tag == "circle":
        return Point(float(el.get("cx")), float(el.get("cy"))).buffer(
            float(el.get("r")), quad_segs=512
        )
    if tag == "rect":
        x, y, w, h = (float(el.get(k)) for k in ("x", "y", "width", "height"))
        r = float(el.get("rx", "0"))
        if r:
            return box(x + r, y + r, x + w - r, y + h - r).buffer(r, quad_segs=512)
        return box(x, y, x + w, y + h)
    coords = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", el.get("d", ""))]
    points = list(zip(coords[::2], coords[1::2], strict=True))
    if el.get("stroke"):
        assert el.get("stroke-linecap") == "round"
        return LineString(points).buffer(
            float(el.get("stroke-width")) / 2, quad_segs=512
        )
    return Polygon(points)


@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("angle", [37.0, 60.7])
@pytest.mark.parametrize("w,h", [(2.4, 1.2), (1.2, 2.4)])
@pytest.mark.parametrize("expansion", [0.0, 0.05])
def test_preview_mask_opening_matches_emitted_film(bottom, angle, w, h, expansion):
    pad, _ = _placed("obround", bottom, angle, w, h)
    model = {**_model(pad), "soldermask_expansion_mm": expansion}
    side = "bottom" if bottom else "top"
    film = gerber.soldermask_gerber(model, side)
    emitted, art = _art_geometry(film)
    assert len(art.strokes) == 1
    svg = gerber_view.render_fab_svg(gerber.export_fab(model, allow_synthesized=True))
    root = ET.fromstring(svg)
    mask_key = f"masklayer-mask-{'B' if bottom else 'F'}_Mask"
    masks = [e for e in root.iter() if e.get("id") == mask_key]
    assert len(masks) == 1, "A stroked aperture must cut an opening in preview film"
    opening = unary_union(
        [
            _svg_shape(e)
            for e in masks[0]
            if e.get("fill") == "#000" or e.get("stroke") == "#000"
        ]
    )
    expected = _land("obround", -angle if bottom else angle, w, h).buffer(
        expansion, quad_segs=512
    )
    assert opening.hausdorff_distance(emitted) < 0.0001
    assert opening.hausdorff_distance(expected) < 0.0001
    assert opening.symmetric_difference(emitted).area < 0.0005
    painted_film = box(0, 0, 30, 25).difference(opening)
    emitted_film = box(0, 0, 30, 25).difference(emitted)
    assert painted_film.symmetric_difference(emitted_film).area < 0.0005


@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("shape", ["rect", "obround"])
def test_served_selection_targets_and_group_extents_follow_visible_land(bottom, shape):
    pad, inst = _placed(shape, bottom)
    model = _model(pad)
    objects = selectables.build(
        model, instances=[inst], pins=[], nets=[], components=[{"component_id": 1}]
    )
    pad_obj = next(o for o in objects if o.identity["kind"] == "pad")
    instance = next(o for o in objects if o.identity["kind"] == "instance")
    assert pad_obj.identity["instance_id"] == instance.identity["instance_id"] == 1
    expected = _land(shape, -37 if bottom else 37)
    files = gerber.export_fab(model, allow_synthesized=True)
    for flip in (False, True):
        root = ET.fromstring(
            gerber_view.render_fab_svg(
                files,
                flip=flip,
                selection_manifest={
                    "board_ref_id": 1,
                    "snapshot": "r14",
                    "instances": [],
                    "objects": objects,
                },
            )
        )
        hit = next(e for e in root.iter() if e.get("data-pcb-object") == "pad:1:1")
        assert hit.get("data-pcb-layer") == ("B_Cu" if bottom else "F_Cu")
        visible_hit = _svg_shape(hit)
        assert visible_hit.hausdorff_distance(expected) < 0.0001
        if shape == "rect":
            point = Point(10.587433, 11.131337 if bottom else 8.868663)
            assert expected.covers(point)
            assert visible_hit.covers(point), (
                "Reported visible copper point must be selectable"
            )
        bounds = selectables._pad_extent(pad)
        assert bounds == pytest.approx(expected.bounds, abs=0.000003)
        group = instance.hits[0]
        assert (
            group["x"],
            group["y"],
            group["x"] + group["w"],
            group["y"] + group["h"],
        ) == pytest.approx(expected.bounds, abs=0.000003)


@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("shape", ["rect", "obround"])
def test_spacer_window_leaves_native_electrode_uncovered(bottom, shape):
    pad, _ = _placed(shape, bottom)
    model = _model({**pad, "role": "electrode"})
    spec = mech2d.MechSpec(window_margin_mm=0.1)
    spacer = next(
        layer
        for layer in mech2d.mechanical_layers(model, spec)
        if layer.name == "spacer_adhesive"
    )
    actual_land = _land(shape, -37 if bottom else 37)
    assert spacer.sheet.intersection(actual_land).area < 1e-12
    assert mech2d._pad_geom(pad).bounds == pytest.approx(actual_land.bounds, abs=0.005)


@pytest.mark.parametrize("field", ["oblique_world_rot", "oblique_rot"])
def test_mechanical_fingerprint_tracks_native_land_rotation(field):
    pad = dict(
        shape="rect", x=10, y=10, w=2.4, h=1.2, role="electrode", **{field: 37.0}
    )
    model = _model(pad)
    changed = _model({**pad, field: 60.7})
    assert mech2d.board_fingerprint(model) != mech2d.board_fingerprint(changed)


def test_mechanical_fingerprint_keeps_consumed_via_coordinate_precision():
    model = {
        **_model(dict(shape="rect", x=10, y=10, w=2.4, h=1.2)),
        "copper": [dict(ctype="via", x=5.000001, y=15, dia_mm=0.6)],
    }
    changed = {**model, "copper": [{**model["copper"][0], "x": 5.000002}]}
    assert mech2d.board_fingerprint(model) != mech2d.board_fingerprint(changed)
