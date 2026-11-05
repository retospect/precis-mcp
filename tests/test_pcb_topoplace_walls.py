"""G2a — truthful walls (topo-route-geometry-truth).

Every pad is a constrained polygon in the mesh on its own layers — pins
in their real shape, ``pad_extra`` lands, a comb's own outline — and an
edge on or inside a foreign pad polygon or a zone polygon is closed on
that polygon's layers, interiors included. The old wall was the pin
vertex's fan boundary: valid only while no other vertex sat inside the
pad ring. These tests pin the four ways it lied (a frozen fanout stub
inside the ring, a land with no ring, a comb read as its bounding box, a
zone read as its ring edges) on hand-built boards small enough to read.
"""

from __future__ import annotations

import dataclasses
import itertools
import types
from typing import Any

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from precis.pcb.topoplace.design import TopoDesign, TopoZone
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.realise import plan_route
from precis.pcb.topoplace.wires import _cand_mask, _make_ctx, _open_layers, build_wires

_RULE = (0.15, 0.15)  # trace, clearance — a gap closes below 0.45 mm


def _rect(x: float, y: float, w: float, h: float) -> np.ndarray:
    return np.asarray(
        [
            [x, y],
            [x + w, y],
            [x + w, y + h],
            [x, y + h],
            [x, y],
        ],
        dtype=np.float64,
    )


def _board(
    pins: list[dict[str, Any]],
    *,
    n_nets: int,
    extras: list[dict[str, Any]] | None = None,
    zones: tuple[TopoZone, ...] = (),
    layer_force: dict[int, int] | None = None,
    n_layers: int = 4,
    size: float = 40.0,
) -> TopoDesign:
    """One part per pin dict (``x``/``y`` its pose, ``dx``/``dy`` the pin
    offset, ``w``/``h`` the pad, ``side`` 0 top / 2 drilled, ``net`` the
    net index, ``shape``/``poly`` the real outline when not a rectangle).
    ``extras`` are the non-primary lands: ``part``, ``dx``/``dy``,
    ``w``/``h``, ``pin`` (-1 = unpinned), ``side``."""
    extras = extras or []
    n = len(pins)
    shape = np.empty(n, dtype=object)
    poly = np.empty(n, dtype=object)
    for i, p in enumerate(pins):
        shape[i] = p.get("shape", "rect")
        poly[i] = p.get("poly")
    ex_shape = np.empty(len(extras), dtype=object)
    ex_poly = np.empty(len(extras), dtype=object)
    for k in range(len(extras)):
        ex_shape[k] = "rect"
        ex_poly[k] = None
    force = np.full(n_nets, -1, dtype=np.int8)
    for net, layer in (layer_force or {}).items():
        force[net] = layer
    return TopoDesign(
        parts=tuple(f"P{i}" for i in range(n)),
        pin_xy=np.asarray([[p.get("dx", 0.0), p.get("dy", 0.0)] for p in pins]),
        pin_part=np.arange(n),
        pin_side=np.asarray([p.get("side", 0) for p in pins], dtype=np.int8),
        pin_label=np.asarray([str(i) for i in range(n)], dtype=object),
        pin_net=np.asarray([p["net"] for p in pins], dtype=np.int32),
        net_pins=tuple(
            np.asarray([i for i, p in enumerate(pins) if p["net"] == k], dtype=np.int64)
            for k in range(n_nets)
        ),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(n, dtype=bool),
        freedoms=np.full(n, 3, dtype=np.uint8),
        outline=_rect(0.0, 0.0, size, size)[:4],
        pose0=np.asarray([[p["x"], p["y"], 0.0, 0.0] for p in pins]),
        net_layer_force=force,
        net_name=tuple(f"N{k}" for k in range(n_nets)),
        pin_wh=np.asarray([[p.get("w", 0.4), p.get("h", 0.4)] for p in pins]),
        pin_shape=shape,
        pin_poly=poly,
        zones=zones,
        n_layers=n_layers,
        pad_extra_xy=np.asarray(
            [[e.get("dx", 0.0), e.get("dy", 0.0)] for e in extras], dtype=np.float64
        ).reshape(-1, 2),
        pad_extra_wh=np.asarray(
            [[e["w"], e["h"]] for e in extras], dtype=np.float64
        ).reshape(-1, 2),
        pad_extra_part=np.asarray([e["part"] for e in extras], dtype=np.int64),
        pad_extra_pin=np.asarray([e.get("pin", -1) for e in extras], dtype=np.int64),
        pad_extra_side=np.asarray([e.get("side", 0) for e in extras], dtype=np.int8),
        pad_extra_shape=ex_shape,
        pad_extra_poly=ex_poly,
    )


def _open(ctx, d: TopoDesign, net: int, n_layers: int = 4) -> np.ndarray:
    return _open_layers(
        ctx,
        d,
        _cand_mask(frozenset(range(n_layers)), n_layers),
        d.net_name[net],
        frozenset(int(p) for p in d.net_pins[net]),
        *_RULE,
    )


def _mids(mesh) -> np.ndarray:
    return 0.5 * (mesh.verts[mesh.edges[:, 0]] + mesh.verts[mesh.edges[:, 1]])


def _edges_in(mesh, geom) -> list[int]:
    """Edges whose midpoint is on or inside ``geom``."""
    grown = geom.buffer(1e-6)
    return [e for e, m in enumerate(_mids(mesh)) if grown.contains(Point(m))]


