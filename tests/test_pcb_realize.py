"""Unit + property tests for precis.pcb.realize — sketch -> copper
geometry. No DB.

Covers: the tangent+arc closed form against an independently-derived
formula (not the implementation's own math, restated), a clearance
property test over many randomized obstacle placements, the gerber.py
round-trip (verifying the "expected model shape actually matches" claim
by exercising the real writer, not just inspecting the dict), per-gap
congestion accounting, the dog-bone-stub policy for plane-served nets,
and rip-up leaving other nets' geometry byte-identical.
"""

from __future__ import annotations

import itertools
import math
import random
from typing import Any

import pytest

from precis.pcb import DEFAULT_STACKUP, gerber
from precis.pcb import drc as pcb_drc
from precis.pcb.capabilities import capability_for
from precis.pcb.connectivity import net_islands
from precis.pcb.ir import from_graph, pin_point
from precis.pcb.planes import point_in_pour
from precis.pcb.realize import (
    PAD_LAYER,
    CongestionWarning,
    Obstacle,
    RealizeConfig,
    _track_from_run,
    pad_geometry,
    pads_for_ir,
    pin_topology,
    re_realize_segments,
    realize,
    rip_net,
    tangent_arc_path,
    to_gerber_model,
)
from precis.pcb.rules import (
    ipc2221_track_width_mm,
    via_capacity_a,
    via_count_for_current,
)

#: This module tests the TANGENT drawer's contract, so it says so rather
#: than riding the default. Those contracts — one straight-or-hugging
#: track per segment, on the segment's own ``seg_layer``, with vias
#: wherever that layer isn't the pad layer — are specifically what the
#: maze router (now the default) does NOT promise: it chooses its own
#: layers and its own path, and reports what it could not route. Both are
#: real and both need tests; the maze router's live in
#: ``tests/test_pcb_maze.py``.
_TANGENT = RealizeConfig(router="tangent")

# ── the closed-form geometric primitive ──────────────────────────────────


def test_tangent_arc_path_straight_line_when_unblocked():
    segs, length = tangent_arc_path((0.0, 0.0), (10.0, 0.0), (0.0, 10.0), 1.0)
    assert segs == [{"shape": "line", "start": [0.0, 0.0], "end": [10.0, 0.0]}]
    assert length == pytest.approx(10.0)


def test_tangent_arc_path_matches_hand_computed_closed_form():
    """S=(-3,0), E=(3,0), obstacle at the origin, radius 1 -- a fully
    symmetric configuration whose length is independently derivable:
    tangent length = sqrt(d^2 - r^2) (Pythagoras on the tangent-radius-
    hypotenuse right triangle); the swept arc's central angle follows
    from the chord between the two tangent points (both at
    y = -r*sin(theta), separated by 2*r*cos(theta)) via the standard
    chord/central-angle relation `chord = 2r*sin(phi/2)`, giving
    `phi = pi - 2*theta`. This formula is written independently of
    realize.py's own implementation (which brute-forces 4 tangent-point
    pairings) -- a match here is a real cross-check, not a restatement.
    """
    d, r = 3.0, 1.0
    theta = math.acos(r / d)
    expected_tangent = math.sqrt(d * d - r * r)
    expected_arc = r * (math.pi - 2 * theta)
    expected_total = 2 * expected_tangent + expected_arc

    segs, length = tangent_arc_path((-3.0, 0.0), (3.0, 0.0), (0.0, 0.0), 1.0)
    assert length == pytest.approx(expected_total, rel=1e-9)

    # shape: line, arc, line
    assert [s["shape"] for s in segs] == ["line", "arc", "line"]
    line1, arc, line2 = segs
    assert math.hypot(
        line1["end"][0] - line1["start"][0], line1["end"][1] - line1["start"][1]
    ) == pytest.approx(expected_tangent, rel=1e-9)
    assert math.hypot(
        line2["end"][0] - line2["start"][0], line2["end"][1] - line2["start"][1]
    ) == pytest.approx(expected_tangent, rel=1e-9)
    assert arc["center"] == [0.0, 0.0]
    # arc central angle from its own endpoints, independently of the
    # "cw" bookkeeping -- must match the hand-derived sweep magnitude.
    a1 = math.atan2(arc["start"][1], arc["start"][0])
    a2 = math.atan2(arc["end"][1], arc["end"][0])
    diff = abs((a2 - a1 + math.pi) % (2 * math.pi) - math.pi)
    assert diff == pytest.approx(expected_arc / r, rel=1e-9)


def test_tangent_arc_path_clears_the_obstacle_everywhere():
    """Property test: for many randomized obstacle placements where the
    straight line IS blocked, every sampled point along the returned
    path (tangent lines + arc) stays outside the clearance circle."""
    rng = random.Random(0)
    trials = 0
    for _ in range(600):
        cx, cy = rng.uniform(-5, 5), rng.uniform(-5, 5)
        r = rng.uniform(0.3, 2.0)
        ang1, ang2 = rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi)
        d1, d2 = rng.uniform(r + 1, r + 6), rng.uniform(r + 1, r + 6)
        start = (cx + d1 * math.cos(ang1), cy + d1 * math.sin(ang1))
        end = (cx + d2 * math.cos(ang2), cy + d2 * math.sin(ang2))

        def dist_to_segment(p, a, b):
            ax, ay = a
            bx, by = b
            px, py = p
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = (
                max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                if l2
                else 0.0
            )
            return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

        if dist_to_segment((cx, cy), start, end) >= r:
            continue  # not actually blocked -- skip, this trial is trivial
        trials += 1
        segs, _length = tangent_arc_path(start, end, (cx, cy), r)
        for seg in segs:
            samples = _sample_segment(seg)
            for p in samples:
                clearance = math.hypot(p[0] - cx, p[1] - cy) - r
                assert clearance >= -1e-6, (start, end, cx, cy, r, seg, clearance)
    assert (
        trials > 50
    )  # sanity: the randomized generator actually produced blocked cases


def _sample_segment(seg, n=20):
    if seg["shape"] == "line":
        ax, ay = seg["start"]
        bx, by = seg["end"]
        return [(ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(n + 1)]
    cx, cy = seg["center"]
    ax, ay = seg["start"]
    bx, by = seg["end"]
    r = math.hypot(ax - cx, ay - cy)
    a1 = math.atan2(ay - cy, ax - cx)
    a2 = math.atan2(by - cy, bx - cx)
    diff = (a2 - a1) % (2 * math.pi)
    if not seg["cw"]:
        sweep = diff
    else:
        sweep = diff - 2 * math.pi
    return [
        (cx + r * math.cos(a1 + sweep * i / n), cy + r * math.sin(a1 + sweep * i / n))
        for i in range(n + 1)
    ]


def test_tangent_arc_path_raises_when_endpoint_inside_obstacle():
    with pytest.raises(ValueError):
        tangent_arc_path((0.0, 0.0), (10.0, 0.0), (0.0, 0.0), 5.0)


# ── gerber.py round-trip: does the model shape actually match? ──────────
def test_realize_output_round_trips_into_gerber_without_shape_errors():
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 0.0},
            {"refdes": "OBSTACLE", "x": 5.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "SIG",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                ],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    obstacles = [Obstacle(instance=2, center=(5.0, 0.0), radius=1.0)]
    result = realize(
        ir,
        obstacles=obstacles,
        config=RealizeConfig(clearance_mm=0.1, router="tangent"),
    )
    assert result.tracks
    track = result.tracks[0]
    assert track.blocked_by == 2  # routed around the obstacle instance
    assert any(s["shape"] == "arc" for s in track.segments)

    model = to_gerber_model(
        result,
        ir,
        layers=[layer["name"] for layer in DEFAULT_STACKUP],
        outline=[[0.0, -5.0], [20.0, -5.0], [20.0, 5.0], [0.0, 5.0]],
    )
    files = gerber.export_gerbers(model, name="realize-test")
    assert files  # no shape errors raised, at least one file produced
    fcu = files["realize-test-F_Cu.gbr"]
    assert "G02" in fcu or "G03" in fcu  # the arc actually emitted an arc opcode


def test_realize_dogbone_stub_for_plane_promoted_net():
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 20.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                ],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.promote_plane(0, 1)  # In1.Cu, the GND plane layer
    result = realize(ir, config=_TANGENT)
    track = result.tracks[0]
    assert track.is_dogbone
    assert track.length_mm <= RealizeConfig().dogbone_stub_mm + 1e-9
    assert track.length_mm < 20.0  # nowhere near the far end -- a stub, not a route


