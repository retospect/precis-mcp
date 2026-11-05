"""Failed fanout witnesses need local shadow subtraction, not a closed portal."""

from __future__ import annotations

import dataclasses
import itertools
import math
from typing import Any

import numpy as np
import pytest
from shapely.geometry import LineString, Point

from precis.pcb.realize import RealizedVia, RealizeResult
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import realise, wires
from precis.pcb.topoplace.mesh import OWNER_NONE, TopoMesh
from tests.test_pcb_topo_plane_fanout import _escape_case, _escape_mesh


def _barrels(ir: Any) -> tuple[RealizedVia, ...]:
    return tuple(
        RealizedVia(-1, list(ir.net_name).index("S"), 7.4, y, 0.45, 0.2, 0, 3, "a")
        for y in (6.8, 8.4, 10.0, 11.6, 13.2)
    )


def test_escape_between_all_five_blocked_witnesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five blocked rays leave (8,7) legal; the real emitter must reach it."""
    td, ir = _escape_case()
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 6.0], [8.0, 14.0], [11.0, 10.0]])
    prior = _barrels(ir)
    for fraction in (0.5, 0.25, 0.75, 1e-6, 1.0 - 1e-6):
        line = LineString([(5.0, 10.0), (8.0, 6.0 + 8.0 * fraction)])
        assert min(line.distance(Point(v.x, v.y)) for v in prior) < 0.375

    def point(_self: Any, edge: int, *_args: Any, **_kw: Any) -> Any:
        return (8.0, 7.0, 0.125) if edge == 2 else None

    monkeypatch.setattr(wires._ViaPlanter, "point", point)
    tracks, vias, missing, done, _need, _reasons, _spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        {"GND": NetRules(0.1, 0.1, 0.45, 0.2), "S": NetRules(0.1, 0.1, 0.45, 0.2)},
        RealizeResult((), prior, ()),
        int(ir.n_layers),
        mesh=mesh,
    )
    assert missing == []
    assert done == [(k, pin)]
    assert len(tracks) == len(vias) == 1
    axis = LineString([tracks[0].segments[0]["start"], tracks[0].segments[-1]["end"]])
    assert min(axis.distance(Point(v.x, v.y)) for v in prior) > 0.375


def test_transition_between_blocked_witness_pairs_reaches_legal_site() -> None:
    """The second portal needs shadow subtraction as well as a seed portal."""
    td, ir = _escape_case(((2.0, 10.0), (27.0, 16.0), (25.0, 3.0), (28.0, 3.0)))
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    mesh = TopoMesh(
        verts=np.asarray(
            [[2.0, 10.0], [5.0, 10.0], [8.0, 6.0], [8.0, 14.0], [11.0, 10.0]]
        ),
        tris=np.asarray([[0, 1, 2], [1, 3, 2], [2, 4, 3]], np.int32),
        edges=np.asarray(
            [[0, 1], [0, 2], [1, 2], [1, 3], [2, 3], [2, 4], [3, 4]], np.int32
        ),
        tri_edges=np.asarray([[0, 2, 1], [3, 4, 2], [5, 6, 4]], np.int32),
        edge_tris=np.asarray(
            [[0, -1], [0, -1], [0, 1], [1, -1], [1, 2], [2, -1], [2, -1]], np.int32
        ),
        constrained=np.zeros(7, dtype=bool),
        pad_vertex={},
        pin_vertex=np.zeros(len(td.pin_net), np.int32),
        edge_owner=np.full((7, 2), OWNER_NONE, np.int32),
        edge_side=np.zeros((7, 2), np.int8),
    )
    ctx = wires._make_ctx(mesh, td, int(ir.n_layers), td.pose0)
    obstacles = [((7.4, y), (7.4, y), 0.375) for y in (6.8, 8.4, 10.0, 11.6, 13.2)]
    called: list[int] = []

    def step(_tri: int, p: Any, q: Any) -> bool:
        return all(
            LineString([p, q]).distance(Point(a)) >= rad - 1e-9
            for a, _b, rad in obstacles
        )

    def interval(tri: int, ps: Any, qs: Any) -> bool:
        called.append(tri)
        return wires._escape_interval_open(obstacles, ps, qs)

    class Planter:
        def point(self, edge: int, *_args: Any, **_kw: Any) -> Any:
            return (8.0, 7.0, 0.125) if edge == 4 else None

    planter: Any = Planter()
    result = wires.fanout_escape(
        ctx,
        mesh,
        pin,
        0,
        k,
        np.asarray([0, 0, 1, 0, 1, 0, 0], np.uint64),
        0.1,
        0.1,
        0.45,
        planter,
        wire_cover={2: [(0.1, 1.0)]},
        step_ok=step,
        verify=lambda pts, _via: all(step(0, a, b) for a, b in itertools.pairwise(pts)),
        iv_ok=interval,
    )
    assert result is not None
    assert 1 in called
    assert math.dist(result[0][-1], (8.0, 7.0)) < 1e-9


@pytest.mark.parametrize("radius,expected", [(0.375, True), (0.9, False)])
def test_shadow_subtraction_preserves_gaps_and_proves_a_blocked_portal(
    radius: float, expected: bool
) -> None:
    obstacles = [((7.4, y), (7.4, y), radius) for y in (6.8, 8.4, 10.0, 11.6, 13.2)]
    assert (
        wires._escape_interval_open(
            obstacles, ((5.0, 10.0), (5.0, 10.0)), ((8.0, 6.0), (8.0, 14.0))
        )
        is expected
    )


@pytest.mark.parametrize(
    "case", ["concave-notch", "no-stub-interior", "no-barrel-interior"]
)
def test_escape_final_guard_checks_the_swept_stub(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A supplied candidate still owes the whole board-edge contract."""
    td, ir = _escape_case()
    outline = (
        [
            [0.0, 0.0],
            [30.0, 0.0],
            [30.0, 20.0],
            [7.0, 20.0],
            [7.0, 9.0],
            [6.5, 9.0],
            [6.5, 20.0],
            [0.0, 20.0],
        ]
        if case == "concave-notch"
        else [[0.0, 9.8], [30.0, 9.8], [30.0, 10.2], [0.0, 10.2]]
    )
    td = dataclasses.replace(td, outline=np.asarray(outline))
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 6.0], [8.0, 14.0], [11.0, 10.0]])

    def escape(*_args: Any, **kw: Any) -> Any:
        pts, via = [(5.0, 10.0), (8.0, 10.0)], (8.0, 10.0, 0.5)
        return (pts, via, False) if kw["verify"](pts, via) else None

    monkeypatch.setattr(wires, "fanout_escape", escape)
    rule = (
        NetRules(0.1, 0.1, 0.45, 0.2)
        if case == "no-barrel-interior"
        else NetRules(0.6, 0.1, 0.1, 0.05)
    )
    tracks, vias, missing, done, _need, _reasons, _spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        {"GND": rule},
        RealizeResult((), (), ()),
        int(ir.n_layers),
        mesh=mesh,
    )
    assert tracks == vias == done == []
    assert missing == [(k, pin)]
