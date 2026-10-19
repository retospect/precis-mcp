"""The single-net vector router (``docs/backlog/pcb-vector-router.md``
slice 3): every returned path is octilinear and clears every foreign body
by the exact kernel; the walled-in fine-pitch escape the grid misses
(gr489923) is found; a fully walled layer is crossed by a through via."""

from __future__ import annotations

import itertools
import math
import random

import pytest
from shapely.geometry import LineString, Point

from precis.pcb import clearance as k
from precis.pcb import vroute as v

_CL = 0.152
_BOUNDS = (0.0, 0.0, 20.0, 20.0)


def _pad(net: str, x: float, y: float, w: float, h: float, layers=(0,)) -> v.Obstacle:
    body = k.body_of({"ctype": "pad", "shape": "rect", "x": x, "y": y, "w": w, "h": h})
    assert body is not None
    return v.Obstacle(body, net, frozenset(layers), blocks_vias=True)


def _wall(net: str, a, b, width: float = 0.3, layers=(0,)) -> v.Obstacle:
    return v.Obstacle(
        k.Body(LineString([a, b]), (), width / 2.0), net, frozenset(layers)
    )


def _space(obs) -> v.Space:
    return v.Space(obs, lambda _net, _layer: _CL)


def _req(
    start, goal, layers=(0,), width=0.2, via=None, terminal_layers=None, **kw
) -> v.Request:
    ends = frozenset(terminal_layers if terminal_layers is not None else layers)
    return v.Request(
        net="SIG",
        width_mm=width,
        layers=tuple(layers),
        start=v.Terminal(float(start[0]), float(start[1]), ends),
        goal=v.Terminal(float(goal[0]), float(goal[1]), ends),
        bounds=_BOUNDS,
        via=via,
        via_layers=tuple(layers),
        **kw,
    )


def _assert_legal(path: v.VPath, obs, width: float, via_dia: float = 0.0) -> None:
    """Independent of the router's caches: every leg octilinear, every leg
    and via clear of every foreign body by the kernel gap."""
    for layer, pts in path.runs:
        for a, b in itertools.pairwise(pts):
            assert v.is_octilinear(a, b, 1e-7), (a, b)
            leg = k.Body(LineString([a, b]), (), width / 2.0)
            for ob in obs:
                if layer in ob.layers and ob.net != "SIG":
                    assert k.gap(leg, ob.body) >= _CL - 1e-7, (a, b, ob)
    for x, y in path.vias:
        via = k.Body(Point(x, y), (), via_dia / 2.0)
        for ob in obs:
            assert k.gap(via, ob.body) >= _CL - 1e-7, ((x, y), ob)


def test_free_space_is_one_octilinear_run() -> None:
    path = v.route(_space([]), _req((2.0, 2.0), (8.0, 2.0)))
    assert path is not None
    assert path.runs == [(0, [(2.0, 2.0), (8.0, 2.0)])]
    assert path.vias == []


def test_off_angle_goal_takes_a_45_and_an_axis_leg() -> None:
    path = v.route(_space([]), _req((2.0, 2.0), (8.0, 4.0)))
    assert path is not None
    ((_layer, pts),) = path.runs
    assert len(pts) == 3
    assert path.length_mm == pytest.approx(4.0 + 2.0 * math.sqrt(2.0))


def test_routes_around_a_foreign_wall() -> None:
    obs = [_wall("W", (5.0, 0.5), (5.0, 8.0))]
    path = v.route(_space(obs), _req((2.0, 4.0), (8.0, 4.0)))
    assert path is not None
    _assert_legal(path, obs, 0.2)
    assert path.length_mm > 6.0


def test_walled_in_fine_pitch_pad_escapes_along_its_axis() -> None:
    """gr489923's geometry: 0.3 x 1.55 mm pads at 0.5 mm pitch, 0.2 mm
    gaps. A 0.2 mm track leaves pad 5 along its long axis (clearing each
    neighbour by 0.25 mm) and reaches a goal 4 mm above the row."""
    obs = [_pad(f"N{i}", 2.0 + 0.5 * i, 5.0, 0.3, 1.55) for i in range(11) if i != 5]
    # A second staggered row above, offset half a pitch.
    obs += [_pad(f"M{i}", 2.25 + 0.5 * i, 7.0, 0.3, 1.55) for i in range(10)]
    obs += [_pad("SIG", 4.5, 5.0, 0.3, 1.55)]  # its own pad: never an obstacle
    path = v.route(_space(obs), _req((4.5, 5.0), (4.5, 11.0)))
    assert path is not None
    _assert_legal(path, obs, 0.2)


def test_a_fully_walled_layer_is_crossed_by_through_vias() -> None:
    wall = _wall("W", (10.0, 0.0), (10.0, 20.0), width=0.5, layers=(0,))
    via = v.ViaSpec(0.6, 0.3)
    req = _req((5.0, 10.0), (15.0, 10.0), layers=(0, 1), via=via, terminal_layers=(0,))
    path = v.route(_space([wall]), req)
    assert path is not None
    assert len(path.vias) == 2
    assert {layer for layer, _pts in path.runs} == {0, 1}
    _assert_legal(path, [wall], 0.2, via_dia=0.6)