def test_plane_fanout_drop_via_clears_its_own_pad():
    """gr — ``_drop_via_site`` (the MAZE router's dog-bone drop, a
    different code path from the tangent drawer's
    :func:`test_realize_dogbone_stub_for_plane_promoted_net` above) used
    to check ``grid.disk_is_free`` only — deliberately same-net-blind, the
    exact question :meth:`~precis.pcb.maze.OccupancyGrid.via_clears_pads`
    exists to ask instead — and never asked it. ``grid.route``'s own via
    search already folds that guard into its candidate mask; this search
    tried candidates without it, so a wide pad's own drop via routinely
    landed ON that pad. Measured on the reference board: 55 of 57 DRC
    findings were exactly this, at essentially every plane-promoted pin.

    U0's pad is the board-default single-pin size (1.0mm round, no
    footprint override needed — this is not a fixture-specific pad, it is
    what a bare board already has), which alone makes the required
    via-to-pad distance bigger than the OLD flat ``dogbone_stub_mm``
    (0.5mm) reach: exactly the shape of the reported defect, not a
    contrived one.
    """
    from precis.pcb.geom import dist

    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 20.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                ],
            }
        ],
    }
    outline = [[-10.0, -10.0], [30.0, -10.0], [30.0, 10.0], [-10.0, 10.0]]
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=outline)
    ir.promote_plane(0, 1)  # In1.Cu, the GND plane layer
    cap = capability_for("4layer")
    config = RealizeConfig(router="maze", fab_caps=cap)
    result = realize(ir, config=config)
    assert not result.unrouted, [r.message for r in result.unrouted_reasons]
    dogbone_vias = [v for v in result.vias if v.endpoint == "a"]
    assert dogbone_vias, "the plane fanout must have dropped at least one via"

    # The geometric property, tied to the router's own conservative
    # enclosing-circle pad radius (pad_geometry's own convention) rather
    # than to the DRC rule's separate (less strict) circumscribed one --
    # this must hold regardless of which check is reading it.
    for pid in range(ir.n_pins):
        point = pin_point(ir, pid)
        if point is None or int(ir.pin_net[pid]) != 0:
            continue
        geom = pad_geometry(ir)[pid]
        pad_radius = math.hypot(geom.w_mm, geom.h_mm) / 2.0
        for v in dogbone_vias:
            gap = dist(point, (v.x, v.y)) - pad_radius - v.dia_mm / 2.0
            assert gap >= config.clearance_mm - 1e-9, (
                "a drop via landed closer to its own pad than the "
                "resolved net clearance allows",
                pid,
                point,
                v,
                gap,
            )

    model = to_gerber_model(
        result, ir, layers=[layer["name"] for layer in DEFAULT_STACKUP], outline=[]
    )
    findings = pcb_drc.check_via_pad_keepout(model, cap)
    assert not findings, [f.detail for f in findings]


def test_signal_layers_delegates_to_ir_routable_layers_and_respects_override():
    """``realize._signal_layers`` used to keep its own four-line copy of
    this query (its own docstring said so); it now calls
    :func:`precis.pcb.ir.routable_layers`, the SAME function
    :func:`precis.pcb.session.signal_layers` delegates to -- one place,
    not two. Also exercises the new ``"routable"`` override actually
    reaching the router's own eligible-layer set: an inner (``role=
    'plane'``) layer with ``"routable": True`` must show up here, not
    just in the IR-level predicate test."""
    from precis.pcb import realize as pcb_realize
    from precis.pcb import session as pcb_session

    stackup: list[dict[str, Any]] = [
        {"name": "F.Cu", "role": "signal"},
        {"name": "In1.Cu", "role": "plane", "routable": True},
        {"name": "In2.Cu", "role": "plane"},
        {"name": "B.Cu", "role": "signal"},
    ]
    ir = from_graph({"instances": [], "nets": []}, stackup=stackup)
    assert pcb_realize._signal_layers(ir) == [0, 1, 3]
    assert pcb_session.signal_layers(ir) == [0, 1, 3]


def test_layer_preferences_default_stackup_matches_a_single_call():
    from precis.pcb.realize import _layer_preferences

    ir = from_graph({"instances": [], "nets": []}, stackup=DEFAULT_STACKUP)
    # F.Cu/B.Cu are the only routable layers in DEFAULT_STACKUP -- both
    # outer, so the outer/inner split degenerates to a single call and
    # must reproduce today's H/V assignment exactly.
    assert _layer_preferences(ir, [0, 3]) == {0: "h", 3: "v"}


def test_layer_preferences_gives_in1_h_and_in2_v_when_all_four_are_routable():
    from precis.pcb.realize import _layer_preferences

    stackup: list[dict[str, Any]] = [
        {"name": "F.Cu", "role": "signal"},
        {"name": "In1.Cu", "role": "plane", "routable": True},
        {"name": "In2.Cu", "role": "plane", "routable": True},
        {"name": "B.Cu", "role": "signal"},
    ]
    ir = from_graph({"instances": [], "nets": []}, stackup=stackup)
    # A single preferred_directions([0,1,2,3]) call would give
    # F.Cu='h', In1.Cu='v', In2.Cu='diagonal', B.Cu='h' -- NOT what a
    # caller who marked In1/In2 routable asked for. The outer/inner split
    # restarts the H/V/diagonal cycle per group instead.
    assert _layer_preferences(ir, [0, 1, 2, 3]) == {
        0: "h",  # F.Cu -- first OUTER routable layer
        3: "v",  # B.Cu -- second OUTER routable layer
        1: "h",  # In1.Cu -- first INNER routable layer
        2: "v",  # In2.Cu -- second INNER routable layer
    }


def test_plane_pour_merges_same_net_copper_and_antipads_foreign_net():
    """A GND fill and a routed SIG net sharing ONE layer — the standard
    4-layer arrangement a layer must be able to express (routing and
    pouring are no longer mutually exclusive; F.Cu is ``role='signal'``
    in :data:`DEFAULT_STACKUP`, i.e. routable, and nothing about
    :func:`precis.pcb.planes.plane_pours` restricts pouring to a
    ``role='plane'`` layer).

    :func:`precis.pcb.planes.plane_pours`'s same-net rule (see that
    module's docstring) has two ways to get it backwards and BOTH look
    like a plausible render: merging your own net's copper into the pour
    wrong leaves GND's own stub/via disconnected from its own pour
    (nothing about "this net has no copper touching itself" is a
    clearance violation, so only :func:`net_islands` catches it);
    failing to antipad a FOREIGN net's copper shorts the pour to SIG's
    trace (a normal-looking solid fill). The antipad half is checked
    with :func:`precis.pcb.planes.point_in_pour` directly (the same
    exact predicate :func:`net_islands` itself uses for pour membership)
    rather than :mod:`precis.pcb.drc`'s clearance check: that module's
    ``_copper_item_polygon`` reads a pour's ``polygon`` only and silently
    drops ``holes`` (found while building this test — a real, pre-existing
    gap reported separately, not this test's to fix or route around by
    weakening the assertion)."""
    graph = {
        "instances": [
            {"refdes": "G1", "x": 5.0, "y": 15.0},
            {"refdes": "G2", "x": 35.0, "y": 15.0},
            {"refdes": "S1", "x": 15.0, "y": 5.0},
            {"refdes": "S2", "x": 25.0, "y": 25.0},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "domain": "electrical",
                "members": [
                    {"refdes": "G1", "pin": "1"},
                    {"refdes": "G2", "pin": "1"},
                ],
            },
            {
                "name": "SIG",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "S1", "pin": "1"},
                    {"refdes": "S2", "pin": "1"},
                ],
            },
        ],
    }
    outline = [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]]
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=outline)
    ir.promote_plane(0, 0)  # GND onto F.Cu -- a ROUTABLE ('signal') layer
    config = RealizeConfig(fab_caps=capability_for("4layer"))
    result = realize(ir, config=config)
    assert result.pours, "GND must actually be poured on F.Cu"
    assert not result.unrouted, [
        (s, r.message) for s, r in zip(result.unrouted, result.unrouted_reasons)
    ]
    sig_tracks = [t for t in result.tracks if t.net_id == 1]
    assert sig_tracks, "SIG must have routed on a layer the pour also covers"
    gnd_vias = [v for v in result.vias if v.net_id == 0]
    assert gnd_vias, "GND's dog-bone fanout must have dropped a via into the plane"

    model = to_gerber_model(
        result,
        ir,
        layers=[layer["name"] for layer in DEFAULT_STACKUP],
        outline=outline,
    )

    # merge: GND's own stub+via must be ONE connected island with its
    # pour -- net_islands only reports a net split across >1 fragment, so
    # GND's absence here IS the merge assertion.
    islands = {i.net for i in net_islands(model)}
    assert "GND" not in islands, net_islands(model)

    # antipad: GND's own via (the connection) must sit INSIDE the pour;
    # SIG's foreign-net trace must sit OUTSIDE it (in the hole cut around
    # it) -- both read straight off the pour geometry plane_pours itself
    # produced, the same predicate net_islands uses for pour membership.
    pour = result.pours[0]
    assert pour["net"] == "GND" and pour["layer"] == "F.Cu"
    gv = gnd_vias[0]
    assert point_in_pour(pour, gv.x, gv.y), "GND's own via must merge into its pour"
    sx, sy = sig_tracks[0].segments[0]["start"]
    assert not point_in_pour(pour, sx, sy), (
        "SIG's foreign-net trace must be antipadded out of the GND pour"
    )


