"""Route clean-up post-pass (gr495397, :mod:`precis.pcb.routeclean`): a
blocking via of another net that forces a kink is moved and the track runs
straight; a board with no improving move comes out byte-identical."""

from __future__ import annotations

import collections
import dataclasses
import itertools
from collections.abc import Sequence
from typing import Any

from precis.pcb import DEFAULT_STACKUP, maze, routeclean
from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import PcbIR, from_graph

_CAPS = capability_for("4layer")


def _drc(
    ir: PcbIR,
    tracks: Sequence[pcb_realize.RealizedTrack],
    vias: Sequence[pcb_realize.RealizedVia],
) -> collections.Counter[tuple[str, str]]:
    """The route job's own check shape: routed-copper DRC by (rule, severity)."""
    probe = pcb_realize.RealizeResult(tuple(tracks), tuple(vias), ())
    findings = pcb_session.routed_drc_findings(ir, probe, capability=_CAPS)
    return collections.Counter((f.rule, f.severity) for f in findings)


def _crossing_graph() -> dict[str, Any]:
    """SIG runs U1 -> U2 along y=0; B crosses it from a top pad at y=-2 to a
    bottom pad at y=+2, so B needs one via somewhere on x=5."""
    return {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "U2", "x": 10.0, "y": 0.0},
            {"refdes": "P1", "x": 5.0, "y": -2.0},
            {"refdes": "P2", "x": 5.0, "y": 2.0, "layer": "bottom"},
        ],
        "nets": [
            {
                "name": "SIG",
                "members": [{"refdes": "U1", "pin": "1"}, {"refdes": "U2", "pin": "1"}],
            },
            {
                "name": "B",
                "members": [{"refdes": "P1", "pin": "1"}, {"refdes": "P2", "pin": "1"}],
            },
        ],
    }


def _seg_of(ir: PcbIR, net: str) -> int:
    return next(s for s in range(ir.n_segments) if ir.net_name[ir.seg_net[s]] == net)


def _kinked_board() -> tuple[
    PcbIR,
    pcb_realize._RouteContext,
    pcb_realize.RealizeConfig,
    routeclean.Outcome,
    dict[int, maze.RoutePath],
]:
    """B's via forced onto SIG's straight line at (5, 0), then SIG routed
    around it by the ordinary search: the kink of gr495397 in miniature."""
    ir = from_graph(_crossing_graph(), stackup=DEFAULT_STACKUP)
    config = pcb_realize.RealizeConfig()
    ctx = pcb_realize._route_context(ir, list(range(ir.n_segments)), config, None, None)
    assert ctx is not None
    seg_sig, seg_b = _seg_of(ir, "SIG"), _seg_of(ir, "B")
    lo, hi = ctx.signal_layers[0], ctx.signal_layers[-1]
    blocking = maze.RoutePath(
        int(ir.seg_net[seg_b]),
        ((5.0, -2.0, lo), (5.0, 0.0, lo), (5.0, 0.0, hi), (5.0, 2.0, hi)),
        4.0,
    )
    grid = maze.OccupancyGrid(
        ctx.spec, clearance_mm=ctx.clearance, net_clearance_mm=ctx.net_clearance
    )
    grid.set_body_mask(ctx.body_mask)
    stamped: dict[int, maze.RoutePath] = {}
    outcome = pcb_realize._route_pass(
        ir,
        [seg_b, seg_sig],
        ctx.plane_ids,
        grid,
        config,
        ctx.rules_by_net,
        ctx.pads,
        ctx.clearance,
        ctx.signal_layers,
        ctx.spec,
        ctx.pad_geoms,
        ink_field=ctx.ink_field,
        island_terminals=ctx.island_terminals,
        net_layers=ctx.net_layers,
        kept={seg_b: blocking},
        stamped=stamped,
    )
    return ir, ctx, config, outcome, stamped


