"""Edge-mating parts (gr451356): the placer puts a side-insertion connector
flush on the outline, mating face out; DRC flags one that is not."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Polygon

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import DEFAULT_STACKUP, drc, edge_mate
from precis.pcb.ir import from_graph
from precis.pcb.optimize import OptimizeConfig, optimize

_OUTLINE = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
_RING = [list(p) for p in _OUTLINE]


def _graph(edge_mate_value: object = {"face": "E"}) -> dict:
    j1: dict = {"refdes": "J1"}
    if edge_mate_value is not None:
        j1["edge_mate"] = edge_mate_value
    instances = [j1, {"refdes": "U1"}, {"refdes": "R1"}, {"refdes": "R2"}]
    nets = [
        {
            "name": "SIG",
            "net_class": "signal",
            "domain": "electrical",
            "members": [
                {"refdes": "J1", "pin": "1"},
                {"refdes": "U1", "pin": "1"},
                {"refdes": "R1", "pin": "1"},
            ],
        },
        {
            "name": "GND",
            "net_class": "ground",
            "domain": "electrical",
            "members": [
                {"refdes": "J1", "pin": "2"},
                {"refdes": "U1", "pin": "2"},
                {"refdes": "R2", "pin": "1"},
            ],
        },
    ]
    return {"instances": instances, "nets": nets}


def _ir(edge_mate_value: object = {"face": "E"}):
    ir = from_graph(_graph(edge_mate_value), stackup=DEFAULT_STACKUP)
    ir.outline = list(_OUTLINE)
    return ir


def test_parse_edge_mate_forms_and_garbage():
    assert edge_mate.parse_edge_mate({"face": "w"}) == edge_mate.EdgeMateSpec(
        "required", 270.0
    )
    assert edge_mate.parse_edge_mate({"face": 90, "require": "preferred"}) == (
        edge_mate.EdgeMateSpec("preferred", 90.0)
    )
    for bad in (None, "E", {}, {"face": "up"}, {"face": "N", "require": "maybe"}):
        assert edge_mate.parse_edge_mate(bad) is None


def test_outward_vector_follows_rotation_and_mirror():
    spec = edge_mate.EdgeMateSpec("required", 90.0)  # local +x
    ox, oy = edge_mate.outward_vector(spec, 90.0)  # rotate CW: +x -> -y
    assert (round(ox, 9), round(oy, 9)) == (0.0, -1.0)
    ox, oy = edge_mate.outward_vector(spec, 0.0, mirrored=True)  # x flips
    assert (round(ox, 9), round(oy, 9)) == (-1.0, 0.0)


def test_placer_puts_connector_on_outline_facing_out():
    ir = _ir()
    result = optimize(ir, OptimizeConfig(iters=300, seed=3))
    assert bool(ir.inst_fixed_xy[0]) and bool(ir.inst_fixed_rot[0])
    (rec,) = edge_mate.records(ir)
    assert rec.refdes == "J1"
    verdict = edge_mate.evaluate(rec, edge_mate.outline_edges(ir.outline))
    assert verdict.state == "ok", verdict
    # Measured, not just "ok": face centre on an edge to < 1e-3 mm, and the
    # face direction equals that edge's outward normal to < 1 degree.
    assert verdict.distance_mm < 1e-3
    assert verdict.angle_deg < 1.0
    # The courtyard stays inside the board (flush, not hanging over).
    assert Polygon(_OUTLINE).buffer(1e-6).covers(Polygon(rec.polygon))
    assert result.positions["J1"][2] % 90.0 == 0.0
    assert drc.check_edge_mating([rec], outline=_RING) == []


def test_placer_respects_locked_rotation():
    ir = _ir({"face": "E"})
    ir.inst_fixed_rot[0] = True  # author pinned rot=0: face stays +x
    optimize(ir, OptimizeConfig(iters=100, seed=1))
    (rec,) = edge_mate.records(ir)
    assert math.isclose(rec.outward[0], 1.0) and float(ir.inst_rot[0]) == 0.0
    # Facing +x with the rotation pinned means the east edge.
    centre, _w = edge_mate.mating_face(rec.polygon, rec.outward)
    assert abs(centre[0] - 40.0) < 1e-3


def test_unconstrained_part_is_untouched():
    ir = _ir(None)
    optimize(ir, OptimizeConfig(iters=50, seed=1))
    assert edge_mate.records(ir) == []
    assert not bool(ir.inst_fixed_xy[0])


def _place(ir, x, y, rot):
    ir.move_instance(0, x=x, y=y, rot=rot)
    return edge_mate.records(ir)


def test_drc_flags_interior_connector():
    ir = _ir()
    recs = _place(ir, 20.0, 15.0, 0.0)
    found = drc.check_edge_mating(recs, outline=_RING)
    assert len(found) == 1
    f = found[0]
    assert (f.rule, f.severity, f.where) == ("edge_mating", "error", "part J1")
    assert f.margin_mm is not None and f.margin_mm < -5.0  # ~15mm in, 0.25 allowed
    assert "cannot reach" in f.detail


def test_drc_flags_edge_part_facing_inward_and_preferred_is_warn():
    ir = _ir({"face": "E", "require": "preferred"})
    optimize(ir, OptimizeConfig(iters=50, seed=2))
    (rec,) = edge_mate.records(ir)
    assert drc.check_edge_mating([rec], outline=_RING) == []
    # Rotate it 180 degrees, then slide it so the (new) face centre sits on
    # the same edge point: on the edge, but now facing into the board.
    c0, _w = edge_mate.mating_face(rec.polygon, rec.outward)
    ir.move_instance(0, rot=(float(ir.inst_rot[0]) + 180.0) % 360.0)
    (turned,) = edge_mate.records(ir)
    c1, _w = edge_mate.mating_face(turned.polygon, turned.outward)
    ir.move_instance(
        0, x=float(ir.inst_x[0]) + c0[0] - c1[0], y=float(ir.inst_y[0]) + c0[1] - c1[1]
    )
    (flipped,) = edge_mate.records(ir)
    found = drc.check_edge_mating([flipped], outline=_RING)
    assert len(found) == 1
    assert found[0].severity == "warn"
    assert "faces into the board" in found[0].detail


def test_no_outline_or_no_records_is_silent():
    ir = _ir()
    recs = _place(ir, 20.0, 15.0, 0.0)
    assert drc.check_edge_mating(recs, outline=None) == []
    assert drc.check_edge_mating(None, outline=_RING) == []


# ── handler level: edge_mating is judged on mutations ───────────────────


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _board(*, mate: dict | None, x: float, y: float) -> dict:
    j1: dict = {
        "refdes": "J1",
        "label": "edge connector",
        "footprint": "0402",
        "x": x,
        "y": y,
        "pins": [{"name": "1"}, {"name": "2"}],
    }
    if mate is not None:
        j1["edge_mate"] = mate
    return {
        "components": [j1],
        "nets": [],
        "connections": [],
        "features": [
            {
                "ftype": "outline",
                "geom": {"path": [[0, 0], [40, 0], [40, 30], [0, 30], [0, 0]]},
            }
        ],
    }


def _j1(pcb, design: str) -> dict:
    ref = pcb.store.get_ref(kind="pcb", id=design)
    (j1,) = [i for i in pcb.store.pcb_graph(ref.id)["instances"] if i["refdes"] == "J1"]
    return j1


def _flush_x(pcb, design: str) -> float:
    """x that seats J1 (face E, rot 0) on the east edge, found by scanning
    the DRC view rather than assuming the courtyard size."""
    for tenth in range(400, 370, -1):  # outermost pose the outline accepts
        x = tenth / 10
        try:
            pcb.put(id=design, args={"op": "move", "refdes": "J1", "x": x, "y": 15.0})
        except BadInput:
            continue
        assert "edge_mating" not in pcb.get(id=design, view="drc").body
        return x
    raise AssertionError("no flush x found")


def _judged_move(pcb, design: str, x: float, y: float):
    """Move J1 inside ``_judged_mutation`` -- the seam the edge_mating verdict
    lives in. (Single/batch ``op='move'`` is judged by the placer's pose rule
    instead, and a re-put never moves an existing part.)"""
    ref = pcb.store.get_ref(kind="pcb", id=design)
    return pcb._judged_mutation(
        ref.id,
        lambda conn: pcb.store.pcb_move_instance(ref.id, "J1", x=x, y=y, conn=conn),
    )


def test_moving_edge_mated_connector_off_the_edge_is_refused(pcb):
    mate = {"face": "E", "require": "required"}
    pcb.put(id="em-move", args=_board(mate=mate, x=20.0, y=15.0))
    x_ok = _flush_x(pcb, "em-move")
    before = _j1(pcb, "em-move")
    with pytest.raises(BadInput, match="edge_mating"):
        _judged_move(pcb, "em-move", 20.0, 15.0)
    after = _j1(pcb, "em-move")
    assert (after["x"], after["y"]) == (before["x"], before["y"]) == (x_ok, 15.0)


def _xy(pcb, design: str) -> tuple[float, float]:
    j1 = _j1(pcb, design)
    return (j1["x"], j1["y"])


def test_op_move_required_edge_mated_off_the_edge_is_refused(pcb):
    mate = {"face": "E", "require": "required"}
    pcb.put(id="em-op", args=_board(mate=mate, x=20.0, y=15.0))
    x_ok = _flush_x(pcb, "em-op")
    with pytest.raises(BadInput, match="edge_mating"):
        pcb.put(id="em-op", args={"op": "move", "refdes": "J1", "x": 20.0, "y": 15.0})
    assert _xy(pcb, "em-op") == (x_ok, 15.0)


def test_op_move_preferred_edge_mated_midboard_is_accepted(pcb):
    mate = {"face": "E", "require": "preferred"}
    pcb.put(id="em-op-pref", args=_board(mate=mate, x=20.0, y=15.0))
    _flush_x(pcb, "em-op-pref")
    pcb.put(id="em-op-pref", args={"op": "move", "refdes": "J1", "x": 20.0, "y": 15.0})
    assert _xy(pcb, "em-op-pref") == (20.0, 15.0)


def test_batch_op_move_required_edge_mated_off_the_edge_is_refused(pcb):
    mate = {"face": "E", "require": "required"}
    pcb.put(id="em-batch", args=_board(mate=mate, x=20.0, y=15.0))
    x_ok = _flush_x(pcb, "em-batch")
    with pytest.raises(BadInput, match="edge_mating"):
        pcb.put(
            id="em-batch",
            args={"op": "move", "moves": [{"refdes": "J1", "x": 20.0, "y": 15.0}]},
        )
    assert _xy(pcb, "em-batch") == (x_ok, 15.0)


def test_standing_midboard_required_part_may_move_closer_not_further(pcb):
    mate = {"face": "E", "require": "required"}
    pcb.put(id="em-stand", args=_board(mate=mate, x=20.0, y=10.0))
    pcb.put(id="em-stand", args={"op": "move", "refdes": "J1", "x": 30.0, "y": 10.0})
    assert _xy(pcb, "em-stand") == (30.0, 10.0)  # closer to the east edge
    with pytest.raises(BadInput, match="edge_mating"):
        pcb.put(
            id="em-stand", args={"op": "move", "refdes": "J1", "x": 15.0, "y": 10.0}
        )
    assert _xy(pcb, "em-stand") == (30.0, 10.0)


def test_adding_edge_mate_to_a_midboard_part_is_visible_not_refused(pcb):
    # A re-put cannot patch an existing part's edge_mate, so the mated part
    # arrives as a new component, mid-board, on a board with no edge_mate.
    pcb.put(id="em-add", args=_board(mate=None, x=20.0, y=15.0))
    mate = {"face": "E", "require": "required"}
    j2 = {
        "refdes": "J2",
        "label": "edge connector",
        "footprint": "0402",
        "x": 10.0,
        "y": 8.0,
        "pins": [{"name": "1"}, {"name": "2"}],
        "edge_mate": mate,
    }
    resp = pcb.put(id="em-add", args={"components": [j2]})
    assert "edge_mating" in resp.body, resp.body
    assert "standing" in resp.body, resp.body
    ref = pcb.store.get_ref(kind="pcb", id="em-add")
    stored = {i["refdes"]: i for i in pcb.store.pcb_graph(ref.id)["instances"]}
    assert stored["J2"]["edge_mate"] == mate


def test_preferred_edge_mate_moved_midboard_is_not_refused(pcb):
    mate = {"face": "E", "require": "preferred"}
    pcb.put(id="em-pref", args=_board(mate=mate, x=20.0, y=15.0))
    _flush_x(pcb, "em-pref")
    _judged_move(pcb, "em-pref", 20.0, 15.0)  # no BadInput: warn tier
    assert _j1(pcb, "em-pref")["x"] == 20.0
