"""Unit tests for the PCB IR (precis.pcb.ir) — construction, the
invalidation cascade, layer bitmasks, the explicit-embedding invariant,
and the graph feasibility checks. No DB.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import (
    NO_NET,
    Level,
    PlaneConnectivity,
    compute_gap_capacity,
    compute_region_density,
    from_graph,
    instance_keepout_radius_mm,
    instance_pad_radius,
    layer_is_pourable,
    layer_is_routable,
    nearest_other_instance,
    per_layer_planar,
    plane_connectivity,
    plane_layers_of,
    pourable_layers,
    propose_rotation_from_positions,
    routable_layers,
    same_layer_crossing_bound,
    same_layer_crossing_count,
    segment_points,
    unconnected_items,
    validate_embedding,
)


def _net(name, cls, *refdes, domain="electrical", pin="1"):
    # `pin` is shared by every member here — fine as long as no instance
    # in this fixture appears in *two* nets with the same pin name (a real
    # pin belongs to exactly one net; from_graph dedups purely on
    # (refdes, pin), so reusing a name across nets would silently merge
    # two distinct nets' membership onto one pin).
    return {
        "name": name,
        "net_class": cls,
        "domain": domain,
        "members": [{"refdes": r, "pin": pin} for r in refdes],
    }


def _star_graph():
    """A 4-instance, 2-net fixture: N1 stars U1-U2-U3 (2 segments), N2
    connects U3-U4 (1 segment). U3's two memberships use distinct pin
    names ("1" vs "2") — a component with more than one pin, as any real
    multi-pin part is."""
    return {
        "instances": [{"refdes": r} for r in ("U1", "U2", "U3", "U4")],
        "nets": [
            _net("N1", "signal", "U1", "U2", "U3"),
            {
                "name": "N2",
                "net_class": "power",
                "domain": "electrical",
                "members": [{"refdes": "U3", "pin": "2"}, {"refdes": "U4", "pin": "1"}],
            },
        ],
    }


# ── construction ─────────────────────────────────────────────────────
def test_from_graph_star_decomposition():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    assert ir.n_instances == 4
    assert ir.n_nets == 2
    # N1 has 3 members -> 2 segments (star, hub = first member U1)
    # N2 has 2 members -> 1 segment
    assert ir.n_segments == 3
    n1_segs = [
        s for s in range(ir.n_segments) if ir.net_name[int(ir.seg_net[s])] == "N1"
    ]
    assert len(n1_segs) == 2


def test_from_graph_populates_pin_pad_size():
    """Every pin's pad SIZE is populated alongside its offset, from the
    same package-family synthesis — the size counterpart to
    ``pin_dx``/``pin_dy`` (``PcbIR.pin_w``'s own docstring: before this,
    every pin in the whole engine read one hardcoded 0.2mm radius
    regardless of package)."""
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    assert ir.pin_pad_synthesized.all(), "no real footprint was supplied"
    # U3 has 2 pins ("1" from N1, "2" from N2) -> the PASSIVE family -> a
    # rectangular (non-circular) pad.
    u3 = list(ir.instance_refdes).index("U3")
    u3_pins = [p for p in range(ir.n_pins) if int(ir.pin_instance[p]) == u3]
    assert len(u3_pins) == 2
    for p in u3_pins:
        assert ir.pin_shape[p] == "rect"
        assert ir.pin_w[p] > 0.0
        assert ir.pin_h[p] > 0.0
    # U1 has exactly one pin -> the SINGLE family -> a round pad, and a
    # DIFFERENT size than U3's -- two packages must not read one constant.
    u1 = list(ir.instance_refdes).index("U1")
    u1_pin = next(p for p in range(ir.n_pins) if int(ir.pin_instance[p]) == u1)
    assert ir.pin_shape[u1_pin] == "circle"
    assert ir.pin_w[u1_pin] == ir.pin_h[u1_pin]
    assert (float(ir.pin_w[u1_pin]), float(ir.pin_h[u1_pin])) != (
        float(ir.pin_w[u3_pins[0]]),
        float(ir.pin_h[u3_pins[0]]),
    )


def test_from_graph_carries_part_lcsc_per_instance():
    """``instance_part_lcsc`` is the join key a caller with real footprint
    data needs (see :func:`precis.pcb.session.footprints_by_refdes`) — an
    instance whose graph dict has no ``part_lcsc`` at all reads back as
    ``None``, never an empty string or a KeyError, and one that does have
    it reads back exactly that value untouched (no case-folding, no
    stripping — the store's ``pcb_footprints_for`` cache is keyed by
    whatever ``pcb_components.part_lcsc`` actually holds)."""
    graph = _star_graph()
    graph["instances"][0]["part_lcsc"] = "C2838500"  # U1
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    u1 = list(ir.instance_refdes).index("U1")
    u2 = list(ir.instance_refdes).index("U2")
    assert ir.instance_part_lcsc[u1] == "C2838500"
    assert ir.instance_part_lcsc[u2] is None


def test_instance_pad_radius_is_offset_only_pad_size_does_not_widen_it():
    """Deliberately offset-only, NOT widened by ``pin_w``/``pin_h`` — see
    :func:`~precis.pcb.ir.instance_pad_radius`'s own docstring for the
    2026-08-29 measurement: folding pad size into this bound (both a
    loose "offset + enclosing circle" version and the exact axis-aligned
    far-corner version) regressed ``tests/test_pcb_reference_end_to_end.
    py``'s acceptance fixture on 2 of 5 seeds — a router capacity limit,
    not a placement-legality bug — so this stays offset-only until that
    gap is closed. Pinned here so a future change to this formula is
    deliberate, not an accidental drift back toward the regressed
    behaviour: two pins at the SAME offset but wildly different pad sizes
    must still produce the SAME radius."""
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    u3 = list(ir.instance_refdes).index("U3")
    u3_pins = [p for p in range(ir.n_pins) if int(ir.pin_instance[p]) == u3]
    for p in u3_pins:
        ir.pin_dx[p] = 1.0
        ir.pin_dy[p] = 0.0
    ir.pin_w[u3_pins[0]] = 4.0
    ir.pin_h[u3_pins[0]] = 4.0
    ir.pin_w[u3_pins[1]] = 0.2
    ir.pin_h[u3_pins[1]] = 0.2
    radius = instance_pad_radius(ir)
    assert radius[u3] == pytest.approx(1.0)


def test_instance_pad_radius_matches_hand_computed_offset_max():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    import numpy as np

    expected: dict[int, float] = {}
    for p in range(ir.n_pins):
        inst = int(ir.pin_instance[p])
        r = math.hypot(float(ir.pin_dx[p]), float(ir.pin_dy[p]))
        expected[inst] = max(expected.get(inst, 0.0), r)
    radius = instance_pad_radius(ir)
    for inst, r in expected.items():
        assert radius[inst] == pytest.approx(r)
    assert not np.isnan(radius).any()


def test_instance_keepout_radius_mm_is_pad_radius_plus_breathing_floored():
    """``instance_keepout_radius_mm`` -- the ONE formula the placer's
    legality check, its seeder, and the DRC courtyard geometry
    (:mod:`precis.handlers.pcb`) must all share -- is exactly
    ``instance_pad_radius(ir) + PAD_BREATHING_MM``, floored at the
    caller-supplied ``min_radius_mm`` (never a value this module invents,
    since a courtyard-policy constant like
    ``cost.COURTYARD_MIN_SEPARATION_MM`` lives ABOVE ``ir.py`` in the
    import order -- see the function's own docstring)."""
    from precis.pcb.ir import PAD_BREATHING_MM

    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    pad_radius = instance_pad_radius(ir)

    # Floor inactive: min_radius_mm below every instance's own pad+breathing
    # figure, so the result is exactly the unfloored formula.
    unfloored = instance_keepout_radius_mm(ir, min_radius_mm=0.0)
    for i in range(ir.n_instances):
        assert unfloored[i] == pytest.approx(float(pad_radius[i]) + PAD_BREATHING_MM)

    # Floor active: an enormous min_radius_mm must win over every instance's
    # own (much smaller) derived figure.
    floored = instance_keepout_radius_mm(ir, min_radius_mm=1000.0)
    assert (floored == 1000.0).all()


@pytest.mark.parametrize("seed", range(12))
def test_instance_courtyard_polygon_never_touches_its_own_pads(seed: int):
    """**The structural property the whole hull-courtyard change exists
    for**: ``hull(own pad outlines) + clearance`` cannot overlap those
    pads, at any pad count, size, offset or aspect ratio. 18 of 22
    courtyard drops on the reference board were exactly that self-tangent,
    and the point of deriving the shape from the pads rather than from a
    radius is that the class becomes unrepresentable, not rarer.

    Fixtures are deliberately ASYMMETRIC — random per-pin offsets AND
    independent w/h — because a symmetric footprint cannot distinguish a
    correct courtyard from one with x and y swapped, or with a sign error
    on one axis.

    Checked at a nonzero stroke: the outline is INK, so the assertion is
    that the drawn band clears the copper, not merely that two idealized
    curves fail to intersect."""
    import random

    from precis.pcb.ir import instance_courtyard_polygon

    rng = random.Random(seed)
    clearance = 0.3
    half_stroke = 0.075
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    inst = 0
    pins = [p for p in range(ir.n_pins) if int(ir.pin_instance[p]) == inst]
    assert pins
    for p in pins:
        ir.pin_dx[p] = rng.uniform(-8.0, 8.0)
        ir.pin_dy[p] = rng.uniform(-3.0, 3.0)
        ir.pin_w[p] = rng.uniform(0.2, 2.5)
        ir.pin_h[p] = rng.uniform(0.2, 1.2)

    poly = instance_courtyard_polygon(ir, inst, clearance_mm=clearance, pins=pins)
    assert len(poly) >= 4  # closed ring, not a degenerate stub

    # Independent geometry: the courtyard's own edges vs each pad rect,
    # by point-to-segment distance — not the builder's overlap predicate.
    from precis.pcb.geom import dist_point_to_segment

    for p in pins:
        cx, cy = float(ir.pin_dx[p]), float(ir.pin_dy[p])
        hw, hh = float(ir.pin_w[p]) / 2.0, float(ir.pin_h[p]) / 2.0
        corners = [
            (cx - hw, cy - hh),
            (cx + hw, cy - hh),
            (cx + hw, cy + hh),
            (cx - hw, cy + hh),
        ]
        for a, b in itertools.pairwise(poly):
            for corner in corners:
                gap = dist_point_to_segment(corner, a, b)
                assert gap > half_stroke, (
                    f"seed {seed}: courtyard edge {a}->{b} runs {gap:.4f}mm from "
                    f"pin {p}'s pad corner {corner} — inside the {half_stroke}mm "
                    "half-stroke, so the drawn outline lands on its own copper"
                )


def test_instance_courtyard_polygon_grows_with_the_clearance_it_is_given():
    """The offset is the caller's fab-derived chain, not a constant this
    function keeps: a larger clearance must produce a strictly larger
    polygon, or the chain is being ignored and a capability change would
    move nothing."""
    from shapely.geometry import Polygon  # type: ignore[import-untyped]

    from precis.pcb.ir import instance_courtyard_polygon

    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    tight = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.1))
    loose = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.4))
    assert loose.area > tight.area
    assert loose.contains(tight)


