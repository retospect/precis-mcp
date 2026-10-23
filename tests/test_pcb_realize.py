"""Unit + property tests for precis.pcb.realize — sketch -> copper
geometry. No DB.

Covers: the realize pipeline on the vector router
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
from typing import Any

import pytest
from shapely.geometry import Point

from precis.pcb import DEFAULT_STACKUP, padplace
from precis.pcb import drc as pcb_drc
from precis.pcb.capabilities import capability_for
from precis.pcb.connectivity import net_islands
from precis.pcb.ir import from_graph, pin_point
from precis.pcb.planes import point_in_pour
from precis.pcb.realize import (
    PAD_LAYER,
    CongestionWarning,
    RealizeConfig,
    pad_geometry,
    pads_for_ir,
    pin_topology,
    realize,
    rip_net,
    to_gerber_model,
)
from precis.pcb.rules import (
    ipc2221_track_width_mm,
)
from tests._pcb_outline import closed_outline

#: The one router — 'vector' — is the default; tests ride it.
_VECTOR = RealizeConfig(router="vector")


# ── gerber.py round-trip: does the model shape actually match? ──────────


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
    # GND pours ON the pad layer itself and the pour floods over both GND
    # pads, so `_prune_redundant_drop_vias` (2026-09-01) removes every
    # dog-bone stub+via as redundant — the pour IS the connection. Zero
    # GND vias is the correct outcome here, not a missing fanout.
    gnd_vias = [v for v in result.vias if v.net_id == 0]
    assert gnd_vias == [], (
        "a pad covered by its own net's same-layer pour needs no drop via"
    )

    model = to_gerber_model(
        result,
        ir,
        layers=[layer["name"] for layer in DEFAULT_STACKUP],
        outline=outline,
    )

    # merge: GND's pads must be ONE connected island with the pour (that
    # is what justified pruning the vias) -- net_islands only reports a
    # net split across >1 fragment, so GND's absence here IS the merge
    # assertion.
    islands = {i.net for i in net_islands(model)}
    assert "GND" not in islands, net_islands(model)

    # antipad: GND's own pad (the connection) must sit INSIDE the pour;
    # SIG's foreign-net trace must sit OUTSIDE it (in the hole cut around
    # it) -- both read straight off the pour geometry plane_pours itself
    # produced, the same predicate net_islands uses for pour membership.
    pour = result.pours[0]
    assert pour["net"] == "GND" and pour["layer"] == "F.Cu"
    assert point_in_pour(pour, 5.0, 15.0), (
        "GND's own pad must merge into its pour (G1's centre)"
    )
    sx, sy = sig_tracks[0].segments[0]["start"]
    assert not point_in_pour(pour, sx, sy), (
        "SIG's foreign-net trace must be antipadded out of the GND pour"
    )


def test_plane_drop_via_keeps_out_of_a_neighbour_signal_pads_escape_lane():
    """gr503666: the fan-out ran before any signal routed and its sweep
    took the first legal site around the pin — on esp32c3 seed 3 that put
    the 3V3 drop via 1.04 mm from U1.SDA, on SDA's only perpendicular exit
    out of a 0.65 mm row, and SDA ``exhausted``. Here the GND pin is the
    second of an 8-pin dual-row column, the pin above it unconnected and
    the two below it signals, with a sliver of fixed copper straight out
    from it and a foreign track down the part's middle: the straight
    site and the reversal are illegal, the one-step diagonals too close
    to the neighbour pads, and the sweep's first legal site is the
    two-step diagonal DOWN — stub across the signal pad's exit and via
    0.34 mm from its lane. The mirror diagonal UP is just as legal and
    crosses nothing but the unconnected pad's side; the drop must take it
    (:func:`realize._escape_lanes`)."""
    from precis.pcb import realize as pcb_realize

    pins = [("U1", str(n)) for n in range(1, 9)]
    graph = {
        "instances": [
            {"refdes": "U1", "x": 10.0, "y": 10.0, "rot": 0.0},
            {"refdes": "B2", "x": 3.0, "y": 11.0},
            {"refdes": "C2", "x": 3.0, "y": 4.0},
            {"refdes": "G2", "x": 30.0, "y": 10.0},
        ],
        "pin_order": pins,
        "nets": [
            {
                "name": "GND",
                "members": [{"refdes": "U1", "pin": "2"}, {"refdes": "G2", "pin": "1"}],
            },
            {
                "name": "SIG_B",
                "members": [{"refdes": "U1", "pin": "3"}, {"refdes": "B2", "pin": "1"}],
            },
            {
                "name": "SIG_C",
                "members": [{"refdes": "U1", "pin": "4"}, {"refdes": "C2", "pin": "1"}],
            },
        ],
        "unconnected": [{"refdes": "U1", "pin": str(n)} for n in (1, 5, 6, 7, 8)],
    }
    outline = [[0.0, 0.0], [40.0, 0.0], [40.0, 20.0], [0.0, 20.0]]
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=outline)
    gnd = list(ir.net_name).index("GND")
    ir.promote_plane(gnd, 3)  # GND on B.Cu: every GND pad needs a drop via
    gnd_pin = next(
        pid
        for pid in range(ir.n_pins)
        if int(ir.pin_net[pid]) == gnd
        and str(ir.instance_refdes[ir.pin_instance[pid]]) == "U1"
    )
    gnd_point = pin_point(ir, gnd_pin)
    assert gnd_point is not None
    gx, gy = gnd_point
    # A sliver of foreign copper straight out (-x) from the GND pad and a
    # track down the part's middle: the perpendicular site and the
    # between-the-columns reversal are illegal.
    sliver = {
        "ctype": "track",
        "net": "X",
        "layer": "F.Cu",
        "width_mm": 0.1,
        "segments": [
            {
                "shape": "line",
                "start": [gx - 1.25, gy - 0.33],
                "end": [gx - 1.25, gy + 0.33],
            }
        ],
    }
    spine = {
        **sliver,
        "segments": [{"shape": "line", "start": [10.0, 8.5], "end": [10.0, 11.5]}],
    }
    config = RealizeConfig(fab_caps=capability_for("4layer"))
    result = realize(ir, config=config, fixed_copper=[sliver, spine])
    drops = [v for v in result.vias if v.net_id == gnd and abs(v.x - gx) < 3.0]
    assert len(drops) == 1, [(v.net_id, v.x, v.y) for v in result.vias]
    via = drops[0]
    rules = pcb_realize._resolve_track_rules(ir, gnd, PAD_LAYER, config)
    assert rules.via_dia_mm is not None
    lanes = pcb_realize._escape_lanes(
        ir,
        pad_geometry(ir, None),
        {gnd},
        {
            n: pcb_realize._resolve_track_rules(ir, n, PAD_LAYER, config)
            for n in range(ir.n_nets)
        },
        lane_len_mm=2.0,
        outer_layers=range(len(ir.stackup)),
    )
    u1_lanes = [lane for lane in lanes if abs(lane[0][0] - gx) < 1e-6]
    assert len(u1_lanes) == 2, lanes  # pins 3 and 4 — the two signal pads
    assert all(b[0] < a[0] for a, b, *_rest in u1_lanes), "lanes run outward (-x)"
    assert {lane[4] for lane in u1_lanes} == {
        list(ir.net_name).index("SIG_B"),
        list(ir.net_name).index("SIG_C"),
    }, "each lane names its pad's net"
    assert via.y > gy, (
        f"the drop must take the lane-free diagonal up, not ({via.x}, {via.y})"
    )
    clearance = config.clearance_mm
    for a, b, half, _layer, _net in u1_lanes:
        gap = pcb_realize._seg_point_dist((via.x, via.y), a, b)
        assert gap >= rules.via_dia_mm / 2.0 + clearance + half - 1e-9, (
            f"GND drop via at ({via.x:.3f}, {via.y:.3f}) sits {gap:.3f} mm from the "
            f"escape lane of the signal pad at {a} — it blocks that pad's only exit"
        )


def test_a_signal_prices_only_the_pending_neighbours_escape_lanes():
    """gr503666, the signal-vs-signal half: the route context carries
    every fine-pitch signal pad's lane, and a net's search is handed the
    lanes of OTHER nets still waiting to route — never its own exit, and
    not a routed net's — grown by clearance and half its own track."""
    from precis.pcb import realize as pcb_realize

    pins = [("U1", str(n)) for n in range(1, 9)]
    graph = {
        "instances": [
            {"refdes": "U1", "x": 10.0, "y": 10.0, "rot": 0.0},
            {"refdes": "B2", "x": 3.0, "y": 11.0},
            {"refdes": "C2", "x": 3.0, "y": 4.0},
            {"refdes": "G2", "x": 30.0, "y": 10.0},
        ],
        "pin_order": pins,
        "nets": [
            {
                "name": "GND",
                "members": [{"refdes": "U1", "pin": "2"}, {"refdes": "G2", "pin": "1"}],
            },
            {
                "name": "SIG_B",
                "members": [{"refdes": "U1", "pin": "3"}, {"refdes": "B2", "pin": "1"}],
            },
            {
                "name": "SIG_C",
                "members": [{"refdes": "U1", "pin": "4"}, {"refdes": "C2", "pin": "1"}],
            },
        ],
        "unconnected": [{"refdes": "U1", "pin": str(n)} for n in (1, 5, 6, 7, 8)],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    names = list(ir.net_name)
    gnd, sig_b, sig_c = names.index("GND"), names.index("SIG_B"), names.index("SIG_C")
    ir.promote_plane(gnd, 3)
    config = RealizeConfig(fab_caps=capability_for("4layer"))
    ctx = pcb_realize._route_context(ir, list(range(ir.n_segments)), config, None, None)
    assert ctx is not None
    assert {lane[4] for lane in ctx.escape_lanes} == {sig_b, sig_c}
    assert all(lane[1][0] < lane[0][0] for lane in ctx.escape_lanes), "outward (-x)"
    zones = pcb_realize._lane_zones(ctx.escape_lanes, sig_b, 0.2, lambda _n: True, ctx)
    assert len(zones) == 1
    (layer, zone), (lane,) = (
        zones[0],
        [lane for lane in ctx.escape_lanes if lane[4] == sig_c],
    )
    assert layer == lane[3]
    assert zone.covers(Point(lane[1]))  # the lane end is inside its zone
    # Grown by the lane's own half-width, the pair clearance and half
    # this track: the zone edge sits that far off the lane axis (within
    # the buffer's polygon approximation of a rounded cap).
    half, clearance = lane[2], ctx.net_clearance.get(sig_b, ctx.clearance)
    assert zone.exterior.distance(Point(lane[1])) == pytest.approx(
        half + clearance + 0.1, abs=0.01
    )
    # SIG_C routed already: nothing left for SIG_B to pay for.
    routed = {sig_c}
    assert (
        pcb_realize._lane_zones(
            ctx.escape_lanes, sig_b, 0.2, lambda n: n not in routed, ctx
        )
        == ()
    )
    # A net with no lane of its own (the plane net) pays for both.
    assert (
        len(pcb_realize._lane_zones(ctx.escape_lanes, gnd, 0.2, lambda _n: True, ctx))
        == 2
    )


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
    config = RealizeConfig(pitch_mm=0.3, router="vector")
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
    result = realize(ir, config=_VECTOR)
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
    result = realize(ir, config=_VECTOR)
    assert {t.net_id for t in result.tracks} == {0, 1}
    other_track_before = next(t for t in result.tracks if t.net_id == 1)

    ripped = rip_net(ir, result, net_id=0)
    assert {t.net_id for t in ripped.tracks} == {1}
    other_track_after = ripped.tracks[0]
    assert other_track_after is other_track_before  # same object, not recomputed


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
    result = realize(ir, config=_VECTOR)
    track = result.tracks[0]
    expected = ipc2221_track_width_mm(5.0, layer_is_outer=True)
    assert track.width_mm == pytest.approx(expected, rel=1e-6)
    assert track.width_mm > 1.0  # nowhere near the 0.25mm fuse


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
    track = realize(ir, config=_VECTOR).tracks[0]
    cap = capability_for("4layer")
    assert track.width_mm == pytest.approx(cap.house_default["trace_width_mm"])


def test_realize_class_rule_override_beats_current_derivation():
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    config = RealizeConfig(
        class_rules={"power": {"track_width_mm": 0.8}}, router="vector"
    )
    track = realize(ir, config=config).tracks[0]
    assert track.width_mm == pytest.approx(0.8)


def test_realize_fab_floor_clamps_a_too_small_class_override():
    ir = from_graph(
        _current_net_graph(0.0, net_class="signal"), stackup=DEFAULT_STACKUP
    )
    ir.set_layer(0, 0)
    config = RealizeConfig(
        class_rules={"signal": {"track_width_mm": 0.001}}, router="vector"
    )
    track = realize(ir, config=config).tracks[0]
    cap = capability_for("4layer")
    jlc_min = cap.jlc_min["trace_width_mm"]
    assert jlc_min is not None
    assert track.width_mm == pytest.approx(jlc_min)


def test_to_gerber_model_uses_per_track_resolved_width_by_default():
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    result = realize(ir, config=_VECTOR)
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
    result = realize(ir, config=_VECTOR)
    assert result.vias == ()


def test_realize_no_via_when_layer_is_unassigned():
    """A segment with no L1 layer assignment yet (UNSET_LAYER) has nothing
    to transition -- must not emit a via."""
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    result = realize(ir, config=_VECTOR)
    assert result.vias == ()


def test_to_gerber_model_explicit_override_still_wins_uniformly():
    ir = from_graph(_current_net_graph(5.0), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    result = realize(ir, config=_VECTOR)
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


def test_pads_for_ir_emits_footprint_pads_no_net_claims():
    """gr451276 — a pad is physical. A cached footprint's pads that the
    netlist never names (NC pins, dead corner lands, an unwired thermal
    slug) occupy board space and the gerbers flash them, so the router's
    obstacle set and DRC's pad set must see them too or they are checking
    a different board than the one that gets made.

    Not promoted to pins: no net, and ``pin`` carries the footprint's own
    label so a finding can name the thing it hit."""
    ir = from_graph(_multi_package_graph(), stackup=DEFAULT_STACKUP)
    footprints = {
        "U0": {
            "pads": [
                # Claimed: net N0 names U0 pin "1".
                {"number": "1", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
                # Unclaimed, and not even named by `pin_map` — the bare
                # pad number is the only label it has.
                {"number": "2", "x": 2.0, "y": 0.5, "w": 0.6, "h": 0.4},
                # Unclaimed but pin_map-named: declared by the footprint,
                # never wired by this design.
                {"number": "9", "x": -2.0, "y": 0.5, "w": 0.6, "h": 0.4},
            ],
            "pin_map": {"1": {"name": "1"}, "9": {"name": "NC1"}},
        }
    }
    pads = pads_for_ir(ir, [layer["name"] for layer in DEFAULT_STACKUP], footprints)
    u0 = [p for p in pads if p["refdes"] == "U0"]
    by_pin = {p["pin"]: p for p in u0}
    assert set(by_pin) == {"1", "2", "NC1"}, (
        f"every U0 footprint pad must be in the model, claimed or not: {by_pin}"
    )
    # U0 sits at the origin unrotated, so footprint-local IS board space.
    assert (by_pin["2"]["x"], by_pin["2"]["y"]) == pytest.approx((2.0, 0.5))
    assert by_pin["2"]["w"] == pytest.approx(0.6)
    assert by_pin["2"]["h"] == pytest.approx(0.4)
    for pin in ("2", "NC1"):
        assert by_pin[pin]["net"] == "", (
            "an unclaimed pad belongs to no net -- attributing one would "
            "hand `connectivity.net_islands` a phantom member"
        )
        assert by_pin[pin]["synthesized"] is False, (
            "this path only exists where a real cached footprint does, so "
            "`export_fab`'s synthesized refusal must not trip on it"
        )
    assert by_pin["1"]["net"] == "N0"


def test_pads_for_ir_places_unclaimed_pads_through_the_instance_pose():
    """An unclaimed pad is transformed by the SAME mirror/rotate rule its
    claimed neighbours are (``padplace.place_footprint_pads``, reused
    rather than re-derived) — a pad the router avoids at the wrong
    coordinate is worse than one it cannot see, since it blocks empty
    board and leaves the real copper unguarded."""
    graph = _multi_package_graph()
    graph["instances"][0]["rot"] = 90.0
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    unclaimed: dict[str, Any] = {"number": "2", "x": 2.0, "y": 0.0, "w": 0.6, "h": 0.4}
    footprints = {
        "U0": {
            "pads": [
                {"number": "1", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
                unclaimed,
            ],
            "pin_map": {"1": {"name": "1"}},
        }
    }
    pads = pads_for_ir(ir, [layer["name"] for layer in DEFAULT_STACKUP], footprints)
    extra = next(p for p in pads if p["refdes"] == "U0" and p["pin"] == "2")
    # Asserted against `place_pad_point` itself, not a hand-computed
    # coordinate: the invariant is "the SAME transform the claimed pads
    # get", and re-deriving the rotation here would be a second copy of
    # the rule, free to drift from it.
    want = padplace.place_pad_point(unclaimed, {"x": 0.0, "y": 0.0, "rot": 90.0})
    assert (extra["x"], extra["y"]) == pytest.approx(want)
    assert (extra["x"], extra["y"]) != pytest.approx((2.0, 0.0)), (
        "the instance pose must actually have been applied"
    )
    # A rect pad at an effective 90 degrees swaps w/h, same as a claimed one.
    assert extra["w"] == pytest.approx(0.4)
    assert extra["h"] == pytest.approx(0.6)


def test_pads_for_ir_invents_no_pads_for_a_synthesized_footprint():
    """No cached footprint means no unclaimed pads to discover: a
    landpattern is synthesized FROM the netlist's pins, so anything beyond
    them would be invented geometry -- the one thing the fab path already
    refuses to do."""
    ir = from_graph(_multi_package_graph(), stackup=DEFAULT_STACKUP)
    pads = pads_for_ir(ir, [layer["name"] for layer in DEFAULT_STACKUP])
    assert len(pads) == ir.n_pins
    assert all(p["net"] for p in pads)


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


def test_pad_geometry_polygon_pin_with_two_raw_pads_keeps_the_first_one():
    """A pin with MORE than one polygon-shaped raw pad sharing its name
    (round-7 finding, docs/backlog/pcb-ewod-multitile.md: an EWOD
    electrode's own footprint emits one raw pad for the electrode BODY
    and a second for its escape STUB, both ``shape: 'polygon'``, both
    ``pin: '1'`` — ":mod:`precis.pcb.generators`'s own docstring, 'nothing
    stops two pads sharing the same pin number'") must resolve to the
    FIRST one's ring, not whichever iterated last. Before this fix,
    ``_real_pad_sizes``'s ``raw_poly_by_name`` overwrote its entry on
    every poly-bearing raw pad, silently picking the LAST — inconsistent
    with the very next loop's own "first wins" rule for size/shape, and
    (for an EWOD board specifically) replacing every electrode's real
    crenellated body with its own tiny escape-stub taper for every
    ``pads_for_ir`` consumer (DRC, connectivity)."""
    ir = from_graph(_multi_package_graph(), stackup=DEFAULT_STACKUP)
    body_poly = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
    stub_poly = [(5.0, 5.0), (5.1, 5.0), (5.0, 5.1)]  # a different, tiny ring elsewhere
    footprints = {
        "U0": {
            "pads": [
                {
                    "number": "1",
                    "x": 0.0,
                    "y": 0.0,
                    "w": 2.0,
                    "h": 2.0,
                    "shape": "polygon",
                    "poly": body_poly,
                },
                {
                    "number": "1",
                    "x": 5.03,
                    "y": 5.03,
                    "w": 0.1,
                    "h": 0.1,
                    "shape": "polygon",
                    "poly": stub_poly,
                },
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
    assert geom.poly is not None
    assert len(geom.poly) == len(body_poly)
    assert set(geom.poly) == {(vx, vy) for vx, vy in body_poly}


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


# ── per-net layer lock (Rulings 2026-09-19 item 7) ───────────────────────

_2LAYER_STACKUP = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "B.Cu", "role": "signal"},
]


def _escape_graph(net_class: str | None) -> dict[str, Any]:
    """U1 (top-mounted, native F.Cu — like an EWOD electrode's own body)
    carries an authored fixed-copper via bridging F.Cu/B.Cu right at its
    own pad, the SAME shape a generator's plaza via/stub gives an
    electrode net; U2 (bottom-mounted, native B.Cu — like a sink channel
    pin) needs no such bridge. Unlocked, the router reaches U1 on its own
    native F.Cu pad directly (the via is just extra, unused, copper);
    locked to B.Cu, U1's native pad is no longer legal and the via's OWN
    B.Cu landing substitutes for it (Rulings 2026-09-19 item 7's own
    "the plaza via's B.Cu end is what the escape should attach to")."""
    net: dict[str, Any] = {
        "name": "ESC",
        "members": [
            {"refdes": "U1", "pin": "1"},
            {"refdes": "U2", "pin": "1"},
        ],
    }
    if net_class is not None:
        net["net_class"] = net_class
    return {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "U2", "x": 5.0, "y": 0.0, "layer": "bottom"},
        ],
        "nets": [net],
    }


_ESCAPE_VIA_FIXED_COPPER = [
    {
        "ctype": "via",
        "net": "ESC",
        "x": 0.0,
        "y": 0.0,
        "span": ["F.Cu", "B.Cu"],
        "dia_mm": 0.3,
    }
]


def test_net_layer_lock_routes_single_layer_where_the_unlocked_net_would_not():
    """A two-pin net locked (``pcb_net_classes.rules["layers"]``) to
    ``["B.Cu"]`` on a 2-layer board routes entirely on B.Cu, no
    ROUTER-added via — via the authored fixed-copper via's own B.Cu
    landing substituting for U1's native (F.Cu, locked-out) pad. Unlocked,
    the SAME geometry is free to reach U1 directly on its own native F.Cu
    pad — a route the lock below must never produce (no track may touch
    F.Cu once the class says ``["B.Cu"]``)."""
    unlocked_ir = from_graph(_escape_graph(None), stackup=_2LAYER_STACKUP)
    unlocked = realize(
        unlocked_ir,
        config=RealizeConfig(router="vector"),
        fixed_copper=_ESCAPE_VIA_FIXED_COPPER,
    )
    assert unlocked.tracks, "sanity: this fixture must actually route"
    assert not unlocked.unrouted

    locked_ir = from_graph(_escape_graph("escape"), stackup=_2LAYER_STACKUP)
    locked = realize(
        locked_ir,
        config=RealizeConfig(
            router="vector", class_rules={"escape": {"layers": ["B.Cu"]}}
        ),
        fixed_copper=_ESCAPE_VIA_FIXED_COPPER,
    )
    assert locked.tracks, "the locked net must still route -- just on B.Cu"
    assert not locked.unrouted
    assert not locked.vias, "a single-layer lock needs no ROUTER-added via"
    assert {t.layer for t in locked.tracks} == {1}  # B.Cu, the locked layer


def test_net_layer_lock_to_a_layer_absent_from_the_stackup_fails_named():
    """A class naming a layer this board's stackup doesn't carry fails
    the net with the closed ``"layer_lock"`` kind, never silently
    widening back to the full routable set."""
    ir = from_graph(_escape_graph("escape"), stackup=_2LAYER_STACKUP)
    result = realize(
        ir,
        config=RealizeConfig(
            router="vector", class_rules={"escape": {"layers": ["In1.Cu"]}}
        ),
    )
    assert not result.tracks
    assert result.unrouted
    reasons = {r.kind for r in result.unrouted_reasons}
    assert reasons == {"layer_lock"}
    assert any("In1.Cu" in r.message for r in result.unrouted_reasons), (
        result.unrouted_reasons
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
    result = realize(ir, config=RealizeConfig(router="vector"))
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
        model,
        capability=capability_for("4layer"),
        unrouted=[],
        outline=closed_outline()["geom"]["path"],
    )
    errors = [f for f in findings if f.severity == "error"]
    assert not errors, [f.detail for f in errors]


# ── the board-edge inset must honour a VIA's own width, not a track's ────


# ── item 1: via shoving in the straighten pass ───────────────────────────


@pytest.mark.slow  # 22s in the 2026-09-26 gate profile

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


# ── item 3: a failed route must say why ──────────────────────────────────


def _frange(a: float, b: float, step: float) -> list[float]:
    n = round((b - a) / step)
    return [a + i * step for i in range(n + 1)]


def test_class_layer_drops_names_the_partial_narrowing():
    """gr472108: a class asking for a plane layer beside a signal one is
    narrowed silently by `_net_class_layers`; `class_layer_drops` says so.
    A fully-unroutable class is NOT listed (it fails as layer_lock)."""
    stackup = [
        {"name": "F.Cu", "role": "signal"},
        {"name": "In2.Cu", "role": "plane"},
        {"name": "B.Cu", "role": "signal"},
    ]
    rules = {
        "esc": {"layers": ["In2.Cu", "B.Cu"]},
        "ok": {"layers": ["F.Cu", "B.Cu"]},
        "none": {"layers": ["In2.Cu"]},
        "plain": {"clearance_mm": 0.2},
    }
    from precis.pcb import realize as pcb_realize

    drops = pcb_realize.class_layer_drops(stackup, rules)
    assert len(drops) == 1
    assert "'esc'" in drops[0] and "In2.Cu" in drops[0] and "B.Cu" in drops[0]
    assert pcb_realize.class_layer_drops(stackup, None) == []


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
    result = realize(ir, config=RealizeConfig(router="vector"))
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


def test_prune_redundant_drop_vias_removes_only_pour_covered_pins():
    """A dogbone whose pad the top pour covers loses BOTH its stub and
    its via (the pour is the connection); one whose pad sits outside the
    pour keeps both. User review 2026-09-01: 'R2 has two superfluous
    vias' — every top-poured net's pin was getting a drop via the pour
    made redundant."""
    from types import SimpleNamespace
    from typing import cast

    import numpy as np

    from precis.pcb import realize as pcb_realize
    from precis.pcb.ir import PcbIR

    ir = cast(
        "PcbIR",
        SimpleNamespace(
            stackup=[{"name": "F.Cu"}, {"name": "B.Cu"}],
            net_name=np.array(["GND"], dtype=object),
        ),
    )
    pours = [
        {
            "layer": "F.Cu",
            "net": "GND",
            "polygon": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
            "holes": [],
        }
    ]

    def dogbone(pad, end):
        return pcb_realize.RealizedTrack(
            0,
            0,
            0,
            ({"shape": "line", "start": list(pad), "end": list(end)},),
            1.0,
            None,
            0.2,
            is_dogbone=True,
        )

    covered = dogbone((5.0, 5.0), (6.0, 5.0))
    outside = dogbone((20.0, 5.0), (21.0, 5.0))
    vias = [
        pcb_realize.RealizedVia(
            seg_id=0,
            net_id=0,
            x=6.0,
            y=5.0,
            dia_mm=0.6,
            drill_mm=0.3,
            layer_lo=0,
            layer_hi=1,
            endpoint="a",
        ),
        pcb_realize.RealizedVia(
            seg_id=0,
            net_id=0,
            x=21.0,
            y=5.0,
            dia_mm=0.6,
            drill_mm=0.3,
            layer_lo=0,
            layer_hi=1,
            endpoint="a",
        ),
    ]
    kept_tracks, kept_vias = pcb_realize._prune_redundant_drop_vias(
        ir, [covered, outside], vias, pours
    )
    assert kept_tracks == [outside]
    assert [(v.x, v.y) for v in kept_vias] == [(21.0, 5.0)]


# ── mounting-hole claims + sprinkle spread (round-3 review items 1/4) ────


def test_spread_order_first_pass_samples_the_whole_list():
    from precis.pcb import realize as pcb_realize

    cands = [(float(i), 0.0) for i in range(100)]
    got = list(pcb_realize._spread_order(cands, 10))
    # every candidate is still visited exactly once …
    assert sorted(got) == cands
    # … and the first cap-sized slice strides the WHOLE list rather than
    # clustering at the head (the measured bottom-rows defect).
    first = [int(x) for x, _ in got[:10]]
    assert first == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]


def test_spread_order_passthrough_when_under_cap():
    from precis.pcb import realize as pcb_realize

    cands = [(1.0, 1.0), (2.0, 2.0)]
    assert list(pcb_realize._spread_order(cands, 10)) == cands


def test_a_pour_antipads_a_bare_npth_hole_by_the_fab_npth_clearance():
    """The pour path's hole blocker grows the same way the router's claim
    does: plane_pours adds `clearance`, so the blocker adds the rest."""
    from types import SimpleNamespace
    from typing import cast

    from precis.pcb import realize as pcb_realize
    from precis.pcb.ir import MountingHole, PcbIR

    ir = cast(
        "PcbIR",
        SimpleNamespace(
            mounting_holes=(
                MountingHole(x=1.0, y=1.0, drill_mm=6.0),
                MountingHole(x=9.0, y=1.0, drill_mm=3.0, ring_dia_mm=5.0, plated=True),
            )
        ),
    )
    bare, ringed = pcb_realize._mounting_hole_blockers(
        ir, ["F.Cu"], npth_clearance_mm=0.45, clearance=0.15
    )
    assert bare["dia_mm"] == pytest.approx(6.0 + 2 * 0.30)
    assert ringed["dia_mm"] == 5.0
    plain = pcb_realize._mounting_hole_blockers(ir, ["F.Cu"])
    assert plain[0]["dia_mm"] == 6.0


def test_npth_clearance_is_the_fab_field_drc_measures():
    from precis.pcb import realize as pcb_realize

    caps = pcb_realize.RealizeConfig().fab_caps
    want = caps.house_default.get("npth_annular_ring_mm") or caps.jlc_min.get(
        "npth_annular_ring_mm"
    )
    assert want and pcb_realize._npth_clearance_mm(pcb_realize.RealizeConfig()) == want


def test_mounting_hole_blockers_are_every_layer_foreign_net_fakes():
    from types import SimpleNamespace
    from typing import cast

    from precis.pcb import realize as pcb_realize
    from precis.pcb.ir import MountingHole, PcbIR

    ir = cast(
        "PcbIR",
        SimpleNamespace(
            mounting_holes=(
                MountingHole(x=4.0, y=4.0, drill_mm=4.3),
                MountingHole(x=6.0, y=6.0, drill_mm=5.6, ring_dia_mm=8.0, plated=True),
                MountingHole(x=0.0, y=0.0, drill_mm=0.0),  # degenerate: skipped
            )
        ),
    )
    layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    blockers = pcb_realize._mounting_hole_blockers(ir, layers)
    assert [(b["x"], b["y"], b["dia_mm"]) for b in blockers] == [
        (4.0, 4.0, 4.3),
        (6.0, 6.0, 8.0),  # the RING is the copper other nets owe clearance to
    ]
    for b in blockers:
        assert b["ctype"] == "via"
        assert b["net"] == ""  # never equals a pour's net -> always antipadded
        assert b["layers"] == layers


# ── courtyard ink avoidance (round-4 silk review, item 8 residue) ────────


def _tiny_square_ring(
    cx: float, cy: float, half: float = 0.05
) -> list[tuple[float, float]]:
    """A small, closed square ring centred on ``(cx, cy)`` — just big
    enough that :meth:`~precis.pcb.realize._InkField.clips` fires for a
    via centred exactly there, small enough that it does not reach a
    neighbouring candidate site in these fixtures."""
    return [
        (cx - half, cy - half),
        (cx + half, cy - half),
        (cx + half, cy + half),
        (cx - half, cy + half),
        (cx - half, cy - half),
    ]


def test_courtyard_ink_field_builds_rings_for_placed_instances_and_empty_for_none():
    """gr — round-4 review residue: vias still clipped courtyard silk near
    U1/C5 (motor board) and D4 (nano board) after round-3's furniture-only
    avoidance. :func:`~precis.pcb.realize._courtyard_ink_field` is the
    route-independent ink field the via-placement sites below now defer
    to. A pinless (no net membership) or unplaced instance contributes no
    ink -- there is no land pattern, and an IR with no instances at all
    contributes none either."""
    from precis.pcb import realize as pcb_realize

    graph = {
        "instances": [{"refdes": "U1", "x": 5.0, "y": 5.0}],
        "nets": [
            {
                "name": "N1",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U1", "pin": "1"}],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    cap = capability_for("4layer")
    field = pcb_realize._courtyard_ink_field(ir, cap)
    assert field.segments, "a placed, pinned instance must contribute ink segments"
    # A coarse sanity bound on where the ring sits, not an exact-shape
    # assertion -- the courtyard polygon's own shape is
    # `instance_courtyard_polygon`'s contract, pinned elsewhere.
    for ax, ay, bx, by, *_bbox in field.segments:
        assert math.hypot(ax - 5.0, ay - 5.0) < 5.0
        assert math.hypot(bx - 5.0, by - 5.0) < 5.0

    empty_ir = from_graph({"instances": [], "nets": []}, stackup=DEFAULT_STACKUP)
    assert pcb_realize._courtyard_ink_field(empty_ir, cap).segments == ()


# ── component-body via surcharge (reworkability) ──────────────────────────
