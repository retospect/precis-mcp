"""Unit tests for the PCB IR (precis.pcb.ir) — construction, the
invalidation cascade, layer bitmasks, the explicit-embedding invariant,
and the graph feasibility checks. No DB.
"""

from __future__ import annotations

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import (
    NO_NET,
    Level,
    PlaneConnectivity,
    compute_gap_capacity,
    compute_region_density,
    from_graph,
    per_layer_planar,
    plane_connectivity,
    propose_rotation_from_positions,
    same_layer_crossing_bound,
    unconnected_items,
    validate_embedding,
)


def _net(name, cls, *refdes, domain="electrical"):
    return {
        "name": name,
        "net_class": cls,
        "domain": domain,
        "members": [{"refdes": r, "pin": "1"} for r in refdes],
    }


def _star_graph():
    """A 4-instance, 2-net fixture: N1 stars U1-U2-U3 (2 segments), N2
    connects U3-U4 (1 segment)."""
    return {
        "instances": [{"refdes": r} for r in ("U1", "U2", "U3", "U4")],
        "nets": [_net("N1", "signal", "U1", "U2", "U3"), _net("N2", "power", "U3", "U4")],
    }


# ── construction ─────────────────────────────────────────────────────
def test_from_graph_star_decomposition():
    ir = from_graph(_star_graph())
    assert ir.n_instances == 4
    assert ir.n_nets == 2
    # N1 has 3 members -> 2 segments (star, hub = first member U1)
    # N2 has 2 members -> 1 segment
    assert ir.n_segments == 3
    n1_segs = [s for s in range(ir.n_segments) if ir.net_name[int(ir.seg_net[s])] == "N1"]
    assert len(n1_segs) == 2


def test_from_graph_leaves_l1_l2_l3_unset():
    ir = from_graph(_star_graph())
    assert (ir.seg_layer == -1).all()
    assert ir.rotation_darts.shape[0] == 0
    import math

    assert all(math.isnan(x) for x in ir.inst_x)


def test_from_graph_unconnected_pins_and_positions():
    graph = _star_graph()
    graph["instances"][0]["x"] = 1.0
    graph["instances"][0]["y"] = 2.0
    graph["unconnected"] = [{"refdes": "U4", "pin": "2"}]
    ir = from_graph(graph)
    assert ir.inst_x[0] == 1.0 and ir.inst_y[0] == 2.0
    unconnected_pins = [p for p in range(ir.n_pins) if int(ir.pin_net[p]) == NO_NET]
    assert len(unconnected_pins) == 1


# ── invalidation cascade ─────────────────────────────────────────────
def test_move_instance_dirties_l3_l4_l5_locally_leaves_l1_l2_clean():
    ir = from_graph(_star_graph())
    u1 = 0  # incident to exactly one segment (N1's U1-U2 star edge... U1 is the hub, incident to 2)
    ir.move_instance(u1, x=5.0, y=6.0)

    assert ir.dirty_l3[u1]
    assert not ir.dirty_l3[1]  # U2 untouched
    touched_segs = set(ir._segs_of_instance[u1])
    assert touched_segs  # U1 is the star hub, incident to >=1 segment
    for s in range(ir.n_segments):
        if s in touched_segs:
            assert ir.dirty_l4[s]
            assert ir.dirty_l5[s]
        else:
            assert not ir.dirty_l4[s]
            assert not ir.dirty_l5[s]

    # the invariant the whole architecture rests on:
    assert not ir.dirty_l1.any()
    assert not ir.dirty_l2.any()


def test_set_layer_dirties_l1_l4_l5_leaves_l2_l3_clean():
    ir = from_graph(_star_graph())
    ir.set_layer(0, 1)
    assert ir.dirty_l1[0] and not ir.dirty_l1[1]
    assert ir.dirty_l4[0] and ir.dirty_l5[0]
    assert not ir.dirty_l2.any()
    assert not ir.dirty_l3.any()


def test_set_layer_rejects_out_of_range():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    import pytest

    with pytest.raises(ValueError):
        ir.set_layer(0, len(DEFAULT_STACKUP))


def test_set_side_dirties_l2_l4_l5_leaves_l1_l3_clean():
    ir = from_graph(_star_graph())
    ir.set_side(0, 1)
    assert ir.dirty_l2[0] and not ir.dirty_l2[1]
    assert ir.dirty_l4[0] and ir.dirty_l5[0]
    assert not ir.dirty_l1.any()
    assert not ir.dirty_l3.any()


