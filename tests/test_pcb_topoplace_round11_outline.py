"""Plane copper must fit inside concave outlines as a complete swept body."""

from dataclasses import replace

import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from precis.pcb.realize import RealizedVia, RealizeResult
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import realise
from tests.test_pcb_topo_fanout_clearance import _case, _line, _track


def _notch(bottom: float) -> np.ndarray:
    return np.asarray(
        [
            (0, 0),
            (30, 0),
            (30, 20),
            (11.15, 20),
            (11.15, bottom),
            (10.65, bottom),
            (10.65, 20),
            (0, 20),
        ],
        dtype=float,
    )


@pytest.mark.parametrize("bottom", [9.0, 10.1], ids=["axis-crosses", "sweep-grazes"])
def test_fanout_sites_keep_the_complete_stub_inside_the_board(
    monkeypatch: pytest.MonkeyPatch, bottom: float
) -> None:
    td, ir = _case(monkeypatch, ((10.0, 10.0),), names=("GND",))
    td = replace(
        td,
        outline=_notch(bottom),
        silk=replace(td.silk, outline_reach_mm=td.silk.outline_reach_mm + 0.45),
    )
    rules = {"GND": NetRules(0.15, 0.15, 0.45, 0.2)}
    tracks, vias, _fallback, deferred = realise._fanout_vias(
        td, ir, 4, td.pose0, rules, {0: 1}
    )
    assert tracks and not deferred, "other directions remain legal"
    board = Polygon(td.outline)
    edge = max(td.edge_clearance_mm, 0.15)
    for track, via in zip(tracks, vias, strict=True):
        axis = _line(track)
        assert board.covers(axis)
        assert axis.distance(board.boundary) >= edge + track.width_mm / 2 - 1e-9
        assert (
            Point(via.x, via.y).distance(board.boundary) >= edge + via.dia_mm / 2 - 1e-9
        )


@pytest.mark.parametrize(
    ("bottom", "end"),
    [(9.0, (11.75, 10.0)), (10.1, (11.75, 10.0)), (20.0, (29.7, 10.0))],
    ids=["stub-crosses-notch", "stub-sweep-grazes-notch", "barrel-edge"],
)
def test_final_fanout_guard_rejects_out_of_board_copper(
    monkeypatch: pytest.MonkeyPatch, bottom: float, end: tuple[float, float]
) -> None:
    td, ir = _case(monkeypatch, ((10.0, 10.0),), names=("GND",))
    td = replace(td, outline=_notch(bottom))
    track = _track(0, 0, [(10.0, 10.0), end])
    via = RealizedVia(-1, 0, *end, 0.45, 0.2, 0, 3, "a")
    assert realise._guard_fanouts(
        td,
        ir,
        td.pose0,
        {"GND": NetRules(0.15, 0.15, 0.45, 0.2)},
        RealizeResult((), (), ()),
        [track],
        [via],
    ) == {0}