def test_plane_flooded_on_two_layers_pours_both_and_stays_one_net_island():
    """gr — a net promoted on SEVERAL layers at once (``net_plane_layers``
    is a bitmask now, not a scalar), which is what a real 4-layer board
    does for GND/PWR — flooded on every plane-role layer it reaches, not
    just one. This pins two things the multi-layer cutover must get right
    together: (1) :func:`~precis.pcb.realize._pour_planes` actually pours
    BOTH layers (one dict entry per layer, keyed by layer -- module note
    on :func:`precis.pcb.planes.plane_pours`'s ``plane_nets`` shape), and
    (2) the two poured SHEETS come out as ONE electrical net, not two
    disconnected fragments — :func:`~precis.pcb.realize._plane_fanout`'s
    one-via-per-pin rule spans from the pad's own layer to the FARTHEST
    poured layer, and a through via barrel connects every layer inside
    that span (RealizedVia is always a contiguous span, never a scalar —
    see that class's own docstring, and :mod:`precis.pcb.connectivity`'s
    via-group union, which joins a via's primitives on every layer
    ``_via_layer_names`` reports, not just its two ends). No separate
    "stitching via" pass is needed for this: the ordinary per-pin drop via
    already IS the stitch."""
    graph = {
        "instances": [
            {"refdes": "G1", "x": 5.0, "y": 15.0},
            {"refdes": "G2", "x": 35.0, "y": 15.0},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "domain": "electrical",
                "members": [
                    {"refdes": "G1", "pin": "1"},
                    {"refdes": "G2", "pin": "1"},
                ],
            },
        ],
    }
    outline = [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]]
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=outline)
    ir.promote_plane(0, 1)  # In1.Cu
    ir.promote_plane(0, 2)  # In2.Cu -- SAME net, a second layer
    config = RealizeConfig(fab_caps=capability_for("4layer"))
    result = realize(ir, config=config)
    assert not result.unrouted, [
        (s, r.message) for s, r in zip(result.unrouted, result.unrouted_reasons)
    ]

    poured_layers = {p["layer"] for p in result.pours}
    assert poured_layers == {"In1.Cu", "In2.Cu"}, (
        "GND must be poured on BOTH of its promoted layers, not just one",
        result.pours,
    )

    gnd_vias = [v for v in result.vias if v.net_id == 0]
    assert gnd_vias, "GND's dog-bone fanout must have dropped a via into the plane(s)"
    # The stitching claim itself: every drop via's span must reach BOTH
    # poured layers (indices 1 and 2), not stop at the nearer one.
    for v in gnd_vias:
        assert v.layer_lo <= 1 and v.layer_hi >= 2, (
            "a plane-fanout via must span every poured layer of its net, "
            "not just the nearest one",
            v,
        )

    model = to_gerber_model(
        result,
        ir,
        layers=[layer["name"] for layer in DEFAULT_STACKUP],
        outline=outline,
    )
    islands = {i.net for i in net_islands(model)}
    assert "GND" not in islands, (
        "GND's two poured sheets must come out as ONE connected net via the "
        "shared through-via, not two disconnected islands",
        net_islands(model),
    )


def test_stitch_one_net_sprinkles_a_via_and_merges_two_overlapping_sheets():
    """gr270637 — the deliberate stitching pass (module docstring's "Plane
    fragment stitching" note), exercised directly against
    :func:`~precis.pcb.realize._stitch_one_net` rather than through a full
    place+route, so the fragmentation is CONSTRUCTED rather than merely
    hoped for: two same-net sheets on DIFFERENT layers that overlap, with
    NO existing via connecting them at all. The test above
    (``test_plane_flooded_on_two_layers_pours_both_and_stays_one_net_island``)
    proves the ordinary per-pin drop via already IS the stitch when it
    happens to span both layers — this test proves the NEW pass still
    works when that incidental mechanism is entirely absent, i.e. it is
    doing real work, not riding along.

    The result is independently re-checked with
    :func:`precis.pcb.connectivity.net_islands` — the SAME independent
    checker the module docstring says this pass must never call from
    inside itself — as a one-shot test assertion, not a loop condition."""
    from precis.pcb import maze as pcb_maze
    from precis.pcb.realize import _pour_polygon, _stitch_one_net
    from precis.pcb.rules import NetRules

    spec = pcb_maze.GridSpec(x0=0.0, y0=0.0, pitch=0.1, nx=300, ny=150, n_layers=4)
    grid = pcb_maze.OccupancyGrid(spec, clearance_mm=0.15)
    rules = NetRules(
        track_width_mm=0.25, clearance_mm=0.15, via_dia_mm=0.5, via_drill_mm=0.25
    )
    config = RealizeConfig()

    pour_a = {
        "ctype": "pour",
        "layer": "F.Cu",
        "net": "GND",
        "polygon": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
    }
    pour_b = {
        "ctype": "pour",
        "layer": "B.Cu",
        "net": "GND",
        "polygon": [[5.0, 0.0], [15.0, 0.0], [15.0, 10.0], [5.0, 10.0]],
    }
    frags = [(0, _pour_polygon(pour_a)), (1, _pour_polygon(pour_b))]

    new_vias, remaining, message = _stitch_one_net(
        0, "GND", frags, [], grid, rules, config, []
    )
    assert remaining == 1, message
    assert not message, "a fully-stitched net must report no failure message"
    assert new_vias, "sprinkle stage must have placed at least one stitching via"
    for v in new_vias:
        assert v.net_id == 0
        assert (v.layer_lo, v.layer_hi) == (0, 1), (
            "a stitching via must span exactly the two sheets it joins",
            v,
        )
        # every placed via must actually land in the overlap ([5, 10] x
        # [0, 10]) it was placed to bridge, not merely somewhere legal.
        assert 5.0 <= v.x <= 10.0 and 0.0 <= v.y <= 10.0, v

    # `net_islands` only counts TRACK/VIA primitives (a bare pour polygon
    # is not itself a graph node -- it only unions whatever already falls
    # inside it), so give each sheet an independent "anchor" -- standing in
    # for a real per-pin drop via elsewhere on that one layer, same as a
    # real board always has SOMETHING landing on a plane -- and confirm the
    # anchors end up in ONE component only via the stitching via(s) above,
    # not because they were placed in the same spot to begin with.
    anchor_a = {
        "ctype": "via",
        "net": "GND",
        "x": 1.0,
        "y": 1.0,
        "dia_mm": 0.3,
        "layers": ["F.Cu"],
    }
    anchor_b = {
        "ctype": "via",
        "net": "GND",
        "x": 14.0,
        "y": 9.0,
        "dia_mm": 0.3,
        "layers": ["B.Cu"],
    }
    model = {
        "layers": ["F.Cu", "B.Cu"],
        "copper": [pour_a, pour_b, anchor_a, anchor_b]
        + [
            {
                "ctype": "via",
                "net": "GND",
                "x": v.x,
                "y": v.y,
                "dia_mm": v.dia_mm,
                "span": ["F.Cu", "B.Cu"],
            }
            for v in new_vias
        ],
        "pads": [],
    }
    islands = net_islands(model)
    assert "GND" not in {i.net for i in islands}, (
        "the independent connectivity checker must agree the two sheets "
        "are now one piece",
        islands,
    )


def test_stitch_one_net_announces_a_gap_no_single_via_can_bridge():
    """gr270637 — pins the FAILURE direction the task brief asks for: two
    same-net, same-layer pieces separated by more than one via's own
    diameter cannot be joined by a single via (module docstring's
    "provable limit" note in :func:`~precis.pcb.realize._stitch_one_net`
    — a via touches copper at one (x, y) with a fixed radius, so by the
    triangle inequality a gap wider than twice that radius is not merely
    hard, it is impossible for one via). The pass must SAY SO rather than
    silently returning a result that still looks stitched: a non-empty
    ``message``, a remaining-piece count > 1, and — critically — no via
    placed at all for this pair (a check that cannot fire must announce
    it, not paper over it with copper that doesn't actually join anything).

    Independently re-checked with
    :func:`precis.pcb.connectivity.net_islands`: the two pieces really are
    reported as two, confirming this isn't a false alarm from this pass's
    own (deliberately independent, module docstring) reasoning."""
    from precis.pcb import maze as pcb_maze
    from precis.pcb.realize import _pour_polygon, _stitch_one_net
    from precis.pcb.rules import NetRules

    spec = pcb_maze.GridSpec(x0=0.0, y0=0.0, pitch=0.1, nx=400, ny=100, n_layers=4)
    grid = pcb_maze.OccupancyGrid(spec, clearance_mm=0.15)
    rules = NetRules(
        track_width_mm=0.25, clearance_mm=0.15, via_dia_mm=0.5, via_drill_mm=0.25
    )
    config = RealizeConfig()

    # Same layer, 15mm apart -- 30x this net's own 0.5mm via diameter, so
    # no single via can ever touch both regardless of what else is (or
    # isn't) on the board.
    pour_a = {
        "ctype": "pour",
        "layer": "F.Cu",
        "net": "GND",
        "polygon": [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 5.0]],
    }
    pour_b = {
        "ctype": "pour",
        "layer": "F.Cu",
        "net": "GND",
        "polygon": [[20.0, 0.0], [25.0, 0.0], [25.0, 5.0], [20.0, 5.0]],
    }
    frags = [(0, _pour_polygon(pour_a)), (0, _pour_polygon(pour_b))]

    new_vias, remaining, message = _stitch_one_net(
        0, "GND", frags, [], grid, rules, config, []
    )
    assert remaining == 2, "two genuinely disjoint same-layer pieces stay two"
    assert message, "an unstitched net must announce itself, not stay silent"
    assert "no spatial overlap" in message
    assert not new_vias, (
        "no via should be placed at all for a pair that is provably "
        "unbridgeable by one -- a via that doesn't actually join anything "
        "would be worse than none",
        new_vias,
    )

    # Same anchor discipline as the positive test above -- a bare pour
    # polygon is not itself a node `net_islands` counts.
    anchor_a = {
        "ctype": "via",
        "net": "GND",
        "x": 2.0,
        "y": 2.0,
        "dia_mm": 0.3,
        "layers": ["F.Cu"],
    }
    anchor_b = {
        "ctype": "via",
        "net": "GND",
        "x": 22.0,
        "y": 2.0,
        "dia_mm": 0.3,
        "layers": ["F.Cu"],
    }
    model = {
        "layers": ["F.Cu"],
        "copper": [pour_a, pour_b, anchor_a, anchor_b],
        "pads": [],
    }
    islands = net_islands(model)
    assert any(i.net == "GND" and i.components == 2 for i in islands), islands


