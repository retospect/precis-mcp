"""Slice 2a legalize pass: ``_part_nets`` pin→net-name mapping and the
same-net zone exemption. ``net_name`` is indexed by the compacted
``net_pins`` slot while ``pin_net`` carries ir net ids — plane and
singleton nets are dropped, so the two index spaces drift apart on any
board that has them (e22b945d6's IndexError on nano/ewod, and the
silent wrong-name reads on id-aligned boards). Plane-net pins keep
their exemption through the decl tables: GND and power are normal
nets, there are no pours (Reto 2026-10-26).

Plus the hole-zone coverage the r4 review asked for: a part-owned
hole's ring rides its owner's pose (``zone_world_ring``, incl. a
rotated + side-flipped owner), the owner exemption (a part is not
shoved off its own hole), plated-vs-bare keep-out widths, part-owned
vs board holes on ``design._zones``, and the in-sweep refresh of a
moved owner's ring."""

from __future__ import annotations

from typing import Any

import numpy as np
from shapely.geometry import Polygon

from precis.pcb import drc
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import FixedVia, MountingHole, from_graph
from precis.pcb.pose import point_to_world
from precis.pcb.topoplace import legalize
from precis.pcb.topoplace.design import TopoDesign, TopoZone, design_from_ir
from precis.pcb.topoplace.legalize import legalize_pose
from precis.pcb.topoplace.proxy import (
    _net_wires,
    all_pin_world,
    evaluate,
    zone_world_ring,
)

_RING = np.asarray([[-0.4, -0.4], [0.4, -0.4], [0.4, 0.4], [-0.4, 0.4], [-0.4, -0.4]])


def _design(
    *,
    parts: tuple[str, ...],
    pin_part: list[int],
    pin_net: list[int],
    pin_xy: list[list[float]] | np.ndarray | None = None,
    net_pins: list[list[int]] | None = None,
    net_name: tuple[str, ...] = (),
    decl_nets: tuple[list[int], ...] = (),
    decl_net_ir: list[int] | None = None,
    decl_net_plane: list[bool] | None = None,
    decl_net_name: tuple[str, ...] = (),
    part_outline: tuple[np.ndarray, ...] = (),
    zones: tuple[TopoZone, ...] = (),
    pin_wh: list[list[float]] | None = None,
    pin_role: list[str] | None = None,
    pose0: list[list[float]],
) -> TopoDesign:
    n_d = len(decl_nets)
    return TopoDesign(
        parts=parts,
        pin_xy=np.asarray(
            pin_xy if pin_xy is not None else np.zeros((len(pin_part), 2)),
            dtype=np.float64,
        ),
        pin_part=np.asarray(pin_part, dtype=np.int64),
        pin_side=np.zeros(len(pin_part), dtype=np.int8),
        pin_label=np.asarray(["1"] * len(pin_part), dtype=object),
        pin_net=np.asarray(pin_net, dtype=np.int32),
        pin_wh=np.asarray(
            pin_wh if pin_wh is not None else np.zeros((len(pin_part), 2)),
            dtype=np.float64,
        ),
        pin_role=np.asarray(
            pin_role if pin_role is not None else [""] * len(pin_part),
            dtype=object,
        ),
        net_pins=tuple(np.asarray(n, dtype=np.int64) for n in (net_pins or [])),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(len(parts), dtype=bool),
        freedoms=np.full(len(parts), 3, dtype=np.uint8),
        outline=np.asarray([[-5.0, -5.0], [25.0, -5.0], [25.0, 25.0], [-5.0, 25.0]]),
        pose0=np.asarray(pose0, dtype=np.float64),
        decl_nets=tuple(np.asarray(n, dtype=np.int64) for n in decl_nets),
        decl_net_ir=np.asarray(decl_net_ir or [-1] * n_d, dtype=np.int32),
        decl_net_plane=np.asarray(decl_net_plane or [False] * n_d, dtype=bool),
        decl_net_name=decl_net_name,
        part_outline=part_outline,
        net_name=net_name,
        zones=zones,
    )


