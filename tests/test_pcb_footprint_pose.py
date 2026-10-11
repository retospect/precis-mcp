"""Every footprint-owned element lands where ONE reference matrix says, for
random instance poses (top and bottom, oblique rotations included).

The expected value is computed by a TEST-LOCAL matrix (mirror-X for bottom,
rotate clockwise, translate) — deliberately not the production
``precis.pcb.pose`` helper, so a drift in the seam is caught, not echoed.
Each production path that maps footprint-local geometry to world is checked
against it: fab pads/drills (``padplace``), IR pins, ``pads_for_ir`` (pad
centres, polygon rings, oblique rings), the router's polygon claim, the DRC
copper polygon, and the courtyard.
"""

from __future__ import annotations

import math
import random

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import DEFAULT_STACKUP, padplace
from precis.pcb.drc import _copper_item_polygon
from precis.pcb.ir import from_graph, instance_courtyard_polygon, pin_point
from precis.pcb.landpattern import place_points
from precis.pcb.realize import _pad_shape, pad_geometry, pads_for_ir
from precis.pcb.session import apply_real_pin_offsets, build_ir

TOL = 1e-4
LAYERS = [str(entry["name"]) for entry in DEFAULT_STACKUP]

#: Polygon pad: vertices in footprint-local mm, centre = bbox centre.
_POLY = [(-1.0, -4.0), (1.0, -4.0), (1.0, -2.0), (-1.0, -2.0), (0.0, -1.5)]
_POLY_C = (0.0, (-4.0 + -1.5) / 2.0)
_OBL = {"x": 2.0, "y": 1.5, "w": 1.2, "h": 0.5, "rot": 30.0}
#: Off-centre non-plated standoff/peg hole, footprint-local mm.
_HOLE = {"x": -3.5, "y": 2.5, "dia_mm": 1.6}


def _footprint() -> dict:
    def pad(number, shape, x, y, w, h, **kw):
        return {
            "number": number,
            "shape": shape,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
            **kw,
        }

    pads = [
        pad("1", "RECT", -2.0, 1.0, 1.0, 0.6),
        pad("2", "RECT", _OBL["x"], _OBL["y"], _OBL["w"], _OBL["h"], rot=_OBL["rot"]),
        pad(
            "3",
            "POLYGON",
            _POLY_C[0],
            _POLY_C[1],
            2.0,
            2.5,
            poly=[list(v) for v in _POLY],
        ),
        pad("4", "ELLIPSE", 3.0, -2.0, 1.6, 1.6, drill=0.8),
    ]
    return {
        "pads": pads,
        "pin_map": {p["number"]: {"name": p["number"], "tags": []} for p in pads},
        "holes": [dict(_HOLE)],
    }


def _graph(x: float, y: float, rot: float, bottom: bool) -> dict:
    return {
        "instances": [
            {
                "refdes": "U1",
                "x": x,
                "y": y,
                "rot": rot,
                "layer": "bottom" if bottom else "top",
            }
        ],
        "nets": [
            {"name": f"N{n}", "members": [{"refdes": "U1", "pin": n}]}
            for n in ("1", "2", "3", "4")
        ],
    }


def _ref(lx: float, ly: float, x: float, y: float, rot: float, bottom: bool):
    """Test-local reference: mirror-X (bottom), rotate CW, translate."""
    if bottom:
        lx = -lx
    t = math.radians(rot)
    return (
        x + lx * math.cos(t) + ly * math.sin(t),
        y - lx * math.sin(t) + ly * math.cos(t),
    )


def _poses() -> list[tuple[float, float, float, bool]]:
    rng = random.Random(20261011)
    poses = [(10.0, 20.0, 270.0, True), (5.0, 5.0, 0.0, True), (7.0, 3.0, 90.0, False)]
    while len(poses) < 18:
        poses.append(
            (
                rng.uniform(-50, 50),
                rng.uniform(-50, 50),
                rng.uniform(0, 360),
                len(poses) % 2 == 0,
            )
        )
    return poses


def _close(a, b, what: str) -> None:
    assert math.hypot(a[0] - b[0], a[1] - b[1]) < TOL, f"{what}: {a} != {b}"


def _ring_close(got, want, what: str) -> None:
    assert len(got) == len(want), what
    for i, (g, w) in enumerate(zip(got, want, strict=True)):
        _close(g, w, f"{what}[{i}]")


