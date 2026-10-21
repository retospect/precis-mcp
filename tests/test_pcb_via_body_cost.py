"""gr502369 / docs/backlog/pcb-board-profile-prototype.md item 4 — the
via-under-courtyard router cost's plumbing: the profile's
``via_body_cost_mm`` reaches ``RealizeConfig`` (``prod`` ⇒ 0, off), the
route context builds the courtyard zones only when the cost is on, and
the ``vias_under_part`` measurement reads the SAME courtyard geometry the
A* pays against.

The A* mechanism itself (detour vs. body via) is pinned in
``tests/test_pcb_vroute.py``'s gr502369 section.
"""

from __future__ import annotations

from shapely.geometry import Point

from precis.pcb import DEFAULT_STACKUP, profiles
from precis.pcb.drc import DEFAULT_COURTYARD_RADIUS_MM, vias_in_courtyard_count
from precis.pcb.ir import (
    COURTYARD_CLEARANCE_MM,
    from_graph,
    world_courtyard_polygons,
)
from precis.pcb.realize import RealizeConfig, _route_context, _via_body_zones

_GRAPH = {
    "instances": [
        {"refdes": "U1", "x": 0.0, "y": 0.0},
        {"refdes": "U2", "x": 5.0, "y": 0.0},
    ],
    "nets": [
        {
            "name": "N1",
            "members": [
                {"refdes": "U1", "pin": "1"},
                {"refdes": "U2", "pin": "1"},
            ],
        }
    ],
}


def test_profile_via_body_cost_threads_into_realize_config() -> None:
    """The one expression ``pcb_route`` builds the config with: the
    profile's figure on ``prototype``, 0 — off — on ``prod``."""
    proto = profiles.profile_rules("prototype").via_body_cost_mm
    prod = profiles.profile_rules("prod").via_body_cost_mm
    assert proto == 6.0
    assert prod is None
    assert RealizeConfig(via_body_cost_mm=proto or 0.0).via_body_cost_mm == 6.0
    assert RealizeConfig(via_body_cost_mm=prod or 0.0).via_body_cost_mm == 0.0


def test_route_context_builds_zones_only_when_the_cost_is_on() -> None:
    """Prod (cost 0) builds no zones at all — the same work, the same
    board, as before the cost existed."""
    ir = from_graph(_GRAPH, stackup=DEFAULT_STACKUP)
    ids = list(range(ir.n_segments))
    off = _route_context(ir, ids, RealizeConfig(), None, None)
    assert off is not None and off.via_body_zones == ()
    on = _route_context(ir, ids, RealizeConfig(via_body_cost_mm=6.0), None, None)
    assert on is not None
    assert on.via_body_zones == _via_body_zones(ir)
    assert len(on.via_body_zones) == 2  # one per placed instance


def test_the_cost_zones_and_the_count_share_one_courtyard_geometry() -> None:
    """``vias_in_courtyard_count`` over the shared world-courtyard recipe
    and the zones the router charges against must agree on which sites
    are under a body."""
    ir = from_graph(_GRAPH, stackup=DEFAULT_STACKUP)
    courtyards = world_courtyard_polygons(
        ir,
        clearance_mm=COURTYARD_CLEARANCE_MM,
        fallback_half_extent_mm=DEFAULT_COURTYARD_RADIUS_MM,
    )
    zones = _via_body_zones(ir)
    sites = [(0.0, 0.0), (5.0, 0.0), (50.0, 50.0)]
    under = vias_in_courtyard_count(sites, courtyards)
    assert under == 2  # U1's and U2's pin centres; the far site is free
    assert [any(zone.covers(Point(p)) for zone in zones) for p in sites] == [
        True,
        True,
        False,
    ]