def test_part_nets_dropped_nets_do_not_break_the_mapping() -> None:
    """ir ids 0-3 collapse to slots 0-1 when the plane (GND) and
    singleton (TP) nets drop out of ``net_pins`` — indexing
    ``net_name`` by ``pin_net`` overran the table. Routed pins still
    name their slot's net; the decl'd plane pin falls back to
    ``decl_net_name``; the undeclared singleton gets no name."""
    # ir nets: 0=GND (plane, dropped), 1=TP (singleton, dropped),
    # 2=SDA -> slot 0, 3=SCL -> slot 1.
    d = _design(
        parts=("U1", "J1"),
        pin_part=[0, 0, 0, 1, 1, 1],
        pin_net=[0, 1, 3, 3, 2, 2],
        net_pins=[[4, 5], [2, 3]],
        net_name=("SDA", "SCL"),
        decl_nets=([0],),
        decl_net_ir=[0],
        decl_net_plane=[True],
        decl_net_name=("GND",),
        pose0=[[0, 0, 0, 0], [10, 0, 0, 0]],
    )
    assert legalize._part_nets(d, 0) == {"GND", "SCL"}
    assert legalize._part_nets(d, 1) == {"SDA", "SCL"}


def test_part_nets_names_a_pin_own_net_not_a_neighbour() -> None:
    """One dropped net shifts every later slot down by one: the pin's
    ir id lands inside ``net_name`` but on the NEXT net's row — the
    silent misread id-aligned boards (taz15, heater) carried. Each pin
    must come back with its own net's name."""
    # ir nets: 0=GND (plane, dropped), 1=SIG_A -> slot 0, 2=SIG_B ->
    # slot 1.
    d = _design(
        parts=("U1", "U2", "J1"),
        pin_part=[0, 1, 2, 2],
        pin_net=[1, 2, 1, 2],
        net_pins=[[0, 2], [1, 3]],
        net_name=("SIG_A", "SIG_B"),
        pose0=[[0, 0, 0, 0], [5, 0, 0, 0], [10, 0, 0, 0]],
    )
    assert legalize._part_nets(d, 0) == {"SIG_A"}
    assert legalize._part_nets(d, 1) == {"SIG_B"}


def test_plane_pin_keeps_the_same_net_zone_exemption() -> None:
    """A part whose only net is a plane net still owns that net's
    authored copper: sitting on a GND fixed-copper zone is legal and
    the part is not shoved off. The foreign-net part on the same zone
    moves."""
    zone = TopoZone(
        kind="fixed_copper",
        ring=np.asarray([[0.0, 0.0], [6.0, 0.0], [6.0, 4.0], [0.0, 4.0], [0.0, 0.0]]),
        net="GND",
        name="fixed copper track",
    )
    # ir nets: 0=GND (plane, dropped — two members change nothing),
    # 1=SIG -> slot 0.
    d = _design(
        parts=("U1", "U2", "J1"),
        pin_part=[0, 1, 2, 2],
        pin_net=[0, 1, 0, 1],
        net_pins=[[1, 3]],
        net_name=("SIG",),
        decl_nets=([0, 2],),
        decl_net_ir=[0],
        decl_net_plane=[True],
        decl_net_name=("GND",),
        part_outline=(_RING, _RING, _RING),
        zones=(zone,),
        pose0=[[2.0, 2.0, 0, 0], [4.0, 2.0, 0, 0], [15.0, 15.0, 0, 0]],
    )
    assert legalize._part_nets(d, 0) == {"GND"}
    res = legalize_pose(d, d.pose0)
    assert res.resolved
    np.testing.assert_allclose(res.pose[0], d.pose0[0])
    assert not np.allclose(res.pose[1], d.pose0[1])
    assert not any(a == 0 for a, _b, _area in res.remaining)


# ── hole zones (9bfd62c9f / 5f11d9e79 coverage) ─────────────────────

_HOLE = np.asarray([[-0.8, -0.8], [0.8, -0.8], [0.8, 0.8], [-0.8, 0.8], [-0.8, -0.8]])


def test_zone_world_ring_rides_a_rotated_flipped_owner() -> None:
    """A part_local ring is stored in the owner's footprint frame: a
    90°-cw bottom-side pose must place it exactly as
    ``pose.point_to_world`` does — mirror then rotate."""
    zone = TopoZone(kind="mounting_hole", ring=_HOLE, part=0, part_local=True)
    d = _design(parts=("J1",), pin_part=[], pin_net=[], pose0=[[3.0, 2.0, 0.0, 0.0]])
    pose = np.asarray([[3.0, 2.0, 1.0, 1.0]])  # rot 90° cw, bottom side
    np.testing.assert_allclose(
        zone_world_ring(d, zone, pose),
        np.asarray(
            [point_to_world(x, y, 3.0, 2.0, 90.0, mirrored=True) for x, y in _HOLE]
        ),
        atol=1e-12,
    )
    # and it MOVES with the pose — a second pose re-places it, 270° cw
    # on the top side
    np.testing.assert_allclose(
        zone_world_ring(d, zone, np.asarray([[8.0, 2.0, 3.0, 0.0]])),
        np.asarray(
            [point_to_world(x, y, 8.0, 2.0, 270.0, mirrored=False) for x, y in _HOLE]
        ),
        atol=1e-12,
    )