def test_instance_courtyard_polygon_is_empty_for_a_pinless_instance():
    """A mounting hole or fiducial has no land pattern, so there is no
    shape to derive — ``[]``, never an invented size. The caller supplies
    whatever body it believes in (the function's own docstring)."""
    from precis.pcb.ir import instance_courtyard_polygon

    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    assert instance_courtyard_polygon(ir, 0, clearance_mm=0.3, pins=[]) == []


def test_from_graph_leaves_l1_l2_l3_unset():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    assert (ir.seg_layer == -1).all()
    # L2's CSR is *shaped* (every dart has a slot) but its content is
    # plain creation order, not a chosen embedding — validate_embedding
    # against real positions is what actually tells "unset" from "set".
    assert ir.rotation_darts.shape[0] == 2 * ir.n_segments
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
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
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
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)
    assert ir.dirty_l1[0] and not ir.dirty_l1[1]
    assert ir.dirty_l4[0] and ir.dirty_l5[0]
    assert not ir.dirty_l2.any()
    assert not ir.dirty_l3.any()


def test_set_layer_rejects_out_of_range():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)

    with pytest.raises(ValueError):
        ir.set_layer(0, len(DEFAULT_STACKUP))


def test_set_side_dirties_l2_l4_l5_leaves_l1_l3_clean():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    ir.set_side(0, 1)
    assert ir.dirty_l2[0] and not ir.dirty_l2[1]
    assert ir.dirty_l4[0] and ir.dirty_l5[0]
    assert not ir.dirty_l1.any()
    assert not ir.dirty_l3.any()