def test_promote_plane_dirties_only_that_nets_segments():
    ir = from_graph(_star_graph())
    n2 = 1  # "N2" power net -> its single segment
    ir.promote_plane(n2, 0)
    assert int(ir.net_plane_layer[n2]) == 0
    n2_segs = {s for s in range(ir.n_segments) if int(ir.seg_net[s]) == n2}
    for s in range(ir.n_segments):
        assert ir.dirty_l1[s] == (s in n2_segs)
        assert ir.dirty_l4[s] == (s in n2_segs)
        assert ir.dirty_l5[s] == (s in n2_segs)
    assert not ir.dirty_l3.any()


def test_clean_clears_the_named_level_only():
    ir = from_graph(_star_graph())
    ir.set_layer(0, 0)
    ir.clean(Level.L1)
    assert not ir.dirty_l1.any()
    assert ir.dirty_l4[0]  # L4 untouched by clean(L1)


# ── layer bitmask / via-span ─────────────────────────────────────────
def test_via_layer_span_is_a_bitmask():
    ir = from_graph(_star_graph())
    through = ir.add_via(layer_span=0b1111, net_id=0)  # spans all 4 layers
    top_only = ir.add_via(layer_span=0b0001, net_id=NO_NET)  # a keepout on layer 0 only
    assert bool(int(ir.via_layer_span[through]) & (1 << 2))  # blocks layer 2 too
    assert not bool(int(ir.via_layer_span[top_only]) & (1 << 1))  # doesn't touch layer 1
    assert int(ir.via_net[top_only]) == NO_NET  # a keepout connects nothing


def test_add_via_does_not_dirty_anything():
    ir = from_graph(_star_graph())
    ir.add_via(layer_span=1, net_id=0)
    assert not ir.dirty_l1.any() and not ir.dirty_l4.any() and not ir.dirty_l5.any()


# ── explicit embedding: propose + validate, never derive ──────────────
def test_propose_rotation_skips_pins_without_positions():
    ir = from_graph(_star_graph())  # no positions set
    proposal = propose_rotation_from_positions(ir)
    assert proposal == {}


def test_set_rotation_is_the_only_way_to_populate_l2():
    ir = from_graph(_star_graph())
    ir.instance_refdes  # sanity: module has no compute_embedding entry point at all
    assert not hasattr(ir, "compute_embedding")
    import precis.pcb.ir as ir_mod

    assert not hasattr(ir_mod, "compute_embedding")


def test_propose_then_apply_then_validate_round_trips():
    graph = _star_graph()
    # place U1 (hub of N1) at origin, U2 east, U3 north — an L-shaped star
    graph["instances"][0].update(x=0.0, y=0.0)
    graph["instances"][1].update(x=10.0, y=0.0)
    graph["instances"][2].update(x=0.0, y=10.0)
    graph["instances"][3].update(x=10.0, y=10.0)
    ir = from_graph(graph)

    proposal = propose_rotation_from_positions(ir)
    pin_u1 = 0  # U1's pin is pin id 0 (first created)
    assert pin_u1 in proposal
    for pin_id, order in proposal.items():
        ir.set_rotation(pin_id, order)

    assert validate_embedding(ir) == []  # stored embedding matches current positions


def test_validate_embedding_catches_a_move_without_mutating_storage():
    graph = _star_graph()
    graph["instances"][0].update(x=0.0, y=0.0)
    graph["instances"][1].update(x=10.0, y=0.0)
    graph["instances"][2].update(x=0.0, y=10.0)
    graph["instances"][3].update(x=10.0, y=10.0)
    ir = from_graph(graph)
    for pin_id, order in propose_rotation_from_positions(ir).items():
        ir.set_rotation(pin_id, order)
    stored_before = ir.rotation_darts.copy()

    # move U2 to the opposite side of U1 from U3 -> the angular order at
    # U1's pin should now disagree with what's stored
    ir.move_instance(1, x=-10.0, y=0.0)
    mismatched = validate_embedding(ir)
    assert mismatched  # something disagrees now
    # the read-only check must never have touched storage
    assert (ir.rotation_darts == stored_before).all()


# ── graph feasibility checks ─────────────────────────────────────────
def test_unconnected_items_reports_pin_and_dangling_net():
    graph = {
        "instances": [{"refdes": "U1"}, {"refdes": "U2"}],
        "nets": [_net("SOLO", "signal", "U1")],  # single member -> dangling
        "unconnected": [{"refdes": "U2", "pin": "3"}],
    }
    ir = from_graph(graph)
    findings = unconnected_items(ir)
    codes = {f["code"] for f in findings}
    assert "unconnected-pin" in codes
    assert "dangling-net" in codes


def test_unconnected_items_clean_fixture_reports_nothing():
    ir = from_graph(_star_graph())
    assert unconnected_items(ir) == []


