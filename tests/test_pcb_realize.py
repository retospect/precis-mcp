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
from precis.pcb import drc as pcb_drc
from precis.pcb.capabilities import capability_for
from precis.pcb.connectivity import net_islands
from precis.pcb.ir import from_graph
from precis.pcb.realize import (
    PAD_LAYER,
    CongestionWarning,
    Obstacle,
    RealizeConfig,
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