def test_blocking_via_is_moved_and_the_kinked_track_runs_straight() -> None:
    ir, ctx, config, outcome, stamped = _kinked_board()
    seg_sig = _seg_of(ir, "SIG")
    length0, bends0 = routeclean.path_length_and_bends(stamped[seg_sig])
    assert bends0 >= 2, "fixture must kink SIG around B's via"
    drc0 = _drc(ir, outcome[0], outcome[1])

    new, report = routeclean.cleanup(ir, ctx, config, outcome, stamped, _drc)

    assert (report.kept, report.tried) == (1, 1)
    assert report.bends_saved == bends0
    assert report.length_saved_mm > 0.5
    assert report.vias_saved == 0
    assert report.line() == (
        f"cleanup: 1 moves kept, 1 tried, length -{report.length_saved_mm:.2f} mm, "
        f"bends -{bends0}"
    )
    sig_tracks = [t for t in new[0] if t.seg_id == seg_sig]
    assert len(sig_tracks) == 1
    ys = {
        round(p[1], 6) for s in sig_tracks[0].segments for p in (s["start"], s["end"])
    }
    assert ys == {0.0}, f"SIG still leaves y=0: {sig_tracks[0].segments}"
    (via,) = new[1]
    assert via.x == 5.0 and 0.0 < abs(via.y) <= routeclean.MOVE_RADIUS_MM
    assert new[2] == outcome[2], "the unrouted set never changes"
    drc1 = _drc(ir, new[0], new[1])
    assert all(n <= drc0.get(k, 0) for k, n in drc1.items()), (drc0, drc1)


def test_a_move_the_drc_check_refuses_leaves_the_outcome_untouched() -> None:
    ir, ctx, config, outcome, stamped = _kinked_board()
    calls = itertools.count()

    def rising(
        ir_: PcbIR,
        tracks: Sequence[pcb_realize.RealizedTrack],
        vias: Sequence[pcb_realize.RealizedVia],
    ) -> dict[tuple[str, str], int]:
        return {("clearance", "warn"): next(calls)}

    new, report = routeclean.cleanup(ir, ctx, config, outcome, dict(stamped), rising)
    assert report.kept == 0 and report.tried >= 1
    assert new is outcome
    line = report.line()
    assert line is not None and line.startswith("cleanup: 0 moves kept")


def _gerber(ir: PcbIR, result: pcb_realize.RealizeResult) -> dict[str, Any]:
    layers = [str(layer["name"]) for layer in DEFAULT_STACKUP]
    return pcb_realize.to_gerber_model(result, ir, layers=layers, outline=[])


def test_no_improving_move_is_byte_identical_to_cleanup_off() -> None:
    """The router already keeps B's via off SIG's line, so nothing improves;
    the pass must then change nothing at all."""
    ir = from_graph(_crossing_graph(), stackup=DEFAULT_STACKUP)
    off = pcb_realize.realize(
        ir, config=pcb_realize.RealizeConfig(route_cleanup=False), cleanup_check=_drc
    )
    on = pcb_realize.realize(
        ir, config=pcb_realize.RealizeConfig(route_cleanup=True), cleanup_check=_drc
    )
    assert off.cleanup is None
    assert on.cleanup is not None and on.cleanup.kept == 0
    assert on.tracks == off.tracks
    assert on.vias == off.vias
    assert on.pours == off.pours
    assert repr(_gerber(ir, on)) == repr(_gerber(ir, off))


def test_no_check_means_no_pass() -> None:
    """A bare realize() (tests, cost probes, the anneal's inner loop) never
    runs the pass: the DRC half of the accept rule is missing."""
    ir = from_graph(_crossing_graph(), stackup=DEFAULT_STACKUP)
    assert pcb_realize.realize(ir).cleanup is None