def _two_pin_net_across(extra_pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Net 0: two pads 20 mm apart on the y = 20 line; the rest after."""
    return [
        {"x": 10.0, "y": 20.0, "net": 0},
        {"x": 30.0, "y": 20.0, "net": 0},
        *extra_pins,
    ]


# -- R1: a stub inside the ring no longer collapses the wall -----------------


def _gnd_pad_with_stub(stub: np.ndarray) -> tuple[TopoDesign, Polygon]:
    pins = _two_pin_net_across([{"x": 20.0, "y": 20.0, "net": 1, "w": 1.6, "h": 1.0}])
    zone = TopoZone(
        kind="fixed_copper", ring=stub, layers=(0,), net="N1", name="fanout stub"
    )
    d = _board(pins, n_nets=2, zones=(zone,))
    return d, Polygon(_rect(19.2, 19.5, 1.6, 1.0))


@pytest.mark.parametrize(
    "stub",
    [
        # clipped: starts at the pad's edge, as _copper_zones now leaves it
        _rect(20.8, 19.85, 2.0, 0.3),
        # unclipped: starts at the pad's centre, ring vertices inside the pad
        _rect(20.0, 19.85, 2.8, 0.3),
    ],
    ids=["clipped", "from-centre"],
)
def test_stub_bearing_pad_still_closes_all_its_area(stub: np.ndarray) -> None:
    d, pad = _gnd_pad_with_stub(stub)
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 4)
    inside = _edges_in(mesh, pad)
    assert len(inside) >= 4, "the pad ring and its fan are in the mesh"
    open0 = _open(ctx, d, 0)
    assert all(not int(open0[e]) & 1 for e in inside), "foreign net, top layer"
    assert all(int(open0[e]) & 0b1110 for e in inside), "an SMD pad bars its face only"
    # its own net leaves the pin freely through the same edges
    open1 = _open(ctx, d, 1)
    assert all(int(open1[e]) & 1 for e in inside)


def test_a_wire_cannot_run_through_the_stub_either() -> None:
    # held to the stub's layer, the signal has to go round pad AND stub
    d, pad = _gnd_pad_with_stub(_rect(20.8, 19.85, 2.0, 0.3))
    d = dataclasses.replace(d, net_layer_force=np.asarray([0, -1], dtype=np.int8))
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 4)
    stub = Polygon(_rect(20.8, 19.85, 2.0, 0.3))
    inside = _edges_in(mesh, stub)
    assert inside
    open0 = _open(ctx, d, 0)
    assert all(not int(open0[e]) & 1 for e in inside)
    w = build_wires(mesh, d, 4, (_RULE, _RULE), pose=d.pose0)
    (b,) = [b for b in w.branches if b.net == 0]
    path = LineString([tuple(p) for p in b.points])
    assert path.length > 20.0 + 1e-3, "it bends"
    assert path.intersection(pad.union(stub).buffer(-1e-6)).is_empty


def test_copper_zones_clip_a_stub_to_outside_its_pad(monkeypatch) -> None:
    """``_copper_zones`` keeps no zone vertex inside a pad ring on the
    stub's layer — the vertex that collapsed the pad's wall."""
    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace import realise

    d, pad = _gnd_pad_with_stub(_rect(30.0, 5.0, 1.0, 1.0))
    monkeypatch.setattr(
        pcb_realize,
        "result_copper_rows",
        lambda *a, **k: [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "N1",
                "width_mm": 0.3,
                "segments": [
                    {"shape": "line", "start": [20.0, 20.0], "end": [23.0, 20.0]}
                ],
            }
        ],
    )
    ir = types.SimpleNamespace(
        stackup=[{"name": n} for n in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")],
        n_layers=4,
    )
    zones = realise._copper_zones(object(), d, ir, d.pose0)
    assert zones, "the part of the stub outside the pad survives"
    core = pad.buffer(-1e-6)
    for z in zones:
        assert z.layers == (0,)
        assert not any(core.contains(Point(v)) for v in z.ring)
    assert max(float(v[0]) for z in zones for v in z.ring) > 22.9


# -- R2: a land with no pin vertex still has a wall --------------------------


def _wire_across_a_land(side: int, **board: Any) -> tuple[TopoDesign, Polygon]:
    """Net 0 runs straight along y = 20 through an unpinned 3 x 1 land."""
    pins = _two_pin_net_across([{"x": 20.0, "y": 24.0, "net": 1}])
    extras = [{"part": 2, "dx": 0.0, "dy": -4.0, "w": 3.0, "h": 1.0, "side": side}]
    d = _board(pins, n_nets=2, extras=extras, **board)
    return d, Polygon(_rect(18.5, 19.5, 3.0, 1.0))


def test_pad_extra_land_closes_its_layer_for_every_net() -> None:
    d, land = _wire_across_a_land(0)
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 4)
    inside = _edges_in(mesh, land)
    assert len(inside) >= 4
    for net in (0, 1):
        open_ = _open(ctx, d, net)
        assert all(not int(open_[e]) & 1 for e in inside), "top layer closed"
        assert all(int(open_[e]) & 0b1110 for e in inside), "inner layers free"


def test_wire_goes_round_a_land_on_the_layer_it_bars() -> None:
    d, land = _wire_across_a_land(0, layer_force={0: 0})
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4, (_RULE, _RULE), pose=d.pose0)
    (b,) = [b for b in w.branches if b.net == 0]
    path = LineString([tuple(p) for p in b.points])
    assert path.length > 20.0 + 1e-3, "it bends"
    assert path.intersection(land.buffer(-1e-6)).is_empty


def test_wire_goes_round_a_drilled_land_on_every_layer() -> None:
    d, land = _wire_across_a_land(2)
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4, (_RULE, _RULE), pose=d.pose0)
    (b,) = [b for b in w.branches if b.net == 0]
    path = LineString([tuple(p) for p in b.points])
    assert path.intersection(land.buffer(-1e-6)).is_empty


def test_a_pinned_land_stays_open_to_its_own_net() -> None:
    """The second land of a pin is that net's own copper: a wire of the
    net crosses it, any other net is walled off."""
    pins = _two_pin_net_across([{"x": 20.0, "y": 24.0, "net": 1}])
    extras = [{"part": 2, "dx": 0.0, "dy": -4.0, "w": 3.0, "h": 1.0, "pin": 0}]
    d = _board(pins, n_nets=2, extras=extras)
    land = Polygon(_rect(18.5, 19.5, 3.0, 1.0))
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 4)
    inside = _edges_in(mesh, land)
    assert inside
    assert all(int(_open(ctx, d, 0)[e]) & 1 for e in inside)
    assert all(not int(_open(ctx, d, 1)[e]) & 1 for e in inside)


# -- R3: a comb is its copper, not its bounding box --------------------------


def _comb(gap: float, *, tooth: float = 1.0) -> np.ndarray:
    """Three fingers pointing +x off a spine at the left, ring relative to
    the pad centre (the middle finger's middle): fingers ``tooth`` wide,
    ``gap`` apart, 8 mm long — a 8 x (3·tooth + 2·gap) bounding box."""
    top = 1.5 * tooth + gap
    ys = [-top, -top + tooth, -top + tooth + gap, -top + 2 * tooth + gap]
    x0, xs, x1 = -4.0, -3.0, 4.0
    return np.asarray(
        [
            (x0, -top),
            (x1, -top),
            (x1, ys[1]),
            (xs, ys[1]),
            (xs, ys[2]),
            (x1, ys[2]),
            (x1, -ys[2]),
            (xs, -ys[2]),
            (xs, -ys[1]),
            (x1, -ys[1]),
            (x1, top),
            (x0, top),
        ]
    )


def _comb_board(gap: float) -> tuple[TopoDesign, Polygon, Polygon, float]:
    ring = _comb(gap)
    top = float(ring[:, 1].max())
    cx, cy = 20.0, 20.0
    mid = 0.5 + gap / 2.0  # the upper gap's centre line, relative
    pins: list[dict[str, Any]] = [
        {
            "x": cx,
            "y": cy,
            "net": 2,
            "w": 8.0,
            "h": 2.0 * top,
            "shape": "polygon",
            "poly": ring,
        },
        # a foreign pin INSIDE the gap, and its partner out on the right
        {"x": cx + 2.0, "y": cy + mid, "net": 0, "w": 0.1, "h": 0.1},
        {"x": cx + 12.0, "y": cy + mid, "net": 0, "w": 0.4, "h": 0.4},
    ]
    # net 0 is held to the comb's own layer: the inner layers are free there
    d = _board(pins, n_nets=3, layer_force={0: 0})
    copper = Polygon(ring + [cx, cy])
    bbox = Polygon(_rect(cx - 4.0, cy - top, 8.0, 2.0 * top))
    return d, copper, bbox, mid


def test_comb_polygon_closes_only_its_real_copper() -> None:
    d, copper, bbox, _mid = _comb_board(gap=1.5)
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 4)
    in_box = _edges_in(mesh, bbox)
    on_copper = set(_edges_in(mesh, copper))
    assert on_copper and len(in_box) > len(on_copper), "gaps have edges of their own"
    walled = {e for e in in_box if any(p == 0 for p, _m in ctx.pad_contrib.get(e, ()))}
    assert walled == on_copper
    open_ = _open(ctx, d, 0)
    gap_edges = [e for e in in_box if e not in on_copper]
    assert any(int(open_[e]) & 1 for e in gap_edges), "a gap is free space on L0"


def test_wire_passes_through_a_gap_between_the_fingers_if_it_fits() -> None:
    d, copper, bbox, _mid = _comb_board(gap=1.5)  # 1.5 mm > 0.45 mm
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4, (_RULE,) * 3, pose=d.pose0)
    assert not w.unreachable
    (b,) = [b for b in w.branches if b.net == 0]
    path = LineString([tuple(p) for p in b.points])
    assert path.intersection(copper.buffer(-1e-6)).is_empty
    assert path.intersection(bbox).length > 1.0, "it went in between the fingers"


def test_wire_cannot_enter_a_gap_it_does_not_fit() -> None:
    d, _copper, _bbox, _mid = _comb_board(gap=0.3)  # 0.3 mm < 0.45 mm
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4, (_RULE,) * 3, pose=d.pose0)
    assert w.unreachable, "the pin in the slot is walled in by the fingers"


# -- R4: a zone is closed through its interior -------------------------------


def _keepout_board(*, net: str = "", layers: tuple[int, ...] = (1, 2, 3)):
    # a padless pin mid-zone gives the CDT an interior vertex to fan out
    pins = _two_pin_net_across(
        [
            {"x": 5.0, "y": 5.0, "net": 1},
            {"x": 20.0, "y": 22.0, "net": 1, "w": 0.0, "h": 0.0},
        ]
    )
    zone = TopoZone(
        kind="keepout" if not net else "fixed_copper",
        ring=_rect(14.0, 14.0, 12.0, 12.0),
        layers=layers,
        net=net,
        name="zone",
    )
    d = _board(pins, n_nets=2, zones=(zone,))
    return d, Polygon(_rect(14.0, 14.0, 12.0, 12.0))


def test_keepout_barring_inner_layers_leaves_its_interior_open_on_layer_0_only() -> (
    None
):
    d, zone = _keepout_board()
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 4)
    # (the 0.1 mm stub pad of the padless pin keeps a wall of its own)
    inside = [e for e in _edges_in(mesh, zone) if not ctx.pad_contrib.get(e)]
    # interior edges, not just the ring: the zone has no vertex but its corners
    ring_only = [e for e in inside if mesh.constrained[e]]
    assert len(inside) > len(ring_only), "edges inside the ring, not only on it"
    open_ = _open(ctx, d, 0)
    assert all(int(open_[e]) == 0b0001 for e in inside)
    outside = [e for e in range(len(mesh.edges)) if e not in set(inside)]
    assert any(int(open_[e]) == 0b1111 for e in outside)


def test_same_net_copper_zone_is_open_to_its_net_and_closed_to_others() -> None:
    d, zone = _keepout_board(net="N0", layers=())
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 4)
    inside = [e for e in _edges_in(mesh, zone) if not ctx.pad_contrib.get(e)]
    assert inside
    assert all(int(_open(ctx, d, 0)[e]) == 0b1111 for e in inside)
    assert all(int(_open(ctx, d, 1)[e]) == 0 for e in inside)


def test_own_pad_stays_open_for_its_net() -> None:
    d, _zone = _keepout_board()
    pad = Polygon(_rect(4.8, 4.8, 0.4, 0.4))
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 4)
    inside = _edges_in(mesh, pad)
    assert inside
    assert all(int(_open(ctx, d, 1)[e]) & 1 for e in inside), "own net leaves its pin"
    assert all(not int(_open(ctx, d, 0)[e]) & 1 for e in inside), "foreign net bars"


# -- review fixes ------------------------------------------------------------


def test_a_flipped_part_walls_the_other_outer_layer() -> None:
    """The side comes from the pose the mesh is built at, not the
    authored ``pin_side``: flipping the part moves its pad's wall."""
    d, pad = _gnd_pad_with_stub(_rect(30.0, 5.0, 1.0, 1.0))
    pose = d.pose0.copy()
    pose[2, 3] = 1.0  # the GND pad's part (P2) goes to the bottom
    flipped = Polygon(_rect(19.2, 19.5, 1.6, 1.0))  # mirrored about x = 20
    mesh = build_mesh(d, pose)
    ctx = _make_ctx(mesh, d, 4, pose)
    inside = _edges_in(mesh, flipped)
    assert inside
    open0 = _open(ctx, d, 0)
    assert all(int(open0[e]) & 1 for e in inside), "the top face is free"
    assert all(not int(open0[e]) & 0b1000 for e in inside), "the bottom is walled"


def test_a_pin_with_no_pad_size_keeps_a_wall() -> None:
    pins = _two_pin_net_across([{"x": 20.0, "y": 24.0, "net": 1, "w": 0.0, "h": 0.0}])
    d = _board(pins, n_nets=2)
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 4)
    assert ctx.pin_wall[2], "the stub ring walls the pin"
    assert all(not int(_open(ctx, d, 0)[e]) & 1 for e in ctx.pin_wall[2])


def test_comb_pin_off_its_copper_is_anchored_on_it() -> None:
    ring = _comb(1.5) + [0.0, 0.65]  # the pad centre now sits in a gap
    top = float(ring[:, 1].max())
    pins: list[dict[str, Any]] = [
        {
            "x": 20.0,
            "y": 20.0,
            "net": 0,
            "w": 8.0,
            "h": 2 * top,
            "shape": "polygon",
            "poly": ring,
        },
        {"x": 32.0, "y": 20.0, "net": 0},
    ]
    d = _board(pins, n_nets=1)
    mesh = build_mesh(d, d.pose0)
    copper = Polygon(ring + [20.0, 20.0])
    anchor = Point(mesh.verts[mesh.pin_vertex[0]])
    assert not copper.contains(Point(20.0, 20.0))
    assert copper.contains(anchor), "the vertex — the wire's end — is on the pad"
    assert anchor.distance(Point(20.0, 20.0)) < 0.3
    w = build_wires(mesh, d, 4, (_RULE,), pose=d.pose0)
    assert not w.unreachable
    # a pin already on its copper keeps its exact position
    on = _comb_board(1.5)[0]
    m2 = build_mesh(on, on.pose0)
    np.testing.assert_allclose(m2.verts[m2.pin_vertex[0]], [20.0, 20.0], atol=1e-9)


def test_comb_mesh_holds_no_flat_triangle() -> None:
    d, *_ = _comb_board(1.5)
    mesh = build_mesh(d, d.pose0)
    a, b, c = (mesh.verts[mesh.tris[:, k]] for k in range(3))
    area2 = np.abs(
        (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
        - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    )
    assert float(area2.min()) > 1e-9


def test_a_via_barrel_is_cut_only_by_drilled_pads(monkeypatch) -> None:
    """Under an SMD pad the barrel is still copper on the inner and
    opposite layers; only a drilled pad takes it out whole."""
    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace import realise

    d, pad = _gnd_pad_with_stub(_rect(30.0, 5.0, 1.0, 1.0))
    barrel = Point(20.0, 20.0).buffer(0.3)
    monkeypatch.setattr(
        pcb_realize,
        "result_copper_rows",
        lambda *a, **k: [
            {
                "ctype": "via",
                "layer": "",
                "net": "N1",
                "x": 20.0,
                "y": 20.0,
                "dia_mm": 0.6,
            }
        ],
    )
    ir = types.SimpleNamespace(
        stackup=[{"name": n} for n in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")],
        n_layers=4,
    )
    zones = realise._copper_zones(object(), d, ir, d.pose0)
    assert zones and all(z.layers == () for z in zones)
    kept = sum(Polygon(z.ring).area for z in zones)
    # the ring circumscribes the barrel (topo-fanclear): it must hold the
    # whole disc and reach no further than the sag bound past it
    assert barrel.area - 1e-9 <= kept <= barrel.buffer(0.02).area + 1e-9


# -- G2g: the assign pass on planted layering -------------------------------


def _cross_board(*, zones: tuple[TopoZone, ...] = (), layer_force=None) -> TopoDesign:
    """Net 0 a through-hole pair (6,20)-(34,20), net 1 an SMD pair
    (20,10)-(20,30): two straight corridors that cross at (20,20)."""
    pins = [
        {"x": 6.0, "y": 20.0, "net": 0, "side": 2},
        {"x": 34.0, "y": 20.0, "net": 0, "side": 2},
        {"x": 20.0, "y": 10.0, "net": 1},
        {"x": 20.0, "y": 30.0, "net": 1},
    ]
    return _board(pins, n_nets=2, zones=zones, layer_force=layer_force)


def _all_layer0(w):
    """Force every branch onto layer 0 with no planted vias — the state a
    same-layer pair reaches ``assign_layers`` in."""
    return dataclasses.replace(
        w,
        branches=tuple(
            dataclasses.replace(b, layers=(0,) * (len(b.edges) + 1), vias=())
            for b in w.branches
        ),
    )


def test_a_live_pair_resolves_by_a_zero_via_whole_branch_flip() -> None:
    """G2g fixture (i): the SMD net crosses the TH net on layer 0 in an
    open mesh; the through-hole branch moves to B.Cu whole — no cut, no
    via — and the plan comes out clean."""
    d = _cross_board()
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4, (_RULE, _RULE), pose=d.pose0)
    plan = plan_route(d, mesh, _all_layer0(w), 4)
    assert plan.crossings == ()
    assert plan.vias == ()
    th = next(i for i, b in enumerate(w.branches) if b.net == 0)
    smd = next(i for i, b in enumerate(w.branches) if b.net == 1)
    assert {p.layer for p in plan.pieces if p.branch == th} == {3}
    assert {p.layer for p in plan.pieces if p.branch == smd} == {0}


def test_a_walled_off_pair_is_a_refused_negotiation_and_an_unreachable() -> None:
    """G2g fixture (ii): the same pair, but the TH net is forced to layer
    0, the inner layers are walled over the SMD corridor (no edge can
    plant a hop), and net 0's fixed copper bars net 1's detours around
    either pad row — no move is legal. G2h: the chord split cannot pass
    the crossing either; the probe without it does, so net 0 is asked to
    yield — it cannot (boxed the same way). S1: the probe is a witness,
    never adopted copper — net 1's stranded comp is named unreachable
    against its crossing partner and net 0 keeps its clean wire."""
    zones = (
        TopoZone(
            kind="keepout",
            ring=_rect(14.0, 8.0, 12.0, 24.0),
            layers=(1, 2, 3),
            name="inner",
        ),
        TopoZone(
            kind="keepout", ring=_rect(0.0, 0.0, 40.0, 9.8), layers=(), name="south"
        ),
        TopoZone(
            kind="keepout", ring=_rect(0.0, 30.2, 40.0, 9.8), layers=(), name="north"
        ),
        TopoZone(
            kind="fixed_copper",
            ring=_rect(0.0, 0.0, 11.0, 40.0),
            layers=(),
            net="N0",
            name="west",
        ),
        TopoZone(
            kind="fixed_copper",
            ring=_rect(29.0, 0.0, 11.0, 40.0),
            layers=(),
            net="N0",
            name="east",
        ),
    )
    d = _cross_board(zones=zones, layer_force={0: 0})
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4, (_RULE, _RULE), pose=d.pose0)
    plan = plan_route(d, mesh, _all_layer0(w), 4)
    assert w.negotiated == ((1, 0, False),)
    assert plan.crossings == ()
    (u,) = w.unreachable
    assert u.net == 1
    assert u.blocked_by == (0,)
    assert u.layered
    assert 1 in plan.unreachable
    assert {b.net for b in w.branches} == {0}
    assert plan.vias == ()
    assert plan.no_via == ()


def test_a_chord_free_witness_that_crosses_nothing_is_adopted(monkeypatch) -> None:
    """S1 review-1: the chord/split view is conservative — its spoke-edge
    copies (P1) can refuse a route that crosses nothing, so the chord-free
    probe comes back ``comps == 1`` with an empty ``cross``. That witness
    is legal copper: ``build_wires`` must adopt it, not name the net
    unreachable. The refusal is fabricated — the first attempt's comps
    are split — so the probe and adoption use the real code path."""
    from precis.pcb.topoplace import wires as wires_mod

    pins = [
        {"x": 6.0, "y": 20.0, "net": 0, "side": 2},
        {"x": 34.0, "y": 20.0, "net": 0, "side": 2},
        {"x": 36.5, "y": 10.0, "net": 1},
        {"x": 36.5, "y": 30.0, "net": 1},
    ]
    d = _board(pins, n_nets=2)
    mesh = build_mesh(d, d.pose0)
    real_grow = wires_mod._grow_net
    refused = False

    def grow(ctx, mesh_, d_, n_layers, k, rule, occ, pin_vids, occupancy, v):
        res = real_grow(ctx, mesh_, d_, n_layers, k, rule, occ, pin_vids, occupancy, v)
        nonlocal refused
        if k == 1 and v.chords is not None and not refused:
            refused = True
            return dataclasses.replace(res, comps=[[0], [1]])
        return res

    monkeypatch.setattr(wires_mod, "_grow_net", grow)
    w = build_wires(mesh, d, 4, (_RULE, _RULE), pose=d.pose0)
    assert refused, "the fixture's refusal ran"
    assert {b.net for b in w.branches} == {0, 1}, "the witness is the wire"
    assert not w.unreachable
    assert w.negotiated == ()


# -- S2: the shove phase -----------------------------------------------------

_NOVIA = (_RULE[0], _RULE[1], 50.0)  # a 50 mm via plants nowhere


def _wall_pair(extra_pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two edge-flush horizontal walls, nets 0 (y = 15) and 2 (y = 25);
    whatever else the fixture adds goes between them in the pin list."""
    return [
        {"x": 0.3, "y": 15.0, "net": 0, "w": 0.6},
        {"x": 39.7, "y": 15.0, "net": 0, "w": 0.6},
        *extra_pins,
        {"x": 0.3, "y": 25.0, "net": 2, "w": 0.6},
        {"x": 39.7, "y": 25.0, "net": 2, "w": 0.6},
    ]


def test_the_shove_refuses_partners_that_fail_the_final_guard(monkeypatch) -> None:
    """Re-growing both flush walls clears topological crossings but leaves
    a swap member short-risk under the final emitter guard. Refuse that swap
    and keep the diagonal net's explicit failure certificate."""
    from precis.pcb.topoplace import wires as wm

    guarded = []
    oracle = wm._Shove._guarded

    def record(shove, st, branches, focus):
        bad = oracle(shove, st, branches, focus)
        guarded.append(bad)
        return bad

    monkeypatch.setattr(wm._Shove, "_guarded", record)
    pins = _wall_pair(
        [
            {"x": 2.0, "y": 10.0, "net": 1},
            {"x": 38.0, "y": 30.0, "net": 1},
        ]
    )
    d = _board(pins, n_nets=3)
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4, (_NOVIA,) * 3, pose=d.pose0)
    assert w.negotiated[0] == (1, 0, False), "the loop's negotiation was refused"
    assert w.shove_grows > 0, "the phase tried the blocking partners"
    assert not any(a for start in w.shove_rounds for a in start), (
        "no unsafe swap accepted"
    )
    assert any(guarded), "the actual emitter guard must reject a swap member"
    (u,) = w.unreachable
    assert u.net == 1 and u.layered
    assert {b.net for b in w.branches} == {0, 2}


def test_the_shove_accepts_a_clean_swap_that_passes_the_final_guard(
    monkeypatch,
) -> None:
    """Separated routes remain admissible after the actual final guard runs."""
    from precis.pcb.topoplace import wires as wm

    d = _board(
        [
            {"x": x, "y": y, "net": net}
            for net, y in enumerate((10.0, 30.0))
            for x in (6.0, 34.0)
        ],
        n_nets=2,
    )
    captured = []

    def capture(shove, st):
        captured.append((shove, st))
        return st

    with monkeypatch.context() as patch:
        patch.setattr(wm._Shove, "run", capture)
        build_wires(build_mesh(d, d.pose0), d, 4, (_RULE,) * 2)
    ((shove, st),) = captured
    st = dataclasses.replace(st, keys={0: (2, 0), 1: (1, 0)})
    atts = {}
    for net in (0, 1):
        att = shove._grow(net, st.occupancy.rebuild([]), [], shove.budget)
        assert att is not None
        atts[net] = dataclasses.replace(att, built=st.builts[net], cross=[])
    monkeypatch.setattr(shove, "_grow", lambda net, *_a, **_kw: atts[net])
    result = shove._swap(st, 0, (1,), [(1, b) for b in st.builts[1]], shove.budget)
    assert result is not None
    assert not result.fails(0) and not result.fails(1)
    branches = [b.branch for bl in result.builts.values() for b in bl]
    assert not shove._guarded(result, branches, {0, 1})


def test_the_accept_rule_refuses_a_swap_that_just_moves_the_failure() -> None:
    """The same two walls but net 1 edge-flush top and bottom: pinned,
    its own wire is a full-height wall no partner can detour around,
    so every swap just moves the failure — the accept rule refuses
    them all and net 1 stays named."""
    pins = _wall_pair(
        [
            {"x": 20.0, "y": 0.3, "net": 1, "h": 0.6},
            {"x": 20.0, "y": 39.7, "net": 1, "h": 0.6},
        ]
    )
    d = _board(pins, n_nets=3)
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4, (_NOVIA,) * 3, pose=d.pose0)
    assert w.shove_grows > 0, "the phase tried"
    assert not any(a for start in w.shove_rounds for a in start), "no swap accepted"
    assert w.negotiated == ((1, 0, False),)
    (u,) = w.unreachable
    assert u.net == 1 and u.layered
    assert {b.net for b in w.branches} == {0, 2}