def test_owner_not_shoved_off_its_own_hole() -> None:
    """A connector's courtyard may cover its own standoff hole: the
    zone sweep exempts the owner (``z.part``), while a neighbour parked
    on the same hole takes the full MTV off it."""
    zone = TopoZone(
        kind="mounting_hole",
        ring=_HOLE,
        part=0,
        part_local=True,
        name="J1 standoff",
    )
    # J1 at (5,5) sits on its own hole (world 5,5); U1 at (6.0,5) has
    # its courtyard (5.6..6.4) on the hole's west rim (..5.8) but clear
    # of J1's courtyard (..5.4) — the hole blocks U1, never J1.
    d = _design(
        parts=("J1", "U1"),
        pin_part=[],
        pin_net=[],
        part_outline=(_RING, _RING),
        zones=(zone,),
        pose0=[[5.0, 5.0, 0, 0], [6.0, 5.0, 0, 0]],
    )
    res = legalize_pose(d, d.pose0)
    np.testing.assert_allclose(res.pose[0], d.pose0[0])
    assert res.pose[1, 0] > 6.1  # pushed +x, off the hole's rim
    assert not any(a == 0 and b >= len(d.parts) for a, b, _ in res.remaining)


def test_zone_sweep_refreshes_a_moved_owners_ring() -> None:
    """``_legalize_zones`` mutates ``pose`` and the part-owned rings in
    the dict it was handed: a moved owner's hole must not stay
    registered at its pre-move spot for the rest of the round."""
    keepout = np.asarray([[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]])
    zones = (
        TopoZone(kind="mounting_hole", ring=_HOLE, part=0, part_local=True),
        TopoZone(kind="keepout", ring=keepout),
    )
    # J1 at (1,1): inside the keepout (the blocking conflict) AND on
    # its own hole (exempt — it moves off the keepout, not the hole).
    d = _design(
        parts=("J1",),
        pin_part=[],
        pin_net=[],
        part_outline=(_RING,),
        zones=zones,
        pose0=[[1.0, 1.0, 0, 0]],
    )
    pose = d.pose0.copy()
    geoms = [legalize._ring_geom(d, pose[i], i) for i in range(len(d.parts))]
    zgs = legalize._zone_geoms(d, pose)
    moved, _ = legalize._legalize_zones(
        d,
        pose,
        geoms,
        pose[:, 3] >= 0.5,
        legalize.through_hole_parts(d),
        zgs,
        [set()],
    )
    assert moved > 0.0
    np.testing.assert_allclose(
        np.asarray(zgs[0].exterior.coords),
        np.asarray(zone_world_ring(d, d.zones[0], pose), dtype=np.float64),
    )


def test_zones_plated_part_owned_vs_bare_board_hole() -> None:
    """A plated annulus (``ring_dia_mm``) widens the keep-out past the
    drill; a part-owned hole stores an owner-local ring
    (``part_local``), a board hole a static world ring at its authored
    position."""
    ir = from_graph(
        {"instances": [{"refdes": "CN1"}], "nets": []},
        mounting_holes=(
            MountingHole(
                x=15.0,
                y=10.0,
                drill_mm=3.0,
                ring_dia_mm=8.0,
                plated=True,
                part="CN1",
                local_x=5.0,
                local_y=0.0,
            ),
            MountingHole(x=10.0, y=20.0, drill_mm=3.0),
        ),
    )
    ir.inst_x[0], ir.inst_y[0] = 10.0, 10.0
    td = design_from_ir(ir, {})
    owned, board = td.zones
    assert owned.part == 0 and owned.part_local
    assert board.part == -1 and not board.part_local
    assert Polygon(owned.ring).area > Polygon(board.ring).area
    np.testing.assert_allclose(
        np.asarray(zone_world_ring(td, owned, td.pose0))[:-1].mean(axis=0),
        [15.0, 10.0],
        atol=1e-9,
    )
    np.testing.assert_allclose(np.asarray(board.ring)[:-1].mean(axis=0), [10.0, 20.0])


