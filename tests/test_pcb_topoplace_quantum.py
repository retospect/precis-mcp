"""The route oracle reserves rounding room even at a broad-phase boundary."""

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

from precis.pcb.topoplace.copper_index import Body, CopperIndex
from precis.pcb.topoplace.design import SilkRoom, TopoDesign


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("shape", ["point", "segment"])
@pytest.mark.parametrize("body_rule", [0.15, 0.3])
def test_route_oracle_reserves_export_quantum(dynamic, shape, body_rule):
    """Both index paths include the margin in candidates and exact limits."""
    index = CopperIndex([] if dynamic else [Body(Point(0, 0), 0.1, 1, body_rule)])
    if dynamic:
        index.add_disc(0, 0, 0.1, net=1, clearance=body_rule)
    near = 0.2 + body_rule + 0.00015
    far = 0.2 + body_rule + 0.00045
    if shape == "point":
        verdicts = index.blocked_points(np.array([[near, 0], [far, 0]]), 0.1, 0.15)
        assert verdicts.tolist() == [True, False]
    else:
        assert index.blocked(LineString([(near, 0), (near, 1)]), 0.1, 0.15)
        assert not index.blocked(LineString([(far, 0), (far, 1)]), 0.1, 0.15)


def test_own_pad_web_reserves_export_quantum():
    index = CopperIndex([Body(Point(0, 0), 0.1, 0)])
    xy = np.array([[0.40015, 0], [0.40045, 0]])
    assert index.blocked_points(xy, 0.1, 0.2, net=0).tolist() == [False, False]
    assert index.blocked_points(xy, 0.1, 0.2, net=0, own_clearance=0.2).tolist() == [
        True,
        False,
    ]


@pytest.mark.parametrize("web,peer_clearance", [(0.9, 0.15), (0.2, 0.9)])
def test_fanout_broadphase_covers_the_largest_pad_rule(
    monkeypatch, web, peer_clearance
):
    """The final exact check cannot protect a pad skipped by its query."""
    from precis.pcb.rules import NetRules
    from precis.pcb.topoplace import realise

    design = TopoDesign(
        parts=("U1",),
        pin_xy=np.array([[0.0, 0.0]]),
        pin_part=np.array([0]),
        pin_side=np.array([0]),
        net_name=("N0",),
        net_pins=(np.array([0]),),
        outline=np.array([[0, 0], [20, 0], [20, 20], [0, 20]]),
        zones=(),
        part_body=(),
        part_outline=(),
        via_mask_web_mm=web,
        edge_clearance_mm=0.0,
        silk=replace(SilkRoom.fallback(), outline_reach_mm=0.0, via_to_silk_mm=0.0),
        pin_label=np.array(["1"], dtype=object),
        pin_net=np.array([0]),
        flex=(),
        swap_groups=(),
        fixed=np.array([False]),
        freedoms=np.array([3]),
        pose0=np.array([[5, 5, 0, 0]]),
    )
    pads = [("N0", box(4.9, 4.9, 5.1, 5.1), 0), ("PEER", box(7.5, 4.9, 7.7, 5.1), 1)]
    monkeypatch.setattr(realise, "_pad_polys", lambda *_args: {0: pads, 1: []})
    monkeypatch.setattr(realise, "_FANOUT_DIRECTIONS", 1)
    monkeypatch.setattr(realise, "_FANOUT_RADII_MM", (1.5,))
    monkeypatch.setattr(realise, "_FANOUT_LADDER_CAP_MM", 0.0)
    rules = {
        "N0": NetRules(0.15, 0.15, via_dia_mm=0.45, via_drill_mm=0.2),
        "PEER": NetRules(0.15, peer_clearance),
    }
    tracks, vias, _, deferred = realise._fanout_vias(
        design,
        SimpleNamespace(net_name=["N0"]),
        2,
        np.array([[5, 5, 0, 0]]),
        rules,
        {0: 0},
    )
    assert not tracks and not vias
    assert deferred == [(0, 0)]
