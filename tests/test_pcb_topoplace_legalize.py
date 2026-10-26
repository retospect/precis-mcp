"""Slice 2a legalize pass: ``_part_nets`` pin→net-name mapping and the
same-net zone exemption. ``net_name`` is indexed by the compacted
``net_pins`` slot while ``pin_net`` carries ir net ids — plane and
singleton nets are dropped, so the two index spaces drift apart on any
board that has them (e22b945d6's IndexError on nano/ewod, and the
silent wrong-name reads on id-aligned boards). Plane-net pins keep
their exemption through the decl tables: GND and power are normal
nets, there are no pours (Reto 2026-10-26)."""

from __future__ import annotations

import numpy as np

from precis.pcb.topoplace import legalize
from precis.pcb.topoplace.design import TopoDesign, TopoZone
from precis.pcb.topoplace.legalize import legalize_pose

_RING = np.asarray([[-0.4, -0.4], [0.4, -0.4], [0.4, 0.4], [-0.4, 0.4], [-0.4, -0.4]])


def _design(
    *,
    parts: tuple[str, ...],
    pin_part: list[int],
    pin_net: list[int],
    net_pins: list[list[int]] | None = None,
    net_name: tuple[str, ...] = (),
    decl_nets: tuple[list[int], ...] = (),
    decl_net_ir: list[int] | None = None,
    decl_net_plane: list[bool] | None = None,
    decl_net_name: tuple[str, ...] = (),
    part_outline: tuple[np.ndarray, ...] = (),
    zones: tuple[TopoZone, ...] = (),
    pose0: list[list[float]],
) -> TopoDesign:
    n_d = len(decl_nets)
    return TopoDesign(
        parts=parts,
        pin_xy=np.zeros((len(pin_part), 2), dtype=np.float64),
        pin_part=np.asarray(pin_part, dtype=np.int64),
        pin_side=np.zeros(len(pin_part), dtype=np.int8),
        pin_label=np.asarray(["1"] * len(pin_part), dtype=object),
        pin_net=np.asarray(pin_net, dtype=np.int32),
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