def test_zones_derives_local_xy_for_a_legacy_part_owned_hole() -> None:
    """A part-owned feature hole carrying no local_x/local_y still
    rides its owner: the ring is derived in the owner's frame at
    import — authored (14,10) under owner (10,10) rot 90° cw is local
    (0,4) — and re-placed under the owner's CURRENT pose."""
    ir = from_graph(
        {"instances": [{"refdes": "CN1"}], "nets": []},
        mounting_holes=(MountingHole(x=14.0, y=10.0, drill_mm=3.0, part="CN1"),),
    )
    ir.inst_x[0], ir.inst_y[0], ir.inst_rot[0] = 10.0, 10.0, 90.0
    td = design_from_ir(ir, {})
    (z,) = td.zones
    assert z.part == 0 and z.part_local
    np.testing.assert_allclose(
        np.asarray(zone_world_ring(td, z, td.pose0))[:-1].mean(axis=0),
        [14.0, 10.0],
        atol=1e-9,
    )
    moved = td.pose0.copy()
    moved[0] = [20.0, 10.0, 0.0, 0.0]
    np.testing.assert_allclose(
        np.asarray(zone_world_ring(td, z, moved))[:-1].mean(axis=0),
        [20.0, 14.0],
        atol=1e-9,
    )


# ── S5: fixed vias as anchors + the per-pad exemption (gr520903) ────


def _via_ring(cx: float, cy: float, r: float = 0.2) -> np.ndarray:
    return np.asarray(
        [
            [cx - r, cy - r],
            [cx + r, cy - r],
            [cx + r, cy + r],
            [cx - r, cy + r],
            [cx - r, cy - r],
        ]
    )


def test_fixed_via_becomes_an_anchor_in_the_proxy() -> None:
    """S5 (gr520903): an authored fixed via joins its net's MST as an
    immovable world point — the airwire runs pad→via→pad, so the
    authored pose where the sink pad sits on its via keeps 0 crossings,
    and moving the sink off its via costs length because the anchor
    does not follow (the ewod-replay failure: the proxy scored the
    sink's authored pose at 47 crossings and 'improved' it to 25 by
    moving it off the vias)."""
    ir = from_graph(
        {
            "instances": [
                {"refdes": "SINK", "x": 0.0, "y": 0.0},
                {"refdes": "GEN", "x": 20.0, "y": 0.0},
            ],
            "nets": [
                {
                    "name": "X",
                    "members": [
                        {"refdes": "SINK", "pin": "1"},
                        {"refdes": "GEN", "pin": "1"},
                    ],
                }
            ],
        }
    )
    ir.pin_dx[0], ir.pin_dy[0] = 10.0, 0.0  # SINK's pad lands on the via
    ir.pin_dx[1], ir.pin_dy[1] = 0.0, 0.0  # GEN's pad at its own origin
    ir.fixed_vias = (FixedVia(x=10.0, y=0.0, dia_mm=0.75, net="X"),)
    td = design_from_ir(ir, {})
    np.testing.assert_allclose(td.anchor_xy, [[10.0, 0.0]])
    assert td.net_anchors[0].tolist() == [0]
    assert td.anchor_net == ("X",)
    wires = _net_wires(td, td.net_pins[0], all_pin_world(td, td.pose0), 0)
    # The MST includes the via point itself as an endpoint.
    assert any(
        np.allclose(w[0:2], [10.0, 0.0]) or np.allclose(w[2:4], [10.0, 0.0])
        for w in wires
    )
    crossings, length = evaluate(td, td.pose0)
    assert crossings == 0
    # SINK 5 mm off-line from its via: the wire must still pass (10,0)
    # — anchored it detours pad→via→pad (15 mm); without the anchor the
    # MST would shortcut pad→pad (~11 mm).
    moved = td.pose0.copy()
    moved[0, 1] = 5.0
    _, length_moved = evaluate(td, moved)
    assert length_moved > length


