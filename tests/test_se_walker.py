"""``se-walker-light-protocol`` slice A — occupancy states, the station
settle's per-state pose, the state-aware chain/drc reads and the sweep.

The fixture is the item's dogfood in miniature: three single-stranded
stub-helix footholds standing up from a track, a rigid walker body with
two anchor ports, two tethered leg strands whose foot domains pair with
whichever stub the station puts them on. ``declare_stations`` writes the
hand-over-hand states; ``relax_chain(state=)`` settles the body per
station and stores its pose; every read then poses the walker from the
stored slot without re-running the settle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import precis_se
from precis.design import states as design_states
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.chain import nucleic
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: Stub footholds along +x. The far one is reachable by default; the
#: short-tether test pushes it out of a 12-nt tether's reach.
FOOTHOLD_X_M = (0.0, 6.0e-9, 12.0e-9)
FAR_X_M = 26.0e-9
STUB_UNITS = 8
FOOT_START = 4  # the foot domain pairs with the stub's top four units
TETHER_NT = 12


def _seed_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _seed_se_migrations(store)
    return SeHandler(hub=hub)


def _stub_ops(name: str, x_m: float) -> list[dict[str, Any]]:
    """A single-stranded stub helix standing along +z at ``x``."""
    length = (STUB_UNITS - 1) * nucleic.B_DNA_RISE_M
    return [
        {"op": "add_block", "name": name},
        {
            "op": "declare_helix",
            "block": name,
            "n_units": STUB_UNITS,
            "path": {
                "waypoints": [
                    [f"{x_m} m", "0 m", "0 m"],
                    [f"{x_m} m", "0 m", f"{length} m"],
                ]
            },
        },
        {"op": "add_block", "name": f"t{name}"},
        {"op": "declare_strand", "block": f"t{name}"},
        {
            "op": "add_domain",
            "strand": f"t{name}",
            "helix": name,
            "start": 0,
            "end": STUB_UNITS,
            "forward": True,
        },
    ]


def _walker_ops(
    *, tether_nt: int = TETHER_NT, stations: bool = True, far_x_m: float | None = None
) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    xs = list(FOOTHOLD_X_M)
    if far_x_m is not None:
        xs[-1] = far_x_m
    for i, x in enumerate(xs):
        ops += _stub_ops(f"f{i}", x)
    ops += [
        # The body: a 2×2×1 nm box hovering above the first two stubs, its
        # two anchor ports on the underside.
        {
            "op": "add_block",
            "name": "w",
            "envelope": "box:w2e-9d2e-9h1e-9",
            "pose": [3.0e-9, 0.0, 7.0e-9],
        },
        {"op": "add_port", "block": "w", "name": "pa", "pose": [-1.0e-9, 0.0, -0.5e-9]},
        {"op": "add_port", "block": "w", "name": "pb", "pose": [1.0e-9, 0.0, -0.5e-9]},
    ]
    for leg, port, foot in (("la", "pa", "f0"), ("lb", "pb", "f1")):
        ops += [
            {"op": "add_block", "name": leg},
            {
                "op": "declare_strand",
                "block": leg,
                "anchor": f"w.{port}",
                "tether_nt": tether_nt,
            },
            {
                "op": "add_domain",
                "strand": leg,
                "helix": foot,
                "start": FOOT_START,
                "end": STUB_UNITS,
                "forward": False,
            },
        ]
    ops.append({"op": "layout_chain"})
    if stations:
        ops.append(
            {
                "op": "declare_stations",
                "walker": "w",
                "legs": ["la", "lb"],
                "footholds": [f"f{i}@{FOOT_START}" for i in range(3)],
                "forward_driver": "405nm",
                "reverse_driver": "365nm",
            }
        )
    return ops


def _put(handler: SeHandler, slug: str, ops: list[dict[str, Any]]) -> str:
    return handler.put(id=slug, text=json.dumps({"ops": ops})).body


def _states(store: Store, slug: str, block: str) -> dict[str, design_states.BlockState]:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    uid = tree.blocks[block].uid
    assert uid is not None
    return {s.name: s for s in design_states.states_for(store, ref.id, uid)}


# ── declaring stations ──────────────────────────────────────────────────


def test_declare_stations_writes_hand_over_hand_states_and_light_edges(
    handler: SeHandler, store: Store
) -> None:
    _put(handler, "walk", _walker_ops())
    states = _states(store, "walk", "w")
    assert set(states) == {"st0", "st1"}
    assert states["st0"].occupancy == {"la.0": "f0@4", "lb.0": "f1@4"}
    assert states["st1"].occupancy == {"la.0": "f1@4", "lb.0": "f2@4"}
    assert states["st0"].pose is None  # derived, not yet settled
    body = handler.get(id="walk", view="block", args={"name": "w"}).body
    assert "st0" in body and "UNRELAXED" in body
    assert "405nm" in body and "365nm" in body
    ref = store.get_ref(kind="se", id="walk")
    assert ref is not None
    uid = persist.load_tree(store, ref.id).blocks["w"].uid
    assert uid is not None
    edges = {
        (t.from_state, t.to_state, t.driver_kind, t.driver_ref)
        for t in design_states.transitions_for(store, ref.id, uid)
    }
    assert edges == {
        ("st0", "st1", "light", "405nm"),
        ("st1", "st0", "light", "365nm"),
    }


def test_declare_states_vets_occupancy_and_refuses_an_authored_pose() -> None:
    tree = SeTree()
    apply_ops(tree, _walker_ops(stations=False))
    with pytest.raises(OpError, match="names no domain of strand 'la'"):
        apply_ops(
            tree,
            [
                {
                    "op": "declare_states",
                    "block": "w",
                    "states": [{"name": "x", "occupancy": {"la.3": "f0@4"}}],
                }
            ],
        )
    with pytest.raises(OpError, match="past the helix's 8 units"):
        apply_ops(
            tree,
            [
                {
                    "op": "declare_states",
                    "block": "w",
                    "states": [{"name": "x", "occupancy": {"la.0": "f0@6"}}],
                }
            ],
        )
    with pytest.raises(OpError, match="DERIVED"):
        apply_ops(
            tree,
            [
                {
                    "op": "declare_states",
                    "block": "w",
                    "states": [{"name": "x", "pose": {"xyz": [0, 0, 0]}}],
                }
            ],
        )
    with pytest.raises(OpError, match="need at least 2 footholds"):
        apply_ops(
            tree,
            [
                {
                    "op": "declare_stations",
                    "walker": "w",
                    "legs": ["la", "lb"],
                    "footholds": ["f0@4"],
                }
            ],
        )


def test_an_anchor_must_name_a_plain_body_block() -> None:
    tree = SeTree()
    apply_ops(tree, _stub_ops("f0", 0.0))
    apply_ops(tree, [{"op": "add_block", "name": "la"}])
    with pytest.raises(OpError, match="does not exist"):
        apply_ops(tree, [{"op": "declare_strand", "block": "la", "anchor": "w.pa"}])
    with pytest.raises(OpError, match="plain rigid body"):
        apply_ops(tree, [{"op": "declare_strand", "block": "la", "anchor": "f0.pa"}])
    with pytest.raises(OpError, match="needs an 'anchor'"):
        apply_ops(tree, [{"op": "declare_strand", "block": "la", "tether_nt": 3}])


# ── state-aware reads ───────────────────────────────────────────────────


def _paired_counts(body: str) -> dict[str, int]:
    """``{helix: paired offsets}`` off the chain view's helix table."""
    out: dict[str, int] = {}
    for line in body.splitlines():
        paired = re.search(r"(\d+) paired", line)
        helix = re.search(r"(?<![\w.#])(f\d)(?![\w\[])", line)
        if paired and helix:
            out[helix.group(1)] = int(paired.group(1))
    return out