def test_plane_pour_antipads_a_pad_that_has_no_track_or_via_at_all():
    """gr — a PAD is not a track or a via, and ``to_gerber_model``'s
    ``model["copper"]`` never carries one (pads are the separate
    ``model["pads"]`` key, :func:`~precis.pcb.realize.pads_for_ir`'s own
    docstring). ``_pour_planes`` used to hand ``plane_pours`` only
    ``["copper"]``, so a fill flowed straight over every pad on its
    layer — on the reference board, essentially every net's pads merged
    into a GND fill on F.Cu. An unconnected pin's pad is the sharpest
    proof: it has NO track and NO via anywhere, so before this fix
    nothing in ``model["copper"]`` sat at its coordinates at all — the
    antipad genuinely did not exist, not merely the wrong shape.

    Covers both fake-blocker shapes :func:`~precis.pcb.realize.
    _pad_blockers` produces: NC1 (a lone pin -> a round, single-pin
    synthesized pad, faked as a ``via``) and R1 (a two-pin part -> a
    rectangular ``passive``-family pad, faked as a ``pour``).
    """
    graph = {
        "instances": [
            {"refdes": "G1", "x": 5.0, "y": 15.0},
            {"refdes": "NC1", "x": 20.0, "y": 15.0},
            {"refdes": "R1", "x": 30.0, "y": 15.0},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "domain": "electrical",
                "members": [{"refdes": "G1", "pin": "1"}],
            },
        ],
        "unconnected": [
            {"refdes": "NC1", "pin": "1"},
            {"refdes": "R1", "pin": "1"},
            {"refdes": "R1", "pin": "2"},
        ],
    }
    outline = [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]]
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=outline)
    ir.promote_plane(0, 0)  # GND onto F.Cu -- the layer NC1/R1's pads sit on
    config = RealizeConfig(fab_caps=capability_for("4layer"))
    result = realize(ir, config=config)
    assert result.pours, "GND must actually be poured on F.Cu"
    pour = result.pours[0]
    assert pour["net"] == "GND" and pour["layer"] == "F.Cu"

    # NC1 is a lone pin (round, single-pin synthesized pad) sitting
    # exactly at its instance's own placement.
    assert not point_in_pour(pour, 20.0, 15.0), (
        "NC1's round pad has no track/via of its own -- it must still be "
        "antipadded out of the GND pour"
    )
    # R1 is a two-pin part -- its own pads straddle the instance centre
    # (landpattern.offsets_for), so read their real placed coordinates
    # back off the IR rather than assuming they sit at (30, 15) itself.
    r1_pins = [
        pid
        for pid in range(ir.n_pins)
        if str(ir.instance_refdes[int(ir.pin_instance[pid])]) == "R1"
    ]
    assert len(r1_pins) == 2, r1_pins
    for pid in r1_pins:
        point = pin_point(ir, pid)
        assert point is not None
        assert not point_in_pour(pour, *point), (
            "R1's rectangular pad has no track/via of its own -- it must "
            "still be antipadded out of the GND pour",
            point,
        )

    # Both pads are real copper (they were checkable at all only because
    # they exist), so a hole must actually have been cut -- not merely
    # "this exact point happens to fall outside the pour's bbox" by luck.
    assert pour.get("holes"), "no antipad hole was cut for either foreign pad"


# ── per-gap capacity accounting ──────────────────────────────────────────
def test_congestion_warning_when_gap_over_capacity():
    instances = [
        {"refdes": "U0", "x": 0.0, "y": 0.0},
        {"refdes": "HUB", "x": 0.1, "y": 0.0},
    ]
    nets = []
    for i in range(6):
        instances.append({"refdes": f"FAR{i}", "x": 10.0, "y": float(i)})
        nets.append(
            {
                "name": f"N{i}",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": f"p{i}"},
                    {"refdes": f"FAR{i}", "pin": "1"},
                ],
            }
        )
    graph = {"instances": instances, "nets": nets}
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    config = RealizeConfig(pitch_mm=0.3, router="tangent")
    # gap from U0 to its nearest OTHER instance (HUB) is 0.1mm -- floor(0.1/0.3) = 0 capacity
    result = realize(ir, config=config)
    assert result.warnings
    w = result.warnings[0]
    assert isinstance(w, CongestionWarning)
    assert w.usage > w.capacity
    assert "gap" in w.message()
    assert f"{w.usage}" in w.message()


def test_no_congestion_warning_when_gap_has_room():
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 50.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "SIG",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                ],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    result = realize(ir, config=_TANGENT)
    assert not result.warnings


# ── rip-up primitives ─────────────────────────────────────────────────────
def _two_net_ir():
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 0.0},
            {"refdes": "U2", "x": 0.0, "y": 10.0},
            {"refdes": "U3", "x": 10.0, "y": 10.0},
        ],
        "nets": [
            {
                "name": "A",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U0", "pin": "1"}, {"refdes": "U1", "pin": "1"}],
            },
            {
                "name": "B",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U2", "pin": "1"}, {"refdes": "U3", "pin": "1"}],
            },
        ],
    }
    return from_graph(graph, stackup=DEFAULT_STACKUP)


def test_rip_net_leaves_other_nets_geometry_untouched():
    ir = _two_net_ir()
    result = realize(ir, config=_TANGENT)
    assert {t.net_id for t in result.tracks} == {0, 1}
    other_track_before = next(t for t in result.tracks if t.net_id == 1)

    ripped = rip_net(ir, result, net_id=0)
    assert {t.net_id for t in ripped.tracks} == {1}
    other_track_after = ripped.tracks[0]
    assert other_track_after is other_track_before  # same object, not recomputed


def test_re_realize_segments_recomputes_only_named_segments():
    ir = _two_net_ir()
    result = realize(ir, config=_TANGENT)
    net_a_seg = next(t.seg_id for t in result.tracks if t.net_id == 0)
    net_b_track_before = next(t for t in result.tracks if t.net_id == 1)

    ir.move_instance(1, x=10.0, y=5.0)  # move U1 -- only net A's segment should change
    updated = re_realize_segments(ir, result, [net_a_seg])

    net_a_track_after = next(t for t in updated.tracks if t.net_id == 0)
    net_b_track_after = next(t for t in updated.tracks if t.net_id == 1)
    assert net_a_track_after.segments[0]["end"] == [10.0, 5.0]
    assert net_b_track_after is net_b_track_before  # untouched


def test_pin_topology_delegates_to_set_side():
    ir = _two_net_ir()
    assert int(ir.seg_side[0]) == 0
    pin_topology(ir, 0, 1)
    assert int(ir.seg_side[0]) == 1
    assert ir.dirty_l2[0]


# ── per-net resolved track width (gr-shaped: pcb-usb-c-pd-nano-testboard
# Gap A — a flat 0.25mm default is a fuse on a 5A rail) ───────────────────
def _current_net_graph(current_a: float, net_class: str = "power"):
    return {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "VBUS",
                "net_class": net_class,
                "domain": "electrical",
                "est_current_a": current_a,
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                ],
            }
        ],
    }


def test_realize_derives_track_width_from_current_on_outer_layer():
    """The exact regression named in the task: 5A must NOT come out at
    the old flat 0.25mm default -- it must land in the multi-mm range an
    outer-layer IPC-2221 trace actually needs."""
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)  # F.Cu -- an outer layer
    result = realize(ir, config=_TANGENT)
    track = result.tracks[0]
    expected = ipc2221_track_width_mm(5.0, layer_is_outer=True)
    assert track.width_mm == pytest.approx(expected, rel=1e-6)
    assert track.width_mm > 1.0  # nowhere near the 0.25mm fuse


def test_realize_inner_layer_gets_a_wider_track_than_outer_for_the_same_current():
    ir_outer = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir_outer.set_layer(0, 0)  # F.Cu
    outer_width = realize(ir_outer, config=_TANGENT).tracks[0].width_mm

    ir_inner = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir_inner.set_layer(0, 1)  # In1.Cu -- an internal layer
    inner_width = realize(ir_inner, config=_TANGENT).tracks[0].width_mm

    assert inner_width > outer_width


def test_realize_no_current_annotation_falls_back_to_fab_floor_width():
    """No current annotation -> "keep today's behaviour": the fab
    capability's house-default width, never an invented current."""
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "SIG",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                ],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    track = realize(ir, config=_TANGENT).tracks[0]
    cap = capability_for("4layer")
    assert track.width_mm == pytest.approx(cap.house_default["trace_width_mm"])


def test_realize_class_rule_override_beats_current_derivation():
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    config = RealizeConfig(
        class_rules={"power": {"track_width_mm": 0.8}}, router="tangent"
    )
    track = realize(ir, config=config).tracks[0]
    assert track.width_mm == pytest.approx(0.8)


def test_realize_fab_floor_clamps_a_too_small_class_override():
    ir = from_graph(
        _current_net_graph(0.0, net_class="signal"), stackup=DEFAULT_STACKUP
    )
    ir.set_layer(0, 0)
    config = RealizeConfig(
        class_rules={"signal": {"track_width_mm": 0.001}}, router="tangent"
    )
    track = realize(ir, config=config).tracks[0]
    cap = capability_for("4layer")
    jlc_min = cap.jlc_min["trace_width_mm"]
    assert jlc_min is not None
    assert track.width_mm == pytest.approx(jlc_min)


def test_to_gerber_model_uses_per_track_resolved_width_by_default():
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    result = realize(ir, config=_TANGENT)
    model = to_gerber_model(
        result,
        ir,
        layers=[layer["name"] for layer in DEFAULT_STACKUP],
        outline=[[0.0, -5.0], [20.0, -5.0], [20.0, 5.0], [0.0, 5.0]],
    )
    width = model["copper"][0]["width_mm"]
    assert width == pytest.approx(result.tracks[0].width_mm)
    assert width > 1.0


# ── via geometry: emitted at layer transitions, spanned + sized correctly
def _two_pin_graph():
    return {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "SIG",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                ],
            }
        ],
    }


def test_realize_no_via_when_track_stays_on_pad_layer():
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, PAD_LAYER)
    result = realize(ir, config=_TANGENT)
    assert result.vias == ()


