"""Native lands contact at their analytic boundaries, without proximity inflation."""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis.pcb import connectivity


def _world(x: float, y: float, angle: float) -> tuple[float, float]:
    a = math.radians(angle)
    return 10 + x * math.cos(a) + y * math.sin(a), 10 - x * math.sin(a) + y * math.cos(
        a
    )


def _land(
    bottom: bool, shape: str = "obround", w: float = 2.4, h: float = 1.2
) -> dict[str, Any]:
    return {
        "net": "N",
        "refdes": "U1",
        "pin": "1",
        "shape": shape,
        "x": 10.0,
        "y": 10.0,
        "w": w,
        "h": h,
        "oblique_rot": 37.0,
        "oblique_world_rot": -37.0 if bottom else 37.0,
        "layer": "B.Cu" if bottom else "F.Cu",
    }


def _probe(bottom: bool, gap: float | None) -> tuple[float, float]:
    # Published repro. Its nearest stadium centreline point is the right cap
    # centre; use that independent circle equation to construct controls.
    x, y = 11.0433, 9.2245
    ex, ey = _world(0.6, 0.0, 37.0)
    if gap is not None:
        dx, dy = x - ex, y - ey
        scale = (0.7 + gap) / math.hypot(dx, dy)
        x, y = ex + dx * scale, ey + dy * scale
    return (x, 20 - y) if bottom else (x, y)


@pytest.mark.parametrize("bottom", [False, True], ids=["front", "back"])
@pytest.mark.parametrize(
    "gap",
    [None, -0.00002, 0.00001, 0.0001],
    ids=["reported", "overlap20nm", "gap10nm", "gap100nm"],
)
def test_obround_round_pad_contact_uses_native_cap(
    bottom: bool, gap: float | None
) -> None:
    land = _land(bottom)
    x, y = _probe(bottom, gap)
    ex, ey = _world(0.6, 0.0, -37.0 if bottom else 37.0)
    analytic_gap = math.hypot(x - ex, y - ey) - 0.7
    if gap is None:
        assert analytic_gap == pytest.approx(-0.00002402448663, abs=1e-12)
    else:
        assert analytic_gap == pytest.approx(gap, abs=1e-12)
    contact = analytic_gap <= connectivity.TOUCH_EPS_MM
    probe = dict(
        net="N",
        refdes="J1",
        pin="1",
        shape="circle",
        x=x,
        y=y,
        w=0.2,
        layer=land["layer"],
    )
    model = dict(layers=["F.Cu", "B.Cu"], pads=[land, probe], copper=[])
    assert (connectivity.net_islands(model) == []) is contact
    pair = frozenset({("U1", "1"), ("J1", "1")})
    assert (pair in connectivity.connected_pin_pairs(model)) is contact


@pytest.mark.parametrize("bottom", [False, True], ids=["front", "back"])
@pytest.mark.parametrize("gap", [None, 0.00001], ids=["reported", "gap10nm"])
def test_fixed_via_contact_shares_native_pad_boundary(
    bottom: bool, gap: float | None
) -> None:
    land = _land(bottom)
    x, y = _probe(bottom, gap)
    via = dict(ctype="via", net="N", x=x, y=y, dia_mm=0.2, layers=[land["layer"]])
    model = dict(layers=["F.Cu", "B.Cu"], pads=[land], copper=[via])
    contact = gap is None
    assert (connectivity.net_islands(model) == []) is contact
    terminals = connectivity.fixed_copper_pin_terminals(model)
    assert (("U1", "1") in terminals) is contact
    assert connectivity.fixed_copper_branches(model) == [
        (frozenset({0}), frozenset({"U1"}) if contact else frozenset())
    ]


@pytest.mark.parametrize("bottom", [False, True], ids=["front", "back"])
@pytest.mark.parametrize("gap", [-0.00002, 0.00001], ids=["overlap20nm", "gap10nm"])
def test_two_native_obround_caps_contact(bottom: bool, gap: float) -> None:
    angle = -37.0 if bottom else 37.0
    first = _land(bottom)
    # Parallel stadiums whose nearest cap centres are 1.2 + gap apart.
    # Their complete centreline lengths are 1.2, so centres are 2.4 + gap apart.
    x, y = _world(2.4 + gap, 0.0, angle)
    second = {**first, "x": x, "y": y, "refdes": "J1"}
    model = dict(layers=["F.Cu", "B.Cu"], pads=[first, second], copper=[])
    assert (connectivity.net_islands(model) == []) is (gap < 0)
    pair = frozenset({("U1", "1"), ("J1", "1")})
    assert (pair in connectivity.connected_pin_pairs(model)) is (gap < 0)