def test_promote_plane_dirties_only_that_nets_segments():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    n2 = 1  # "N2" power net -> its single segment
    ir.promote_plane(n2, 0)
    assert plane_layers_of(int(ir.net_plane_layers[n2])) == [0]
    n2_segs = {s for s in range(ir.n_segments) if int(ir.seg_net[s]) == n2}
    for s in range(ir.n_segments):
        assert ir.dirty_l1[s] == (s in n2_segs)
        assert ir.dirty_l4[s] == (s in n2_segs)
        assert ir.dirty_l5[s] == (s in n2_segs)
    assert not ir.dirty_l3.any()


def test_clean_clears_the_named_level_only():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    ir.clean(Level.L1)
    assert not ir.dirty_l1.any()
    assert ir.dirty_l4[0]  # L4 untouched by clean(L1)


# ── routable vs pourable: two independent per-layer questions ─────────
def test_default_stackup_routable_and_pourable_match_legacy_role_split():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    # F.Cu/B.Cu (role='signal') routable; In1.Cu/In2.Cu (role='plane') not.
    assert routable_layers(ir) == [0, 3]
    # In1.Cu/In2.Cu (role='plane') pourable; F.Cu/B.Cu (role='signal') not
    # -- this is the AUTOMATIC-annealer eligibility set only (see
    # layer_is_pourable's docstring), never a ceiling on an authored
    # op='plane_net' assignment.
    assert pourable_layers(ir) == [1, 2]