def test_realize_no_via_when_layer_is_unassigned():
    """A segment with no L1 layer assignment yet (UNSET_LAYER) has nothing
    to transition -- must not emit a via."""
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    result = realize(ir, config=_TANGENT)
    assert result.vias == ()


def test_realize_no_via_for_a_dogbone_stub():
    """The plane-served dog-bone stub's via-to-plane connection is
    planes.py's job (module docstring) -- not this task's scope."""
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    ir.promote_plane(0, 1)  # In1.Cu
    result = realize(ir, config=_TANGENT)
    assert result.tracks[0].is_dogbone
    assert result.vias == ()


def test_realize_emits_a_via_at_a_layer_transition():
    """The exact gap this task closes: a segment realized on a
    non-pad layer must get vias at BOTH its endpoints."""
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)  # In1.Cu -- not PAD_LAYER
    result = realize(ir, config=_TANGENT)
    assert result.vias
    endpoints = {v.endpoint for v in result.vias}
    assert endpoints == {"a", "b"}
    for v in result.vias:
        assert v.net_id == 0
        assert v.seg_id == 0


def test_realize_via_span_covers_only_the_transitioned_layers():
    """Span correctness: a via between F.Cu (pad layer) and In1.Cu must
    NOT claim to reach In2.Cu/B.Cu -- a blind via must not appear on
    layers it doesn't actually span (the exact prior scalar-layer bug's
    consequence, restated as a span-boundary check)."""
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)  # In1.Cu
    result = realize(ir, config=_TANGENT)
    layers = [layer["name"] for layer in DEFAULT_STACKUP]
    model = to_gerber_model(
        result,
        ir,
        layers=layers,
        outline=[[0.0, -5.0], [20.0, -5.0], [20.0, 5.0], [0.0, 5.0]],
    )
    vias = [c for c in model["copper"] if c["ctype"] == "via"]
    assert vias
    for v in vias:
        assert "layer" not in v  # never a scalar layer -- span/layers only
        assert v["span"] == ["F.Cu", "In1.Cu"]
        assert "In2.Cu" not in v["span"] and "B.Cu" not in v["span"]


def test_realize_via_span_a_through_via_when_transitioning_to_the_far_outer_layer():
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 3)  # B.Cu -- the far outer layer, still != PAD_LAYER
    result = realize(ir, config=_TANGENT)
    assert result.vias
    for v in result.vias:
        assert v.layer_lo == 0
        assert v.layer_hi == 3


def test_realize_via_sized_via_the_shared_resolver():
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)
    config = RealizeConfig(fab_caps=capability_for("4layer"), router="tangent")
    result = realize(ir, config=config)
    assert result.vias
    expected_dia = config.fab_caps.house_default.get(
        "via_diameter_mm"
    ) or config.fab_caps.jlc_min.get("via_diameter_mm")
    for v in result.vias:
        assert v.dia_mm == pytest.approx(expected_dia)
        assert v.drill_mm is not None and v.drill_mm > 0


def test_realize_via_count_scales_with_net_current():
    """A single via cannot carry a real power rail -- the current-derived
    stitched-group requirement."""
    ir = from_graph(_current_net_graph(5.0, net_class="power"), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)  # force a layer transition so vias are emitted
    config = RealizeConfig(fab_caps=capability_for("4layer"), router="tangent")
    result = realize(ir, config=config)
    assert result.vias
    dia = result.vias[0].dia_mm
    expected_n = via_count_for_current(5.0, dia)
    assert expected_n > 1  # sanity: 5A genuinely needs more than one via
    per_endpoint = {"a": 0, "b": 0}
    for v in result.vias:
        per_endpoint[v.endpoint] += 1
    assert per_endpoint == {"a": expected_n, "b": expected_n}
    assert expected_n * via_capacity_a(dia) >= 5.0


def test_realize_via_count_defaults_to_one_with_no_current_annotation():
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)
    result = realize(ir, config=_TANGENT)
    per_endpoint = {"a": 0, "b": 0}
    for v in result.vias:
        per_endpoint[v.endpoint] += 1
    assert per_endpoint == {"a": 1, "b": 1}


def test_rip_net_removes_its_vias_too():
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)
    result = realize(ir, config=_TANGENT)
    assert result.vias
    ripped = rip_net(ir, result, net_id=0)
    assert ripped.vias == ()


def test_re_realize_segments_recomputes_vias_for_the_replaced_segment():
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)
    result = realize(ir, config=_TANGENT)
    assert result.vias
    ir.set_layer(0, PAD_LAYER)  # no longer a layer transition
    updated = re_realize_segments(ir, result, [0])
    assert updated.vias == ()


def test_to_gerber_model_explicit_override_still_wins_uniformly():
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    result = realize(ir, config=_TANGENT)
    model = to_gerber_model(
        result,
        ir,
        layers=[layer["name"] for layer in DEFAULT_STACKUP],
        outline=[[0.0, -5.0], [20.0, -5.0], [20.0, 5.0], [0.0, 5.0]],
        track_width_mm=0.33,
    )
    assert model["copper"][0]["width_mm"] == pytest.approx(0.33)


# ── pad SIZE: the single answer every consumer reads (gr263082 follow-on)
# ───────────────────────────────────────────────────────────────────────
def _multi_package_graph():
    """U0: a single-pin part (SINGLE family -> round pad). U1: an 8-pin
    part (DUAL family -> rectangular pad). Two different packages, so
    ``pads_for_ir`` must not hand them the same size — see
    ``PcbIR.pin_w``'s own docstring for the defect this closes (before it,
    every pad in the engine read one hardcoded 0.2mm radius)."""
    return {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 0.0},
        ],
        "nets": [{"name": "N0", "members": [{"refdes": "U0", "pin": "1"}]}]
        + [
            {"name": f"P{i}", "members": [{"refdes": "U1", "pin": str(i)}]}
            for i in range(8)
        ],
    }


def test_pads_for_ir_gives_different_packages_different_sizes():
    ir = from_graph(_multi_package_graph(), stackup=DEFAULT_STACKUP)
    pads = pads_for_ir(ir, [layer["name"] for layer in DEFAULT_STACKUP])
    by_net = {p["net"]: p for p in pads}
    single_pad, multi_pad = by_net["N0"], by_net["P0"]
    assert single_pad["shape"] == "circle"
    assert multi_pad["shape"] == "rect"
    assert (single_pad["w"], single_pad.get("h", single_pad["w"])) != (
        multi_pad["w"],
        multi_pad["h"],
    )
    assert all(p["synthesized"] for p in pads), "no real footprint was supplied"


def test_pads_for_ir_carries_refdes_and_pin_even_when_synthesized():
    """gr — gerber.py's X2 ``%TO.P,<refdes>,<pin>*%`` object attribute
    (the fab viewer's hover tooltip) fires only when a pad dict carries
    BOTH ``refdes`` and ``pin`` — the IR already resolves both for every
    pin, so a tooltip should never be forced to say only a net and a
    coordinate. Checked on U1's pin ``"3"`` specifically (not just any
    pad) to pin the VALUE, not merely the key's presence -- and on a
    SYNTHESIZED pad, since identity is a property of the pin, not of
    whether its geometry happens to be a real measurement or a bound."""
    ir = from_graph(_multi_package_graph(), stackup=DEFAULT_STACKUP)
    pads = pads_for_ir(ir, [layer["name"] for layer in DEFAULT_STACKUP])
    assert all(p["synthesized"] for p in pads), "no real footprint was supplied"
    by_net = {p["net"]: p for p in pads}
    u1_pin3 = by_net["P3"]
    assert u1_pin3["refdes"] == "U1"
    assert u1_pin3["pin"] == "3"
    u0_pin1 = by_net["N0"]
    assert u0_pin1["refdes"] == "U0"
    assert u0_pin1["pin"] == "1"


def test_pads_for_ir_prefers_a_real_footprint_pad_over_synthesis():
    """``footprints`` (a cached ``part_footprints`` row per refdes) must
    win over :mod:`precis.pcb.landpattern` synthesis for the pins it
    covers, and mark exactly those pins ``synthesized=False`` — a bound
    invented by this module must never be reported as a measurement, and a
    real measurement must never stay marked as a bound (module docstring's
    ``PcbIR.pin_pad_synthesized`` discipline)."""
    ir = from_graph(_multi_package_graph(), stackup=DEFAULT_STACKUP)
    footprints = {
        "U0": {
            "pads": [
                {
                    "number": "1",
                    "x": 0.0,
                    "y": 0.0,
                    "w": 3.3,
                    "h": 1.7,
                    "shape": "RECT",
                }
            ],
            "pin_map": {"1": {"name": "1"}},
        }
    }
    pads = pads_for_ir(ir, [layer["name"] for layer in DEFAULT_STACKUP], footprints)
    by_net = {p["net"]: p for p in pads}
    real_pad = by_net["N0"]
    assert real_pad["synthesized"] is False
    assert real_pad["w"] == pytest.approx(3.3)
    assert real_pad["h"] == pytest.approx(1.7)
    assert real_pad["shape"] == "rect"
    # U1 has no cached footprint in `footprints` -- still synthesized.
    assert by_net["P0"]["synthesized"] is True


def test_pad_geometry_real_override_survives_instance_rotation():
    """A real rect pad's w/h must SWAP when the instance sits at an
    effective 90-degree rotation — the same convention
    :mod:`precis.pcb.padplace` already applies for the fab-export path
    (:func:`_real_pad_sizes` reuses that function rather than re-deriving
    the swap here, precisely so the two paths cannot disagree)."""
    graph = _multi_package_graph()
    graph["instances"][0]["rot"] = 90.0
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    footprints = {
        "U0": {
            "pads": [
                {"number": "1", "x": 0.0, "y": 0.0, "w": 3.3, "h": 1.7, "shape": "RECT"}
            ],
            "pin_map": {"1": {"name": "1"}},
        }
    }
    geoms = pad_geometry(ir, footprints)
    u0_pin = next(
        p
        for p in range(ir.n_pins)
        if str(ir.instance_refdes[int(ir.pin_instance[p])]) == "U0"
    )
    geom = geoms[u0_pin]
    assert geom.synthesized is False
    assert geom.w_mm == pytest.approx(1.7)
    assert geom.h_mm == pytest.approx(3.3)