def test_view_chain_with_a_state_reports_that_station(
    handler: SeHandler,
) -> None:
    _put(handler, "walk", _walker_ops())
    declared = _paired_counts(handler.get(id="walk", view="chain").body)
    assert declared == {"f0": 4, "f1": 4, "f2": 0}
    st0 = _paired_counts(
        handler.get(id="walk", view="chain", args={"state": {"w": "st0"}}).body
    )
    assert st0 == {"f0": 4, "f1": 4, "f2": 0}
    st1 = _paired_counts(
        handler.get(id="walk", view="chain", args={"state": {"w": "st1"}}).body
    )
    assert st1 == {"f0": 0, "f1": 4, "f2": 4}
    # The read is transient: the rows as declared come back unchanged.
    assert _paired_counts(handler.get(id="walk", view="chain").body) == declared


def test_a_free_leg_is_exempt_from_dangling_and_pairing(
    handler: SeHandler,
) -> None:
    ops = _walker_ops(stations=False) + [
        {
            "op": "declare_states",
            "block": "w",
            "states": [
                {"name": "lifted", "occupancy": {"la.0": None, "lb.0": "f1@4"}},
                {"name": "down", "occupancy": {"la.0": "f0@4", "lb.0": "f1@4"}},
            ],
        }
    ]
    _put(handler, "free", ops)
    lifted = handler.get(id="free", view="chain", args={"state": {"w": "lifted"}})
    assert _paired_counts(lifted.body) == {"f0": 0, "f1": 4, "f2": 0}
    drc = handler.get(id="free", view="drc", args={"state": {"w": "lifted"}}).body
    assert "chain_dangling_domain" not in drc