@pytest.mark.parametrize("x,y,rot,bottom", _poses())
def test_every_footprint_element_follows_the_reference_matrix(x, y, rot, bottom):
    fp = _footprint()
    pads_by = {p["number"]: p for p in fp["pads"]}

    def ref(lx, ly):
        return _ref(lx, ly, x, y, rot, bottom)

    centre = {n: ref(p["x"], p["y"]) for n, p in pads_by.items()}
    poly_ring = [ref(vx, vy) for vx, vy in _POLY]
    th_drill = ref(3.0, -2.0)
    hw, hh = _OBL["w"] / 2, _OBL["h"] / 2
    obl_ring = []
    t = math.radians(_OBL["rot"])
    for cx_, cy_ in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        # the pad's own rotation first (CW), then the instance pose
        px = cx_ * math.cos(t) + cy_ * math.sin(t)
        py = -cx_ * math.sin(t) + cy_ * math.cos(t)
        obl_ring.append(ref(_OBL["x"] + px, _OBL["y"] + py))

    # 1. fab path (padplace -> gerber model / Excellon).
    inst = {
        "refdes": "U1",
        "x": x,
        "y": y,
        "rot": rot,
        "layer": "bottom" if bottom else "top",
    }
    fab_pads, fab_drills = padplace.place_footprint_pads(
        fp["pads"], inst, layers=LAYERS
    )
    seen: dict = {}
    for p in fab_pads:
        seen.setdefault(p["pin"], p)
    for n, c in centre.items():
        _close((seen[n]["x"], seen[n]["y"]), c, f"fab pad {n}")
    _ring_close([tuple(v) for v in seen["3"]["poly"]], poly_ring, "fab polygon ring")
    assert len(fab_drills) == 1
    _close((fab_drills[0]["x"], fab_drills[0]["y"]), th_drill, "excellon drill")

    # 2. IR path.
    ir = from_graph(_graph(x, y, rot, bottom), stackup=DEFAULT_STACKUP)
    footprints = {"U1": fp}
    apply_real_pin_offsets(ir, footprints)
    geoms = pad_geometry(ir, footprints)
    by_pin: dict = {}
    for p in pads_for_ir(ir, LAYERS, footprints):
        by_pin.setdefault(p["pin"], p)
    pid_of = {str(ir.pin_label[i]): i for i in range(ir.n_pins)}
    assert set(pid_of) == set(centre)
    for label, pid in pid_of.items():
        pt = pin_point(ir, pid)
        assert pt is not None
        _close(pt, centre[label], f"IR pin {label}")
        _close(
            (by_pin[label]["x"], by_pin[label]["y"]),
            centre[label],
            f"model pad {label}",
        )

    pt3 = pin_point(ir, pid_of["3"])
    assert pt3 is not None
    _ring_close(
        [tuple(v) for v in by_pin["3"]["poly"]], poly_ring, "model polygon ring"
    )
    claim = _pad_shape(
        geoms[pid_of["3"]],
        pt3,
        float(ir.inst_rot[0]),
        mirrored=bool(ir.inst_bottom[0]),
    )
    _ring_close(list(claim.poly), poly_ring, "router polygon claim")
    drc_poly = _copper_item_polygon({**by_pin["3"], "ctype": "pad"})
    assert drc_poly is not None
    got = list(drc_poly.exterior.coords)[:-1]
    assert len(got) == len(poly_ring)
    for w in poly_ring:  # ring start/orientation is shapely's choice
        assert min(math.hypot(g[0] - w[0], g[1] - w[1]) for g in got) < TOL, (
            f"DRC copper polygon missing vertex {w}"
        )

    assert by_pin["2"]["shape"] == "polygon", "oblique pad should be a ring"
    _ring_close([tuple(v) for v in by_pin["2"]["poly"]], obl_ring, "oblique ring")

    # 2b. Footprint holes: build_ir derives one MountingHole per instance
    # hole at the instance's pose, owned by the part.
    graph = _graph(x, y, rot, bottom)
    graph["instances"][0]["footprint"] = "FP"
    full = build_ir(graph, local_footprints_by_name={"FP": fp})
    (hole,) = full.mounting_holes
    _close((hole.x, hole.y), ref(_HOLE["x"], _HOLE["y"]), "ir.mounting_holes")
    assert hole.drill_mm == pytest.approx(_HOLE["dia_mm"])
    assert hole.part == "U1"

    # 3. Courtyard (footprint-local hull placed by landpattern.place_points).
    local = instance_courtyard_polygon(
        ir, 0, clearance_mm=0.2, pads=ir.footprint_pads or None
    )
    assert local
    placed = place_points(local, cx=x, cy=y, rot_deg=rot, mirrored=bottom)
    _ring_close(placed, [ref(lx, ly) for lx, ly in local], "courtyard")


# ── holes live on the footprint ─────────────────────────────────────────
@pytest.fixture
def pcb(store) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def _fp_with_hole(name: str = "FP", holes=None) -> dict:
    return {
        "name": name,
        "pads": [{"pin": "1", "shape": "rect", "x": 0.0, "y": 0.0, "w": 1.0}],
        "holes": [{"x": 3.0, "y": -2.0, "dia_mm": 1.6}] if holes is None else holes,
    }