def test_explicit_routable_flag_overrides_a_plane_role_layer():
    # Annotated because the entries are heterogeneous (some carry a bool
    # `routable`, some don't) and mypy joins them to `list[object]`.
    stackup: list[dict[str, Any]] = [
        {"name": "F.Cu", "role": "signal"},
        {"name": "In1.Cu", "role": "plane", "routable": True},
        {"name": "In2.Cu", "role": "plane", "routable": True},
        {"name": "B.Cu", "role": "signal"},
    ]
    ir = from_graph(_star_graph(), stackup=stackup)
    # both inner layers stay 'plane' (still auto-pourable) but are now
    # ALSO routable -- carrying both traces and copper fill is the whole
    # point (a layer used to be either/or, gated on role alone).
    assert routable_layers(ir) == [0, 1, 2, 3]
    assert pourable_layers(ir) == [1, 2]


def test_explicit_pourable_false_overrides_a_plane_role_layer():
    stackup = [{"name": "In1.Cu", "role": "plane", "pourable": False}]
    assert layer_is_routable(stackup[0]) is False
    assert layer_is_pourable(stackup[0]) is False


def test_explicit_routable_false_overrides_a_signal_role_layer():
    stackup = [{"name": "F.Cu", "role": "signal", "routable": False}]
    assert layer_is_routable(stackup[0]) is False
    # pourable still falls through to the legacy role rule -- routable and
    # pourable are independent, an override on one never implies the other.
    assert layer_is_pourable(stackup[0]) is False


