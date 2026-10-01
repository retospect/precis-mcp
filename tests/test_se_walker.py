"""``se-walker-light-protocol`` slice A — occupancy states, the station
settle's per-state pose, the state-aware chain/drc reads and the sweep.

The fixture is the item's dogfood in miniature: three single-stranded
stub-helix footholds standing up from a track, a rigid walker body with
two anchor ports, two tethered leg strands whose foot domains pair with
whichever stub the station puts them on. ``declare_stations`` writes the
hand-over-hand states; ``relax_chain(state=)`` settles the body per
station and stores its pose; every read then poses the walker from the
stored slot without re-running the settle.

Slice B (the tail of this file): the ratchet guard on a transition
(``params.guard`` → ``chain_transition_guard``), the spectral channel
budget (``chain_channel_budget`` / ``chain_spectral_crosstalk``, the
photoswitch item's DRC) and ``make_steps``, the transitions as an ordered
``make`` tree.
"""

from __future__ import annotations

import json
import re
from typing import Any

import numpy as np
import pytest

from precis.design import states as design_states
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.make import MakeHandler
from precis.handlers.material import MaterialHandler
from precis.store import Store
from precis_se import compose as se_compose
from precis_se import persist
from precis_se.chain import nucleic
from precis_se.chain import spectral as chain_spectral
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops

#: Stub footholds along +x. The far one is reachable by default; the
#: short-tether test pushes it out of a 12-nt tether's reach.
FOOTHOLD_X_M = (0.0, 6.0e-9, 12.0e-9)
FAR_X_M = 26.0e-9
STUB_UNITS = 8
FOOT_START = 4  # the foot domain pairs with the stub's top four units
TETHER_NT = 12


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
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
    # … the block view says whose pose it shows (gr458145) …
    posed_body = handler.get(
        id="walk", view="block", args={"name": "w", "state": {"w": "st1"}}
    ).body
    assert "(state 'st1', STORED by relax_chain(state=)" in posed_body
    assert "(authored, parent frame" not in posed_body
    plain_body = handler.get(id="walk", view="block", args={"name": "w"}).body
    assert "(authored, parent frame" in plain_body
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


# ── slice B: the ratchet guard ──────────────────────────────────────────


def _edges(block: str, edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"op": "declare_transitions", "block": block, "transitions": edges}


def _light(from_state: str, to_state: str, ref: str, **params: Any) -> dict[str, Any]:
    return {
        "from_state": from_state,
        "to_state": to_state,
        "driver_kind": "light",
        "driver_ref": ref,
        "params": params,
    }


def test_a_guard_is_vetted_like_an_occupancy() -> None:
    tree = SeTree()
    apply_ops(tree, _walker_ops())

    def declare(guard: Any) -> list[dict[str, Any]]:
        return [_edges("w", [_light("st0", "st1", "405nm", guard=guard)])]

    with pytest.raises(OpError, match="names no domain row"):
        apply_ops(tree, declare({"lx.0": "bound"}))
    with pytest.raises(OpError, match="occupancy target"):
        apply_ops(tree, declare({"lb.0": "sometimes"}))
    with pytest.raises(OpError, match="does not exist"):
        apply_ops(tree, declare({"lb.0": "f9@4"}))
    with pytest.raises(OpError, match="non-empty JSON object"):
        apply_ops(tree, declare([]))
    apply_ops(tree, declare({"lb.0": "Bound", "la.0": "f0@4"}))
    pending = tree.blocks["w"].pending_transitions
    assert pending is not None
    assert pending[0]["params"]["guard"] == {"lb.0": "bound", "la.0": "f0@4"}


def test_a_violated_ratchet_guard_is_a_drc_error(handler: SeHandler) -> None:
    # st0 binds la.0 → f0@4 and lb.0 → f1@4; st1 binds la.0 → f1@4, lb.0 → f2@4.
    ops = _walker_ops() + [
        _edges(
            "w",
            [
                # violated: lb.0 is bound at st0
                _light("st0", "st1", "405nm", guard={"lb.0": "free"}),
                # holds: la.0 IS at f1@4 in st1
                _light("st1", "st0", "365nm", guard={"la.0": "f1@4"}),
            ],
        )
    ]
    _put(handler, "guard", ops)
    body = handler.get(id="guard", view="drc").body
    assert body.count("chain_transition_guard") == 1, body
    assert "w st0→st1" in body
    assert "lb.0 is f1@4, guard wants free" in body

    # A guard that holds everywhere leaves the drc quiet on the rule.
    _put(
        handler,
        "guard-ok",
        _walker_ops()
        + [_edges("w", [_light("st0", "st1", "405nm", guard={"lb.0": "bound"})])],
    )
    assert "chain_transition_guard" not in handler.get(id="guard-ok", view="drc").body


