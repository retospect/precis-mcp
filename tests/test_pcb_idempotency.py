"""Unit tests for :func:`precis.pcb.session.content_hash` (gr266041) — the
``op=`` idempotency digest must cover every store surface the
``pcb_place``/``pcb_route`` workers actually read, but must NOT cover the
fields those same jobs write back (else a completed job's own checkpoint
mints a fresh duplicate on the next identical resubmit). No DB — every
case constructs the raw store-row shapes by hand.
"""

from __future__ import annotations

from typing import Any

from precis.pcb.session import content_hash

_GRAPH = {
    "instances": [
        {"refdes": "U1", "x": 1.0, "y": 2.0, "rot": 0, "fixed": False},
        {"refdes": "U2", "x": 5.0, "y": 6.0, "rot": 90, "fixed": True},
    ],
    "nets": [
        {
            "name": "GND",
            "net_class": "power",
            "members": [{"refdes": "U1", "pin": "1"}, {"refdes": "U2", "pin": "2"}],
        },
    ],
    "board": {
        "stackup": [
            {"name": "F.Cu"},
            {"name": "In1.Cu"},
            {"name": "In2.Cu"},
            {"name": "B.Cu"},
        ]
    },
}
_PARAMS = {"pcb_ref_id": 1}


def test_session_state_none_reproduces_the_pre_gr266041_digest() -> None:
    """A literal pin — a future refactor of ``content_hash`` must not
    silently drift the digest of every in-flight job's idempotency key on
    deploy."""
    assert content_hash(_GRAPH, _PARAMS) == "d98b729f1115d3708c462fb1"
    assert content_hash(_GRAPH, _PARAMS, session_state=None) == (
        "d98b729f1115d3708c462fb1"
    )


def test_authored_plane_assignment_changes_the_digest() -> None:
    base_state = {"planes": [{"layer": "In1.Cu", "net": "GND", "source": "authored"}]}
    changed_state: dict[str, Any] = {
        "planes": [{"layer": "In2.Cu", "net": "GND", "source": "authored"}]
    }
    h1 = content_hash(_GRAPH, _PARAMS, session_state=base_state)
    h2 = content_hash(_GRAPH, _PARAMS, session_state=changed_state)
    assert h1 != h2


def test_authored_route_topology_override_changes_the_digest() -> None:
    base_state: dict[str, Any] = {
        "routes": {
            "GND": {
                "topology": [{"a": "U1.1", "b": "U2.2", "side": 0}],
                "status": "unrouted",
                "fail": None,
                "meta": {},
                "tree": [],
                "layer_assign": [],
            }
        }
    }
    changed_state: dict[str, Any] = {
        "routes": {
            "GND": {
                "topology": [{"a": "U1.1", "b": "U2.2", "side": 1}],
                "status": "unrouted",
                "fail": None,
                "meta": {},
                "tree": [],
                "layer_assign": [],
            }
        }
    }
    h1 = content_hash(_GRAPH, _PARAMS, session_state=base_state)
    h2 = content_hash(_GRAPH, _PARAMS, session_state=changed_state)
    assert h1 != h2


