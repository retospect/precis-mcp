"""Export preserves oblique pad geometry and nearly closed copper arcs."""

import math

import pytest
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from precis.pcb import drc, epro, epro_write, gerber, padplace
from precis.pcb.capabilities import capability_for
from precis.pcb.clearance import body_of, gap
from precis.pcb.gerber_view import parse_gerber


def _art_geometry(text):
    art = parse_gerber(text)
    shapes = [Polygon(r.ring) for r in art.regions if r.solid]
    shapes += [
        LineString(s.points).buffer(s.width / 2, quad_segs=128) for s in art.strokes
    ]
    for f in art.flashes:
        w = f.aperture.sizes[0]
        h = f.aperture.sizes[-1]
        if f.aperture.shape == "C":
            shapes.append(Point(f.x, f.y).buffer(w / 2, quad_segs=128))
        elif f.aperture.shape == "R":
            shapes.append(box(f.x - w / 2, f.y - h / 2, f.x + w / 2, f.y + h / 2))
        else:
            a, b = (
                ((f.x - (w - h) / 2, f.y), (f.x + (w - h) / 2, f.y))
                if w >= h
                else ((f.x, f.y - (h - w) / 2), (f.x, f.y + (h - w) / 2))
            )
            shapes.append(LineString([a, b]).buffer(min(w, h) / 2, quad_segs=128))
    return unary_union(shapes), art


@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("writer", ["copper", "mask", "paste"])
@pytest.mark.parametrize("w,h", [(2.0, 0.5), (0.5, 2.0)])
@pytest.mark.parametrize("angle", [45.0, 315.0])
def test_gerber_preserves_oblique_pad_shape(shape, writer, w, h, angle):
    pad = dict(
        shape=shape,
        x=5.0,
        y=5.0,
        w=w,
        h=h,
        layer="F.Cu",
        net="N0",
        refdes="U1",
        pin="1",
        oblique_rot=15.0,
        oblique_world_rot=angle,
    )
    model = dict(layers=["F.Cu", "B.Cu"], pads=[pad], copper=[])
    if writer == "copper":
        text = gerber.copper_gerber(model, "F.Cu")
    elif writer == "mask":
        text = gerber.soldermask_gerber(model, "top")
    else:
        text = gerber.solderpaste_gerber(model, "top")
    copper, art = _art_geometry(text)
    margin = 0.05 if writer == "mask" else 0.0
    extent = (
        (2.0 + 0.5 + 4 * margin) / (2 * math.sqrt(2))
        if shape == "rect"
        else ((2.0 - 0.5) / (2 * math.sqrt(2)) + 0.25 + margin)
    )
    assert copper.bounds == pytest.approx(
        (5 - extent, 5 - extent, 5 + extent, 5 + extent), abs=3e-6
    )
    via_gap = copper.distance(Point(6.35, 5)) - 0.225
    assert via_gap >= 0.2
    # Equal bounds cannot distinguish CW from CCW or a width/height swap.
    sign = 1 if angle == 45.0 else -1
    inside = Point(5.5, 5.0 + (-sign if w > h else sign) * 0.5)
    outside = Point(5.5, 5.0 + (sign if w > h else -sign) * 0.5)
    assert copper.covers(inside)
    assert not copper.covers(outside)
    if writer == "copper":
        objects = art.regions + art.strokes + art.flashes
        assert all(o.net == "N0" and o.refdes == "U1" and o.pin == "1" for o in objects)


@pytest.mark.parametrize("in_footprint", [False, True])
def test_long_diagonal_pad_keeps_its_angle_through_fab_and_easyeda(in_footprint):
    angle = 45.000049
    inst = dict(
        refdes="U1",
        x=250.000079,
        y=250.000079,
        rot=0.0 if in_footprint else angle,
        layer="top",
        footprint="PAD",
    )
    local = dict(
        number="1",
        shape="RECT",
        x=0.0,
        y=0.0,
        w=600.000051,
        h=0.500051,
        rot=angle if in_footprint else 0.0,
        layer="F.Cu",
    )
    pads, _ = padplace.place_footprint_pads(
        [local], inst, layers=["F.Cu", "B.Cu"], pin_to_net={"1": "N0"}
    )
    via = dict(
        ctype="via",
        net="N0",
        x=462.733970347,
        y=38.044639770,
        dia_mm=0.45,
        drill_mm=0.2,
        span=["F.Cu", "B.Cu"],
    )
    model = dict(
        layers=["F.Cu", "B.Cu"],
        outline=[[0, 0], [600, 0], [600, 600], [0, 600]],
        pads=pads,
        copper=[via],
        drills=[],
        instances=[inst],
        silkscreen={"top": [], "bottom": []},
    )
    assert pads[0]["oblique_world_rot"] == angle
    assert drc.check_via_mask_web(model, capability_for("4layer")) == []
    exported = epro_write.epro_files(model, slug="long-pad")
    assert exported.warnings == []
    project = epro.read_archive(epro_write.zip_epro(exported.files))
    design, _ = epro.build_design(project)
    component = design.components[0]
    footprint = next(
        fp for fp in design.footprints if fp["name"] == component["footprint"]
    )
    read_pads, _ = padplace.place_footprint_pads(
        footprint["pads"], component, layers=["F.Cu", "B.Cu"], pin_to_net={"1": "N0"}
    )
    assert (
        drc.check_via_mask_web({**model, "pads": read_pads}, capability_for("4layer"))
        == []
    )


@pytest.mark.parametrize("cw", [True, False])
def test_easyeda_nearly_full_arc_preserves_its_centre(cw):
    theta = math.radians(-359.999 if cw else 359.999)
    segment = dict(
        shape="arc",
        start=[11.0, 10.0],
        end=[10 + math.cos(theta), 10 + math.sin(theta)],
        center=[10.0, 10.0],
        cw=cw,
    )
    track = dict(
        ctype="track", net="N0", layer="F.Cu", width_mm=0.15, segments=[segment]
    )
    via = dict(
        ctype="via",
        net="N1",
        x=8.54,
        y=10.0,
        dia_mm=0.45,
        drill_mm=0.2,
        span=["F.Cu", "B.Cu"],
    )
    model = dict(
        layers=["F.Cu", "B.Cu"],
        outline=[[0, 0], [20, 0], [20, 20], [0, 20]],
        copper=[track, via],
        pads=[],
        drills=[],
        instances=[],
        silkscreen={"top": [], "bottom": []},
    )
    exported = epro_write.epro_files(model, slug="near-circle")
    assert exported.warnings == [
        "2 net(s) have no component to declare against and are not in the file: N0, N1"
    ]
    project = epro.read_archive(epro_write.zip_epro(exported.files))
    doc = project.pcb()
    _, frame = epro.board_outline(doc)
    copper, warnings = epro.measured_copper(doc, frame)
    assert warnings == []
    read_tracks = [c for c in copper if c["ctype"] == "track"]
    assert read_tracks
    for row in read_tracks:
        for arc in row["segments"]:
            assert arc["center"] == pytest.approx([10.0, 10.0], abs=5e-6)
            assert arc["cw"] == cw
        track_body, via_body = body_of(row), body_of(via)
        assert track_body is not None and via_body is not None
        assert gap(track_body, via_body) >= 0.15