def test_a_via_never_sits_in_a_pad_of_any_net() -> None:
    """Reto, gr496850: no via-in-pad. A through via keeps the clearance
    from every pad, its own net's included."""
    own = _pad("SIG", 5.0, 10.0, 1.2, 1.2, layers=(0, 1))
    wall = _wall("W", (10.0, 0.0), (10.0, 20.0), width=0.5, layers=(0,))
    req = _req(
        (5.0, 10.0),
        (15.0, 10.0),
        layers=(0, 1),
        via=v.ViaSpec(0.6, 0.3),
        terminal_layers=(0,),
    )
    path = v.route(_space([own, wall]), req)
    assert path is not None
    for x, y in path.vias:
        via = k.Body(Point(x, y), (), 0.3)
        assert k.gap(via, own.body) >= _CL - 1e-7


def test_the_expansion_budget_is_shared_by_both_windows() -> None:
    """Work, not the clock, bounds a search: a request that cannot reach
    its goal spends at most ``max_expansions`` over both windows."""
    box_in = [
        _wall("W", (3.0, 3.0), (7.0, 3.0)),
        _wall("W", (7.0, 3.0), (7.0, 7.0)),
        _wall("W", (7.0, 7.0), (3.0, 7.0)),
        _wall("W", (3.0, 7.0), (3.0, 3.0)),
    ]
    stats: dict = {}
    req = _req((5.0, 5.0), (15.0, 15.0), max_expansions=40)
    assert v.route(_space(box_in), req, stats=stats) is None
    assert stats["expansions"] <= 40


def test_no_path_returns_none() -> None:
    box_in = [
        _wall("W", (3.0, 3.0), (7.0, 3.0)),
        _wall("W", (7.0, 3.0), (7.0, 7.0)),
        _wall("W", (7.0, 7.0), (3.0, 7.0)),
        _wall("W", (3.0, 7.0), (3.0, 3.0)),
    ]
    assert v.route(_space(box_in), _req((5.0, 5.0), (15.0, 15.0))) is None


@pytest.mark.parametrize("seed", range(10))
def test_random_fields_give_legal_paths_or_none(seed: int) -> None:
    rng = random.Random(seed)
    obs = []
    for i in range(25):
        x, y = rng.uniform(2, 18), rng.uniform(2, 18)
        if rng.random() < 0.5:
            obs.append(
                _pad(f"P{i}", x, y, rng.uniform(0.3, 1.5), rng.uniform(0.3, 1.5))
            )
        else:
            ang = rng.uniform(0, math.pi)
            ln = rng.uniform(1, 4)
            obs.append(
                _wall(f"T{i}", (x, y), (x + ln * math.cos(ang), y + ln * math.sin(ang)))
            )
    path = v.route(_space(obs), _req((1.0, 1.0), (19.0, 19.0)))
    if path is not None:
        _assert_legal(path, obs, 0.2)


@pytest.mark.parametrize("seed", range(10))
def test_batched_leg_check_matches_the_single_leg_predicate(seed: int) -> None:
    """``Space.clears_legs`` is ``Space.clears`` for many legs at once: the
    search's choices must not depend on which one answered."""
    rng = random.Random(100 + seed)
    obs = []
    for i in range(30):
        x, y = rng.uniform(2, 18), rng.uniform(2, 18)
        kind = rng.random()
        if kind < 0.4:
            obs.append(
                _pad(f"P{i}", x, y, rng.uniform(0.3, 1.5), rng.uniform(0.3, 1.5))
            )
        elif kind < 0.7:
            obs.append(_wall(f"T{i}", (x, y), (x + rng.uniform(-3, 3), y)))
        else:
            arc = k.Arc(x, y, rng.uniform(0.3, 2.0), rng.uniform(0, 6.28), 1.5)
            obs.append(v.Obstacle(k.Body(None, (arc,), 0.1), f"A{i}", frozenset((0,))))
    obs.append(_pad("SIG", 10.0, 10.0, 1.0, 1.0))
    space = _space(obs)
    legs = [
        (
            (rng.uniform(0, 20), rng.uniform(0, 20)),
            (rng.uniform(0, 20), rng.uniform(0, 20)),
        )
        for _ in range(200)
    ]
    # Zero-length legs are track ends (the node filter's use).
    legs += [(a, a) for a, _b in legs[:50]]
    batch = space.clears_legs(legs, 0.1, "SIG", 0).tolist()
    single = [
        space.clears(k.Body(LineString([a, b]), (), 0.1), "SIG", 0) for a, b in legs
    ]
    assert batch == single


def test_octagon_corners_circumscribe_the_grown_body() -> None:
    body = k.body_of(
        {"ctype": "pad", "shape": "rect", "x": 1.0, "y": 2.0, "w": 0.6, "h": 1.2}
    )
    assert body is not None
    corners = v.octagon_corners(body, 0.3)
    for c in corners:
        assert k.signed_gap(k.Body(Point(c), (), 0.0), body) >= 0.3 - 1e-9