def test_per_pad_fixed_via_exemption() -> None:
    """The placement gate judges per pad; legalize now mirrors it
    (gr520903). U1's pad on the RING of a same-net via is a legal cover
    and the part is NOT moved. U2's FOREIGN-net pad on a via refuses
    (the genuinely violating pad). U3's courtyard over a via with NO
    pad on it is covered copper, not an anchor — the old per-part rule
    exempted U3 because its net IS the via's net; the per-pad rule does
    not. U4's COPPER-FIELD pad covering the annulus is the gate's
    electrode carve-out — exempt. U5's ORDINARY pad under the annulus
    is what the gate reports as ``via_in_pad`` (gr496850) — refused."""
    zones = (
        TopoZone(kind="fixed_via", ring=_via_ring(10.0, 10.0), net="X", name="via A"),
        TopoZone(kind="fixed_via", ring=_via_ring(20.0, 10.0), net="X", name="via B"),
        TopoZone(kind="fixed_via", ring=_via_ring(15.0, 20.0), net="X", name="via C"),
        TopoZone(kind="fixed_via", ring=_via_ring(5.0, 15.0), net="X", name="via D"),
        TopoZone(kind="fixed_via", ring=_via_ring(10.0, 5.0), net="X", name="via E"),
    )
    d = _design(
        parts=("U1", "U2", "U3", "U4", "U5"),
        pin_part=[0, 1, 2, 3, 4],
        pin_net=[0, 1, 0, 0, 0],
        pin_xy=[[0.35, 0.0], [-0.5, 0.0], [0.0, -3.0], [0.0, 0.0], [0.0, 0.0]],
        pin_wh=[[0.1, 0.1], [0.1, 0.1], [0.1, 0.1], [0.6, 0.6], [0.6, 0.6]],
        pin_role=["", "", "", "electrode", ""],
        net_pins=[[0, 2, 3, 4]],
        net_name=("X",),
        decl_nets=([0], [1]),
        decl_net_ir=[0, 1],
        decl_net_plane=[False, False],
        decl_net_name=("X", "Y"),
        part_outline=(_RING, _RING, _RING, _RING, _RING),
        zones=zones,
        pose0=[
            [10.0, 10.0, 0.0, 0.0],  # U1: pad (10.35,10) on via A's ring, net X
            [20.5, 10.0, 0.0, 0.0],  # U2: pad (20,10) sits on via B, net Y
            [15.5, 20.0, 0.0, 0.0],  # U3: courtyard covers via C, pad far off
            [5.0, 15.0, 0.0, 0.0],  # U4: electrode pad covers via D, net X
            [10.0, 5.0, 0.0, 0.0],  # U5: ordinary pad covers via E, net X
        ],
    )
    res = legalize_pose(d, d.pose0)
    np.testing.assert_allclose(res.pose[0], d.pose0[0])
    np.testing.assert_allclose(res.pose[3], d.pose0[3])
    assert not np.allclose(res.pose[1], d.pose0[1])
    assert not np.allclose(res.pose[2], d.pose0[2])
    assert not np.allclose(res.pose[4], d.pose0[4])
    assert res.resolved
    assert not any(a in (0, 3) and b >= len(d.parts) for a, b, _area in res.remaining)


def test_per_pad_exemption_agrees_with_the_place_gate() -> None:
    """The gate legalize mirrors is ``drc.check_via_pad_keepout`` over
    the placed model — the SAME predicate decides both. On the
    fixture's geometry: an on-net pad on the ring anchors the via (no
    findings), a foreign-net pad on it still reports, and an ORDINARY
    pad under the annulus is ``via_in_pad`` while a copper-field pad
    passes — the four verdicts ``_via_zone_exempt`` gives above."""
    via = {
        "ctype": "via",
        "net": "X",
        "x": 10.0,
        "y": 10.0,
        "dia_mm": 0.4,
        "drill_mm": 0.2,
        "layers": ["F.Cu"],
        "fixed": True,
    }
    cap = capability_for("4layer")

    def model(pads: list[dict[str, Any]]) -> dict[str, Any]:
        return {"layers": ["F.Cu"], "copper": [via], "pads": pads}

    # U1's case: pad edge 0.05mm past the ring, on-net — anchored.
    ring_pad = {
        "layer": "F.Cu",
        "net": "X",
        "shape": "rect",
        "x": 10.35,
        "y": 10.0,
        "w": 0.2,
        "h": 0.2,
    }
    assert drc.check_via_pad_keepout(model([ring_pad]), cap) == []
    # U2's case: the same pad on a FOREIGN net refuses.
    foreign = {**ring_pad, "net": "Y"}
    (f,) = drc.check_via_pad_keepout(model([foreign]), cap)
    assert f.rule == "via_pad_keepout"
    # U5's case: an ordinary pad under the annulus is via_in_pad.
    under = {**ring_pad, "x": 10.0}
    (g,) = drc.check_via_pad_keepout(model([under]), cap)
    assert g.rule == "via_in_pad"
    # U4's case: a copper-field pad covering it passes.
    electrode = {**under, "role": "electrode"}
    assert drc.check_via_pad_keepout(model([electrode]), cap) == []