@pytest.mark.parametrize("change_layer", [False, True], ids=["via", "layer-and-via"])
def test_a_probe_with_an_unrepairable_bigon_is_not_adopted(
    monkeypatch, change_layer: bool
) -> None:
    """Two geometric crossings cancel in 2D, but the planted via cannot
    move with a bigon swap. With a layer change only the first crossing
    is a short. Refusing the probe must preserve the earlier net."""
    from precis.pcb.topoplace import wires as wm
    from precis.pcb.topoplace.crossings import edge_xy

    d = _board(
        [
            {"x": 6.0, "y": 20.0, "net": 0, "side": 2},
            {"x": 34.0, "y": 20.0, "net": 0, "side": 2},
            {"x": 10.0, "y": 15.0, "net": 1, "side": 2},
            {"x": 30.0, "y": 15.0, "net": 1, "side": 2},
        ],
        n_nets=2,
        extras=[{"part": 1, "dy": 10.0, "w": 0.4, "h": 0.4, "side": 0}],
    )
    mesh = build_mesh(d, d.pose0)
    points = [(10.0, 15.0), (20.0, 24.0), (30.0, 15.0)]
    # Derive the injected bent wire's actual corridor from its geometric
    # intersections, retaining every crossed mesh edge and its adjacency.
    # The apex is inside a triangle; no synthetic shortcut edge is used.
    cuts = []
    arc = 0.0
    for p, q in itertools.pairwise(points):
        leg = LineString([p, q])
        for e, (u, v) in enumerate(mesh.edges):
            edge = LineString(mesh.verts[[u, v]])
            hit = leg.intersection(edge)
            if hit.geom_type != "Point":
                continue
            distance = leg.project(hit)
            if 1e-8 < distance < leg.length - 1e-8:
                t = edge.project(hit) / edge.length
                assert 1e-8 < t < 1.0 - 1e-8, "the fixture avoids mesh vertices"
                cuts.append((arc + distance, e, t))
        arc += leg.length
    cuts.sort()
    midpoint = LineString(points[:2]).interpolate(cuts[0][0] / 2)
    tris = [
        next(
            int(t)
            for t in mesh.edge_tris[cuts[0][1]]
            if t >= 0 and Polygon(mesh.verts[mesh.tris[t]]).contains(midpoint)
        )
    ]
    for _arc, e, _t in cuts:
        a, b = (int(t) for t in mesh.edge_tris[e])
        assert tris[-1] in (a, b)
        tris.append(b if tris[-1] == a else a)
        assert tris[-1] >= 0
    assert int(mesh.pin_vertex[3]) in mesh.tris[tris[-1]]
    edges = tuple(e for _arc, e, _t in cuts)
    ts = tuple(t for _arc, _e, t in cuts)
    hop = next(i for i, e in enumerate(edges) if edge_xy(mesh, e, ts[i])[1] > 21.0)
    x, y = edge_xy(mesh, edges[hop], ts[hop])
    pi = 1 if x < 20.0 else 2
    points.insert(pi, (x, y))
    branch = wm.WirePath(
        net=1,
        pin_a=2,
        pin_b=3,
        tris=tuple(tris),
        edges=edges,
        t=ts,
        points=tuple(points),
        layers=tuple(3 if change_layer and i > hop else 0 for i in range(len(tris))),
        vias=(wm.PlantedVia(hop, pi, x, y, 0.6, 0.3, 0.3),),
    )
    real_grow = wm._grow_net
    first = True
    saw_cancelled = False

    def grow(ctx_, mesh_, d_, n_layers, k, rule, occ, pin_vids, occupancy, view):
        res = real_grow(
            ctx_, mesh_, d_, n_layers, k, rule, occ, pin_vids, occupancy, view
        )
        nonlocal first, saw_cancelled
        if k == 0:
            if first:
                first = False
                return res
            # The earlier wire cannot yield during negotiation.
            return dataclasses.replace(res, comps=[[0], [1]])
        if view.chords is not None:
            return dataclasses.replace(res, comps=[[0], [1]], built=[], cross=[])
        hits = occupancy.crossings(branch)
        assert hits == [], "the real 2D oracle cancels this empty bigon"
        saw_cancelled = True
        return dataclasses.replace(
            res, comps=[[0, 1]], built=[wm._Built(branch, edges)], cross=hits
        )

    monkeypatch.setattr(wm, "_net_order", lambda *_args: [0, 1])
    monkeypatch.setattr(wm, "_grow_net", grow)
    w = build_wires(mesh, d, 4, (_RULE, _RULE), pose=d.pose0)
    assert saw_cancelled
    assert {b.net for b in w.branches} == {0}
    (unreachable,) = w.unreachable
    assert unreachable.net == 1
    assert unreachable.blocked_by == (0,)
    assert unreachable.layered


