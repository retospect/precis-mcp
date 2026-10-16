"""gr489913 (a): pcb_route's pre-route refusal says whose placement it is.

taz15 jo484766 refused with "the placement is already illegal" over the
anneal's OWN output poses (none of that geometry was on the stored board),
which read as a corrupt import. These pin the pure helpers the refusal is
built from; the DB-backed gate tests in
``test_pcb_route_placement_gate_epro.py`` pin the stored-placement arm.
"""

from __future__ import annotations

import math

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.workers.job_types import pcb_route


def _ir():
    graph = {
        "instances": [{"refdes": "U0"}, {"refdes": "U1"}, {"refdes": "U2"}],
        "nets": [
            {
                "name": "N0",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "a"},
                    {"refdes": "U1", "pin": "b"},
                ],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.inst_x[:] = [0.0, 5.0, math.nan]
    ir.inst_y[:] = [0.0, 5.0, math.nan]
    ir.inst_rot[:] = [0.0, 0.0, 0.0]
    return ir


def test_moved_refdes_counts_pose_and_rotation_changes_not_unplaced_nans():
    ir = _ir()
    before = (ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy())
    assert pcb_route._moved_refdes(ir, before) == []
    ir.inst_x[0] = 1.0
    ir.inst_rot[1] = 90.0
    assert pcb_route._moved_refdes(ir, before) == ["U0", "U1"]
    ir.inst_x[2], ir.inst_y[2] = 3.0, 3.0  # placed from unplaced is a move
    assert pcb_route._moved_refdes(ir, before) == ["U0", "U1", "U2"]


def test_fixed_refdes_lists_xy_pinned_parts_only():
    ir = _ir()
    assert pcb_route._fixed_refdes(ir) == []
    ir.inst_fixed_xy[1] = True
    ir.inst_fixed_rot[2] = True  # rotation-only lock still lets it shift
    assert pcb_route._fixed_refdes(ir) == ["U1"]


def test_refusal_names_the_annealed_placement_when_a_violator_moved():
    moved = [f"P{i}" for i in range(11)] + ["SW3"]
    reason = pcb_route._refusal_reason(
        "pre-route DRC: 4 placement error(s)", moved, ["SW3"], 96
    )
    assert "refusing to route" in reason
    assert "ANNEALED placement is illegal" in reason
    assert "moved 12 of 96 part(s)" in reason
    assert "violators among them: SW3" in reason
    assert "already illegal" not in reason
    assert "force=True" in reason


def test_refusal_blames_the_stored_placement_when_no_violator_moved():
    # A pinned pair overlapping in the stored board while the anneal moved
    # an unrelated free part is still the stored placement's fault.
    for moved in ([], ["R7"]):
        reason = pcb_route._refusal_reason(
            "pre-route DRC: 1 placement error(s)", moved, [], 2
        )
        assert "the placement is already illegal" in reason
        assert "ANNEALED" not in reason
        assert "force=True" in reason