# ── slice B: the spectral channel budget ────────────────────────────────


def _switch(name: str, refs: tuple[str, str]) -> list[dict[str, Any]]:
    """A plain two-state block driven by two light channels."""
    return [
        {"op": "add_block", "name": name, "envelope": "box:w1e-9d1e-9h1e-9"},
        {
            "op": "declare_states",
            "block": name,
            "states": [{"name": "a"}, {"name": "b"}],
        },
        _edges(name, [_light("a", "b", refs[0]), _light("b", "a", refs[1])]),
    ]


def test_wavelength_parsing_and_the_gaussian_crosstalk() -> None:
    parse = chain_spectral.parse_wavelength_nm
    assert parse("405nm") == 405.0  # exactly: the m→nm noise is rounded away
    assert parse("405 nm") == pytest.approx(405.0)
    assert parse("0.405 um") == pytest.approx(405.0)
    assert parse("405") == pytest.approx(405.0)
    assert parse("blue LED") is None
    assert parse(None) is None
    band = chain_spectral.Band(centre_nm=470.0, fwhm_nm=40.0, source="test")
    assert chain_spectral.crosstalk(470.0, band) == pytest.approx(1.0)
    assert chain_spectral.crosstalk(450.0, band) == pytest.approx(0.5, abs=1e-6)
    assert chain_spectral.crosstalk(370.0, band) < 1e-6


def test_the_channel_budget_is_design_wide_and_crosstalk_names_the_pair(
    handler: SeHandler,
) -> None:
    # The walker spends 405nm + 365nm; two more switches spend four: six
    # channels against a budget of four. 450 vs 470 at the coded 40 nm FWHM
    # is 50 % direct excitation; the walker's own 365 vs 405 is 6 %.
    ops = (
        _walker_ops()
        + [{"op": "set_optics", "medium_index": 1.33, "channels_available": 4}]
        + _switch("sw1", ("450nm", "470nm"))
        + _switch("sw2", ("500nm", "520nm"))
    )
    _put(handler, "budget", ops)
    body = handler.get(id="budget", view="drc").body
    assert "chain_channel_budget" in body
    assert "6 independently addressed channel(s)" in body
    assert "> 4 available" in body
    assert "chain_spectral_crosstalk" in body
    assert "450nm ↔ 470nm" in body
    assert "50% of its own peak" in body
    assert "centre ASSUMED at the pump" in body
    assert "365nm ↔ 405nm" not in body

    # No authored budget → no budget row; the crosstalk pair stands.
    _put(handler, "nobudget", _walker_ops() + _switch("sw1", ("450nm", "470nm")))
    quiet = handler.get(id="nobudget", view="drc").body
    assert "chain_channel_budget" not in quiet
    assert "450nm ↔ 470nm" in quiet

    with pytest.raises(BadInput, match="channels_available"):
        _put(
            handler,
            "badbudget",
            _walker_ops()
            + [{"op": "set_optics", "medium_index": 1.33, "channels_available": 0}],
        )