def _board(slug: str, pcb, x=10.0, y=20.0, rot=270.0, layer="bottom") -> None:
    pcb.put(
        id=slug,
        args={
            "footprints": [_fp_with_hole()],
            "components": [
                {
                    "refdes": "J1",
                    "label": "conn",
                    "footprint": "FP",
                    "x": x,
                    "y": y,
                    "rot": rot,
                    "layer": layer,
                    "pins": [{"name": "1"}],
                }
            ],
            "nets": [{"name": "N"}],
            "connections": [{"net": "N", "refdes": "J1", "pin": "1"}],
        },
    )


def _ir_of(pcb, store, slug: str):
    ref = store.get_ref(kind="pcb", id=slug)
    return pcb._build_ir(ref.id, store.pcb_graph(ref.id))


def test_put_footprints_round_trips_holes(pcb, store):
    _board("h-rt", pcb)
    ref = store.get_ref(kind="pcb", id="h-rt")
    assert store.pcb_local_footprints_for(ref.id)["FP"]["holes"] == [
        {"x": 3.0, "y": -2.0, "dia_mm": 1.6}
    ]


def test_hole_at_3_minus2_on_a_flipped_rot_270_part(pcb, store):
    _board("h-flip", pcb)
    (hole,) = _ir_of(pcb, store, "h-flip").mounting_holes
    want = _ref(3.0, -2.0, 10.0, 20.0, 270.0, True)
    assert hole.x == pytest.approx(want[0], abs=TOL)
    assert hole.y == pytest.approx(want[1], abs=TOL)
    assert hole.part == "J1"


def test_a_moved_part_keeps_its_hole_offset_from_the_pad(pcb, store):
    _board("h-move", pcb, rot=0.0, layer="top")
    before = _ir_of(pcb, store, "h-move").mounting_holes[0]
    pcb.put(id="h-move", args={"op": "move", "refdes": "J1", "x": 40.0, "y": 5.0})
    after = _ir_of(pcb, store, "h-move").mounting_holes[0]
    assert (before.x - 10.0, before.y - 20.0) == pytest.approx((3.0, -2.0), abs=TOL)
    assert (after.x - 40.0, after.y - 5.0) == pytest.approx((3.0, -2.0), abs=TOL)


def test_put_footprints_rejects_unknown_keys_and_bad_holes(pcb):
    with pytest.raises(BadInput, match="unknown key.*hole.*accepted"):
        pcb.put(id="h-bad", args={"footprints": [{**_fp_with_hole(), "hole": []}]})
    for bad in ([{"x": 0, "y": 0, "dia_mm": 0}], [{"x": 0, "y": 0}], "nope"):
        with pytest.raises(BadInput, match="holes"):
            pcb.put(id="h-bad", args={"footprints": [_fp_with_hole(holes=bad)]})


def test_off_centre_peg_holes_import_onto_the_footprint_not_as_features(store):
    """SATA-like pegs at +-19 mm: stored footprint-local, no part-owned
    absolute ``mounting_hole`` feature."""
    from precis.pcb import epro

    pegs = [{"x": -19.0, "y": 0.0, "dia_mm": 1.6}, {"x": 19.0, "y": 0.0, "dia_mm": 1.6}]
    design = epro.Design(
        footprints=[_fp_with_hole("SATA", holes=pegs)],
        components=[
            {
                "refdes": "U33",
                "label": "sata",
                "footprint": "SATA",
                "x": 50.0,
                "y": 30.0,
                "rot": 90.0,
                "pins": [{"name": "1", "pad": "1"}],
            }
        ],
    )
    ref, _created, _counts = store.pcb_apply(
        slug="sata",
        title="sata",
        components=design.components,
        nets=[],
        connections=[],
        features=[],
        footprints=design.footprints,
    )
    assert store.pcb_local_footprints_for(ref.id)["SATA"]["holes"] == pegs
    assert not [
        f for f in store.pcb_features_list(ref.id) if f["ftype"] == "mounting_hole"
    ]


def test_footprint_holes_replace_legacy_part_owned_features_in_the_ir():
    """A legacy absolute part-owned hole for a refdes whose footprint now
    carries holes must not be drilled a second time; board holes and other
    parts' legacy features stay."""
    from precis.pcb.ir import MountingHole

    graph = _graph(10.0, 20.0, 0.0, False)
    graph["instances"][0]["footprint"] = "FP"
    legacy = (
        MountingHole(x=13.5, y=17.5, drill_mm=1.6, part="U1"),
        MountingHole(x=1.0, y=1.0, drill_mm=3.2),
        MountingHole(x=5.0, y=5.0, drill_mm=1.0, part="U9"),
    )
    ir = build_ir(
        graph, mounting_holes=legacy, local_footprints_by_name={"FP": _footprint()}
    )
    assert [(h.part, h.local_x is not None) for h in ir.mounting_holes] == [
        (None, False),
        ("U9", False),
        ("U1", True),
    ]
