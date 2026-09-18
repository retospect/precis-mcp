"""Shape-aware pad claims — gripe 346962.

Before this change :meth:`~precis.pcb.maze.OccupancyGrid.stamp_disk` (and
:func:`~precis.pcb.realize._stamp_pads`, which drove it) only ever claimed
an ENCLOSING CIRCLE for a pad, regardless of the pad's true shape. At fine
pitch that circle is wider than the pitch itself — a 2.0mm x 0.5mm QFP pad
claims a 1.03mm-radius disc against an 0.8mm pin pitch — so a
neighbouring pad's disc could claim a pad's own CENTRE cell, and
:meth:`~precis.pcb.maze.OccupancyGrid.route` refused before ever searching
(``goal_ok`` false, zero expansions).

Two levels of coverage:

1. :class:`~precis.pcb.maze.OccupancyGrid`'s new shape primitives
   (``stamp_shape``/``stamp_pad_shape``/``claim_centre``) in isolation —
   rect vs. circle vs. polygon claims, CONTESTED semantics, and the
   sub-cell fallback.
2. A QFP-scale regression through :func:`~precis.pcb.realize._stamp_pads`
   itself, reproducing the gripe's own numbers (2.0mm x 0.5mm pads, 0.8mm
   pitch, all four sides of a ring so both the un-rotated and the
   ``rot=90`` swapped orientation are exercised, the way a real cached
   footprint authors a QFP): every pad keeps its own centre cell, and a
   route from a field pad to one of the ring pads succeeds.
"""

from __future__ import annotations

import math

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph, pin_point
from precis.pcb.maze import CONTESTED, FREE, GridSpec, OccupancyGrid, PadShape, grid_for
from precis.pcb.realize import _pad_shape, _side_layer, _stamp_pads, pad_geometry
from precis.pcb.session import apply_real_pin_offsets


def _spec(
    nx: int = 60, ny: int = 60, n_layers: int = 1, pitch: float = 0.1
) -> GridSpec:
    return GridSpec(0.0, 0.0, pitch, nx, ny, n_layers)


# ── OccupancyGrid.stamp_shape / stamp_pad_shape / claim_centre ──────────


def test_rect_claim_covers_exactly_the_true_box_not_the_enclosing_circle():
    grid = OccupancyGrid(_spec(), clearance_mm=0.0)
    cx, cy = 3.0, 3.0
    shape = PadShape("rect", cx, cy, w_mm=2.0, h_mm=0.5)
    grid.stamp_pad_shape((0,), shape, net_id=1)
    owner = grid.owner[0]

    # Inside the box (short axis is Y, half-height 0.25mm).
    ix, iy = grid.spec.to_cell(cx + 0.9, cy)
    assert owner[iy, ix] == 1
    ix, iy = grid.spec.to_cell(cx, cy + 0.2)
    assert owner[iy, ix] == 1

    # Outside the box but INSIDE the old enclosing circle
    # (radius hypot(2.0, 0.5)/2 ~= 1.03mm) -- the corner region a
    # circle claim would have stolen from a neighbour, a rect claim
    # must leave free.
    corner_r = math.hypot(2.0, 0.5) / 2.0
    assert corner_r > 1.0
    ix, iy = grid.spec.to_cell(cx + 0.9, cy + 0.4)
    assert math.hypot(0.9, 0.4) < corner_r
    assert owner[iy, ix] == FREE


def test_poly_claim_covers_the_ring_interior_and_nothing_outside():
    grid = OccupancyGrid(_spec(), clearance_mm=0.0)
    poly = ((5.0, 5.0), (5.6, 5.0), (5.6, 5.6), (5.0, 5.6))
    shape = PadShape("poly", 5.3, 5.3, poly=poly)
    grid.stamp_pad_shape((0,), shape, net_id=2)
    owner = grid.owner[0]

    ix, iy = grid.spec.to_cell(5.3, 5.3)
    assert owner[iy, ix] == 2
    ix, iy = grid.spec.to_cell(6.0, 6.0)
    assert owner[iy, ix] == FREE


