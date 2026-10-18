"""Route clean-up post-pass (gr495397, :mod:`precis.pcb.routeclean`): a
blocking via of another net that forces a kink is moved and the track runs
straight; a board with no improving move comes out byte-identical."""

from __future__ import annotations

import collections
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