def test_layer_with_no_role_or_flags_is_neither_routable_nor_pourable():
    layer = {"name": "stiffener"}
    assert layer_is_routable(layer) is False
    assert layer_is_pourable(layer) is False


# ── layer bitmask / via-span ─────────────────────────────────────────
def test_via_layer_span_is_a_bitmask():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    through = ir.add_via(layer_span=0b1111, net_id=0)  # spans all 4 layers
    top_only = ir.add_via(layer_span=0b0001, net_id=NO_NET)  # a keepout on layer 0 only
    assert bool(int(ir.via_layer_span[through]) & (1 << 2))  # blocks layer 2 too
    assert not bool(
        int(ir.via_layer_span[top_only]) & (1 << 1)
    )  # doesn't touch layer 1
    assert int(ir.via_net[top_only]) == NO_NET  # a keepout connects nothing


def test_add_via_does_not_dirty_anything():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    ir.add_via(layer_span=1, net_id=0)
    assert not ir.dirty_l1.any() and not ir.dirty_l4.any() and not ir.dirty_l5.any()


# ── explicit embedding: propose + validate, never derive ──────────────
def test_propose_rotation_skips_pins_without_positions():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)  # no positions set
    proposal = propose_rotation_from_positions(ir)
    assert proposal == {}


def test_no_compute_embedding_entry_point_exists():
    # the whole invariant: no code path can silently re-derive L2 from
    # positions — there is no such method on the class or the module.
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    assert not hasattr(ir, "compute_embedding")
    import precis.pcb.ir as ir_mod

    assert not hasattr(ir_mod, "compute_embedding")


def _three_leaf_star():
    """A degree-3 hub H with leaves A/B/C at 0°/120°/240° — a degree-2 star
    can't exercise rotation order at all (any 2-element cyclic sequence is
    trivially a rotation of itself; nothing to get wrong), so the
    embedding tests need a real fork."""
    return {
        "instances": [{"refdes": r} for r in ("H", "A", "B", "C")],
        "nets": [_net("N", "signal", "H", "A", "B", "C")],
    }


def test_propose_then_apply_then_validate_round_trips():
    graph = _three_leaf_star()
    graph["instances"][0].update(x=0.0, y=0.0)  # H
    graph["instances"][1].update(x=10.0, y=0.0)  # A: 0deg
    graph["instances"][2].update(x=-5.0, y=8.660)  # B: 120deg
    graph["instances"][3].update(x=-5.0, y=-8.660)  # C: 240deg
    ir = from_graph(graph)

    proposal = propose_rotation_from_positions(ir)
    pin_h = 0  # H's pin is pin id 0 (first created, hub of the star)
    assert pin_h in proposal
    assert len(proposal[pin_h]) == 3
    for pin_id, order in proposal.items():
        ir.set_rotation(pin_id, order)

    assert validate_embedding(ir) == []  # stored embedding matches current positions


def test_validate_embedding_catches_a_move_without_mutating_storage():
    graph = _three_leaf_star()
    graph["instances"][0].update(x=0.0, y=0.0)  # H
    graph["instances"][1].update(x=10.0, y=0.0)  # A: 0deg
    graph["instances"][2].update(x=-5.0, y=8.660)  # B: 120deg
    graph["instances"][3].update(x=-5.0, y=-8.660)  # C: 240deg
    ir = from_graph(graph)
    for pin_id, order in propose_rotation_from_positions(ir).items():
        ir.set_rotation(pin_id, order)
    stored_before = ir.rotation_darts.copy()

    # move A from 0deg to 150deg -> past B (120deg), reversing A and B's
    # relative order around H — a genuine reordering, not a rotation of
    # the original cyclic order.
    ir.move_instance(1, x=-8.660, y=5.0)
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
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
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
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    assert same_layer_crossing_bound(ir, 3) == 0  # nothing assigned to layer 3
    assert per_layer_planar(ir, 3)


