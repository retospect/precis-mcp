"""A copper-clear fan offset must not create a new pair of crossings."""

from __future__ import annotations

import dataclasses

import numpy as np

from precis.pcb.topoplace import realise
from precis.pcb.topoplace.crossings import crossing_events
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.mesh import TopoMesh
from precis.pcb.topoplace.wires import WirePath, Wires


def test_bend_fan_refuses_an_increase_in_proper_crossings(monkeypatch) -> None:
    """The pair is already braided elsewhere; a shared touch is no new braid."""
    d = TopoDesign(
        parts=(),
        pin_xy=np.empty((0, 2)),
        pin_part=np.asarray([], dtype=np.int64),
        pin_side=np.asarray([], dtype=np.int8),
        pin_label=np.asarray([], dtype=object),
        pin_net=np.asarray([], dtype=np.int32),
        net_pins=(np.asarray([], dtype=np.int64),) * 2,
        net_name=("A", "B"),
        flex=(),
        swap_groups=(),
        fixed=np.asarray([], dtype=bool),
        freedoms=np.asarray([], dtype=np.uint8),
        outline=np.empty((0, 2)),
        pose0=np.empty((0, 4)),
    )
    mesh = TopoMesh(
        verts=np.asarray([[0.0, 0.0]]),
        tris=np.empty((0, 3), dtype=np.int32),
        edges=np.empty((0, 2), dtype=np.int32),
        tri_edges=np.empty((0, 3), dtype=np.int32),
        edge_tris=np.empty((0, 2), dtype=np.int32),
        constrained=np.asarray([], dtype=bool),
        pad_vertex={},
    )
    a = WirePath(
        net=0,
        pin_a=-1,
        pin_b=-1,
        tris=(),
        edges=(),
        t=(),
        points=((-4.0, 0.0), (-2.0, 0.0), (0.0, 0.0), (2.0, -0.2), (4.0, -0.4)),
    )
    b = dataclasses.replace(
        a,
        net=1,
        points=((-3.0, -1.0), (-3.0, 1.0), (-1.0, 1.0), (0.0, 0.0), (1.0, 1.0)),
    )
    wires = Wires(
        branches=(a, b),
        order=(),
        capacity_mm=np.asarray([]),
        net_layers=(frozenset({0}),) * 2,
        unreachable=(),
        gap_free_mm=np.empty((0, 2)),
        gap_cap=np.empty((0, 2), dtype=np.int64),
    )
    assert len(crossing_events(mesh, wires.branches)[(0, 1)]) == 1

    def lift(prev, p, nxt, clear, **kwargs):
        return (0.0, 1.0) if p == (0.0, 0.0) and prev == (-2.0, 0.0) else p

    monkeypatch.setattr(realise, "_lift_bend", lift)
    # No pads or board edges exist in this geometry-only mesh.
    monkeypatch.setattr(realise, "obstacle_vertices", lambda *_: frozenset())
    paths = realise._spoke_offsets(d, mesh, wires, 2)
    changed = tuple(
        dataclasses.replace(br, points=path)
        for br, path in zip(wires.branches, paths, strict=True)
    )
    assert len(crossing_events(mesh, changed)[(0, 1)]) == 1
    assert paths[0][2] == (0.0, 0.0)
