"""`precis.pcb.connectivity` — is each net's copper actually one piece?

This module's contract is narrow and the tests are about the two ways it
can be useless: saying "connected" about copper that is not (which hides
the defect it exists to find) and saying "disconnected" about copper that
is (which trains a reader to ignore it).
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from precis.pcb import connectivity, planes

_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def _track(
    net: str, pts: list[tuple[float, float]], *, layer: str = "F.Cu", w: float = 0.2
) -> dict[str, Any]:
    return {
        "ctype": "track",
        "layer": layer,
        "net": net,
        "width_mm": w,
        "segments": [
            {"shape": "line", "start": list(a), "end": list(b)}
            for a, b in itertools.pairwise(pts)
        ],
    }


def _via(net: str, x: float, y: float, span: tuple[str, str]) -> dict[str, Any]:
    return {
        "ctype": "via",
        "net": net,
        "x": x,
        "y": y,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "span": list(span),
    }


def _pad(net: str, x: float, y: float, *, layer: str = "F.Cu") -> dict[str, Any]:
    return {
        "layer": layer,
        "net": net,
        "shape": "circle",
        "x": x,
        "y": y,
        "w": 0.4,
        "h": 0.4,
    }


def _poly_pad(
    net: str,
    cx: float,
    cy: float,
    poly: list[tuple[float, float]],
    *,
    layer: str = "F.Cu",
) -> dict[str, Any]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return {
        "layer": layer,
        "net": net,
        "shape": "polygon",
        "x": cx,
        "y": cy,
        "w": max(xs) - min(xs),
        "h": max(ys) - min(ys),
        "poly": [list(p) for p in poly],
    }


def _model(copper: list[dict[str, Any]], pads: list[dict[str, Any]]) -> dict[str, Any]:
    return {"layers": _LAYERS, "copper": copper, "pads": pads}


def test_a_track_joining_two_pads_is_one_component() -> None:
    model = _model(
        [_track("N", [(0.0, 0.0), (5.0, 0.0)])],
        [_pad("N", 0.0, 0.0), _pad("N", 5.0, 0.0)],
    )
    assert connectivity.net_islands(model) == []


def test_a_track_stopping_short_of_its_pad_is_reported() -> None:
    """The 120-of-162 defect, at the scale where it stops being survivable.

    The track ends 0.5mm from the pad — well outside the pad's own 0.2mm
    radius, so there is no copper between them. Every clearance, width and
    edge rule passes this board.
    """
    model = _model(
        [_track("N", [(0.0, 0.0), (4.5, 0.0)])],
        [_pad("N", 0.0, 0.0), _pad("N", 5.0, 0.0)],
    )
    islands = connectivity.net_islands(model)
    assert [(i.net, i.components) for i in islands] == [("N", 2)]


def test_a_branch_missing_its_trunk_is_reported() -> None:
    """The 32-endpoint defect: a T-junction that misses.

    A 0.2mm branch aimed at a 0.2mm trunk but landing 0.3mm off it. This is
    the case a pad would have absorbed and a trace does not — which is why
    the same root cause was survivable at one end of a route and severing
    at the other.
    """
    trunk = _track("N", [(0.0, 0.0), (10.0, 0.0)])
    branch = _track("N", [(5.0, 0.3), (5.0, 4.0)])
    model = _model([trunk, branch], [_pad("N", 0.0, 0.0)])
    assert [(i.net, i.components) for i in connectivity.net_islands(model)] == [
        ("N", 2)
    ]

    touching = _track("N", [(5.0, 0.05), (5.0, 4.0)])
    assert connectivity.net_islands(_model([trunk, touching], [])) == []


def test_a_via_connects_its_own_layers_and_only_those() -> None:
    """A barrel is the one connection in the model that is not an overlap:
    two disks at the same point on different layers do not touch in any
    planar sense, so it has to be asserted from the span."""
    top = _track("N", [(0.0, 0.0), (2.0, 0.0)])
    bottom = _track("N", [(2.0, 0.0), (4.0, 0.0)], layer="B.Cu")
    joined = _model([top, bottom, _via("N", 2.0, 0.0, ("F.Cu", "B.Cu"))], [])
    assert connectivity.net_islands(joined) == []

    # Same geometry, no via: the two runs are on different layers and
    # nothing bridges them.
    assert [
        (i.net, i.components)
        for i in connectivity.net_islands(_model([top, bottom], []))
    ] == [("N", 2)]


def test_two_nets_touching_does_not_make_either_one_connected() -> None:
    """Connectivity is per net. Copper of a DIFFERENT net running through
    the same point is a clearance violation, not a connection — and a
    union-find that forgot to bucket by net would call both nets healthy
    for exactly the wrong reason."""
    model = _model(
        [
            _track("A", [(0.0, 0.0), (1.0, 0.0)]),
            _track("B", [(0.5, 0.0), (0.5, 1.0)]),
            _track("A", [(3.0, 0.0), (4.0, 0.0)]),
        ],
        [],
    )
    assert [(i.net, i.components) for i in connectivity.net_islands(model)] == [
        ("A", 2)
    ]


def test_a_net_with_no_copper_is_not_reported_here() -> None:
    """That is the `unrouted` question. Answering it from this side would
    report one defect twice under two names."""
    assert connectivity.net_islands(_model([], [_pad("N", 0.0, 0.0)])) == []


def test_a_pour_joins_the_vias_that_land_in_it() -> None:
    pour = {
        "ctype": "pour",
        "layer": "In1.Cu",
        "net": "GND",
        "polygon": [[0.0, 0.0], [20.0, 0.0], [20.0, 20.0], [0.0, 20.0]],
    }
    copper = [
        _track("GND", [(2.0, 2.0), (3.0, 2.0)]),
        _via("GND", 3.0, 2.0, ("F.Cu", "In1.Cu")),
        _track("GND", [(15.0, 15.0), (16.0, 15.0)]),
        _via("GND", 16.0, 15.0, ("F.Cu", "In1.Cu")),
        pour,
    ]
    assert connectivity.net_islands(_model(copper, [])) == []

    # Move the pour out from under both vias and the two halves separate.
    far = {**pour, "polygon": [[50.0, 50.0], [60.0, 50.0], [60.0, 60.0], [50.0, 60.0]]}
    islands = connectivity.net_islands(_model(copper[:-1] + [far], []))
    assert [(i.net, i.components) for i in islands] == [("GND", 2)]


def test_a_pour_hole_is_not_copper() -> None:
    """A foreign via through a plane leaves a real antipad. Treating the
    exterior ring as solid would claim copper exactly where the hole is —
    and the via sitting in that hole would be reported connected to a plane
    it is insulated from."""
    pour = {
        "ctype": "pour",
        "layer": "In1.Cu",
        "net": "GND",
        "polygon": [[0.0, 0.0], [20.0, 0.0], [20.0, 20.0], [0.0, 20.0]],
        "holes": [[[9.0, 9.0], [11.0, 9.0], [11.0, 11.0], [9.0, 11.0]]],
    }
    assert planes.point_in_pour(pour, 5.0, 5.0)
    assert not planes.point_in_pour(pour, 10.0, 10.0)
    assert not planes.point_in_pour(pour, 25.0, 5.0)


@pytest.mark.parametrize("gap", [0.0, 1e-9])
def test_exactly_touching_counts_as_connected(gap: float) -> None:
    """The tolerance is float noise, not a "nearly connected" allowance.
    Coordinates the realizer means to be identical arrive through
    resampling and collinear-merge arithmetic."""
    model = _model(
        [
            _track("N", [(0.0, 0.0), (1.0, 0.0)]),
            _track("N", [(1.0 + gap, 0.0), (2.0, 0.0)]),
        ],
        [],
    )
    assert connectivity.net_islands(model) == []


def test_a_polygon_pad_touched_on_a_diagonal_vertex_is_connected() -> None:
    """gr339236's own geometry, restated for `net_islands` (the two
    sibling functions already got this fix; `_pad_primitives` had not).

    A square `shape == 'polygon'` pad's inscribed disk has radius 1.0mm,
    but its own corner sits `sqrt(2)` ~= 1.414mm from centre -- 0.414mm
    outside that disk. A trace landing exactly on that corner is real
    copper touching the pad's real outline, so this must read as ONE
    component. Under the old inscribed-disk-only approximation the same
    board reads as TWO -- the false "disconnected net" finding this test
    exists to catch.
    """
    poly = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
    model = _model(
        [_track("N", [(3.0, 3.0), (1.0, 1.0)], w=0.1)],
        [_poly_pad("N", 0.0, 0.0, poly)],
    )
    assert connectivity.net_islands(model) == []


def test_a_polygon_pad_with_no_resolvable_outline_falls_back_conservatively() -> None:
    """A `shape == 'polygon'` pad whose `poly` is missing/degenerate has no
    exact outline to test against. The fallback disk must be the
    CIRCUMSCRIBED one (`hypot(w, h) / 2`), not the inscribed one:
    `net_islands`'s touch test is a negative predicate ("gap > eps, so NOT
    touching"), and an under-approximation is only sound for the opposite,
    positive predicate. A trace landing on the corner a real 2x2mm square
    pad would have (inscribed radius 1.0mm, corner at `sqrt(2)`mm) must
    still read as connected even though this pad's outline could not be
    resolved.
    """
    pad = {
        "layer": "F.Cu",
        "net": "N",
        "shape": "polygon",
        "x": 0.0,
        "y": 0.0,
        "w": 2.0,
        "h": 2.0,
        "poly": [],  # degenerate: authored as a polygon, but unresolvable
    }
    model = _model([_track("N", [(3.0, 3.0), (1.0, 1.0)], w=0.1)], [pad])
    assert connectivity.net_islands(model) == []