# ── same_layer_crossing_count: the GEOMETRIC crossings backing ──────────
# (found on contact 2026-08-28 — replaces same_layer_crossing_bound as the
# `crossings` cost term's backing; see that function's own docstring for
# the forest proof of why it had to).
def _crossing_pair_graph():
    """Two 2-member nets whose straight-line airwires visibly cross — the
    two diagonals of a square (U0-U1 and U2-U3). Neither net shares an
    instance or a pin with the other, so `from_graph`'s star decomposition
    (trivial for a 2-member net) yields exactly one segment per net."""
    return {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 10.0},
            {"refdes": "U2", "x": 0.0, "y": 10.0},
            {"refdes": "U3", "x": 10.0, "y": 0.0},
        ],
        "nets": [
            _net("A", "signal", "U0", "U1"),
            _net("B", "signal", "U2", "U3"),
        ],
    }


def test_same_layer_crossing_count_finds_a_genuine_geometric_crossing():
    ir = from_graph(_crossing_pair_graph(), stackup=DEFAULT_STACKUP)
    ir.seg_layer[:] = 0
    assert same_layer_crossing_count(ir, 0) == 1

    # move one segment to another layer -- the crossing resolves, on
    # EITHER layer (only one segment sits alone on each now)
    ir.set_layer(0, 1)
    assert same_layer_crossing_count(ir, 0) == 0
    assert same_layer_crossing_count(ir, 1) == 0


def test_same_layer_crossing_count_zero_for_star_spokes_sharing_a_hub():
    """The case that would otherwise make every net look self-crossing: a
    star's spokes all emanate from the SAME hub pin position. Naively
    treating them as ordinary segments risks manufacturing false
    crossings between spokes that only ever touch at that shared point;
    `same_layer_crossing_count` must not."""
    graph = {
        "instances": [{"refdes": "HUB", "x": 0.0, "y": 0.0}]
        + [
            {"refdes": f"U{i}", "x": 10.0 * math.cos(a), "y": 10.0 * math.sin(a)}
            for i, a in enumerate((0.3, 1.1, 2.4, 3.9, 5.2))
        ],
        "nets": [_net("N", "signal", "HUB", "U0", "U1", "U2", "U3", "U4")],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.seg_layer[:] = 0
    assert ir.n_segments == 5  # one star, 5 spokes off HUB
    assert same_layer_crossing_count(ir, 0) == 0


def test_same_layer_crossing_count_excludes_unplaced_segments_not_origin():
    """An unplaced (NaN) pin must be EXCLUDED, never treated as sitting at
    (0, 0) -- that would manufacture phantom crossings among every
    unplaced net's segments."""
    graph = _crossing_pair_graph()
    del graph["instances"][2]["x"], graph["instances"][2]["y"]  # U2 unplaced
    del graph["instances"][3]["x"], graph["instances"][3]["y"]  # U3 unplaced
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.seg_layer[:] = 0
    assert segment_points(ir, 1) is None  # net B's segment: unplaced
    assert same_layer_crossing_count(ir, 0) == 0


def test_segment_points_returns_instance_centroids_or_none():
    ir = from_graph(_crossing_pair_graph(), stackup=DEFAULT_STACKUP)
    assert segment_points(ir, 0) == ((0.0, 0.0), (10.0, 10.0))

    ir2 = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)  # no positions at all
    assert segment_points(ir2, 0) is None


# ── plane connectivity ────────────────────────────────────────────────
def test_plane_connectivity_zero_stitches_not_ok():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    ir.promote_plane(0, 1)
    result = plane_connectivity(ir, 0, 1)
    assert isinstance(result, PlaneConnectivity)
    assert result.stitch_vias == []
    assert not result.ok