# ── item 2: a stitched via group must connect to its own trace ──────────


def _wall_graph(n_wall: int = 16) -> dict:
    """Two far-apart pins on a 5A net, separated by a picket fence of
    grounded (unconnected) pads tight enough that no VBUS trace fits
    between any two of them — the exact shape
    ``tests/workers/test_pcb_route.py``'s
    ``test_pcb_route_persists_realized_vias_at_a_layer_transition`` uses to
    force a layer transition. At 5A the ampacity-derived via count is > 1
    (``via_count_for_current``), so the stitched GROUP the docs/backlog/
    pcb-engine-plan.md defect needs actually forms — every group on the
    ESP32-C3 reference board is size 1, so it never fires there."""
    return {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "U2", "x": 24.0, "y": 0.0},
        ]
        + [{"refdes": f"W{i}", "x": 12.0, "y": -8.0 + i * 1.1} for i in range(n_wall)],
        "nets": [
            {
                "name": "VBUS",
                "net_class": "power",
                "est_current_a": 5.0,
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            },
        ],
        "unconnected": [{"refdes": f"W{i}", "pin": "1"} for i in range(n_wall)],
    }


def test_unconnected_pin_pad_is_not_invisible_to_the_router():
    """LATENT DEFECT, found while building the ``_wall_graph`` fixture
    above (an unconnected/NC "wall" pad that turned out not to block
    anything): ``precis.pcb.ir.NO_NET`` is ``-1``, the exact same value as
    ``precis.pcb.maze.FREE``. A router that stamps an unconnected pin's pad
    under ``net=NO_NET`` makes it numerically indistinguishable from empty
    board to every OTHER net's ``owner != FREE`` foreign-copper test — the
    pad is real copper (a test point, an NC pin, a mounting hole) that
    another net could then route straight through at zero clearance, with
    DRC never the wiser (the router's own "claim before you draw"
    guarantee is exactly what this breaks). A single obstacle pin, dead
    centre of the only straight line between two other pins, pins the
    fix directly rather than relying on ``_wall_graph``'s 16 pins to prove
    it incidentally."""
    graph = {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "U2", "x": 10.0, "y": 0.0},
            {"refdes": "NC1", "x": 5.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "SIG",
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            }
        ],
        "unconnected": [{"refdes": "NC1", "pin": "1"}],
    }
    ir = from_graph(graph, stackup=[{"name": "F.Cu", "role": "signal"}])
    result = realize(ir, config=RealizeConfig(router="maze"))
    assert result.tracks, "the router drew nothing -- this test would be vacuous"
    for t in result.tracks:
        for seg in t.segments:
            if seg["shape"] != "line":
                continue  # this fixture has no obstacle to hug -- lines only
            ax, ay = seg["start"]
            bx, by = seg["end"]
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            t_param = (
                max(0.0, min(1.0, ((5.0 - ax) * dx + (0.0 - ay) * dy) / length2))
                if length2 > 1e-12
                else 0.0
            )
            gap = math.hypot(5.0 - (ax + t_param * dx), 0.0 - (ay + t_param * dy))
            assert gap > 0.1, (
                "a track passed through the unconnected pin's pad "
                f"(gap={gap:.4f}mm): {seg}"
            )


def test_stitched_via_group_stays_connected_to_its_own_trace():
    """docs/backlog/pcb-engine-plan.md "a stitched via group is not
    connected to its own trace": for ``n_vias > 1`` the group used to
    spread along x through the transition point at ``via_dia + clearance``
    pitch, so no via ever sat AT the point the trace ended — the trace
    terminated in the ``clearance/2`` gap between two annuli, a real
    electrical break DRC's clearance rules cannot see (nothing was too
    close; something was simply not there). ``net_islands`` is exactly the
    "more than one connected component" check that catches it."""
    ir = from_graph(_wall_graph(), stackup=DEFAULT_STACKUP)
    result = realize(ir, config=RealizeConfig(router="maze"))
    assert result.tracks, "the router drew nothing -- this test would be vacuous"
    assert not result.unrouted, "the wall must not strand the connection outright"

    by_seg: dict[int, list] = {}
    for v in result.vias:
        by_seg.setdefault(v.seg_id, []).append(v)
    stitched = [group for group in by_seg.values() if len(group) > 1]
    assert stitched, (
        "VBUS at 5A must stitch more than one via at its layer transition -- "
        f"otherwise this test isn't exercising the defect at all: {result.vias}"
    )

    layers = [layer["name"] for layer in DEFAULT_STACKUP]
    model = to_gerber_model(result, ir, layers=layers, outline=[])
    islands = net_islands(model)
    assert not islands, [(isl.net, isl.components, isl.witnesses) for isl in islands]

    # The claim the fix leans on: the stitch bar must sit INSIDE the group
    # extent the search already cleared, so it introduces no new clearance
    # obligation. Checked for real, not assumed -- a clean geometric DRC
    # pass over the same model is the property that would fail if the bar
    # spilled outside what was cleared.
    findings = pcb_drc.run_geometric_drc(
        model, capability=capability_for("4layer"), unrouted=[]
    )
    errors = [f for f in findings if f.severity == "error"]
    assert not errors, [f.detail for f in errors]


# ── the board-edge inset must honour a VIA's own width, not a track's ────


def test_realize_maze_edge_inset_clears_a_via_wider_than_any_track():
    """gr — a via's copper footprint is its own diameter, not the
    thinnest track width on the board, and the shared occupancy-grid clip
    (:func:`precis.pcb.realize._outline_clip`, fed straight into
    :func:`precis.pcb.maze.grid_for`) is the ONE boundary both a track's
    centreline and a via's centre are routed against. Sizing that clip
    off ``max(track_width_mm)`` alone (the historical bug) let a via
    search place a via's centre as close as ``edge_min +
    track_width/2`` from the true board edge — legal for a track that
    thin, short of the ``via_radius + edge_min`` a via that wide actually
    needs.

    The fixture: one net (class ``"bigvia"``) whose ``via_dia_mm`` is
    forced (via ``RealizeConfig.class_rules``) to exceed its own
    ``track_width_mm`` by more than ``2 * house_edge_min`` — the task's
    own precondition for "this fixture actually stresses the fix", pinned
    below rather than assumed. U1/U2 sit just inside the FIXED clip
    (``edge_inset``, computed here from the same
    :func:`~precis.pcb.rules.resolve_net_rules` inputs
    :func:`_realize_maze` itself resolves — tied to the formula, not to a
    hardcoded coordinate), and a dense picket wall of foreign-net pads —
    real copper only on ``PAD_LAYER`` (an SMD assumption), so it blocks
    every in-plane crossing AND every via candidate near it (``via_ok``
    ORs the foreign mask across every layer) while leaving B.Cu
    completely open — forces a mandatory layer-transition via right next
    to that wall, at essentially the SAME tight y both pads already sit
    at. If the clip regressed to a track-only inset, this exact via would
    land inside ``[house_edge_min, edge_inset)`` of the true edge: legal
    ground for a track, short for this via by construction.
    """
    from precis.pcb.rules import resolve_net_rules

    cap = capability_for("4layer")
    via_override = {"via_dia_mm": 1.5}
    config = RealizeConfig(
        router="maze", fab_caps=cap, class_rules={"bigvia": via_override}
    )
    rules = resolve_net_rules(
        "bigvia", layer_is_outer=True, fab_caps=cap, overrides=via_override
    )
    edge_min = cap.house_default.get("board_edge_clearance_vcut_mm") or cap.jlc_min.get(
        "board_edge_clearance_vcut_mm"
    )
    assert edge_min is not None
    # The task's own precondition: this via must be wide enough, relative
    # to the track it's compared against, that a track-sized inset would
    # be a CLEAR (not epsilon-scale) shortfall for it.
    assert rules.via_dia_mm is not None
    assert rules.via_dia_mm - rules.track_width_mm > 2 * edge_min

    clearance = max(config.clearance_mm, rules.clearance_mm)
    edge_inset = (
        max(clearance, edge_min) + max(rules.track_width_mm, rules.via_dia_mm) / 2.0
    )
    edge_inset_track_only = max(clearance, edge_min) + rules.track_width_mm / 2.0
    assert edge_inset > edge_inset_track_only  # the fix must widen it here

    margin = 0.2  # comfortably inside the FIXED clip -- a real placement,
    # not one that forces the grid to extend past its own bounds (see
    # _outline_clip's "never below the pads themselves" carve-out, a
    # separate and already-acknowledged limitation this test is not
    # about).
    y0 = edge_inset + margin
    board_h = edge_inset * 2.0 + 2.0
    board_w = 24.0
    wall_x = board_w / 2.0

    wall_ys = _frange(-1.0, board_h + 1.0, 0.25)
    graph = {
        "instances": [
            {"refdes": "U1", "x": 4.0, "y": y0},
            {"refdes": "U2", "x": board_w - 4.0, "y": y0},
        ]
        + [{"refdes": f"W{i}", "x": wall_x, "y": y} for i, y in enumerate(wall_ys)],
        "nets": [
            {
                "name": "BIGVIA",
                "net_class": "bigvia",
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            }
        ],
        "unconnected": [{"refdes": f"W{i}", "pin": "1"} for i in range(len(wall_ys))],
    }
    outline = [[0.0, 0.0], [board_w, 0.0], [board_w, board_h], [0.0, board_h]]
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=outline)

    result = realize(ir, config=config)
    assert not result.unrouted, [r.message for r in result.unrouted_reasons]
    assert result.vias, (
        "the wall must force a layer-transition via -- otherwise this "
        "fixture exercises nothing"
    )

    via_radius = rules.via_dia_mm / 2.0
    required = via_radius + edge_min
    for v in result.vias:
        dist_to_edge = min(v.x, board_w - v.x, v.y, board_h - v.y)
        # This is the real, physical requirement -- independent of how
        # _realize_maze happens to compute its own inset.
        assert dist_to_edge >= required - 1e-9, (
            "a via landed closer to the board edge than its own diameter "
            f"allows: dist={dist_to_edge:.4f}mm required={required:.4f}mm",
            v,
        )
        # And this fixture must actually be landing the via near the tight
        # edge -- not somewhere far off where the fix would be untested.
        assert dist_to_edge < edge_inset + 1.0, (
            "via landed suspiciously far from the tight edge -- this "
            "fixture no longer stresses the boundary",
            v,
            dist_to_edge,
        )

    layers = [layer["name"] for layer in DEFAULT_STACKUP]
    model = to_gerber_model(result, ir, layers=layers, outline=outline)
    findings = pcb_drc.check_board_edge_clearance(model, cap, outline=outline)
    # Scoped to the VIA specifically, per this test's own remit: the wall
    # is a stack of synthetic, deliberately edge-hugging "unconnected"
    # pins whose only job is to force the layer-change topology above --
    # they are not real manufacturable copper, and check_board_edge_
    # clearance now (correctly, a separate fix) also grades PADS by the
    # same rule, which is a different question from the one this test
    # answers.
    via_findings = [f for f in findings if f.objects[0].get("ctype") == "via"]
    assert not via_findings, [f.detail for f in via_findings]