def test_octilinear_yardstick() -> None:
    assert routeclean.octilinear_min_bends((0.0, 0.0), (10.0, 0.0)) == 0
    assert routeclean.octilinear_min_bends((0.0, 0.0), (3.0, 3.0)) == 0
    assert routeclean.octilinear_min_bends((0.0, 0.0), (10.0, 3.0)) == 1
    assert routeclean.octilinear_distance((0.0, 0.0), (3.0, 4.0)) == (
        4.0 + (2.0**0.5 - 1.0) * 3.0
    )
    jog = maze.RoutePath(
        0, ((0.0, 0.0, 0), (1.0, 1.0, 0), (9.0, 1.0, 0), (10.0, 0.0, 0)), 0.0
    )
    length, bends = routeclean.path_length_and_bends(jog)
    assert bends == 2 and abs(length - (8.0 + 2 * 2.0**0.5)) < 1e-9
    via_hop = maze.RoutePath(
        0, ((0.0, 0.0, 0), (5.0, 0.0, 0), (5.0, 0.0, 3), (5.0, 4.0, 3)), 0.0
    )
    assert routeclean.path_length_and_bends(via_hop) == (9.0, 0)


def _fanout_ir(sig_y: float | None, sig_x: float = 5.0) -> PcbIR:
    """Two GND pins on a plane (each gets a fan-out drop via) and, when
    ``sig_y`` is given, SIG running vertically through (``sig_x``, ``sig_y``)
    — clear of G1's own pad, which sits 2.5 mm west of its drop via."""
    instances: list[dict[str, Any]] = [
        {"refdes": "G1", "x": 5.0, "y": 3.0},
        {"refdes": "G2", "x": 25.0, "y": 12.0},
    ]
    nets: list[dict[str, Any]] = [
        {
            "name": "GND",
            "net_class": "ground",
            "domain": "electrical",
            "members": [{"refdes": "G1", "pin": "1"}, {"refdes": "G2", "pin": "1"}],
        }
    ]
    if sig_y is not None:
        instances += [
            {"refdes": "S1", "x": sig_x, "y": sig_y - 6.0},
            {"refdes": "S2", "x": sig_x, "y": sig_y + 6.0},
        ]
        members = [{"refdes": "S1", "pin": "1"}, {"refdes": "S2", "pin": "1"}]
        nets.append({"name": "SIG", "members": members})
    outline = [[-10.0, -10.0], [40.0, -10.0], [40.0, 25.0], [-10.0, 25.0]]
    ir = from_graph(
        {"instances": instances, "nets": nets}, stackup=DEFAULT_STACKUP, outline=outline
    )
    ir.promote_plane(0, 1)  # GND onto In1.Cu
    return ir


def test_a_plane_fanout_via_that_blocks_a_track_is_moved() -> None:
    """Reto 2026-10-18: any via may move. G1's drop via lands on SIG's
    straight line; the clean-up bans that site, the fan-out takes its next
    legal one, and SIG runs straighter."""
    config = pcb_realize.RealizeConfig(router="maze", fab_caps=_CAPS)
    bare = pcb_realize.realize(_fanout_ir(None), config=config)
    (g1_via,) = [v for v in bare.vias if abs(v.x - 5.0) < 3.0 and abs(v.y - 3.0) < 3.0]

    ir = _fanout_ir(g1_via.y, g1_via.x)
    off_config = pcb_realize.RealizeConfig(
        router="maze", fab_caps=_CAPS, route_cleanup=False
    )
    off = pcb_realize.realize(ir, config=off_config, cleanup_check=_drc)
    on = pcb_realize.realize(ir, config=config, cleanup_check=_drc)
    assert not off.unrouted and not on.unrouted
    sig = _seg_of(ir, "SIG")

    def bends(res: pcb_realize.RealizeResult) -> int:
        track = next(t for t in res.tracks if t.seg_id == sig)
        return max(0, len(track.segments) - 1)

    assert bends(off) >= 2, "fixture must kink SIG around G1's drop via"
    assert on.cleanup is not None and on.cleanup.kept >= 1
    assert bends(on) < bends(off)
    gnd = next(i for i, name in enumerate(ir.net_name) if name == "GND")
    fan_off = [v for v in off.vias if v.net_id == gnd]
    fan_on = [v for v in on.vias if v.net_id == gnd]
    assert len(fan_on) == len(fan_off), "a fan-out move never adds or drops a via"
    assert (g1_via.x, g1_via.y) not in {(v.x, v.y) for v in fan_on}
    drc_off, drc_on = _drc(ir, off.tracks, off.vias), _drc(ir, on.tracks, on.vias)
    assert all(n <= drc_off.get(k, 0) for k, n in drc_on.items()), (drc_off, drc_on)