def test_plane_connectivity_one_stitch_is_a_single_point_of_failure():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    ir.promote_plane(0, 1)
    ir.add_via(layer_span=(1 << 1), net_id=0)
    assert not plane_connectivity(ir, 0, 1).ok


def test_plane_connectivity_two_stitches_ok():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    ir.promote_plane(0, 1)
    ir.add_via(layer_span=(1 << 1), net_id=0)
    ir.add_via(layer_span=(1 << 1) | (1 << 0), net_id=0)
    result = plane_connectivity(ir, 0, 1)
    assert result.ok
    assert len(result.stitch_vias) == 2


def test_plane_connectivity_ignores_vias_on_other_layers_or_nets():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)
    ir.promote_plane(0, 1)
    ir.add_via(layer_span=(1 << 0), net_id=0)  # right net, wrong layer
    ir.add_via(layer_span=(1 << 1), net_id=1)  # right layer, wrong net
    assert plane_connectivity(ir, 0, 1).stitch_vias == []


# ── L4 metric annotations ────────────────────────────────────────────
def test_compute_gap_capacity_leaves_unplaced_segments_nan():
    ir = from_graph(_star_graph(), stackup=DEFAULT_STACKUP)  # no positions
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


# ── fixed='xy'|'rot'|'both': two independent lock bits ──────────────────
def test_from_graph_splits_fixed_xy_and_fixed_rot():
    graph = {
        "instances": [
            {"refdes": "U1", "fixed": "xy"},
            {"refdes": "U2", "fixed": "rot"},
            {"refdes": "U3", "fixed": "both"},
            {"refdes": "U4"},
        ],
        "nets": [],
    }
    ir = from_graph(graph)
    assert bool(ir.inst_fixed_xy[0]) and not bool(ir.inst_fixed_rot[0])
    assert not bool(ir.inst_fixed_xy[1]) and bool(ir.inst_fixed_rot[1])
    assert bool(ir.inst_fixed_xy[2]) and bool(ir.inst_fixed_rot[2])
    assert not bool(ir.inst_fixed_xy[3]) and not bool(ir.inst_fixed_rot[3])


# ── nearest_other_instance: the id-tracking gap search ───────────────────
def _place(graph, coords):
    for i, (x, y) in enumerate(coords):
        graph["instances"][i].update(x=x, y=y)
    return graph


def test_nearest_other_instance_returns_distance_and_realizing_id():
    graph = _place(_star_graph(), [(0.0, 0.0), (10.0, 0.0), (0.0, 5.0), (100.0, 100.0)])
    ir = from_graph(graph)
    found = nearest_other_instance(ir, 0)  # segment U1-U2 (endpoints 0, 1)
    assert found is not None
    gap, nearest_id = found
    assert nearest_id == 2  # U3 at (0,5) is closer to U1 than U4 is to either endpoint
    assert gap == pytest.approx(5.0)


def test_nearest_other_instance_none_when_position_unset():
    ir = from_graph(_star_graph())  # no positions at all
    assert nearest_other_instance(ir, 0) is None


def test_compute_gap_capacity_seg_ids_restricts_recompute():
    graph = _place(_star_graph(), [(0.0, 0.0), (10.0, 0.0), (0.0, 5.0), (100.0, 100.0)])
    ir = from_graph(graph)
    compute_gap_capacity(ir, pitch_mm=1.0, seg_ids=[0])
    assert not math.isnan(ir.seg_gap_capacity[0])
    assert math.isnan(ir.seg_gap_capacity[1])  # untouched: not in seg_ids
    # a subsequent unrestricted call still recomputes everything, matching
    # the pre-existing (seg_ids=None) behaviour.
    compute_gap_capacity(ir, pitch_mm=1.0)
    assert not math.isnan(ir.seg_gap_capacity[1])