# ── item 1: via shoving in the straighten pass ───────────────────────────


def test_shove_vias_on_by_default():
    """Measured to cost nothing on the ESP32-C3 reference fixture (seeds
    1-5: DRC and routed count identical with/without, segments/copper
    never up on any seed) — see :attr:`RealizeConfig.shove_vias`'s own
    docstring for the table. Unlike ``preferred_directions``, there is no
    correctness/routability trade to weigh here, so it defaults on."""
    assert RealizeConfig().shove_vias is True


def test_shove_vias_never_increases_segment_count_or_breaks_connectivity():
    """Shoving a via can only remove copper or leave it unchanged -- never
    add a bend, never change which segments routed, and never touch
    connectivity. Measured on the same (deterministic -- the maze search
    and straighten pass have no randomness of their own) IR with and
    without the flag."""
    ir = from_graph(_wall_graph(), stackup=DEFAULT_STACKUP)
    layers = [layer["name"] for layer in DEFAULT_STACKUP]

    baseline = realize(ir, config=RealizeConfig(router="maze", shove_vias=False))
    shoved = realize(ir, config=RealizeConfig(router="maze", shove_vias=True))

    assert baseline.unrouted == shoved.unrouted, (
        "shoving must never change which connections route"
    )
    assert len(shoved.vias) == len(baseline.vias), (
        "shoving moves a via, it does not add or remove one"
    )
    n_seg_baseline = sum(len(t.segments) for t in baseline.tracks)
    n_seg_shoved = sum(len(t.segments) for t in shoved.tracks)
    assert n_seg_shoved <= n_seg_baseline, (
        f"shoving produced MORE segments ({n_seg_shoved} > {n_seg_baseline}) -- "
        "it must only ever remove copper"
    )

    model = to_gerber_model(shoved, ir, layers=layers, outline=[])
    islands = net_islands(model)
    assert not islands, [(isl.net, isl.components, isl.witnesses) for isl in islands]
    findings = pcb_drc.run_geometric_drc(
        model, capability=capability_for("4layer"), unrouted=[]
    )
    errors = [f for f in findings if f.severity == "error"]
    assert not errors, [f.detail for f in errors]


# ── any-angle shortcutting: _collapse_straight ────────────────────────────


def _octile_staircase(
    start: tuple[float, float], end: tuple[float, float], layer: int = 0
) -> list[tuple[float, float, int]]:
    """A unit-pitch octile-only path (0/45/90/135-degree steps) from
    ``start`` to ``end`` — the exact shape an A* search on this grid can
    emit, approximating whatever angle ``end - start`` actually is."""
    pts: list[tuple[float, float, int]] = [(start[0], start[1], layer)]
    x, y = start
    tx, ty = end
    while (x, y) != (tx, ty):
        dx = 1 if tx > x else (-1 if tx < x else 0)
        dy = 1 if ty > y else (-1 if ty < y else 0)
        if dx and dy and abs(tx - x) > 0.5 and abs(ty - y) > 0.5:
            x, y = x + dx, y + dy
        elif dx:
            x += dx
        else:
            y += dy
        pts.append((x, y, layer))
    return pts


def test_collapse_straight_reduces_an_octile_staircase_to_the_true_diagonal():
    """An 11-vertex octile staircase from (0,0) to (10,3) approximates a
    16.7-degree line — an angle no single A* step on this grid can draw.
    In free space the taut-string answer is the two endpoints and nothing
    else, so :func:`_collapse_straight` must find exactly that rather than
    a 45-degree-limited partial collapse — this is the empirical proof
    behind :func:`_collapse_straight`'s own docstring claim."""
    from precis.pcb import maze as pcb_maze
    from precis.pcb import realize as pcb_realize

    pts = _octile_staircase((0.0, 0.0), (10.0, 3.0))
    assert len(pts) == 11  # a genuine staircase, not already a straight run

    spec = pcb_maze.grid_for(
        [(0.0, 0.0), (10.0, 3.0)], n_layers=1, bounds=(-5.0, -5.0, 20.0, 20.0)
    )
    grid = pcb_maze.OccupancyGrid(spec, clearance_mm=0.1)
    step = spec.pitch / 2.0
    out = pcb_realize._collapse_straight(pts, grid, net_id=0, radius=0.1, step=step)
    assert out == [(0.0, 0.0, 0), (10.0, 3.0, 0)]


def test_collapse_straight_stops_exactly_at_a_real_blocking_obstacle():
    """The same staircase, with one foreign-net obstacle sitting on the
    direct chord: the collapse must still find the longest legal skip on
    each side of the obstacle, not fall back to every original vertex."""
    from precis.pcb import maze as pcb_maze
    from precis.pcb import realize as pcb_realize

    pts = _octile_staircase((0.0, 0.0), (10.0, 3.0))
    spec = pcb_maze.grid_for(
        [(0.0, 0.0), (10.0, 3.0)], n_layers=1, bounds=(-5.0, -5.0, 20.0, 20.0)
    )
    grid = pcb_maze.OccupancyGrid(spec, clearance_mm=0.1)
    grid.stamp_disk((0,), 5.0, 1.5, 0.6, net_id=999)  # foreign copper on the chord
    step = spec.pitch / 2.0
    out = pcb_realize._collapse_straight(pts, grid, net_id=0, radius=0.1, step=step)
    assert out == [(0.0, 0.0, 0), (6.0, 3.0, 0), (10.0, 3.0, 0)]
    # every emitted chord must itself test free -- the guarantee this pass
    # exists to preserve, not merely assumed of its own output.
    for a, b in itertools.pairwise(out):
        assert pcb_realize._chord_is_free(grid, a, b, 0, 0.1, step)


def test_collapse_straight_never_lengthens_a_path():
    """Every collapse drops a vertex only when its surrounding chord tests
    free — by the triangle inequality that can only shorten or preserve
    total length, never grow it. Checked over randomized octile-ish
    staircases against randomized obstacle layouts, so most (not all)
    chords are free and the collapse is doing real, partial work."""
    from precis.pcb import maze as pcb_maze
    from precis.pcb import realize as pcb_realize
    from precis.pcb.geom import dist

    rng = random.Random(7)
    for _trial in range(20):
        pts: list[tuple[float, float, int]] = [(0.0, 0.0, 0)]
        x, y = 0.0, 0.0
        for _ in range(rng.randint(5, 15)):
            dx, dy = rng.choice([-1, 0, 1]), rng.choice([-1, 0, 1])
            if dx == 0 and dy == 0:
                continue
            x, y = x + dx, y + dy
            pts.append((x, y, 0))
        if len(pts) < 3:
            continue
        spec = pcb_maze.grid_for(
            [(p[0], p[1]) for p in pts], n_layers=1, bounds=(-20.0, -20.0, 20.0, 20.0)
        )
        grid = pcb_maze.OccupancyGrid(spec, clearance_mm=0.1)
        for _ in range(rng.randint(0, 4)):
            grid.stamp_disk(
                (0,), rng.uniform(-10, 10), rng.uniform(-10, 10), 0.4, net_id=999
            )
        step = spec.pitch / 2.0
        before = sum(
            dist((a[0], a[1]), (b[0], b[1])) for a, b in itertools.pairwise(pts)
        )
        out = pcb_realize._collapse_straight(
            list(pts), grid, net_id=0, radius=0.1, step=step
        )
        after = sum(
            dist((a[0], a[1]), (b[0], b[1])) for a, b in itertools.pairwise(out)
        )
        assert after <= before + 1e-9, (before, after, pts, out)