def test_a_material_band_replaces_the_assumed_one(
    handler: SeHandler, store: Store, hub: Hub
) -> None:
    _put(handler, "band", _walker_ops() + _switch("sw1", ("450nm", "470nm")))
    material = MaterialHandler(hub=hub)
    material.put(id="mat-band", title="mat-band")
    material.put(id="mat-band", property=se_compose.FWHM_KEY, value=10, unit="nm")
    material.put(
        id="mat-band", property=se_compose.LAMBDA_MAX_KEY, value=455, unit="nm"
    )
    design_ref = store.get_ref(kind="se", id="band")
    mat_ref = store.get_ref(kind="material", id="mat-band")
    assert design_ref is not None and mat_ref is not None
    store.add_link(
        src_ref_id=design_ref.id,
        dst_ref_id=mat_ref.id,
        relation="made-of",
        meta={"block": "sw1"},
    )
    tree = persist.load_tree(store, design_ref.id)
    tree.own_slug = "band"  # load_tree leaves it unset; the handler sets it
    band_of = chain_spectral.band_resolver(store, tree)
    used = {
        c.label: c
        for c in chain_spectral.channels(
            {
                "sw1": design_states.transitions_for(
                    store, design_ref.id, tree.blocks["sw1"].uid or 0
                )
            }
        )
    }
    band = band_of(used["450nm"])
    assert band.fwhm_nm == pytest.approx(10.0)
    assert band.centre_nm == pytest.approx(455.0)
    assert "from a material row" in band.source
    # The band is the BLOCK's (one lambda_max/fwhm per block), so both of
    # sw1's channels read it: the finding now quotes the material figures
    # instead of the assumption.
    rows = chain_spectral.findings(
        list(used.values()), channels_available=None, band_of=band_of
    )
    assert [r.rule for r in rows] == ["chain_spectral_crosstalk"]
    assert "FWHM 10 nm from a material row" in rows[0].detail
    assert "ASSUMED" not in rows[0].detail
    # The walker's own channels have no material row and say so.
    w_uid = tree.blocks["w"].uid or 0
    walker = chain_spectral.channels(
        {"w": design_states.transitions_for(store, design_ref.id, w_uid)}
    )
    assert "ASSUMED" in band_of(walker[0]).source


# ── slice B: make_steps ─────────────────────────────────────────────────


def test_make_steps_writes_an_ordered_make_tree_linked_to_the_design(
    handler: SeHandler, store: Store, hub: Hub
) -> None:
    with pytest.raises(BadInput, match="SAVED design"):
        _put(handler, "fresh", _walker_ops() + [{"op": "make_steps", "block": "w"}])
    # A switch: a → b → c by light, c → a thermally (untaken from a: a is
    # already visited; taken from b, where it is the way onward).
    ops = _walker_ops() + [
        {"op": "add_block", "name": "sw", "envelope": "box:w1e-9d1e-9h1e-9"},
        {
            "op": "declare_states",
            "block": "sw",
            "states": [
                {"name": "a"},
                {"name": "b", "descr": "half open"},
                {"name": "c"},
                {"name": "d"},  # no edge leaves it
            ],
        },
        _edges(
            "sw",
            [
                _light("a", "b", "450nm", duration_s=30),
                _light("b", "c", "470nm"),
                {"from_state": "c", "to_state": "a", "driver_kind": "thermal"},
            ],
        ),
    ]
    _put(handler, "proto", ops)
    echo = handler.edit(id="proto", ops=[{"op": "make_steps", "block": "sw"}]).body
    assert "make:proto-sw-protocol" in echo
    assert "a → b → c" in echo
    body = MakeHandler(hub=hub).get(id="proto-sw-protocol").body
    assert "2 steps" in body
    first = body.index("step 1: illuminate at 450nm")
    second = body.index("step 2: illuminate at 470nm")
    assert first < second
    assert "half open" in body
    assert "wavelength_nm" in body and "450.0" in body and "duration_s" in body
    assert "station=b" in body and "station=c" in body
    assert "makes:" in body  # the design's made-by edge
    make_ref = store.get_ref(kind="make", id="proto-sw-protocol")
    assert make_ref is not None
    incoming = store.links_for(make_ref.id, direction="in", relation="made-by")
    design_ref = store.get_ref(kind="se", id="proto")
    assert design_ref is not None
    assert [lk.src_ref_id for lk in incoming] == [design_ref.id]

    # Re-running would duplicate the steps: refused, a fresh slug works.
    with pytest.raises(BadInput, match="already exists"):
        handler.edit(id="proto", ops=[{"op": "make_steps", "block": "sw"}])
    again = handler.edit(
        id="proto",
        ops=[{"op": "make_steps", "block": "sw", "start": "b", "make": "p2"}],
    ).body
    assert "2 step(s)" in again and "b → c → a" in again
    assert "step 2: thermal" in MakeHandler(hub=hub).get(id="p2").body
    with pytest.raises(BadInput, match="no transition leaves"):
        handler.edit(
            id="proto",
            ops=[{"op": "make_steps", "block": "sw", "start": "d", "make": "p3"}],
        )
    # The walker's own stations: one forward step, the reverse edge untaken.
    walker = handler.edit(id="proto", ops=[{"op": "make_steps", "block": "w"}]).body
    assert "1 step(s)" in walker and "st0 → st1" in walker