def test_the_pass_skips_a_board_with_copper_drc_errors_and_says_why() -> None:
    """Reto 2026-10-19: only a finished board is cleaned up. Silk errors do
    not count; a copper error does."""
    ir, ctx, config, outcome, stamped = _kinked_board()

    def copper_errors(
        ir_: PcbIR,
        tracks: Sequence[pcb_realize.RealizedTrack],
        vias: Sequence[pcb_realize.RealizedVia],
    ) -> dict[tuple[str, str], int]:
        return {("clearance", "error"): 3, ("silk_missing", "error"): 9}

    new, report = routeclean.cleanup(
        ir, ctx, config, outcome, dict(stamped), copper_errors
    )
    assert new is outcome
    assert (report.kept, report.tried) == (0, 0)
    assert report.line() == "cleanup: skipped (0 unrouted, 3 copper DRC errors)"


def test_the_pass_skips_a_board_with_an_unrouted_net_and_says_why() -> None:
    ir, ctx, config, outcome, stamped = _kinked_board()
    unrouted = (outcome[0], outcome[1], [_seg_of(ir, "B")], outcome[3], outcome[4])

    new, report = routeclean.cleanup(ir, ctx, config, unrouted, dict(stamped), _drc)
    assert new is unrouted
    assert report.tried == 0
    assert report.line() == "cleanup: skipped (1 unrouted, 0 copper DRC errors)"


def test_the_pass_runs_when_only_silk_rules_fail() -> None:
    ir, ctx, config, outcome, stamped = _kinked_board()

    def silk_only(
        ir_: PcbIR,
        tracks: Sequence[pcb_realize.RealizedTrack],
        vias: Sequence[pcb_realize.RealizedVia],
    ) -> dict[tuple[str, str], int]:
        return {("silk_missing", "error"): 40, ("silk_edge_clearance", "error"): 2}

    _new, report = routeclean.cleanup(
        ir, ctx, config, outcome, dict(stamped), silk_only
    )
    assert report.skipped == ""
    assert report.kept == 1 and report.tried == 1


def test_a_raising_drc_check_skips_the_pass_and_leaves_the_raise_to_the_gate() -> None:
    ir, ctx, config, outcome, stamped = _kinked_board()

    def boom(
        ir_: PcbIR,
        tracks: Sequence[pcb_realize.RealizedTrack],
        vias: Sequence[pcb_realize.RealizedVia],
    ) -> dict[tuple[str, str], int]:
        raise ValueError("drc exploded")

    new, report = routeclean.cleanup(ir, ctx, config, outcome, dict(stamped), boom)
    assert new is outcome
    assert report.line() == "cleanup: skipped (DRC check raised ValueError)"


def test_a_trial_grid_carries_the_per_net_layer_clearance() -> None:
    """The route's own passes build their grid with ctx.net_layer_clearance;
    a trial that left it out would accept copper the real pass refuses."""
    ir, ctx, config, outcome, stamped = _kinked_board()
    sig, b = _seg_of(ir, "SIG"), _seg_of(ir, "B")
    sig_net, b_net = int(ir.seg_net[sig]), int(ir.seg_net[b])
    plain = routeclean._trial(ir, ctx, config, stamped, sig_net, b_net)
    assert plain is not None
    wide = dataclasses.replace(
        ctx, net_layer_clearance={(sig_net, layer): 3.0 for layer in ctx.signal_layers}
    )
    strict = routeclean._trial(ir, wide, config, stamped, sig_net, b_net)
    if strict is not None:
        assert strict[1][sig] != plain[1][sig], "SIG ignored its 3 mm layer clearance"
