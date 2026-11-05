"""Physical drill and pad extents survive placement and export."""

import math
from types import SimpleNamespace
from typing import Any

import pytest
from shapely.geometry import Polygon

from precis.pcb import drc, epro, epro_verify, epro_write, gerber, local, padplace
from precis.pcb.clearance import FAB_QUANTUM_MM


def _pad(**changes):
    return dict(
        number="1",
        shape="rect",
        x=0.0,
        y=0.0,
        w=2.0,
        h=1.0,
        rot=0.0,
        drill=0.6,
        slot={"w": 1.2, "h": 0.6},
        **changes,
    )


def _instance(angle, side="bottom"):
    return dict(refdes="J1", x=5.1234, y=6.2345, rot=angle, layer=side)


@pytest.mark.parametrize("angle", [23.7, 45.0, 90.0])
@pytest.mark.parametrize("side", ["top", "bottom"])
def test_quantized_supported_slot_recovers_in_authored_frame(angle, side):
    inst = _instance(angle, side)
    placed, _ = padplace.place_footprint_pads([_pad()], inst, layers=["F.Cu"])
    recovered = epro_write._local_pad(placed[0], inst)
    # Independently, inverse R(instance) M maps the end-centre distance
    # back to 0.6 mm on the pad X axis. Endpoint rounding is 0.0001 mm.
    assert recovered.slot is not None
    assert recovered.slot == pytest.approx((1.2, 0.6), abs=0.0001)
    assert not recovered.slot_rounded


def test_offline_model_exports_the_placed_slot_to_excellon(monkeypatch):
    inst = _instance(90.0)
    pads, _ = padplace.place_footprint_pads([_pad()], inst, layers=["F.Cu", "B.Cu"])
    monkeypatch.setattr(
        local.pcb_realize, "to_gerber_model", lambda *a, **k: {"pads": pads}
    )
    design = SimpleNamespace(
        layer_names=["F.Cu", "B.Cu"],
        outline=[],
        mounting_holes=[],
        graph={"instances": [inst]},
        features=[],
        stackup=[],
        fab_caps={},
    )
    placement: Any = SimpleNamespace(design=design, ir=None, footprints={}, poses={})
    model = local._model(placement)
    assert len(model["drills"]) == 1
    # A CW 90-degree rotation turns the horizontal 0.6 mm centre line
    # vertical; bottom reflection reverses its endpoint order only.
    assert sorted(model["drills"][0]["slot"]) == sorted(
        [
            [5.1234, 5.9345],
            [5.1234, 6.5345],
        ]
    )
    files = gerber.excellon_files(model)
    assert any("G85" in content for content in files.values())


@pytest.mark.parametrize("angle", [0.04, 89.96, 180.04, 269.96])
@pytest.mark.parametrize("width", [0.2, 2.0, 600.0])
def test_near_axis_snapping_has_a_linear_extremity_bound(angle, width):
    raw = dict(number="1", shape="rect", x=0.0, y=0.0, w=width, h=0.5)
    inst = dict(x=0.0, y=0.0, rot=angle, layer="top")
    placed, _ = padplace.place_footprint_pads([raw], inst, layers=["F.Cu"])
    theta = math.radians(angle)
    expected = Polygon(
        [
            (
                x * math.cos(theta) + y * math.sin(theta),
                -x * math.sin(theta) + y * math.cos(theta),
            )
            for x, y in [
                (-width / 2, -0.25),
                (width / 2, -0.25),
                (width / 2, 0.25),
                (-width / 2, 0.25),
            ]
        ]
    )
    got = drc._copper_item_polygon({"ctype": "pad", **placed[0]})
    assert got is not None
    assert got.hausdorff_distance(expected) <= FAB_QUANTUM_MM / 2


@pytest.mark.parametrize("angle", [0.0, 23.7, 90.0])
@pytest.mark.parametrize("side", ["top", "bottom"])
def test_offset_hole_moves_with_pad_frame_and_exports_back(angle, side):
    inst = _instance(angle, side)
    raw = _pad(drill_offset={"x": 0.25, "y": -0.125})
    raw["rot"] = 37.0
    pads, drills = padplace.place_footprint_pads([raw], inst, layers=["F.Cu"])
    theta = math.radians(37.0)
    x, y = (
        0.25 * math.cos(theta) - 0.125 * math.sin(theta),
        -0.25 * math.sin(theta) - 0.125 * math.cos(theta),
    )
    if side == "bottom":
        x = -x
    theta = math.radians(angle)
    expected = (
        inst["x"] + x * math.cos(theta) + y * math.sin(theta),
        inst["y"] - x * math.sin(theta) + y * math.cos(theta),
    )
    assert (drills[0]["x"], drills[0]["y"]) == pytest.approx(expected, abs=0.00005)
    recovered = epro_write._local_pad(pads[0], inst)
    body = epro_write._pad_body(recovered, 0)
    assert body["padOffsetX"] * 0.0254 == pytest.approx(0.25, abs=0.0001)
    assert body["padOffsetY"] * -0.0254 == pytest.approx(-0.125, abs=0.0001)