def test_a_jobs_own_write_back_does_not_change_its_idempotency_key() -> None:
    """THE REGRESSION TEST THAT MATTERS: two ``session_state``s that agree
    on every AUTHORED fact (route topology, authored plane/pin-swap rows)
    but disagree on everything a completed ``pcb_place``/``pcb_route`` job
    itself writes back (route ``status``/``fail``/``meta``/``tree``/
    ``layer_assign``, and any ``source == 'derived'`` plane/pin-swap row)
    must hash IDENTICALLY — otherwise a job's own checkpoint would mint a
    fresh duplicate job on the very next identical resubmit."""
    before = {
        "routes": {
            "GND": {
                "topology": [{"a": "U1.1", "b": "U2.2", "side": 0}],
                "status": "unrouted",
                "fail": None,
                "meta": {},
                "tree": [],
                "layer_assign": [],
            }
        },
        "planes": [
            {
                "layer": "In1.Cu",
                "net": "GND",
                "region_hint": None,
                "source": "authored",
            },
        ],
        "pin_swaps": [
            {"refdes": "U1", "pin": "1", "net": "GND", "source": "authored"},
        ],
    }
    # A run of `pcb_route` completed: status/fail/meta/tree/layer_assign
    # all got checkpointed, and the optimizer minted its own derived plane
    # + pin-swap rows alongside the untouched authored ones. `topology`
    # (the only route field that matters) and the authored rows are
    # unchanged.
    after = {
        "routes": {
            "GND": {
                "topology": [{"a": "U1.1", "b": "U2.2", "side": 0}],
                "status": "routed",
                "fail": {"some": "detail"},
                "meta": {"last_route": {"iters": 500}},
                "tree": [{"a": "U1.1", "b": "U2.2"}],
                "layer_assign": [{"a": "U1.1", "b": "U2.2", "layer": 0}],
            }
        },
        "planes": [
            {
                "layer": "In1.Cu",
                "net": "GND",
                "region_hint": None,
                "source": "authored",
            },
            {
                "layer": "In2.Cu",
                "net": "VCC",
                "region_hint": "core",
                "source": "derived",
            },
        ],
        "pin_swaps": [
            {"refdes": "U1", "pin": "1", "net": "GND", "source": "authored"},
            {"refdes": "U2", "pin": "2", "net": "VCC", "source": "derived"},
        ],
    }
    h_before = content_hash(_GRAPH, _PARAMS, session_state=before)
    h_after = content_hash(_GRAPH, _PARAMS, session_state=after)
    assert h_before == h_after


def test_digest_is_order_independent() -> None:
    state_a: dict[str, Any] = {
        "features": [
            {
                "ftype": "outline",
                "x": 0.0,
                "y": 0.0,
                "rot": 0,
                "layer": None,
                "fixed": True,
                "geom": {"pts": [[0, 0], [10, 0], [10, 10]]},
                "note": None,
            },
            {
                "ftype": "hole",
                "x": 5.0,
                "y": 5.0,
                "rot": 0,
                "layer": None,
                "fixed": True,
                "geom": {"r": 1.5},
                "note": "M3",
            },
        ],
        "routes": {
            "GND": {"topology": [{"a": "U1.1", "b": "U2.2", "side": 0}]},
            "VCC": {"topology": [{"a": "U1.2", "b": "U2.1", "side": 1}]},
        },
        "pin_swaps": [
            {"refdes": "U1", "pin": "1", "net": "GND", "source": "authored"},
            {"refdes": "U2", "pin": "2", "net": "VCC", "source": "authored"},
        ],
        "planes": [
            {
                "layer": "In1.Cu",
                "net": "GND",
                "region_hint": None,
                "source": "authored",
            },
            {
                "layer": "In2.Cu",
                "net": "VCC",
                "region_hint": "core",
                "source": "authored",
            },
        ],
        "measures": [
            {
                "metric": "length_mm",
                "direction": "min",
                "goal": None,
                "strength": 1.0,
                "weight": 1.0,
                "operands": ["GND"],
                "reason": "shorter is better",
            },
            {
                "metric": "crossings",
                "direction": "min",
                "goal": 0,
                "strength": 1.0,
                "weight": 1.0,
                "operands": [],
                "reason": None,
            },
        ],
        "fixed_copper": [
            {
                "ctype": "track",
                "layer": 0,
                "net": "GND",
                "generator_name": "ewod",
                "envelope": {"layers": 4},
            },
            {
                "ctype": "via",
                "layer": 1,
                "net": "VCC",
                "generator_name": "ewod",
                "envelope": {"layers": 4},
            },
        ],
    }

    # Same facts, every list reversed.
    def _rev(key: str) -> list[Any]:
        rows: list[Any] = state_a[key]
        return list(reversed(rows))

    routes: dict[str, Any] = state_a["routes"]
    state_b: dict[str, Any] = {
        "features": _rev("features"),
        "routes": dict(reversed(list(routes.items()))),
        "pin_swaps": _rev("pin_swaps"),
        "planes": _rev("planes"),
        "measures": _rev("measures"),
        "fixed_copper": _rev("fixed_copper"),
    }
    h_a = content_hash(_GRAPH, _PARAMS, session_state=state_a)
    h_b = content_hash(_GRAPH, _PARAMS, session_state=state_b)
    assert h_a == h_b
    # Stable across two identical reads too.
    assert h_a == content_hash(_GRAPH, _PARAMS, session_state=state_a)