@pytest.mark.parametrize("foreign_pad", [False, True], ids=["repairable", "pad-hit"])
def test_a_crossing_witness_requires_an_emittable_bigon_swap(
    foreign_pad: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty same-layer bigon can swap unless it acquires a foreign pad."""
    from precis.pcb.topoplace import realise as rm
    from precis.pcb.topoplace import wires as wm
    from precis.pcb.topoplace.crossings import branch_geom, crossing_events
    from precis.pcb.topoplace.mesh import TopoMesh

    # The two vertical mesh edges are crossed at y=0 by a and y=1 by b.
    mesh = TopoMesh(
        verts=np.asarray([(1.0, -2.0), (1.0, 2.0), (4.0, -2.0), (4.0, 2.0)]),
        edges=np.asarray([(0, 1), (2, 3)]),
        tris=np.zeros((0, 3), dtype=np.int32),
        tri_edges=np.zeros((0, 3), dtype=np.int32),
        edge_tris=np.zeros((2, 2), dtype=np.int32),
        constrained=np.zeros(2, dtype=bool),
        pad_vertex={},
    )
    a = wm.WirePath(
        net=0,
        pin_a=0,
        pin_b=1,
        points=((0.0, 0.0), (5.0, 0.0)),
        tris=(0, 1, 2),
        edges=(0, 1),
        t=(0.5, 0.5),
        layers=(0, 0, 0),
    )
    b = dataclasses.replace(
        a,
        net=1,
        points=((0.0, -1.0), (1.0, 1.0), (4.0, 1.0), (5.0, -1.0)),
        t=(0.75, 0.75),
    )
    assert len(crossing_events(mesh, [a, b])[(0, 1)]) == 2
    pad = _rect(2.3, 0.9, 0.4, 0.4)
    occupancy: Any = types.SimpleNamespace(
        mesh=mesh,
        _line_tree=None,
        lines=[(b, LineString(b.points), branch_geom(mesh, b), b.layers)],
        obstacles=np.asarray([(2.5, 1.1)]) if foreign_pad else np.zeros((0, 2)),
        corridor=types.SimpleNamespace(
            ctx=types.SimpleNamespace(
                pad_poly=(Polygon(pad),) if foreign_pad else (),
                pad_pin=np.asarray([0]) if foreign_pad else np.zeros(0, dtype=int),
            ),
            d=_board([{"x": 2.5, "y": 1.1, "net": 1}], n_nets=2),
            rules_used=(_RULE, _RULE),
        ),
    )
    occupancy.nearby = lambda line: wm._Occupancy.nearby(occupancy, line)
    # The pad's centre is outside the bigon, so homotopy alone cancels it.
    assert wm._Occupancy.crossings(occupancy, a) == []
    assert bool(wm._Occupancy.crossings(occupancy, a, witness=True)) == foreign_pad

    if not foreign_pad:
        return
    d = _board(
        [
            {"x": x, "y": y, "net": net, "w": 0.0, "h": 0.0}
            for net, points in ((0, a.points), (1, b.points))
            for x, y in (points[0], points[-1])
        ],
        n_nets=2,
        extras=[{"part": 2, "dx": 2.5, "dy": 2.1, "w": 0.4, "h": 0.4, "pin": 2}],
    )
    d = dataclasses.replace(d, outline=_rect(-2.0, -3.0, 9.0, 8.0)[:4])
    wires = wm.Wires(
        branches=(a, b),
        order=((0, 1), (0, 1)),
        capacity_mm=np.zeros(2),
        net_layers=(frozenset({0}),) * 2,
        unreachable=(),
        gap_free_mm=np.full((2, 4), -1.0),
        gap_cap=np.full((2, 4), -1),
    )
    original = rm._repair_bigon
    results = []
    ignore_pad_refusal = False

    def repair(a_, b_, x, y, gap, on_foreign_pad, **kwargs):
        # Remove exactly one refusal from the shared oracle, retaining
        # all geometry, via and layer checks. Both callers must respond.
        if ignore_pad_refusal:
            on_foreign_pad = lambda _geom, _net: 0.0
        result = original(a_, b_, x, y, gap, on_foreign_pad, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(rm, "_repair_bigon", repair)
    assert wm._Occupancy.crossings(occupancy, a, witness=True)
    assert results and all(result is None for result in results)
    results.clear()
    refused_paths = rm._spoke_offsets(d, mesh, wires, 4)
    assert results and all(result is None for result in results)

    ignore_pad_refusal = True
    results.clear()
    assert wm._Occupancy.crossings(occupancy, a, witness=True) == []
    assert results and all(result is not None for result in results)
    results.clear()
    repaired_paths = rm._spoke_offsets(d, mesh, wires, 4)
    assert results and all(result is not None for result in results)
    assert repaired_paths != refused_paths
