"""Unit tests for the copper tiling primitives (precis.pcb.tiling) —
weighted expansion, sliver culling, tile-net connectivity, and neck-down.
No DB, no network: pure shapely geometry over synthetic skeletons.
"""

from __future__ import annotations

import pytest
from shapely.geometry import (  # type: ignore[import-untyped]
    LineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from precis.pcb.objectives import objectives_for_connection
from precis.pcb.tiling import (
    NetTilingSpec,
    cull_slivers,
    drop_floating_pieces,
    expansion_rate_from_objective,
    find_acute_angles,
    find_floating_pieces,
    grow_tiles,
    neck_down_at_pads,
)

_BOARD = box(0.0, 0.0, 20.0, 20.0)
_MIN_HALF = 0.1
_CLEARANCE = 0.2


def _power_and_rf_objectives():
    power_obj, _ = objectives_for_connection("power")
    rf_obj, _ = objectives_for_connection("rf")
    return power_obj, rf_obj


# ── expansion_rate_from_objective ────────────────────────────────────────
def test_expansion_rate_power_wants_wide_rf_wants_narrow():
    power_obj, rf_obj = _power_and_rf_objectives()
    assert expansion_rate_from_objective(power_obj) > 0.0
    # rf: low_resistance(0.2) - low_capacitance(0.9) < 0 -> clamped to 0
    assert expansion_rate_from_objective(rf_obj) == 0.0


def test_expansion_rate_signal_class_stays_at_minimum():
    signal_obj, _ = objectives_for_connection("signal")
    # low_resistance(0.1) - low_capacitance(0.2) < 0 -> clamped to 0
    assert expansion_rate_from_objective(signal_obj) == 0.0


# ── grow_tiles: per-net weight ────────────────────────────────────────────
def test_low_resistance_net_ends_wider_than_low_capacitance_net():
    """Two structurally identical skeletons (same straight-line shape and
    length), placed far enough apart to never interact — the only
    difference is which objective drives each net's expansion weight. A
    low-resistance (power-class) net must end up wider than a
    low-capacitance (rf-class) net grown from the same starting shape."""
    power_obj, rf_obj = _power_and_rf_objectives()
    skel_power = LineString([(1.0, 2.0), (18.0, 2.0)])
    skel_rf = LineString([(1.0, 15.0), (18.0, 15.0)])
    specs = [
        NetTilingSpec(
            net_id=1,
            skeleton=skel_power,
            min_half_width_mm=_MIN_HALF,
            expansion_rate=expansion_rate_from_objective(power_obj),
            max_half_width_mm=0.6,
        ),
        NetTilingSpec(
            net_id=2,
            skeleton=skel_rf,
            min_half_width_mm=_MIN_HALF,
            expansion_rate=expansion_rate_from_objective(rf_obj),
            max_half_width_mm=0.6,
        ),
    ]
    tiles = grow_tiles(specs, _BOARD, clearance_mm=_CLEARANCE)
    assert tiles[1].area > tiles[2].area


def test_fixed_width_controlled_impedance_net_not_widened():
    """A controlled-impedance (rf preset) net's tile must stay EXACTLY at
    the class-minimum width — no growth at all, not just "less growth"."""
    _power_obj, rf_obj = _power_and_rf_objectives()
    skel = LineString([(1.0, 10.0), (18.0, 10.0)])
    spec = NetTilingSpec(
        net_id=1,
        skeleton=skel,
        min_half_width_mm=_MIN_HALF,
        expansion_rate=expansion_rate_from_objective(rf_obj),
        max_half_width_mm=1.0,
    )
    tiles = grow_tiles([spec], _BOARD, clearance_mm=_CLEARANCE)
    expected = skel.buffer(_MIN_HALF).intersection(_BOARD)
    assert tiles[1].equals(expected)
    assert tiles[1].area == expected.area


def test_grown_net_respects_its_max_half_width_cap():
    power_obj, _ = _power_and_rf_objectives()
    skel = LineString([(1.0, 10.0), (18.0, 10.0)])
    spec = NetTilingSpec(
        net_id=1,
        skeleton=skel,
        min_half_width_mm=_MIN_HALF,
        expansion_rate=expansion_rate_from_objective(power_obj),
        max_half_width_mm=0.3,
    )
    tiles = grow_tiles([spec], _BOARD, clearance_mm=_CLEARANCE)
    capped = skel.buffer(0.3).intersection(_BOARD)
    # grown to (approximately) the cap, never past it
    assert tiles[1].area <= capped.area + 1e-6
    assert tiles[1].area > skel.buffer(_MIN_HALF).area


def test_plane_net_fills_the_remainder():
    """Ground fills what remains because it has the most skeleton and no
    cap (decisions log) — implemented here as a final remainder pass."""
    power_obj, rf_obj = _power_and_rf_objectives()
    skel_power = LineString([(1.0, 2.0), (18.0, 2.0)])
    skel_rf = LineString([(1.0, 15.0), (18.0, 15.0)])
    capped_specs = [
        NetTilingSpec(
            net_id=1,
            skeleton=skel_power,
            min_half_width_mm=_MIN_HALF,
            expansion_rate=expansion_rate_from_objective(power_obj),
            max_half_width_mm=0.5,
        ),
        NetTilingSpec(
            net_id=2,
            skeleton=skel_rf,
            min_half_width_mm=_MIN_HALF,
            expansion_rate=expansion_rate_from_objective(rf_obj),
            max_half_width_mm=0.5,
        ),
    ]
    gnd_spec = NetTilingSpec(
        net_id=3,
        skeleton=LineString([(0.0, 0.0), (0.0, 0.001)]),
        min_half_width_mm=0.0,
        is_plane=True,
    )
    tiles = grow_tiles(capped_specs + [gnd_spec], _BOARD, clearance_mm=_CLEARANCE)
    assert 0.0 < tiles[3].area < _BOARD.area
    # the plane doesn't overlap either capped net's claimed footprint
    assert tiles[3].intersection(tiles[1]).area == 0.0
    assert tiles[3].intersection(tiles[2]).area == 0.0


def test_mutual_growth_keeps_clearance_between_two_capped_nets():
    """Two nets growing toward each other both stop with clearance_mm
    between them — neither eats the whole gap."""
    power_obj, _ = _power_and_rf_objectives()
    skel_a = LineString([(1.0, 9.0), (18.0, 9.0)])
    skel_b = LineString([(1.0, 11.0), (18.0, 11.0)])
    rate = expansion_rate_from_objective(power_obj)
    specs = [
        NetTilingSpec(
            net_id=1,
            skeleton=skel_a,
            min_half_width_mm=_MIN_HALF,
            expansion_rate=rate,
            max_half_width_mm=2.0,
        ),
        NetTilingSpec(
            net_id=2,
            skeleton=skel_b,
            min_half_width_mm=_MIN_HALF,
            expansion_rate=rate,
            max_half_width_mm=2.0,
        ),
    ]
    tiles = grow_tiles(specs, _BOARD, clearance_mm=_CLEARANCE)
    gap = tiles[1].distance(tiles[2])
    # within one growth step's discretization slack of the exact clearance
    assert gap >= _CLEARANCE - 0.01
    assert gap < _CLEARANCE + 0.5  # and didn't just stop growing early


# ── sliver / acute-angle cull ─────────────────────────────────────────────
def _spike_polygon() -> Polygon:
    """A deliberately acute-angled spike (an acid trap at its tip)."""
    return Polygon([(0, 0), (10, 0), (10, 1), (0.5, 1), (0, 10), (0, 0)])


def test_find_acute_angles_flags_the_spike_tip():
    angles = find_acute_angles(_spike_polygon(), min_angle_deg=30.0)
    assert len(angles) >= 1
    assert all(angle < 30.0 for _vertex, angle in angles)


def test_find_acute_angles_empty_on_a_rectangle():
    rect = box(0, 0, 5, 5)
    assert find_acute_angles(rect, min_angle_deg=30.0) == []


def test_cull_slivers_shrinks_the_spike():
    spike = _spike_polygon()
    opened = cull_slivers(spike, min_width_mm=0.6)
    assert opened.area < spike.area
    # the sliver tip is gone: far fewer/no acute vertices survive at the
    # same threshold that flagged the original.
    assert len(find_acute_angles(opened, min_angle_deg=30.0)) < len(
        find_acute_angles(spike, min_angle_deg=30.0)
    )


def test_cull_slivers_leaves_a_healthy_rectangle_alone():
    rect = box(0, 0, 5, 5)
    opened = cull_slivers(rect, min_width_mm=0.2)
    assert opened.area == pytest.approx(rect.area, rel=0.05)


# ── every tile must connect to its own net ───────────────────────────────
def test_find_floating_pieces_detects_the_disconnected_island():
    connected = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    floating = Polygon([(5, 5), (6, 5), (6, 6), (5, 6)])
    tile = MultiPolygon([connected, floating])
    skeleton = LineString([(0.5, 0.5), (0.9, 0.9)])

    found = find_floating_pieces(tile, skeleton)
    assert len(found) == 1
    assert found[0].equals(floating)


def test_find_floating_pieces_empty_when_everything_touches_skeleton():
    connected = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    skeleton = LineString([(0.5, 0.5), (0.9, 0.9)])
    assert find_floating_pieces(connected, skeleton) == []


def test_drop_floating_pieces_keeps_only_connected_copper():
    connected = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    floating = Polygon([(5, 5), (6, 5), (6, 6), (5, 6)])
    tile = MultiPolygon([connected, floating])
    skeleton = LineString([(0.5, 0.5), (0.9, 0.9)])

    fixed = drop_floating_pieces(tile, skeleton)
    assert fixed.area == connected.area
    assert find_floating_pieces(fixed, skeleton) == []


def test_drop_floating_pieces_returns_empty_when_nothing_connects():
    floating = Polygon([(5, 5), (6, 5), (6, 6), (5, 6)])
    skeleton = LineString([(0.5, 0.5), (0.9, 0.9)])
    fixed = drop_floating_pieces(floating, skeleton)
    assert fixed.is_empty


# ── neck-down at pads ──────────────────────────────────────────────────────
def test_neck_down_narrows_the_tile_near_pads():
    skel = LineString([(0.0, 0.0), (5.0, 0.0)])
    wide = skel.buffer(0.5)  # a widened tile along the whole run
    necked = neck_down_at_pads(
        wide,
        skel,
        [(0.0, 0.0), (5.0, 0.0)],
        min_half_width_mm=0.1,
        neck_length_mm=0.5,
    )
    assert necked.area < wide.area
    # the middle of the run (far from either pad) is untouched: still wide
    mid_point = Point(2.5, 0.45)  # inside the 0.5-half-width tile, not the neck
    assert wide.contains(mid_point)
    assert necked.contains(mid_point)


def test_neck_down_is_a_no_op_with_no_pads():
    skel = LineString([(0.0, 0.0), (5.0, 0.0)])
    wide = skel.buffer(0.5)
    assert (
        neck_down_at_pads(wide, skel, [], min_half_width_mm=0.1, neck_length_mm=0.5)
        is wide
    )
