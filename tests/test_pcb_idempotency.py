"""Unit tests for :func:`precis.pcb.session.content_hash` (gr266041) — the
``op=`` idempotency digest must cover every store surface the
``pcb_place``/``pcb_route`` workers actually read, but must NOT cover the
fields those same jobs write back (else a completed job's own checkpoint
mints a fresh duplicate on the next identical resubmit). No DB — every
case constructs the raw store-row shapes by hand.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, cast

from precis.pcb.session import content_hash

if TYPE_CHECKING:
    from precis.store import Store

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


def test_soft_pin_changes_the_digest() -> None:
    pinned: dict[str, Any] = copy.deepcopy(_GRAPH)
    pinned["instances"][0]["max_travel_mm"] = 2.0
    assert content_hash(pinned, _PARAMS) != content_hash(_GRAPH, _PARAMS)


def test_pour_nets_participate_only_when_set() -> None:
    """gr486958: ``pour_nets`` rides the stackup entry — a board whose
    fills all carry a net is different routing input than the same
    entries without the key, while a pour-free stackup keeps its
    pre-key shape (the pinned digest above carries no key)."""
    poured: dict[str, Any] = copy.deepcopy(_GRAPH)
    poured["board"]["stackup"][3]["pour_nets"] = ["GND"]
    assert content_hash(poured, _PARAMS) != content_hash(_GRAPH, _PARAMS)
    # And the imported form — the key on a signal-role entry — likewise.
    imported: dict[str, Any] = copy.deepcopy(_GRAPH)
    imported["board"]["stackup"] = [
        {"name": "F.Cu", "role": "signal"},
        {"name": "In1.Cu", "role": "plane", "plane_net": "GND"},
        {"name": "In2.Cu", "role": "signal"},
        {"name": "B.Cu", "role": "signal", "pour_nets": ["GND"]},
    ]
    assert content_hash(imported, _PARAMS) != content_hash(_GRAPH, _PARAMS)


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


# --- one load_snapshot for the digest and the workers (gr266041) -----------

# Keys `PcbHandler._enqueue_op` hand-listed in `session_state` before
# `DesignSnapshot.session_state()` replaced the literal.
_ENQUEUE_OP_SESSION_KEYS = {"features", "routes", "planes", "measures", "fixed_copper"}


class _FakeStore:
    def __init__(self, board_id: int | None) -> None:
        self.board_id = board_id
        self.fixed_calls: list[int] = []

    def pcb_graph(self, ref_id: int) -> dict[str, Any]:
        graph: dict[str, Any] = copy.deepcopy(_GRAPH)
        board: dict[str, Any] = dict(graph["board"])
        board["board_id"] = self.board_id
        graph["board"] = board
        return graph

    def pcb_features_list(self, ref_id: int) -> list[Any]:
        return [{"kind": "outline"}]

    def pcb_routes_get(self, ref_id: int) -> dict[str, Any]:
        return {"GND": {"topology": "pin_side"}}

    def pcb_planes_list(self, ref_id: int) -> list[Any]:
        return [{"net": "GND", "layer": "In1.Cu", "source": "authored"}]

    def pcb_measures_list(self, ref_id: int) -> list[Any]:
        return [{"metric": "proximity"}]

    def pcb_fixed_copper_list(self, board_id: int) -> list[Any]:
        self.fixed_calls.append(board_id)
        return [{"fixed_id": 1}]


def test_session_state_has_exactly_the_keys_enqueue_op_used() -> None:
    from precis.pcb.session_store import load_snapshot

    snap = load_snapshot(cast("Store", _FakeStore(board_id=7)), 1)
    state = snap.session_state()
    assert set(state) == _ENQUEUE_OP_SESSION_KEYS
    assert state["fixed_copper"] == [{"fixed_id": 1}]
    # The digest the snapshot feeds is the one the hand-built dict gave.
    assert content_hash(snap.graph, _PARAMS, session_state=state) == content_hash(
        snap.graph,
        _PARAMS,
        session_state={
            "features": [{"kind": "outline"}],
            "routes": {"GND": {"topology": "pin_side"}},
            "planes": [{"net": "GND", "layer": "In1.Cu", "source": "authored"}],
            "measures": [{"metric": "proximity"}],
            "fixed_copper": [{"fixed_id": 1}],
        },
    )


def test_load_snapshot_boardless_has_empty_fixed_copper() -> None:
    from precis.pcb.session_store import load_snapshot

    store = _FakeStore(board_id=None)
    snap = load_snapshot(cast("Store", store), 1)
    assert snap.fixed_copper == []
    assert store.fixed_calls == []


# Direct hashed-surface reads a worker may keep, as {file: {call: reason}}.
# Empty today: both workers take these surfaces from `load_snapshot`. A new
# entry must say why the re-read is deliberate (e.g. after the job's own
# write), or the digest and the worker's inputs drift apart again.
_DIRECT_READ_ALLOWLIST: dict[str, dict[str, str]] = {}


def test_workers_read_hashed_surfaces_through_load_snapshot() -> None:
    import re
    from pathlib import Path

    import precis.workers.job_types.pcb_place as place_mod

    direct = re.compile(
        r"store\.(pcb_(?:graph|features_list|routes_get|planes_list"
        r"|measures_list|fixed_copper_list))\("
    )
    for mod in (place_mod,):
        path = Path(mod.__file__ or "")
        src = path.read_text(encoding="utf-8")
        assert "load_snapshot(" in src, f"{path.name} must call load_snapshot("
        allowed = _DIRECT_READ_ALLOWLIST.get(path.name, {})
        for call in direct.findall(src):
            assert call in allowed, (
                f"{path.name} reads store.{call}( directly; take it from "
                "load_snapshot or add a reasoned _DIRECT_READ_ALLOWLIST entry"
            )
        for call, reason in allowed.items():
            assert reason.strip(), f"allowlist entry {call} needs a reason"