# ── slice C: view='stations' ────────────────────────────────────────────


def _distances(body: str) -> dict[str, str]:
    """``{state: 'to target' cell}`` off the stations table — the one
    length-with-unit cell in a row."""
    out: dict[str, str] = {}
    for line in body.splitlines():
        m = re.match(r"\s*(st\d)\b", line)
        if not m:
            continue
        cell = re.search(r"(\d+(?:\.\d+)? [a-zµ]*m)\b", line)
        out[m.group(1)] = cell.group(1) if cell else "—"
    return out


def test_view_stations_reports_the_cursor_per_settled_station(
    handler: SeHandler, store: Store
) -> None:
    ops = _walker_ops() + [
        # A cursor on the body's top face pointing up, and a feedstock site
        # above the far foothold pointing down at the track.
        {
            "op": "add_port",
            "block": "w",
            "name": "cursor",
            "pose": [0.0, 0.0, 0.5e-9],
            "direction": [0.0, 0.0, 1.0],
        },
        {
            "op": "add_block",
            "name": "site",
            "envelope": "box:w1e-9d1e-9h1e-9",
            "pose": [12.0e-9, 0.0, 12.0e-9],
        },
        {
            "op": "add_port",
            "block": "site",
            "name": "s",
            "pose": [0.0, 0.0, -0.5e-9],
            "direction": [0.0, 0.0, -1.0],
        },
    ]
    _put(handler, "stn", ops)
    args: dict[str, Any] = {"walker": "w", "target": "site.s"}
    before = handler.get(id="stn", view="stations", args=args).body
    assert before.count("UNRELAXED") >= 2, before
    assert "2 station(s) UNRELAXED" in before
    assert _distances(before) == {"st0": "—", "st1": "—"}

    for st in ("st0", "st1"):
        handler.edit(id="stn", ops=[{"op": "relax_chain", "state": {"w": st}}])
    after = handler.get(id="stn", view="stations", args=args).body
    assert "\tUNRELAXED" not in after  # the header sentence still names it
    assert "cursor w.cursor" in after and "target site.s" in after
    dist = _distances(after)
    assert set(dist) == {"st0", "st1"} and "—" not in dist.values()
    assert dist["st0"] != dist["st1"]  # the cursor moved with the walker
    # Cursor up, site port down: head-on at st0 (the authored pose); the
    # settle at st1 rolls the body, so the approach opens — but stays a
    # number, one per station.
    angles = re.findall(r"\t(\d+)°", after)
    assert len(angles) == 2 and angles[0] == "0", after
    assert 0 <= int(angles[1]) < 90
    # A layout_chain helix end is a target before any atoms exist
    # (gr458316): the segment's 3p port carries its backbone-exit pose and
    # an outward direction, so every settled station gets a distance and
    # an approach angle against it.
    end = handler.get(
        id="stn", view="stations", args={"walker": "w", "target": "f2.s0.3p"}
    ).body
    assert "target f2.s0.3p" in end
    end_dist = _distances(end)
    assert set(end_dist) == {"st0", "st1"} and "—" not in end_dist.values()
    assert len(re.findall(r"\t(\d+)°", end)) == 2

    # No cursor named and none called 'cursor' → walker geometry only.
    _put(handler, "bare", _walker_ops())
    bare = handler.get(id="bare", view="stations", args={"walker": "w"}).body
    assert "walker xyz" in bare and "cursor" not in bare.splitlines()[0]

    with pytest.raises(BadInput, match="requires args"):
        handler.get(id="stn", view="stations")
    with pytest.raises(BadInput, match="no port 'nose'"):
        handler.get(id="stn", view="stations", args={"walker": "w", "cursor": "nose"})
    with pytest.raises(BadInput, match="'<block>.<port>'"):
        handler.get(id="stn", view="stations", args={"walker": "w", "target": "site"})
    with pytest.raises(BadInput, match="no declared states"):
        handler.get(id="stn", view="stations", args={"walker": "site"})
