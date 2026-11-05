"""Board-frame land geometry follows pad turn, mirror, then instance turn."""

import math

import pytest
from shapely.geometry import LineString, Polygon, box

from precis.pcb import DEFAULT_STACKUP, drc, epro, epro_write, gerber, padplace, realize
from precis.pcb.clearance import body_of, gap
from precis.pcb.ir import from_graph
from precis.pcb.session import apply_real_pin_offsets
from tests.test_pcb_export_round9 import _art_geometry

_ANGLES = [0.0, 45.0, 90.0, 135.0, 180.0, 270.0, 23.7]
_LAYERS = ["F.Cu", "B.Cu"]


def _input(shape, side, inst_angle, pad_angle, w=2.0, h=0.5):
    inst = dict(refdes="U1", x=5.0, y=5.0, rot=inst_angle, layer=side, footprint="PAD")
    local = dict(
        number="1", shape=shape, x=0.0, y=0.0, rot=pad_angle, w=w, h=h, layer="F.Cu"
    )
    return local, inst


def _expected_polygon(local, inst, margin=0.0):
    """Transform every authored vertex; do not infer a combined pad angle."""
    w, h = local["w"] + 2 * margin, local["h"] + 2 * margin
    if local["shape"] == "rect":
        authored = box(-w / 2, -h / 2, w / 2, h / 2)
    else:
        endpoints = (
            [(-(w - h) / 2, 0), ((w - h) / 2, 0)]
            if w > h
            else [(0, -(h - w) / 2), (0, (h - w) / 2)]
        )
        authored = LineString(endpoints).buffer(min(w, h) / 2, quad_segs=128)
    pc, ps = math.cos(math.radians(local["rot"])), math.sin(math.radians(local["rot"]))
    ic, ins = math.cos(math.radians(inst["rot"])), math.sin(math.radians(inst["rot"]))
    ring = []
    for x, y in authored.exterior.coords:
        px, py = x * pc + y * ps + local["x"], -x * ps + y * pc + local["y"]
        if inst["layer"] == "bottom":
            px = -px
        ring.append((inst["x"] + px * ic + py * ins, inst["y"] - px * ins + py * ic))
    return Polygon(ring)


def _placed(local, inst):
    pads, drills = padplace.place_footprint_pads(
        [local], inst, layers=_LAYERS, pin_to_net={"1": "N0"}
    )
    assert drills == []
    assert len(pads) == 1
    return pads[0]


@pytest.mark.parametrize("side", ["top", "bottom"])
@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("w,h", [(2.0, 0.5), (0.5, 2.0)])
@pytest.mark.parametrize("inst_angle", _ANGLES)
@pytest.mark.parametrize("pad_angle", _ANGLES)
def test_placed_pad_matches_independently_transformed_polygon(
    side, shape, w, h, inst_angle, pad_angle
):
    local, inst = _input(shape, side, inst_angle, pad_angle, w, h)
    placed = _placed(local, inst)
    assert placed["layer"] == ("B.Cu" if side == "bottom" else "F.Cu")
    expected = _expected_polygon(local, inst)
    item = {"ctype": "pad", **placed}
    polygon = drc._copper_item_polygon(item)
    body = body_of(item)
    assert polygon is not None
    assert body is not None and body.core is not None and not body.arcs
    kernel_polygon = body.core.buffer(body.r, quad_segs=128) if body.r else body.core
    # DRC uses 64 segments per quadrant; the oracle uses 128. At radius
    # 0.25 mm their maximum sagitta difference is below 0.00002 mm.
    assert polygon.hausdorff_distance(expected) < 0.000021
    assert kernel_polygon.hausdorff_distance(expected) < 0.000005


@pytest.mark.parametrize("side", ["top", "bottom"])
@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("w,h", [(2.0, 0.5), (0.5, 2.0)])
@pytest.mark.parametrize("inst_angle", _ANGLES)
@pytest.mark.parametrize("pad_angle", _ANGLES)
def test_easyeda_inverse_keeps_the_same_board_frame_polygon(
    side, shape, w, h, inst_angle, pad_angle
):
    local, inst = _input(shape, side, inst_angle, pad_angle, w, h)
    placed = _placed(local, inst)
    recovered = epro_write._local_pad(placed, inst)
    recovered_local = dict(
        shape=recovered.shape,
        x=recovered.x,
        y=recovered.y,
        w=recovered.w,
        h=recovered.h,
        rot=recovered.rot,
    )
    # Inspect the exported local representation independently, so mutually
    # compensating placement and export defects cannot make this pass.
    assert (
        _expected_polygon(recovered_local, inst).hausdorff_distance(
            _expected_polygon(local, inst)
        )
        < 0.000005
    )