@pytest.mark.parametrize("bottom", [False, True], ids=["front", "back"])
@pytest.mark.parametrize("touching", [False, True], ids=["gap20nm", "crossing"])
def test_two_rotated_rectangles_use_both_full_lands(
    bottom: bool, touching: bool
) -> None:
    angle = -37.0 if bottom else 37.0
    first = _land(bottom, "rect", 4.0, 0.2)
    x, y = _world(1.0, 1.0, angle) if touching else _world(2.10002, 0.0, angle)
    second = {**first, "w": 0.2, "h": 4.0, "x": x, "y": y, "refdes": "J1"}
    model = dict(layers=["F.Cu", "B.Cu"], pads=[first, second], copper=[])
    assert (connectivity.net_islands(model) == []) is touching
    pair = frozenset({("U1", "1"), ("J1", "1")})
    assert (pair in connectivity.connected_pin_pairs(model)) is touching


@pytest.mark.parametrize("bottom", [False, True], ids=["front", "back"])
@pytest.mark.parametrize("pending", [False, True], ids=["actual", "pending"])
@pytest.mark.parametrize(
    "case,touching",
    [
        ("centre", True),
        ("cap20nm", True),
        ("gap20nm", False),
        ("hole20nm", False),
        ("hole-overlap20nm", True),
    ],
)
def test_pad_region_contact_uses_whole_land_and_respects_holes(
    bottom: bool, pending: bool, case: str, touching: bool
) -> None:
    land = {**_land(bottom), "oblique_rot": 0.0, "oblique_world_rot": 0.0}
    probe = dict(
        net="N",
        refdes="J1",
        pin="1",
        shape="circle",
        x=13.0,
        y=10.0,
        w=0.2,
        layer=land["layer"],
    )
    # Native stadium ends at x=11.2. The original centre case contains the
    # pad centre x=10 but excludes the left centreline endpoint x=9.4.
    left = 9.5 if case == "centre" else 11.19998 if case == "cap20nm" else 11.20002
    ring = [[left, 9.0], [14.0, 9.0], [14.0, 11.0], [left, 11.0]]
    holes = []
    if case.startswith("hole"):
        ring = [[8.0, 8.0], [14.0, 8.0], [14.0, 12.0], [8.0, 12.0]]
        right = 11.19998 if case == "hole-overlap20nm" else 11.20002
        holes = [[[8.7, 8.7], [right, 8.7], [right, 11.3], [8.7, 11.3]]]
    pour = dict(ctype="pour", net="N", layer=land["layer"], polygon=ring, holes=holes)
    model: dict[str, Any] = dict(layers=["F.Cu", "B.Cu"], pads=[land, probe], copper=[])
    if pending:
        model["pending_pours"] = [
            dict(net="N", layer=land["layer"], region=dict(points=ring, holes=holes))
        ]
    else:
        model["copper"] = [pour]
    assert (connectivity.net_islands(model) == []) is touching
    if not pending:
        pair = frozenset({("U1", "1"), ("J1", "1")})
        assert (pair in connectivity.connected_pin_pairs(model)) is touching


@pytest.mark.parametrize("bottom", [False, True], ids=["front", "back"])
def test_pour_fragments_join_through_obround_land(bottom: bool) -> None:
    land = {**_land(bottom), "oblique_rot": 0.0, "oblique_world_rot": 0.0}
    # Two disjoint strips contact the opposite caps of one native land,
    # with neither strip holding the pad's first centreline endpoint.
    rings = [
        [[8.5, 9.5], [9.0, 9.5], [9.0, 10.5], [8.5, 10.5]],
        [[11.0, 9.5], [11.5, 9.5], [11.5, 10.5], [11.0, 10.5]],
    ]
    pours = [
        dict(ctype="pour", net="N", layer=land["layer"], polygon=ring) for ring in rings
    ]
    model = dict(layers=["F.Cu", "B.Cu"], pads=[land], copper=pours)
    assert connectivity.pour_components(model) == {("N", land["layer"]): 1}