def _complete_graph(n: int, layer: int = 0):
    """K_n on one net (a star decomposition won't give K_n edges, so build
    the segment set directly by hand rather than through from_graph's
    star policy — this fixture needs every pairwise edge)."""
    instances = [{"refdes": f"U{i}"} for i in range(n)]
    # one dummy net per pair, each with exactly those 2 members, so
    # from_graph's per-net star decomposition happens to yield exactly
    # one segment per net == exactly the edge we want.
    nets = []
    for i in range(n):
        for j in range(i + 1, n):
            nets.append(_net(f"E{i}_{j}", "signal", f"U{i}", f"U{j}"))
    ir = from_graph({"instances": instances, "nets": nets})
    ir.seg_layer[:] = layer
    return ir


def test_same_layer_crossing_bound_zero_for_a_tree():
    # a tree (n-1 edges on n vertices) is always planar
    graph = _star_graph()
    ir = from_graph(graph)
    ir.seg_layer[:] = 0
    assert same_layer_crossing_bound(ir, 0) == 0
    assert per_layer_planar(ir, 0)


def test_same_layer_crossing_bound_positive_for_k5():
    # K5: 5 vertices, 10 edges; 3V-6 = 9 -> bound = 1 (K5 is famously non-planar)
    ir = _complete_graph(5)
    assert same_layer_crossing_bound(ir, 0) == 1
    assert not per_layer_planar(ir, 0)


def test_same_layer_crossing_bound_refine_is_at_least_coarse():
    ir = _complete_graph(5)
    coarse = same_layer_crossing_bound(ir, 0, refine=False)
    fine = same_layer_crossing_bound(ir, 0, refine=True)
    assert fine >= coarse


def test_crossing_bound_never_negative_on_empty_layer():
    ir = from_graph(_star_graph())
    assert same_layer_crossing_bound(ir, 3) == 0  # nothing assigned to layer 3
    assert per_layer_planar(ir, 3)


# ── plane connectivity ────────────────────────────────────────────────
def test_plane_connectivity_zero_stitches_not_ok():
    ir = from_graph(_star_graph())
    ir.promote_plane(0, 1)
    result = plane_connectivity(ir, 0)
    assert isinstance(result, PlaneConnectivity)
    assert result.stitch_vias == []
    assert not result.ok


def test_plane_connectivity_one_stitch_is_a_single_point_of_failure():
    ir = from_graph(_star_graph())
    ir.promote_plane(0, 1)
    ir.add_via(layer_span=(1 << 1), net_id=0)
    assert not plane_connectivity(ir, 0).ok


def test_plane_connectivity_two_stitches_ok():
    ir = from_graph(_star_graph())
    ir.promote_plane(0, 1)
    ir.add_via(layer_span=(1 << 1), net_id=0)
    ir.add_via(layer_span=(1 << 1) | (1 << 0), net_id=0)
    result = plane_connectivity(ir, 0)
    assert result.ok
    assert len(result.stitch_vias) == 2


def test_plane_connectivity_ignores_vias_on_other_layers_or_nets():
    ir = from_graph(_star_graph())
    ir.promote_plane(0, 1)
    ir.add_via(layer_span=(1 << 0), net_id=0)  # right net, wrong layer
    ir.add_via(layer_span=(1 << 1), net_id=1)  # right layer, wrong net
    assert plane_connectivity(ir, 0).stitch_vias == []


# ── L4 metric annotations ────────────────────────────────────────────
def test_compute_gap_capacity_leaves_unplaced_segments_nan():
    ir = from_graph(_star_graph())  # no positions
    compute_gap_capacity(ir)
    import math

    assert all(math.isnan(v) for v in ir.seg_gap_capacity)


def test_compute_gap_capacity_populates_once_placed():
    graph = _star_graph()
    for i, x in enumerate((0.0, 10.0, 0.0, 20.0)):
        graph["instances"][i].update(x=x, y=0.0)
    ir = from_graph(graph)
    compute_gap_capacity(ir, pitch_mm=1.0)
    import math

    assert all(not math.isnan(v) for v in ir.seg_gap_capacity)
    assert (ir.seg_gap_capacity >= 0).all()


def test_compute_region_density_groups_nearby_segments():
    graph = _star_graph()
    for i, (x, y) in enumerate([(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (100.0, 100.0)]):
        graph["instances"][i].update(x=x, y=y)
    ir = from_graph(graph)
    compute_region_density(ir, cell_mm=5.0)
    # N1's two segments (U1-U2, U1-U3) sit in the same small cell -> density >= 1
    assert ir.seg_region_density[0] >= 1.0