@pytest.mark.parametrize("shape", ["rect", "obround"])
def test_bottom_equal_45_turns_keep_the_horizontal_via_gap(shape):
    """Caught the inst+pad swap: gap was 0.125 mm instead of 0.875 mm."""
    local, inst = _input(shape, "bottom", 45.0, 45.0)
    placed = _placed(local, inst)
    pad_body = body_of({"ctype": "pad", **placed})
    via_body = body_of(dict(ctype="via", x=5.0, y=6.35, dia_mm=0.45))
    assert pad_body is not None and via_body is not None
    assert gap(pad_body, via_body) == pytest.approx(0.875, abs=1e-9)


@pytest.mark.parametrize("side", ["top", "bottom"])
@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("w,h", [(2.0, 0.5), (0.5, 2.0)])
@pytest.mark.parametrize("pad_angle", _ANGLES)
def test_ir_fabrication_and_router_pad_geometry_match_the_authored_outline(
    side, shape, w, h, pad_angle
):
    local, inst = _input(shape, side, 45.0, pad_angle, w, h)
    graph = dict(
        instances=[inst, dict(refdes="U2", x=20.0, y=20.0)],
        nets=[
            dict(
                name="N0",
                members=[dict(refdes="U1", pin="1"), dict(refdes="U2", pin="1")],
            )
        ],
    )
    footprints = {"U1": dict(pads=[local], pin_map={"1": dict(name="1")})}
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(ir, footprints)
    pads = realize.pads_for_ir(ir, _LAYERS, footprints)
    pad = next(p for p in pads if p["refdes"] == "U1")
    assert pad["layer"] == ("B.Cu" if side == "bottom" else "F.Cu")
    expected = _expected_polygon(local, inst)
    actual = drc._copper_item_polygon({"ctype": "pad", **pad})
    assert actual is not None
    # The IR retains native lands; only this Shapely comparison tessellates.
    assert actual.hausdorff_distance(expected) < 0.000021
    pid = next(
        i
        for i in range(ir.n_pins)
        if str(ir.instance_refdes[int(ir.pin_instance[i])]) == "U1"
    )
    geom = realize.pad_geometry(ir, footprints)[pid]
    if geom.axis_aligned:
        router_polygon = box(
            5 - geom.w_mm / 2,
            5 - geom.h_mm / 2,
            5 + geom.w_mm / 2,
            5 + geom.h_mm / 2,
        )
        # For axis-aligned capsules the router consumes w/h with shape;
        # bounds pin that representation without replacing it by a box.
        assert router_polygon.bounds == pytest.approx(expected.bounds, abs=0.000005)


@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("writer", ["copper", "mask", "paste"])
def test_bottom_equal_45_turns_keep_the_horizontal_shape_on_every_film(shape, writer):
    local, inst = _input(shape, "bottom", 45.0, 45.0)
    placed = _placed(local, inst)
    model = dict(layers=_LAYERS, pads=[placed], copper=[])
    if writer == "copper":
        text = gerber.copper_gerber(model, "B.Cu")
    elif writer == "mask":
        text = gerber.soldermask_gerber(model, "bottom")
    else:
        text = gerber.solderpaste_gerber(model, "bottom")
    film, _ = _art_geometry(text)
    expected = _expected_polygon(local, inst, margin=0.05 if writer == "mask" else 0.0)
    assert film.hausdorff_distance(expected) < 0.000005
    assert film.bounds == pytest.approx(expected.bounds, abs=0.000005)


@pytest.mark.parametrize("shape", ["rect", "obround"])
def test_bottom_equal_45_turns_keep_geometry_through_easyeda_archive(shape):
    local, inst = _input(shape, "bottom", 45.0, 45.0)
    model = dict(
        layers=_LAYERS,
        outline=[[0, 0], [10, 0], [10, 10], [0, 10]],
        pads=[_placed(local, inst)],
        copper=[],
        drills=[],
        instances=[inst],
        silkscreen={"top": [], "bottom": []},
    )
    archive = epro_write.epro_files(model, slug="bottom-pad")
    assert archive.warnings == []
    design, _ = epro.build_design(epro.read_archive(epro_write.zip_epro(archive.files)))
    component = design.components[0]
    footprint = next(
        fp for fp in design.footprints if fp["name"] == component["footprint"]
    )
    assert component["layer"] == "bottom"
    read_pads, _ = padplace.place_footprint_pads(
        footprint["pads"], component, layers=_LAYERS, pin_to_net={"1": "N0"}
    )
    actual = drc._copper_item_polygon({"ctype": "pad", **read_pads[0]})
    assert actual is not None
    assert actual.hausdorff_distance(_expected_polygon(local, inst)) < 0.000021
