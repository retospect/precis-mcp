"""Real planter continuation after complete escape-stub rejection."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import numpy as np
import pytest
from shapely.geometry import LineString, Point

from precis.pcb.realize import RealizedVia, RealizeResult
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import realise, wires
from precis.pcb.topoplace.copper_index import Body, CopperIndex
from precis.pcb.topoplace.design import TopoZone
from precis.pcb.topoplace.mesh import OWNER_NONE
from tests.test_pcb_topo_plane_fanout import _escape_case, _escape_mesh


@pytest.mark.parametrize("smaller_fallback", [False, True])
def test_a_rejected_first_site_keeps_the_portals_other_real_candidates(
    monkeypatch: pytest.MonkeyPatch,
    smaller_fallback: bool,
) -> None:
    """Caught by the real emitter: five barrels hide a legal portal ray.

    The first plantable barrel is (8,10), but its stub crosses a foreign
    barrel. The same portal has legal sites between the five shadows.
    """
    td, ir = _escape_case()
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    signal = list(ir.net_name).index("S")
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 6.0], [8.0, 14.0], [11.0, 10.0]])
    prior = tuple(
        RealizedVia(-1, signal, 7.4, y, 0.45, 0.2, 0, 3, "a")
        for y in (6.8, 8.4, 10.0, 11.6, 13.2)
    )
    offered: list[tuple[tuple[float, float, float], bool]] = []
    escape: Any = wires.fanout_escape

    def observe(*args: Any, verify: Any, **kwargs: Any) -> Any:
        def check(points: Any, site: Any) -> bool:
            accepted = bool(verify(points, site))
            offered.append((site, accepted))
            return accepted

        return escape(*args, verify=check, **kwargs)

    monkeypatch.setattr(wires, "fanout_escape", observe)
    tracks, vias, missing, done, need, reasons, spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        {name: NetRules(0.1, 0.1, 0.45, 0.2) for name in td.net_name},
        RealizeResult((), prior, ()),
        int(ir.n_layers),
        mesh=mesh,
        min_via_dia=0.25 if smaller_fallback else None,
        min_via_drill=0.1 if smaller_fallback else None,
    )
    assert offered and math.dist(offered[0][0][:2], (8.0, 10.0)) < 1e-9
    assert not offered[0][1]
    if smaller_fallback:
        assert offered[1] == offered[0], "both original barrel choices must fail first"
    assert missing == [], f"missed a legal same-portal escape: {reasons}"
    assert done == [(k, pin)]
    assert need == reasons == {}
    assert spans == {(k, pin): (0, 3)}
    assert len(tracks) == len(vias) == 1
    assert offered[-1][1]
    assert math.dist((vias[0].x, vias[0].y), (8.0, 10.0)) > 0.1
    assert tracks[0].length_mm < 10.0


def _portal(*, zones: tuple[TopoZone, ...] = ()) -> tuple[Any, Any, Any, int, int]:
    td, _ir = _escape_case()
    td = replace(td, zones=zones)
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 6.0], [8.0, 14.0], [11.0, 10.0]])
    ctx = wires._make_ctx(mesh, td, 4, td.pose0)
    k = list(td.net_name).index("GND")
    return mesh, ctx, wires._ViaPlanter(ctx, td), k, int(td.net_pins[k][0])


def test_every_blocked_stub_is_rejected_without_reoffering_the_first_site() -> None:
    """A fully blocked portal exhausts its bounded real candidates."""
    mesh, ctx, planter, k, pin = _portal()
    copper = CopperIndex([Body(LineString([(6.5, 5.0), (6.5, 15.0)]), r=0.225)])
    offered = []

    def verify(points: list[tuple[float, float]], site: Any) -> bool:
        offered.append(site)
        return not copper.blocked(LineString(points), 0.05, 0.1, net=k)

    result = wires.fanout_escape(
        ctx,
        mesh,
        pin,
        0,
        k,
        np.full(5, 0b1111),
        0.1,
        0.1,
        0.45,
        planter,
        verify=verify,
    )
    expected = list(planter.points(2, 0.225, 0.1, k))
    assert result is None
    assert offered == expected
    assert 1 < len(offered) <= 64


def test_a_successful_first_stub_never_consumes_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mesh, ctx, planter, k, pin = _portal()
    first = planter.point(2, 0.225, 0.1, k)
    points = planter.points
    yielded = []

    def observe(*args: Any, **kwargs: Any) -> Any:
        for site in points(*args, **kwargs):
            yielded.append(site)
            yield site

    monkeypatch.setattr(planter, "points", observe)
    result = wires.fanout_escape(
        ctx,
        mesh,
        pin,
        0,
        k,
        np.full(5, 0b1111),
        0.1,
        0.1,
        0.45,
        planter,
        verify=lambda _points, _site: True,
    )
    assert result is not None and result[1] == first
    assert yielded == [first]


def test_continuation_rechecks_mutated_and_distinct_dynamic_indexes() -> None:
    _mesh, _ctx, planter, k, _pin = _portal()
    dyn = CopperIndex([])
    original = list(planter.points(2, 0.225, 0.1, k, dyn))
    assert original and planter.point(2, 0.225, 0.1, k, dyn) == original[0]
    x, y, _t = original[0]
    dyn.add_disc(x, y, 0.225)
    changed = list(planter.points(2, 0.225, 0.1, k, dyn))
    assert changed and changed == [
        p for p in original if math.dist(p[:2], (x, y)) >= 0.550225
    ]
    assert planter.point(2, 0.225, 0.1, k, dyn) == changed[0]
    other = CopperIndex([])
    assert list(planter.points(2, 0.225, 0.1, k, other)) == original
    assert planter._dyn_refs[id(dyn)] is dyn
    assert planter._dyn_refs[id(other)] is other


def test_a_later_portals_established_success_precedes_new_alternatives() -> None:
    """Do not move an already successful fanout into room a later pad needs."""
    td, _ir = _escape_case()
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 6.0], [8.0, 14.0], [11.0, 10.0]])
    mesh = replace(
        mesh,
        verts=np.asarray([*mesh.verts, [14.0, 6.0]]),
        tris=np.asarray([[0, 1, 2], [1, 3, 2], [1, 4, 3]], np.int32),
        edges=np.asarray([*mesh.edges, [1, 4], [3, 4]], np.int32),
        tri_edges=np.asarray([[0, 2, 1], [3, 4, 2], [5, 6, 3]], np.int32),
        edge_tris=np.asarray(
            [[0, -1], [0, -1], [0, 1], [1, 2], [1, -1], [2, -1], [2, -1]],
            np.int32,
        ),
        constrained=np.zeros(7, bool),
        edge_owner=np.full((7, 2), OWNER_NONE, np.int32),
        edge_side=np.zeros((7, 2), np.int8),
    )
    ctx = wires._make_ctx(mesh, td, 4, td.pose0)
    planter = wires._ViaPlanter(ctx, td)
    k = list(td.net_name).index("GND")
    first = planter.point(2, 0.225, 0.1, k)
    later = planter.point(3, 0.225, 0.1, k)
    assert first is not None and later is not None
    copper = CopperIndex([Body(Point(6.5, 10.0), r=0.225)])
    offered = []

    def verify(points: list[tuple[float, float]], site: Any) -> bool:
        offered.append(site)
        return not copper.blocked(LineString(points), 0.05, 0.1, net=k)

    # A continuation of the earlier portal could succeed too, so trying
    # it eagerly would change the successful baseline site and path.
    assert any(
        not copper.blocked(LineString([(5.0, 10.0), p[:2]]), 0.05, 0.1, net=k)
        for p in planter.points(2, 0.225, 0.1, k)
    )
    result = wires.fanout_escape(
        ctx,
        mesh,
        int(td.net_pins[k][0]),
        0,
        k,
        np.full(7, 0b1111),
        0.1,
        0.1,
        0.45,
        planter,
        verify=verify,
    )
    assert offered == [first, later]
    assert result == ([(5.0, 10.0), later[:2]], later, False)


def test_an_existing_smaller_via_success_precedes_larger_via_continuation() -> None:
    """Astra review caught a changed success across the diameter fallback.

    Historical 626 emits a straight 3 mm stub to the smaller via at
    (8,10). The normal first site's stub is blocked; its opposite-side
    alternative must not replace that established smaller-via success.
    """
    td, ir = _escape_case()
    td = replace(
        td,
        zones=(
            TopoZone(
                "keepout",
                np.asarray([(8.25, 9.4), (8.35, 9.4), (8.35, 10.6), (8.25, 10.6)]),
                (3,),
            ),
        ),
    )
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 6.0], [8.0, 14.0], [11.0, 10.0]])
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    ctx = wires._make_ctx(mesh, td, int(ir.n_layers), td.pose0)
    planter = wires._ViaPlanter(ctx, td)
    normal = planter.point(2, 0.225, 0.1, k)
    small = planter.point(2, 0.125, 0.1, k)
    assert normal is not None and small is not None
    assert math.dist(normal[:2], (8.0, 9.1)) < 1e-9
    assert math.dist(small[:2], (8.0, 10.0)) < 1e-9
    prior = RealizedVia(
        -1,
        list(ir.net_name).index("S"),
        6.5,
        10.0 + (normal[1] - 10.0) / 2.0,
        0.1,
        0.05,
        0,
        3,
        "a",
    )
    tracks, vias, missing, done, need, reasons, spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        {name: NetRules(0.1, 0.1, 0.45, 0.2) for name in td.net_name},
        RealizeResult((), (prior,), ()),
        int(ir.n_layers),
        mesh=mesh,
        min_via_dia=0.25,
        min_via_drill=0.1,
    )
    assert missing == [] and done == [(k, pin)]
    assert need == reasons == {}
    assert spans == {(k, pin): (0, 3)}
    assert len(tracks) == len(vias) == 1
    assert vias[0].dia_mm == 0.25 and vias[0].drill_mm == 0.1
    assert math.dist((vias[0].x, vias[0].y), (8.0, 10.0)) < 1e-9
    assert tracks[0].segments == (
        {"shape": "line", "start": [5.0, 10.0], "end": [8.0, 10.0]},
    )
    assert abs(tracks[0].length_mm - 3.0) < 1e-9


def test_continuation_preserves_static_interval_and_drill_filters() -> None:
    zone = TopoZone(
        "keepout",
        np.asarray([(7.9, 9.9), (8.1, 9.9), (8.1, 10.1), (7.9, 10.1)]),
        (3,),
    )
    _mesh, ctx, planter, k, _pin = _portal(zones=(zone,))
    # One physical hole with a 0.20 mm fabrication wall. Its drill is
    # independent of both the new via's copper land and electrical gap.
    planter.hole_ix = CopperIndex([Body(Point(8.0, 8.0), r=0.3, clearance=0.2)])
    small = list(planter.points(2, 0.225, 0.1, k, lo=0.1, hi=0.9, drill_mm=0.05))
    large = list(planter.points(2, 0.225, 0.1, k, lo=0.1, hi=0.9, drill_mm=0.5))
    assert small and large and len(large) < len(small)
    assert large == [p for p in small if math.dist(p[:2], (8.0, 8.0)) >= 0.750225]
    assert all(0.1 - 1e-9 <= p[2] <= 0.9 + 1e-9 for p in small)
    assert all(abs(p[1] - 10.0) >= 0.425225 for p in small)
    assert planter.point(2, 0.225, 0.1, k, lo=0.1, hi=0.9, drill_mm=0.5) == large[0]
    assert list(planter.points(2, 0.225, 0.1, k, lo=0.5, hi=0.5)) == []
    ctx.board[0] = True
    assert list(planter.points(0, 0.225, 0.1, k)) == []
