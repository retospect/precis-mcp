"""``resolve_crossings`` memo and obstacle prefilter change speed, never the
answer; a repeated scan of unchanged branches runs no bigon test."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, Polygon
from shapely.ops import substring

from precis.pcb.topoplace import crossings
from precis.pcb.topoplace.crossings import Cross, crossing_events, resolve_crossings
from precis.pcb.topoplace.mesh import TopoMesh
from precis.pcb.topoplace.wires import WirePath


def _mesh() -> TopoMesh:
    return TopoMesh(
        verts=np.zeros((1, 2)),
        tris=np.zeros((0, 3), np.int32),
        edges=np.zeros((0, 2), np.int32),
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((0, 2), np.int32),
        constrained=np.zeros(0, dtype=bool),
        pad_vertex={},
    )


def _wire(net: int, pts: Sequence[tuple[float, float]]) -> WirePath:
    return WirePath(
        net=net,
        pin_a=2 * net,
        pin_b=2 * net + 1,
        tris=(),
        edges=(),
        t=(),
        points=tuple(pts),
    )


def _naive_empty(pa: Any, pb: Any, x: Cross, y: Cross, obs: np.ndarray) -> bool:
    def line(pts: Any) -> Any:
        return LineString([p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]])

    arc_a = substring(line(pa), x[0], y[0])
    arc_b = substring(line(pb), y[1], x[1])
    if arc_a.geom_type != "LineString" or arc_b.geom_type != "LineString":
        return False
    poly = Polygon([*arc_a.coords, *arc_b.coords])
    if not poly.is_valid or poly.area <= 1e-9:
        return False
    inner = poly.buffer(-1e-6)
    if inner.is_empty or not len(obs):
        return True
    return not bool(shapely.contains_xy(inner, obs[:, 0], obs[:, 1]).any())


def _naive_resolve(branches: Any, obs: np.ndarray, events: Any) -> Any:
    regions: dict[Any, Any] = {}
    cancelled: list[Any] = []
    for (a, b), evs in events.items():
        pa, pb = branches[a].points, branches[b].points
        while len(evs) >= 2:
            by_a = sorted(range(len(evs)), key=lambda i: evs[i][0])
            rank_b = {
                i: r
                for r, i in enumerate(sorted(range(len(evs)), key=lambda i: evs[i][1]))
            }
            gone = next(
                (
                    (i, j)
                    for i, j in itertools.pairwise(by_a)
                    if abs(rank_b[i] - rank_b[j]) == 1
                    and _naive_empty(pa, pb, evs[i], evs[j], obs)
                ),
                None,
            )
            if gone is None:
                break
            cancelled.append((a, b, evs[gone[0]], evs[gone[1]]))
            evs = [e for k, e in enumerate(evs) if k not in gone]
        if evs:
            regions[(a, b)] = [e[2] for e in evs]
    return regions, cancelled


def _random_case(seed: int) -> tuple[list[WirePath], np.ndarray]:
    rng = np.random.default_rng(seed)
    branches = []
    for net in range(3):
        n = int(rng.integers(3, 8))
        xs = np.sort(rng.uniform(0, 10, n))
        ys = rng.uniform(0, 4, n)
        branches.append(_wire(net, list(zip(xs.tolist(), ys.tolist()))))
    obs = rng.uniform(0, 10, (int(rng.integers(0, 40)), 2)) * np.array([1.0, 0.4])
    return branches, obs


@pytest.mark.parametrize("seed", range(40))
def test_resolve_crossings_matches_uncached_reference(seed: int) -> None:
    branches, obs = _random_case(seed)
    mesh = _mesh()
    events = crossing_events(mesh, branches)
    want = _naive_resolve(branches, obs, {k: list(v) for k, v in events.items()})
    crossings._PAIR_MEMO.clear()
    assert resolve_crossings(mesh, branches, obs, events) == want
    assert resolve_crossings(mesh, branches, obs, events) == want  # memo hit


def test_repeat_scan_of_unchanged_branches_tests_no_bigon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real = crossings.bigon_empty

    def counting(*args: Any, **kw: Any) -> bool:
        nonlocal calls
        calls += 1
        return real(*args, **kw)

    monkeypatch.setattr(crossings, "bigon_empty", counting)
    crossings._PAIR_MEMO.clear()
    mesh = _mesh()
    for seed in range(40):
        branches, obs = _random_case(seed)
        events = crossing_events(mesh, branches)
        if any(len(v) >= 2 for v in events.values()):
            break
    else:
        pytest.fail("no seed gave a pair with two crossings")
    first = resolve_crossings(mesh, branches, obs, events)
    n_first = calls
    assert n_first > 0
    assert resolve_crossings(mesh, branches, obs, events) == first
    assert calls == n_first
