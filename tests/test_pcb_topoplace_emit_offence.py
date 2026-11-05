"""A narrow peer crossing must be repaired at the crossing, not the midpoint."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
from shapely.geometry import LineString

from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.rules import NetRules
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.realise import (
    RoutePiece,
    RoutePlan,
    _emit_shapes,
    _guarded_branches,
)


def test_emit_repairs_peer_crossing_between_offence_samples() -> None:
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
    ir = SimpleNamespace(net_name=np.asarray(d.net_name))
    rules = {
        name: NetRules(track_width_mm=0.001, clearance_mm=0.0) for name in d.net_name
    }
    plan = RoutePlan(
        pieces=(
            RoutePiece(net=0, branch=0, layer=0, points=((0.0, 0.0), (10.0, 0.0))),
            RoutePiece(
                net=1,
                branch=1,
                layer=0,
                points=((9.994, -0.004), (9.994, 0.004)),
            ),
        ),
        vias=(),
        layers=(),
    )
    assert _guarded_branches(plan, d, ir, 2, d.pose0, rules) == {0: {0}, 1: {1}}
    # The 0.05-mm offence grid misses the 0.001225-mm-wide collision
    # near A's endpoint. A midpoint kink cannot detour around B there.
    shaped = _emit_shapes(plan, d, ir, 2, d.pose0, rules)
    assert _guarded_branches(shaped, d, ir, 2, d.pose0, rules) == {}
    first, peer = (LineString(pc.points) for pc in shaped.pieces)
    assert first.distance(peer) >= 0.001 + FAB_QUANTUM_MM
    assert len(shaped.pieces[0].points) > 2
