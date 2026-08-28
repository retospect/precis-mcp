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

import math
import random

import pytest

from precis.pcb import DEFAULT_STACKUP, gerber
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import from_graph
from precis.pcb.realize import (
    CongestionWarning,
    Obstacle,
    RealizeConfig,
    pin_topology,
    re_realize_segments,
    realize,
    rip_net,
    tangent_arc_path,
    to_gerber_model,
)
from precis.pcb.rules import ipc2221_track_width_mm

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
    result = realize(ir, obstacles=obstacles, config=RealizeConfig(clearance_mm=0.1))
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
    result = realize(ir)
    track = result.tracks[0]
    assert track.is_dogbone
    assert track.length_mm <= RealizeConfig().dogbone_stub_mm + 1e-9
    assert track.length_mm < 20.0  # nowhere near the far end -- a stub, not a route


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
    config = RealizeConfig(pitch_mm=0.3)
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
    result = realize(ir)
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
    result = realize(ir)
    assert {t.net_id for t in result.tracks} == {0, 1}
    other_track_before = next(t for t in result.tracks if t.net_id == 1)

    ripped = rip_net(ir, result, net_id=0)
    assert {t.net_id for t in ripped.tracks} == {1}
    other_track_after = ripped.tracks[0]
    assert other_track_after is other_track_before  # same object, not recomputed


def test_re_realize_segments_recomputes_only_named_segments():
    ir = _two_net_ir()
    result = realize(ir)
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
    result = realize(ir)
    track = result.tracks[0]
    expected = ipc2221_track_width_mm(5.0, layer_is_outer=True)
    assert track.width_mm == pytest.approx(expected, rel=1e-6)
    assert track.width_mm > 1.0  # nowhere near the 0.25mm fuse


def test_realize_inner_layer_gets_a_wider_track_than_outer_for_the_same_current():
    ir_outer = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir_outer.set_layer(0, 0)  # F.Cu
    outer_width = realize(ir_outer).tracks[0].width_mm

    ir_inner = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir_inner.set_layer(0, 1)  # In1.Cu -- an internal layer
    inner_width = realize(ir_inner).tracks[0].width_mm

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
    track = realize(ir).tracks[0]
    cap = capability_for("4layer")
    assert track.width_mm == pytest.approx(cap.house_default["trace_width_mm"])


def test_realize_class_rule_override_beats_current_derivation():
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    config = RealizeConfig(class_rules={"power": {"track_width_mm": 0.8}})
    track = realize(ir, config=config).tracks[0]
    assert track.width_mm == pytest.approx(0.8)


def test_realize_fab_floor_clamps_a_too_small_class_override():
    ir = from_graph(
        _current_net_graph(0.0, net_class="signal"), stackup=DEFAULT_STACKUP
    )
    ir.set_layer(0, 0)
    config = RealizeConfig(class_rules={"signal": {"track_width_mm": 0.001}})
    track = realize(ir, config=config).tracks[0]
    cap = capability_for("4layer")
    jlc_min = cap.jlc_min["trace_width_mm"]
    assert jlc_min is not None
    assert track.width_mm == pytest.approx(jlc_min)


def test_to_gerber_model_uses_per_track_resolved_width_by_default():
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    result = realize(ir)
    model = to_gerber_model(
        result,
        ir,
        layers=[layer["name"] for layer in DEFAULT_STACKUP],
        outline=[[0.0, -5.0], [20.0, -5.0], [20.0, 5.0], [0.0, 5.0]],
    )
    width = model["copper"][0]["width_mm"]
    assert width == pytest.approx(result.tracks[0].width_mm)
    assert width > 1.0


def test_to_gerber_model_explicit_override_still_wins_uniformly():
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    result = realize(ir)
    model = to_gerber_model(
        result,
        ir,
        layers=[layer["name"] for layer in DEFAULT_STACKUP],
        outline=[[0.0, -5.0], [20.0, -5.0], [20.0, 5.0], [0.0, 5.0]],
        track_width_mm=0.33,
    )
    assert model["copper"][0]["width_mm"] == pytest.approx(0.33)