def test_collapse_straight_never_crosses_a_layer_change():
    """A via -- a same-(x, y), different-layer consecutive pair -- must
    survive untouched: shortcutting across it would draw copper straight
    through a hole the path never actually plates on the way it takes."""
    from precis.pcb import maze as pcb_maze
    from precis.pcb import realize as pcb_realize

    pts = [
        (0.0, 0.0, 0),
        (1.0, 0.0, 0),
        (2.0, 0.0, 0),
        (2.0, 0.0, 1),  # the via transition -- same (x, y), other layer
        (3.0, 0.0, 1),
        (4.0, 0.0, 1),
    ]
    spec = pcb_maze.grid_for([(p[0], p[1]) for p in pts], n_layers=2)
    grid = pcb_maze.OccupancyGrid(spec, clearance_mm=0.1)
    step = spec.pitch / 2.0
    out = pcb_realize._collapse_straight(pts, grid, net_id=0, radius=0.1, step=step)
    assert out == [(0.0, 0.0, 0), (2.0, 0.0, 0), (2.0, 0.0, 1), (4.0, 0.0, 1)]


# ── item 3: a failed route must say why ──────────────────────────────────


def _frange(a: float, b: float, step: float) -> list[float]:
    n = round((b - a) / step)
    return [a + i * step for i in range(n + 1)]


def _boxed_scenario(*, gap_mm: float | None) -> tuple:
    """U2 sits at the centre of a closed box of tightly-pitched pads on an
    unrelated net (999, via the low-level ``pads`` tuple form
    :func:`precis.pcb.realize._stamp_pads` reads); U1 sits outside it.
    ``gap_mm=None`` seals every side solidly (U2 is topologically
    unreachable); a float leaves a controlled notch of that width in the
    east wall (U2 is reachable only through it, at any width up to the
    notch's own clear opening).

    Returns everything :func:`precis.pcb.realize._diagnose_unrouted` needs,
    called directly (module-private -- this is the low-level counterpart
    to ``tests/test_pcb_maze.py``'s own direct ``OccupancyGrid``/``route``
    tests, chosen here because it is the only way to control the exact
    corridor width a `'width'` vs. `'congestion'` diagnosis turns on)."""
    from precis.pcb import maze as pcb_maze
    from precis.pcb import realize as pcb_realize
    from precis.pcb.rules import NetRules

    cx, cy, half, pitch, radius = 5.0, 5.0, 2.0, 0.15, 0.5
    wall_net = 999
    pads: list = []
    for y in _frange(cy - half, cy + half, pitch):
        pads.append(((cx - half, y), wall_net, radius))
        if gap_mm is None or abs(y - cy) > gap_mm / 2.0:
            pads.append(((cx + half, y), wall_net, radius))
    for x in _frange(cx - half, cx + half, pitch):
        pads.append(((x, cy - half), wall_net, radius))
        pads.append(((x, cy + half), wall_net, radius))

    graph = {
        "instances": [
            {"refdes": "U1", "x": cx + half + 3.0, "y": cy},
            {"refdes": "U2", "x": cx, "y": cy},
        ],
        "nets": [
            {
                "name": "N1",
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            }
        ],
    }
    ir = from_graph(graph, stackup=[{"name": "F.Cu", "role": "signal"}])
    all_points = [p for p, _, _ in pads] + [
        (cx + half + 3.0, cy),
        (cx, cy),
    ]
    spec = pcb_maze.grid_for(all_points, n_layers=1)
    return pcb_realize, ir, 0, spec, pads, 0.15, [0], NetRules


def test_diagnose_unrouted_reason_no_path_when_the_endpoint_is_walled_in():
    pcb_realize, ir, seg_id, spec, pads, clearance, signal_layers, NetRules = (
        _boxed_scenario(gap_mm=None)
    )
    reason = pcb_realize._diagnose_unrouted(
        ir,
        seg_id,
        spec,
        pads,
        clearance,
        signal_layers,
        NetRules(track_width_mm=0.2, clearance_mm=clearance),
        n_vias=1,
        group_extent=None,
        max_expansions=80_000,
    )
    assert reason.kind == "no_path", reason.message


def test_diagnose_unrouted_reason_congestion_when_a_corridor_exists_in_isolation():
    pcb_realize, ir, seg_id, spec, pads, clearance, signal_layers, NetRules = (
        _boxed_scenario(gap_mm=3.0)
    )
    reason = pcb_realize._diagnose_unrouted(
        ir,
        seg_id,
        spec,
        pads,
        clearance,
        signal_layers,
        NetRules(track_width_mm=0.2, clearance_mm=clearance),
        n_vias=1,
        group_extent=None,
        max_expansions=80_000,
    )
    assert reason.kind == "congestion", reason.message


def test_diagnose_unrouted_reason_width_when_the_required_trace_does_not_fit():
    pcb_realize, ir, seg_id, spec, pads, clearance, signal_layers, NetRules = (
        _boxed_scenario(gap_mm=3.0)
    )
    reason = pcb_realize._diagnose_unrouted(
        ir,
        seg_id,
        spec,
        pads,
        clearance,
        signal_layers,
        NetRules(track_width_mm=5.0, clearance_mm=clearance),
        n_vias=1,
        group_extent=None,
        max_expansions=80_000,
    )
    assert reason.kind == "width", reason.message


def test_unrouted_reason_unpourable_plane_names_its_own_cause():
    """A plane-promoted net with no board outline used to fall into the
    generic ``unrouted`` bucket with no distinguishing cause at all
    (docs/backlog/pcb-engine-plan.md "BOARD TWO" finding 2's sibling
    note). It must now name itself, not just "unrouted"."""
    graph = {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "U2", "x": 5.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.promote_plane(0, 1)  # In1.Cu -- but no outline is authored below
    result = realize(ir, config=RealizeConfig(router="maze"))
    assert result.unrouted, "a plane with nowhere to pour must be reported unrouted"
    reasons = {r.seg_id: r for r in result.unrouted_reasons}
    assert reasons, "every unrouted segment must carry a reason"
    assert all(r.kind == "unpourable_plane" for r in reasons.values()), [
        r.kind for r in reasons.values()
    ]


# ── _track_from_run's fillet budget ───────────────────────────────────────
# A filleted track must stay inside the copper envelope of the straight
# track it replaces, or the router's clearance proof stops covering it. That
# budget had NO test at all until a review found the enforcement was a
# no-op at setback-clamped corners; these cover the enforcement itself, at
# the level that actually ships copper.
def _arc_points(seg: dict[str, Any], n: int = 64) -> list[tuple[float, float]]:
    cx, cy = seg["center"]
    sx, sy = seg["start"]
    ex, ey = seg["end"]
    r = math.hypot(sx - cx, sy - cy)
    a1 = math.atan2(sy - cy, sx - cx)
    a2 = math.atan2(ey - cy, ex - cx)
    sweep = (a2 - a1) % (2 * math.pi)
    if seg["cw"]:
        sweep -= 2 * math.pi
    return [
        (cx + r * math.cos(a1 + sweep * i / n), cy + r * math.sin(a1 + sweep * i / n))
        for i in range(n + 1)
    ]


def _gap_to_path(p: tuple[float, float], pts: list[tuple[float, float]]) -> float:
    best = math.inf
    for a, b in itertools.pairwise(pts):
        vx, vy = b[0] - a[0], b[1] - a[1]
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2
        t = max(0.0, min(1.0, t))
        best = min(best, math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vy)))
    return best


def _sharp_corner_run(
    interior_deg: float, leg: float
) -> list[tuple[float, float]]:
    """A run whose interior angle at the middle vertex is `interior_deg`.
    Deliberately NOT 90/135 degrees: pure grid paths cannot reach the
    clamped regime, but `_straighten`/`_shove_vias`/`_snap_to_pads` all
    emit off-grid angles, so that is the regime that ships."""
    out = math.pi - math.radians(interior_deg)
    return [(-leg, 0.0), (0.0, 0.0), (leg * math.cos(out), leg * math.sin(out))]


@pytest.mark.parametrize("interior_deg", [30.0, 45.0, 60.0, 75.0])
def test_track_from_run_fillet_stays_inside_the_straight_track_envelope(
    interior_deg: float,
):
    """The invariant: no filleted arc point may sit further from the
    original mitered centreline than half the track width. Measured off the
    emitted segments -- NOT by re-running geom's own deviation formula,
    which is the producer's math and would check nothing."""
    width = 0.25
    run = _sharp_corner_run(interior_deg, 4.0 * width)
    tracks = _track_from_run(
        1, 1, "F.Cu", list(run), width, fillet_radius_mm=1.5 * width
    )
    assert len(tracks) == 1
    arcs = [s for s in tracks[0].segments if s.get("shape") == "arc"]
    assert arcs, "fixture must actually produce a fillet"
    worst = max(_gap_to_path(p, run) for s in arcs for p in _arc_points(s))
    assert worst <= width / 2.0 + 1e-9, (
        f"{interior_deg}deg corner: fillet bulges {worst:.6f}mm past the "
        f"mitered path, budget is {width / 2.0:.6f}mm"
    )


def test_track_from_run_still_fillets_when_the_budget_is_not_binding():
    """Failure direction for the test above: a cap that collapsed the
    radius to ~0 would satisfy every budget assertion while silently
    disabling the feature. A gentle, long-legged corner must still round."""
    width = 0.25
    run = _sharp_corner_run(150.0, 20.0)
    tracks = _track_from_run(
        1, 1, "F.Cu", list(run), width, fillet_radius_mm=1.5 * width
    )
    arcs = [s for s in tracks[0].segments if s.get("shape") == "arc"]
    assert len(arcs) == 1
    radius = math.hypot(
        arcs[0]["start"][0] - arcs[0]["center"][0],
        arcs[0]["start"][1] - arcs[0]["center"][1],
    )
    assert radius == pytest.approx(1.5 * width, rel=1e-9), (
        "an unconstrained corner must keep the full requested radius"
    )