def test_stamp_shape_dilates_a_polygon_claim_by_its_margin():
    grid = OccupancyGrid(_spec(), clearance_mm=0.0)
    poly = ((5.0, 5.0), (5.6, 5.0), (5.6, 5.6), (5.0, 5.6))
    shape = PadShape("poly", 5.3, 5.3, poly=poly)
    grid.stamp_shape((0,), shape, 0.2, net_id=2)
    owner = grid.owner[0]

    # Just outside the ring, but within the 0.2mm margin of its nearest
    # edge (x=5.6).
    ix, iy = grid.spec.to_cell(5.7, 5.3)
    assert owner[iy, ix] == 2
    # Well outside both the ring and the margin.
    ix, iy = grid.spec.to_cell(6.0, 6.0)
    assert owner[iy, ix] == FREE


def test_shaped_contest_semantics_match_stamp_disk():
    grid = OccupancyGrid(_spec(), clearance_mm=0.05)
    s1 = PadShape("circle", 1.0, 1.0, 0.3, 0.3)
    s2 = PadShape("circle", 1.05, 1.0, 0.3, 0.3)
    grid.stamp_shape((0,), s1, 0.05, net_id=7, contest=True)
    grid.stamp_shape((0,), s2, 0.05, net_id=9, contest=True)
    owners = set(grid.owner[0].ravel().tolist())
    assert CONTESTED in owners
    assert 7 in owners and 9 in owners


def test_sub_cell_shape_falls_back_to_its_own_nearest_cell():
    grid = OccupancyGrid(_spec(pitch=1.0, nx=10, ny=10), clearance_mm=0.05)
    tiny = PadShape("rect", 3.3, 3.3, w_mm=0.01, h_mm=0.01)
    grid.stamp_pad_shape((0,), tiny, net_id=5)
    cx, cy = grid.spec.to_cell(3.3, 3.3)
    assert grid.owner[0, cy, cx] == 5


def test_claim_centre_overrides_even_a_contested_cell():
    grid = OccupancyGrid(_spec(), clearance_mm=0.05)
    ix, iy = grid.spec.to_cell(0.5, 0.5)
    grid.owner[0, iy, ix] = CONTESTED
    grid.claim_centre((0,), 0.5, 0.5, net_id=3)
    assert grid.owner[0, iy, ix] == 3


# ── QFP-scale regression: gripe 346962's own numbers ─────────────────────

#: 2.0mm x 0.5mm pads, 0.8mm pitch -- the gripe's own figures. The base
#: pad is authored LONG-axis-along-Y (a bottom/top edge pad's natural,
#: un-rotated orientation); a side (left/right) pad gets the SAME base
#: pad with ``"rot": 90`` -- the way a real cached QFP footprint authors
#: it -- which `padplace.place_footprint_pads` swaps into a
#: long-axis-along-X board footprint, exercising `pad_geometry`'s
#: rotation-aware axis_aligned/swap path (gripe 346962 item 2).
_PAD_W, _PAD_H = 0.5, 2.0
_PITCH = 0.8
_HALF = 1.6  # ring half-extent


def _qfp_ring_footprint() -> dict:
    offsets = (-_PITCH, 0.0, _PITCH)
    pads = []
    pin_map = {}
    number = 1

    def _pad(x: float, y: float, rot: float, name: str) -> None:
        nonlocal number
        pads.append(
            {
                "number": str(number),
                "shape": "RECT",
                "x": x,
                "y": y,
                "w": _PAD_W,
                "h": _PAD_H,
                "rot": rot,
                "layer": "F.Cu",
                "drill": None,
            }
        )
        pin_map[str(number)] = {"name": name, "tags": []}
        number += 1

    for i, dx in enumerate(offsets):
        _pad(dx, -_HALF, 0.0, f"B{i + 1}")  # bottom edge, un-rotated
    for i, dy in enumerate(offsets):
        _pad(_HALF, dy, 90.0, f"R{i + 1}")  # right edge, rot=90 (swapped)
    for i, dx in enumerate(offsets):
        _pad(dx, _HALF, 0.0, f"T{i + 1}")  # top edge, un-rotated
    for i, dy in enumerate(offsets):
        _pad(-_HALF, dy, 90.0, f"L{i + 1}")  # left edge, rot=90 (swapped)
    return {"pads": pads, "pin_map": pin_map}