@pytest.mark.parametrize("side", ["top", "bottom"])
@pytest.mark.parametrize("angle", [23.7, 45.0, 90.0])
def test_offset_slot_survives_archive_and_ir_paths(angle, side):
    from precis.pcb import DEFAULT_STACKUP, realize
    from precis.pcb.ir import from_graph
    from precis.pcb.session import apply_real_pin_offsets

    inst = {**_instance(angle, side), "footprint": "OFFSET"}
    raw = _pad(drill_offset={"x": 0.25, "y": -0.125})
    raw["rot"] = 37.0
    graph = dict(
        instances=[inst, dict(refdes="U2", x=15.0, y=15.0)],
        nets=[
            dict(
                name="N",
                members=[dict(refdes="J1", pin="1"), dict(refdes="U2", pin="1")],
            )
        ],
    )
    footprints = {"J1": dict(pads=[raw], pin_map={"1": {"name": "1"}})}
    layers = ["F.Cu", "B.Cu"]
    expected, holes = padplace.place_footprint_pads([raw], inst, layers=layers)
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(ir, footprints)
    ir_pads = [
        p for p in realize.pads_for_ir(ir, layers, footprints) if p["refdes"] == "J1"
    ]
    ir_holes = padplace.drills_for_pads(ir_pads)
    assert len(ir_holes) == 1
    assert (ir_holes[0]["x"], ir_holes[0]["y"]) == (holes[0]["x"], holes[0]["y"])
    for got, want in zip(ir_holes[0]["slot"], holes[0]["slot"], strict=True):
        assert got == pytest.approx(want, abs=0.0001)
    model = dict(
        layers=layers,
        outline=[[0, 0], [20, 0], [20, 20], [0, 20]],
        pads=expected,
        copper=[],
        drills=holes,
        instances=[inst],
        silkscreen={"top": [], "bottom": []},
    )
    archive = epro_write.epro_files(model, slug="offset-slot")
    assert not archive.warnings
    data = epro_write.zip_epro(archive.files)
    verdict = epro_verify.verify_export(model, data, stats=archive.stats)
    assert not verdict.fails, verdict.render()
    design, _ = epro.build_design(epro.read_archive(data))
    component = design.components[0]
    fp = next(f for f in design.footprints if f["name"] == component["footprint"])
    reread, got_holes = padplace.place_footprint_pads(
        fp["pads"], component, layers=layers
    )
    assert len(got_holes) == 1
    assert (got_holes[0]["x"], got_holes[0]["y"]) == pytest.approx(
        (holes[0]["x"], holes[0]["y"]), abs=0.0001
    )
    for got, want in zip(
        sorted(reread[0]["slot"]), sorted(expected[0]["slot"]), strict=True
    ):
        assert got == pytest.approx(want, abs=0.0002)


def test_placement_land_probe_uses_the_same_bounded_snap():
    from precis.pcb import DEFAULT_STACKUP
    from precis.pcb.ir import from_graph
    from precis.pcb.session import land_rects_by_instance

    graph = dict(
        instances=[dict(refdes="U1", x=5.0, y=5.0), dict(refdes="U2", x=10.0, y=10.0)],
        nets=[
            dict(
                name="N",
                members=[dict(refdes="U1", pin="1"), dict(refdes="U2", pin="1")],
            )
        ],
    )
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    raw = dict(number="1", shape="rect", x=0.0, y=0.0, w=0.2, h=0.05, rot=89.96)
    rects, _ = land_rects_by_instance(ir, {"U1": {"pads": [raw]}})
    # .2 x .05 mm at .04 degrees from a quarter turn moves its corners
    # only hypot(.2,.05)*sin(.02 degrees) = .00007196 mm.
    assert math.hypot(0.2, 0.05) * math.sin(math.radians(0.02)) < FAB_QUANTUM_MM / 2
    assert rects[0][0].tolist() == pytest.approx([0.0, 0.0, 0.025, 0.1])