# ── the station settle ──────────────────────────────────────────────────


def _pose_of(handler: SeHandler, slug: str, state: str | None) -> list[float]:
    args: dict[str, Any] = {"name": "w"}
    if state is not None:
        args["state"] = {"w": state}
    body = handler.get(id=slug, view="block", args=args).body
    for line in body.splitlines():
        if line.startswith("pose:"):
            inner = line[line.index("[") + 1 : line.index("]")]
            return [float(v) for v in inner.split(",")]
    raise AssertionError(f"no pose line in\n{body}")


def test_station_settle_stores_a_per_state_pose_the_reads_apply_without_relaxing(
    handler: SeHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put(handler, "walk", _walker_ops())
    default = _pose_of(handler, "walk", None)
    for st in ("st0", "st1"):
        echo = handler.edit(
            id="walk", ops=[{"op": "relax_chain", "state": {"w": st}}]
        ).body
        assert (
            f"station settle: w@{st}" in echo and "1 per-state pose(s) stored" in echo
        )
        assert "2 leg tether(s)" in echo
    states = _states(store, "walk", "w")
    assert states["st0"].pose is not None and states["st1"].pose is not None
    p0 = np.asarray(states["st0"].pose["xyz"])
    p1 = np.asarray(states["st1"].pose["xyz"])
    # Hand-over-hand: the body follows its legs down the track (+x) —
    # as far as the rear tether needs to come within reach of its new
    # foothold (the springs are one-sided: a slack tether pulls nothing).
    assert p1[0] > p0[0] + 0.5e-9, (p0, p1)
    # The block's own default pose is untouched by station settles …
    assert _pose_of(handler, "walk", None) == default
    # … and a posed read applies the stored slot with zero settles.
    import precis_se.chain.relax as relax_mod

    calls: list[Any] = []

    def no_settle(*a: Any, **k: Any) -> Any:
        calls.append(a)
        raise AssertionError("a posed read must not settle")

    monkeypatch.setattr(relax_mod, "relax_bundle", no_settle)
    read0 = _pose_of(handler, "walk", "st0")
    read1 = _pose_of(handler, "walk", "st1")
    assert calls == []
    assert np.allclose(read0, p0, rtol=1e-5, atol=0.0)
    assert np.allclose(read1, p1, rtol=1e-5, atol=0.0)
    assert not np.allclose(read0, read1, rtol=0.0, atol=1.0e-11)
    # Re-declaring the stations keeps the relaxed poses.
    handler.edit(
        id="walk",
        ops=[
            {
                "op": "declare_stations",
                "walker": "w",
                "legs": ["la", "lb"],
                "footholds": [f"f{i}@{FOOT_START}" for i in range(3)],
            }
        ],
    )
    again = _states(store, "walk", "w")
    assert again["st0"].pose == states["st0"].pose
    assert again["st1"].pose == states["st1"].pose


def test_a_station_settle_needs_a_saved_design_and_a_declared_state(
    handler: SeHandler,
) -> None:
    with pytest.raises(BadInput, match="SAVED design"):
        _put(
            handler,
            "fresh",
            _walker_ops() + [{"op": "relax_chain", "state": {"w": "st0"}}],
        )
    _put(handler, "walk", _walker_ops())
    with pytest.raises(BadInput, match="no state 'st9' — declared: st0, st1"):
        handler.edit(id="walk", ops=[{"op": "relax_chain", "state": {"w": "st9"}}])
    with pytest.raises(BadInput, match="chain helix"):
        handler.edit(
            id="walk",
            ops=[
                {
                    "op": "declare_states",
                    "block": "f0",
                    "states": [{"name": "a"}, {"name": "b"}],
                }
            ],
        )
        handler.edit(id="walk", ops=[{"op": "relax_chain", "state": {"f0": "a"}}])


# ── the sweep ───────────────────────────────────────────────────────────


def test_sweep_reports_unrelaxed_stations_then_checks_each_settled_one(
    handler: SeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put(handler, "walk", _walker_ops())
    body = handler.get(id="walk", view="sweep").body
    assert "2 UNRELAXED (chain_state_unrelaxed)" in body
    assert body.count("chain_state_unrelaxed") >= 3  # verdict + one row per station
    for st in ("st0", "st1"):
        handler.edit(id="walk", ops=[{"op": "relax_chain", "state": {"w": st}}])
    import precis_se.handler as handler_mod

    overlaps = handler_mod.se_validate.envelope_overlaps
    chain = handler_mod.se_chain_drc.findings
    seen = {"overlaps": 0, "chain": 0}

    def spy_overlaps(*a: Any, **k: Any) -> Any:
        seen["overlaps"] += 1
        return overlaps(*a, **k)

    def spy_chain(*a: Any, **k: Any) -> Any:
        seen["chain"] += 1
        return chain(*a, **k)

    monkeypatch.setattr(handler_mod.se_validate, "envelope_overlaps", spy_overlaps)
    monkeypatch.setattr(handler_mod.se_chain_drc, "findings", spy_chain)
    body = handler.get(id="walk", view="sweep").body
    assert seen == {"overlaps": 2, "chain": 2}
    assert "2/2 combination(s) checked" in body
    assert "chain_state_unrelaxed" not in body
    assert "no interference in any checked state" in body


def test_a_short_tether_fails_only_the_far_station_as_chain_loop_short(
    handler: SeHandler,
) -> None:
    _put(handler, "short", _walker_ops(tether_nt=TETHER_NT, far_x_m=FAR_X_M))
    for st in ("st0", "st1"):
        handler.edit(id="short", ops=[{"op": "relax_chain", "state": {"w": st}}])
    near = handler.get(id="short", view="drc", args={"state": {"w": "st0"}}).body
    far = handler.get(id="short", view="drc", args={"state": {"w": "st1"}}).body
    assert "chain_loop_short" not in near
    assert "chain_loop_short" in far and "tether" in far
    sweep = handler.get(id="short", view="sweep").body
    assert "1 failing combination(s)" in sweep
    assert "w=st1" in sweep and "tether" in sweep