def _qfp_ring_graph() -> dict:
    """One ``U1`` instance carrying the ring above, plus a ``U2.F1`` field
    pin far away sharing ``B2``'s net (the "route from a field pad to one
    of the ring pads" leg) -- every other ring pin sits on its own
    singleton net, same as an independent EWOD electrode/sink escape."""
    pin_names = (
        [f"B{i + 1}" for i in range(3)]
        + [f"R{i + 1}" for i in range(3)]
        + [f"T{i + 1}" for i in range(3)]
        + [f"L{i + 1}" for i in range(3)]
    )
    nets = [
        {
            "name": "FIELD_NET",
            "members": [{"refdes": "U1", "pin": "B2"}, {"refdes": "U2", "pin": "F1"}],
        }
    ]
    for name in pin_names:
        if name == "B2":
            continue
        nets.append({"name": f"NET_{name}", "members": [{"refdes": "U1", "pin": name}]})
    return {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "U2", "x": 10.0, "y": 0.0},
        ],
        "nets": nets,
    }


def test_qfp_ring_at_0p8mm_pitch_keeps_every_pad_its_own_centre_cell_and_routes():
    ir = from_graph(_qfp_ring_graph(), stackup=DEFAULT_STACKUP)
    footprints = {"U1": _qfp_ring_footprint()}
    # `from_graph` alone only ever gives a pin its SYNTHESIZED
    # landpattern position (package-family guesswork); a real footprint's
    # actual pad coordinates only reach `pin_point` through this same
    # gripe-338983 join `pad_geometry` already uses for SIZE (module
    # docstring's own "route through session.apply_real_pin_offsets"
    # note) -- without it every point below is the synthesized bound, not
    # this fixture's real 0.8mm-pitch ring at all.
    apply_real_pin_offsets(ir, footprints)
    pad_geoms = pad_geometry(ir, footprints)

    pads: list = []
    pins_by_label: dict[str, int] = {}
    for pid in range(ir.n_pins):
        point = pin_point(ir, pid)
        assert point is not None
        geom = pad_geoms[pid]
        inst_rot = float(ir.inst_rot[int(ir.pin_instance[pid])])
        shape = _pad_shape(geom, point, inst_rot)
        layers = (_side_layer(ir, int(ir.pin_instance[pid]), [0]),)
        net = int(ir.pin_net[pid])
        pads.append((point, net, shape, layers))
        if str(ir.instance_refdes[int(ir.pin_instance[pid])]) == "U1":
            pins_by_label[str(ir.pin_label[pid])] = pid

    # Sanity: the fixture actually bites -- the OLD enclosing-circle
    # radius for these pads is well over half the 0.8mm pitch, so two
    # adjacent ring pads' circles genuinely overlap.
    enclosing_r = math.hypot(_PAD_W, _PAD_H) / 2.0
    assert enclosing_r > _PITCH / 2.0

    grid = OccupancyGrid(
        grid_for([p for p, _, _, _ in pads], n_layers=1, margin_mm=3.0),
        clearance_mm=0.1,
    )
    _stamp_pads(grid, pads)

    for label, pid in pins_by_label.items():
        point = pin_point(ir, pid)
        assert point is not None
        net = int(ir.pin_net[pid])
        layer = _side_layer(ir, int(ir.pin_instance[pid]), [0])
        ix, iy = grid.spec.to_cell(*point)
        assert grid.owner[layer, iy, ix] == net, (
            f"pin {label}'s own centre cell is not owned by its own net "
            f"(owner={grid.owner[layer, iy, ix]}, expected {net})"
        )

    b2_pid = pins_by_label["B2"]
    b2_point = pin_point(ir, b2_pid)
    field_pid = next(
        p
        for p in range(ir.n_pins)
        if str(ir.instance_refdes[int(ir.pin_instance[p])]) == "U2"
    )
    field_point = pin_point(ir, field_pid)
    assert b2_point is not None and field_point is not None
    net_id = int(ir.pin_net[b2_pid])
    assert net_id == int(ir.pin_net[field_pid])

    path = grid.route(
        net_id,
        field_point,
        b2_point,
        layers=[0],
        width_mm=0.15,
        pad_layer=0,
    )
    assert path is not None, (
        "a field pad could not reach an 0.8mm-pitch ring pad's own pad"
    )
