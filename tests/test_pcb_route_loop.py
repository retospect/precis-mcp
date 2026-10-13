"""Tests for the anneal's route-feedback outer loop —
docs/backlog/pcb-anneal-closes-router-loop.md.

Unit half is DB-free (same synthetic-graph discipline as
``test_pcb_optimize.py``): the priced expansion moves (OUTLINE_GROW /
LAYER_ADD) and their cap/price refusals, the router-feedback margin
entries, and the budgeted loop + constraint-conflict digest against a
scripted ``route`` callable.

The slow half runs the loop on the authored USB-C PD Nano fixture
(``tests/fixtures/pcb/nano_usb_pd_testboard.py``, the doc's acceptance
board) end to end through ``realize()`` and asserts the digest shape
and that the routed count does not regress — the pinned baseline lives
in ``tests/test_pcb_nano_usb_pd_testboard.py`` and is only re-pinned if
this loop changes IT, which it does not: the loop is a separate entry
point, and DEFAULT_SCHEDULE/money channels are untouched unless the
caller opts in.
"""

from __future__ import annotations

import random

import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.cost import CostConfig
from precis.pcb.ir import from_graph
from precis.pcb.optimize import (
    MOVE_GENERATORS,
    DemandCell,
    ExpansionCaps,
    Move,
    MoveKind,
    OptimizeConfig,
    OptimizeEngine,
    RouteFailure,
    RouteLoopConfig,
    RouteOutcome,
    anneal_route_loop,
    conflict_digest_text,
    seed_placement,
)

_TWO_LAYER = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "B.Cu", "role": "signal"},
]

_CAPS = ExpansionCaps(
    outline_max_mm=(60.0, 60.0), max_copper_layers=4, max_fab_usd=500.0
)


def _board(n: int = 8) -> dict:
    """Same shape test_pcb_optimize's `_board` builds: a chain net plus a
    fanout net so segments exist for feedback tests."""
    instances = [{"refdes": f"U{i}"} for i in range(n)]
    nets = [
        {
            "name": f"N{i}",
            "net_class": "signal",
            "domain": "electrical",
            "members": [
                {"refdes": f"U{i}", "pin": f"n{i}a"},
                {"refdes": f"U{i + 1}", "pin": f"n{i}b"},
            ],
        }
        for i in range(n - 1)
    ]
    return {"instances": instances, "nets": nets}


def _ir(n: int = 8, *, outline=(-20.0, -20.0, 20.0, 20.0), stackup=None, seed=0):
    ir = from_graph(_board(n), stackup=stackup or [dict(e) for e in DEFAULT_STACKUP])
    if outline is not None:
        x0, y0, x1, y1 = outline
        ir.outline = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    seed_placement(ir, random.Random(seed))
    return ir


def _engine(ir, **kw):
    return OptimizeEngine(ir, OptimizeConfig(**kw))


def test_expansion_generators_inert_without_caps():
    """Missing caps are a refusal, not infinity: both generators decline
    even when called directly, on a board they could otherwise act on."""
    ir = _ir()
    engine = _engine(ir)
    rng = random.Random(0)
    assert MOVE_GENERATORS[MoveKind.OUTLINE_GROW](engine, rng, 5.0) is None
    assert MOVE_GENERATORS[MoveKind.LAYER_ADD](engine, rng, 5.0) is None
    assert engine.expansion_status() == "disabled:no-caps"
    assert engine._money_outline_area == 0.0 and engine._money_layer_add == 0.0


def test_outline_grow_applies_prices_and_undoes_exactly():
    ir = _ir()
    engine = _engine(ir, expansion=_CAPS)
    assert engine.expansion_status() == "available"
    rate = engine.config.cost.board_area_usd_per_mm2
    before_money = engine.money()
    assert engine._money_outline_area == pytest.approx(40.0 * 40.0 * rate)

    rng = random.Random(1)
    move = MOVE_GENERATORS[MoveKind.OUTLINE_GROW](engine, rng, 5.0)
    assert move is not None and move.kind == MoveKind.OUTLINE_GROW
    engine.apply_move(move)
    grown = outline = ir.outline
    assert grown != list(move.old_outline)
    x0, y0, x1, y1 = (
        min(p[0] for p in grown),
        min(p[1] for p in grown),
        max(p[0] for p in grown),
        max(p[1] for p in grown),
    )
    # Exactly one dimension grew by the priced delta.
    assert engine._money_outline_area == pytest.approx((x1 - x0) * (y1 - y0) * rate)
    assert engine.money() > before_money

    engine.undo_move(move)
    assert ir.outline == list(move.old_outline)
    assert engine._money_outline_area == pytest.approx(40.0 * 40.0 * rate)
    assert engine.money() == pytest.approx(before_money)


def test_outline_grow_refuses_dim_cap_and_records():
    ir = _ir()
    tight = ExpansionCaps((41.0, 41.0), 4, 500.0)
    engine = _engine(ir, expansion=tight)
    rng = random.Random(2)
    for _ in range(20):
        assert MOVE_GENERATORS[MoveKind.OUTLINE_GROW](engine, rng, 5.0) is None
    assert any("outline_max_mm" in r for r in engine._expansion_refusals)


def test_outline_grow_refuses_unavailable_price_never_zero():
    ir = _ir()
    engine = _engine(
        ir,
        expansion=_CAPS,
        cost=CostConfig(board_area_usd_per_mm2=0.0),
    )
    rng = random.Random(3)
    assert MOVE_GENERATORS[MoveKind.OUTLINE_GROW](engine, rng, 5.0) is None
    assert engine.expansion_status().startswith("unavailable-price")
    assert any("unavailable-price" in r for r in engine._expansion_refusals)


def test_outline_grow_refuses_edge_mate_edge():
    """An edge carrying a required edge-mated part is never grown."""
    graph = _board(6)
    graph["instances"][0]["edge_mate"] = {"face": "S", "require": "required"}
    ir = from_graph(graph, stackup=[dict(e) for e in DEFAULT_STACKUP])
    ir.outline = [(-20.0, -20.0), (20.0, -20.0), (20.0, 20.0), (-20.0, 20.0)]
    seed_placement(ir, random.Random(0))
    engine = _engine(ir, expansion=_CAPS)
    # The snapped part's face points -y (S) => edge 2 protected.
    assert 2 in engine._edge_mate_edges


def test_layer_add_2to4_symmetric_and_undo():
    ir = _ir(stackup=[dict(e) for e in _TWO_LAYER])
    engine = _engine(ir, expansion=_CAPS)
    old_layers = {int(ir.seg_layer[s]) for s in range(ir.n_segments)}
    rng = random.Random(4)
    move = MOVE_GENERATORS[MoveKind.LAYER_ADD](engine, rng, 5.0)
    assert move is not None and move.kind == MoveKind.LAYER_ADD
    money_before = engine.money()
    engine.apply_move(move)
    assert ir.n_layers == 4
    assert [ly["name"] for ly in ir.stackup] == [
        "F.Cu",
        "In1.Cu",
        "In2.Cu",
        "B.Cu",
    ]
    # B.Cu segs remapped 1 -> 3; nothing sits on the new pair.
    assert {int(ir.seg_layer[s]) for s in range(ir.n_segments)} == {
        (ly + 2 if ly >= 1 else ly) for ly in old_layers
    }
    assert engine._money_layer_add == pytest.approx(2.0 * engine.config.cost.layer_usd)
    assert engine.money() > money_before
    # The new layers are routable signal layers.
    assert set(engine._signal_layers) == {0, 1, 2, 3}

    engine.undo_move(move)
    assert ir.n_layers == 2
    assert [ly["name"] for ly in ir.stackup] == ["F.Cu", "B.Cu"]
    assert {int(ir.seg_layer[s]) for s in range(ir.n_segments)} == old_layers
    assert engine._money_layer_add == 0.0
    assert engine.money() == pytest.approx(money_before)


def test_layer_add_refuses_illegal_stack_on_4layer():
    """4 + 2 = 6 layers has no capability row — refused, not made valid
    by a low cost (the cap must allow it first, so this test's cap
    does)."""
    ir = _ir()  # DEFAULT_STACKUP: 4 layers
    caps = ExpansionCaps((60.0, 60.0), 8, 500.0)
    engine = _engine(ir, expansion=caps)
    rng = random.Random(5)
    for _ in range(10):
        assert MOVE_GENERATORS[MoveKind.LAYER_ADD](engine, rng, 5.0) is None
    assert any("illegal-stack" in r for r in engine._expansion_refusals)


def test_layer_add_refuses_layer_cap():
    ir = _ir(stackup=[dict(e) for e in _TWO_LAYER])
    caps = ExpansionCaps((60.0, 60.0), 3, 500.0)
    engine = _engine(ir, expansion=caps)
    rng = random.Random(6)
    assert MOVE_GENERATORS[MoveKind.LAYER_ADD](engine, rng, 5.0) is None
    assert any("max_copper_layers" in r for r in engine._expansion_refusals)


def test_route_feedback_penalises_failed_seg_and_clears_on_move():
    ir = _ir()
    engine = _engine(ir)
    seg = 0
    a, b = int(ir.seg_pin_a[seg]), int(ir.seg_pin_b[seg])
    ia = int(ir.pin_instance[a])
    x, y = float(ir.inst_x[ia]), float(ir.inst_y[ia])
    money_before = engine.money()
    outcome = RouteOutcome(
        routed=0,
        total=ir.n_segments,
        complete=False,
        failures=(
            RouteFailure(
                net=str(ir.net_name[int(ir.seg_net[seg])]),
                cause="congestion",
                points=((x, y),),
                seg_id=seg,
                detail="lost the corridor",
            ),
        ),
    )
    engine.apply_route_feedback(outcome)
    # The implicated cell now carries a hard margin penalty on this seg.
    assert ("route_failure", seg) in engine._margin
    assert engine._margin[("route_failure", seg)].raw > 0.0
    assert engine.risk() > 0.0
    # Routing-demand units stay out of USD: money() did not move.
    assert engine.money() == pytest.approx(money_before)
    # The digest aggregates it to a RegionEntry.
    regions = {r.region: r for r in engine.digest().regions}
    assert any(r.peak_term == "route_failure" for r in regions.values())

    # Move BOTH endpoint instances several cells over: the segment no
    # longer spans its implicated cell at all, and the penalty clears —
    # the evidence steers, it is not a constant.
    ib = int(ir.pin_instance[b])
    xb, yb = float(ir.inst_x[ib]), float(ir.inst_y[ib])
    cell = engine.config.region_cell_mm
    dx, dy = 3 * cell + 1.0, 2 * cell + 1.0
    engine.apply_move(
        Move(
            MoveKind.TRANSLATE,
            (ia, ib),
            ((x, y, float(ir.inst_rot[ia])), (xb, yb, float(ir.inst_rot[ib]))),
            (
                (x + dx, y + dy, float(ir.inst_rot[ia])),
                (xb + dx, yb + dy, float(ir.inst_rot[ib])),
            ),
        )
    )
    assert ("route_failure", seg) not in engine._margin


def test_route_feedback_demand_reaches_region():
    ir = _ir()
    engine = _engine(ir)
    seg = 1
    mx, my = engine._midpoint(seg)
    outcome = RouteOutcome(
        routed=ir.n_segments,
        total=ir.n_segments,
        complete=True,
        demand=(DemandCell(x_mm=mx, y_mm=my, usage=3, layer=0, pitch_mm=0.1),),
    )
    engine.apply_route_feedback(outcome)
    tv = engine._margin.get(("route_demand", seg))
    assert tv is not None and tv.raw == pytest.approx(1.0)  # usage 3 -> full
    assert "usage=3" in tv.region and "@L0" in tv.region
    # Unknown-cause evidence preserved verbatim.
    assert engine._route_evidence is outcome


def test_gap_warning_demand_uses_its_own_normalisation():
    """A hard-grid CongestionWarning's usage is nets-over-capacity, not
    PathFinder claims — usage 3 over capacity 2 is HALF demand
    (overage 1.0 of a 2-strand gap), not full."""
    ir = _ir()
    engine = _engine(ir)
    seg = 1
    mx, my = engine._midpoint(seg)
    outcome = RouteOutcome(
        routed=ir.n_segments,
        total=ir.n_segments,
        complete=True,
        demand=(
            DemandCell(
                x_mm=mx,
                y_mm=my,
                usage=3,
                capacity=2,
                pitch_mm=0.1,
                source="gap_warning",
            ),
        ),
    )
    engine.apply_route_feedback(outcome)
    tv = engine._margin.get(("route_demand", seg))
    assert tv is not None and tv.raw == pytest.approx(0.5)
    assert "3/2cap" in tv.region and "gap-warning" in tv.region


def test_escape_feedback_ties_to_terminal_instance():
    ir = _ir()
    engine = _engine(ir)
    seg = 2
    a, _b = int(ir.seg_pin_a[seg]), int(ir.seg_pin_b[seg])
    ia = int(ir.pin_instance[a])
    x, y = float(ir.inst_x[ia]), float(ir.inst_y[ia])
    outcome = RouteOutcome(
        routed=0,
        total=1,
        complete=False,
        failures=(
            RouteFailure(
                net="GND",
                cause="no_path",
                points=((x, y),),
                escape=True,
                instances=(ia,),
                detail="walled in",
            ),
        ),
    )
    engine.apply_route_feedback(outcome)
    tv = engine._margin.get(("route_escape", ia))
    assert tv is not None and tv.raw == 1.0
    assert tv.region == str(ir.instance_refdes[ia])


def test_escape_cells_union_across_failures():
    """Two escape failures implicating the same terminal UNION their
    cells — the second never overwrites the first."""
    ir = _ir()
    engine = _engine(ir)
    seg = 0
    a = int(ir.seg_pin_a[seg])
    ia = int(ir.pin_instance[a])
    cell = engine.config.region_cell_mm
    x, y = float(ir.inst_x[ia]), float(ir.inst_y[ia])
    outcome = RouteOutcome(
        routed=0,
        total=1,
        complete=False,
        failures=(
            RouteFailure(
                net="GND",
                cause="plane_drop_blocked",
                points=((x + 6 * cell, y),),
                escape=True,
                instances=(ia,),
            ),
            RouteFailure(
                net="VCC",
                cause="plane_drop_blocked",
                points=((x - 6 * cell, y + 6 * cell),),
                escape=True,
                instances=(ia,),
            ),
        ),
    )
    engine.apply_route_feedback(outcome)
    assert len(engine._escape_by_inst[ia]) == 2


def _scripted_route(outcomes):
    it = iter(outcomes)
    return lambda _ir: next(it)


def test_loop_completes_and_digest_reports():
    """Scripted: round 0 fails a net, the anneal moves on, round 1 is
    clean — the loop ends complete and the digest names the evidence."""
    ir = _ir()
    seg = 0
    a = int(ir.seg_pin_a[seg])
    ia = int(ir.pin_instance[a])
    fail = RouteOutcome(
        routed=0,
        total=ir.n_segments,
        complete=False,
        failures=(
            RouteFailure(
                net="N0",
                cause="congestion",
                points=((float(ir.inst_x[ia]), float(ir.inst_y[ia])),),
                seg_id=seg,
            ),
        ),
    )
    done = RouteOutcome(routed=ir.n_segments, total=ir.n_segments, complete=True)
    result = anneal_route_loop(
        ir,
        OptimizeConfig(iters=200, seed=3, expansion=_CAPS),
        _scripted_route([fail, done]),
    )
    assert result.complete
    assert result.digest.complete
    assert result.digest.rounds_run == 2
    assert result.best_round == 1
    assert result.digest.expansion_status == "available"
    text = conflict_digest_text(result)
    assert "complete" in text


def test_loop_clamps_rounds_and_reports_conflict():
    """Three scripted failures: rounds clamp at the authored max of
    three, the digest names the failed net, the cap, the cause, and a
    marginal price with units — never placement advice."""
    ir = _ir()
    seg = 1
    a = int(ir.seg_pin_a[seg])
    ia = int(ir.pin_instance[a])
    fail = RouteOutcome(
        routed=0,
        total=ir.n_segments,
        complete=False,
        failures=(
            RouteFailure(
                net="N1",
                cause="no_path",
                points=((float(ir.inst_x[ia]), float(ir.inst_y[ia])),),
                seg_id=seg,
                detail="walled in",
            ),
        ),
    )
    result = anneal_route_loop(
        ir,
        OptimizeConfig(iters=50, seed=7, expansion=_CAPS),
        _scripted_route([fail, fail, fail, fail]),
        loop=RouteLoopConfig(rounds=10, budget_s=600.0),
    )
    assert result.digest.rounds_run == 3  # authored max, hard-clamped
    assert not result.complete
    d = result.digest
    assert "N1" in d.unrouted_nets
    names = {c.name for c in d.constraints}
    assert any(n.startswith("failed_net") for n in names)
    assert any(n in ("outline", "outline_cap") for n in names)
    assert any(n in ("layer_pair", "layer_cap") for n in names)
    # Every constraint carries its basis and a marginal price or an
    # explicit unavailable price — units are stated, none inferred.
    for c in d.constraints:
        assert c.basis
        assert c.effect_if_freed == "unknown"
    outline_c = next(c for c in d.constraints if c.name == "outline")
    assert outline_c.marginal_usd is not None and outline_c.marginal_usd > 0.0
    text = conflict_digest_text(result)
    assert "N1" in text and "price-unavailable" not in text.split("outline")[0]


def test_best_round_restore_carries_expansion_state(monkeypatch):
    """Round 1's board carries an accepted LAYER_ADD + recorded refusals;
    restoring best=round 1 must keep the pair priced and the refusals
    listed — the digest is built from a rebuilt engine."""
    ir = _ir(stackup=[dict(e) for e in _TWO_LAYER])

    def fail_out(n):
        return RouteOutcome(
            routed=0,
            total=ir.n_segments,
            complete=False,
            failures=tuple(
                RouteFailure(
                    net=f"N{i}",
                    cause="congestion",
                    points=((float(ir.inst_x[i]), float(ir.inst_y[i])),),
                    seg_id=0,
                )
                for i in range(n)
            ),
        )

    added: list = []

    def fake_anneal(self, rng):
        if not added:
            mv = MOVE_GENERATORS[MoveKind.LAYER_ADD](self, rng, 0.0)
            if mv is not None:
                self.apply_move(mv)
                added.append(mv)
        self._refuse_expansion("test-refusal")

    monkeypatch.setattr(OptimizeEngine, "anneal", fake_anneal)
    result = anneal_route_loop(
        ir,
        OptimizeConfig(
            iters=10,
            seed=1,
            expansion=ExpansionCaps((60.0, 60.0), 4, 500.0),
        ),
        _scripted_route([fail_out(2), fail_out(1), fail_out(3)]),
    )
    assert added, "the fake anneal never applied the pair"
    assert result.best_round == 1
    assert ir.n_layers == 4
    names = {c.name for c in result.digest.constraints}
    # On the restored 4-layer board the next pair sits over the authored
    # layer cap — named 'layer_cap' — and the refusal record survived
    # the engine rebuild.
    assert {"layer_pair", "layer_cap"} & names
    assert "refused_expansion" in names
    pair_c = next(
        c
        for c in result.digest.constraints
        if c.name in names & {"layer_pair", "layer_cap"}
    )
    assert pair_c.marginal_usd == pytest.approx(2.0 * 5.0)
    # The accepted pair's fab USD is still priced after the restore.
    assert result.digest.estimated_fab_usd >= 2.0 * 5.0


def test_loop_deterministic_same_seed():
    """The same seed twice gives an identical RouteLoopResult on an
    identical board — digest, rounds, moves and the restored IR."""
    outcomes = [
        RouteOutcome(
            routed=0,
            total=7,
            complete=False,
            failures=(
                RouteFailure(
                    net="N0",
                    cause="congestion",
                    points=((5.0, 5.0),),
                    seg_id=0,
                ),
            ),
        ),
        RouteOutcome(routed=3, total=7, complete=False),
    ]
    ir1, ir2 = _ir(seed=42), _ir(seed=42)
    r1 = anneal_route_loop(
        ir1,
        OptimizeConfig(iters=200, seed=9, expansion=_CAPS),
        _scripted_route(list(outcomes)),
    )
    r2 = anneal_route_loop(
        ir2,
        OptimizeConfig(iters=200, seed=9, expansion=_CAPS),
        _scripted_route(list(outcomes)),
    )
    assert r1 == r2
    assert (ir1.inst_x == ir2.inst_x).all()
    assert (ir1.inst_y == ir2.inst_y).all()
    assert ir1.outline == ir2.outline
    assert [ly["name"] for ly in ir1.stackup] == [ly["name"] for ly in ir2.stackup]


def test_loop_best_round_restored():
    """When the anneal makes round 1 worse, the IR comes back to the
    BEST round (fewest failures), not the last."""
    ir = _ir()
    seg = 0
    a = int(ir.seg_pin_a[seg])
    ia = int(ir.pin_instance[a])
    pt = (float(ir.inst_x[ia]), float(ir.inst_y[ia]))
    one = RouteOutcome(
        routed=1,
        total=ir.n_segments,
        complete=False,
        failures=(
            RouteFailure(net="N0", cause="congestion", points=(pt,), seg_id=seg),
        ),
    )
    two = RouteOutcome(
        routed=0,
        total=ir.n_segments,
        complete=False,
        failures=(
            RouteFailure(net="N0", cause="congestion", points=(pt,), seg_id=seg),
            RouteFailure(net="N1", cause="congestion", points=(pt,), seg_id=seg + 1),
        ),
    )
    result = anneal_route_loop(
        ir,
        OptimizeConfig(iters=100, seed=11),
        _scripted_route([one, two]),
    )
    assert result.best_round == 0
    assert result.outcome.routed == 1


# ── slow: the loop on the authored Nano acceptance board ─────────────────

pytestmark_slow = pytest.mark.slow


@pytest.mark.slow
def test_nano_loop_digest_shape_and_no_regression(store) -> None:
    """The budgeted outer loop on the USB-C PD Nano fixture: digest
    shape is asserted, and the loop's routed count must not regress
    against its own first route (and is reported against the pinned
    38/64 baseline for the record).

    Measured runtime ~221 s in the containerised suite (2026-10-13):
    3000-iter place anneal + two full maze-route rounds with negotiate=5.
    """
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.pcb import feedback as pcb_feedback
    from precis.pcb import realize as pcb_realize
    from precis.pcb import session as pcb_session
    from precis.pcb.optimize import optimize
    from tests.fixtures.pcb import nano_usb_pd_testboard as fx

    for lcsc, fp in fx.CATALOG_FOOTPRINTS.items():
        store.part_footprint_put(lcsc, fp)
    pcb = PcbHandler(hub=Hub(store=store))
    created = pcb.put(id="nano-usb-pd-loop", args=dict(fx.DESIGN))
    assert "created" in created.body
    ref = store.get_ref(kind="pcb", id="nano-usb-pd-loop")
    assert ref is not None

    graph = store.pcb_graph(ref.id)
    board_id = (graph.get("board") or {}).get("board_id")
    assert board_id is not None
    features = store.pcb_features_list(ref.id)
    ir = pcb_session.build_ir(
        graph,
        outline=pcb_session.outline_from_features(features),
        mounting_holes=pcb_session.mounting_holes_from_features(features),
        footprints_by_lcsc=store.pcb_footprints_for(ref.id),
        local_footprints_by_name=store.pcb_local_footprints_for(ref.id),
        fixed_copper=store.pcb_fixed_copper_list(int(board_id)),
    )
    footprints = pcb_session.footprints_by_refdes(
        ir,
        store.pcb_footprints_for(ref.id),
        local_footprints_by_name=store.pcb_local_footprints_for(ref.id),
        local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
    )
    fixed_copper = store.pcb_fixed_copper_list(int(board_id))
    realize_config = pcb_realize.RealizeConfig(
        class_rules=graph.get("net_classes"), negotiate_iterations=5
    )

    def route(ir_):
        rres = pcb_realize.realize(
            ir_,
            config=realize_config,
            footprints=footprints,
            fixed_copper=fixed_copper,
        )
        return pcb_feedback.outcome_from_realize(ir_, rres)

    # Same place+route anneal the route job runs (seed 1, the pinned
    # baseline's seed), then the loop with authored caps: the outline may
    # grow to 120x100, no extra layers (the cap allows but the table
    # refuses 6-layer — exercised as the illegal-stack control).
    seed = 1
    base_config = OptimizeConfig(iters=3000, seed=seed)
    optimize(ir, base_config)
    caps = ExpansionCaps(
        outline_max_mm=(120.0, 100.0), max_copper_layers=6, max_fab_usd=200.0
    )
    result = anneal_route_loop(
        ir,
        OptimizeConfig(iters=1500, seed=seed, expansion=caps),
        route,
        loop=RouteLoopConfig(rounds=2, budget_s=900.0),
    )

    d = result.digest
    assert d.rounds_run in (1, 2)
    assert d.expansion_status == "available"
    assert d.estimated_fab_usd > 0.0
    # The digest reports honestly either way: complete, or named
    # constraints with witnesses, units and bases — never advice.
    if not d.complete:
        assert d.unrouted_nets
        assert d.constraints
        for c in d.constraints:
            assert c.basis
            assert c.effect_if_freed == "unknown"
        text = conflict_digest_text(result)
        assert "unrouted nets" in text or "failed_net" in text
    # Non-regression on BOTH axes: the restored best state routed at
    # least what the pinned baseline pins (tests/test_pcb_nano_usb_
    # pd_testboard.py::BASELINE_ROUTED — this run's denominators match,
    # round 0 reproduced it exactly at seed 1), and at least what the
    # loop's own first attempt did.
    from tests.test_pcb_nano_usb_pd_testboard import BASELINE_ROUTED

    assert result.outcome.routed >= BASELINE_ROUTED
    assert result.outcome.routed >= result.rounds[0].routed
    print(
        f"\nloop: rounds={d.rounds_run} routed "
        f"{[r.routed for r in result.rounds]}/{result.rounds[0].total} "
        f"(pipeline baseline {BASELINE_ROUTED}/64) complete={d.complete} "
        f"expansion={d.expansion_status} est_fab=${d.estimated_fab_usd:.2f}"
    )
    print(conflict_digest_text(result))


def _nano_loop_inputs(store, slug: str):
    """Hydrate the Nano fixture once and return ``(ir_factory, route)`` —
    ``ir_factory()`` builds a fresh, identical IR off the stored rows;
    ``route`` is the loop's realize adapter and may serve either IR
    (its inputs — footprints, fixed copper, the class rules — are
    read-only and shared)."""
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.pcb import feedback as pcb_feedback
    from precis.pcb import realize as pcb_realize
    from precis.pcb import session as pcb_session
    from tests.fixtures.pcb import nano_usb_pd_testboard as fx

    for lcsc, fp in fx.CATALOG_FOOTPRINTS.items():
        store.part_footprint_put(lcsc, fp)
    pcb = PcbHandler(hub=Hub(store=store))
    created = pcb.put(id=slug, args=dict(fx.DESIGN))
    assert "created" in created.body
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    board_id = (graph.get("board") or {}).get("board_id")
    assert board_id is not None
    fixed_copper = store.pcb_fixed_copper_list(int(board_id))
    realize_config = pcb_realize.RealizeConfig(
        class_rules=graph.get("net_classes"), negotiate_iterations=5
    )

    def ir_factory():
        features = store.pcb_features_list(ref.id)
        return pcb_session.build_ir(
            graph,
            outline=pcb_session.outline_from_features(features),
            mounting_holes=pcb_session.mounting_holes_from_features(features),
            footprints_by_lcsc=store.pcb_footprints_for(ref.id),
            local_footprints_by_name=store.pcb_local_footprints_for(ref.id),
            fixed_copper=fixed_copper,
        )

    probe = ir_factory()
    footprints = pcb_session.footprints_by_refdes(
        probe,
        store.pcb_footprints_for(ref.id),
        local_footprints_by_name=store.pcb_local_footprints_for(ref.id),
        local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
    )

    def route(ir_):
        rres = pcb_realize.realize(
            ir_,
            config=realize_config,
            footprints=footprints,
            fixed_copper=fixed_copper,
        )
        return pcb_feedback.outcome_from_realize(ir_, rres)

    return ir_factory, route


@pytest.mark.slow
def test_nano_loop_deterministic_same_seed(store) -> None:
    """The seeded Nano pipeline repeat (the spec's determinism
    acceptance): two full runs at seed 1 — place anneal, then the loop —
    produce an identical ``RouteLoopResult``: same routed set, same
    accepted expansions, same digest text, same restored IR."""
    from precis.pcb.optimize import optimize

    ir_factory, route = _nano_loop_inputs(store, "nano-usb-pd-det")
    ir1, ir2 = ir_factory(), ir_factory()
    seed = 1
    caps = ExpansionCaps(
        outline_max_mm=(120.0, 100.0), max_copper_layers=6, max_fab_usd=200.0
    )
    results = []
    for ir in (ir1, ir2):
        optimize(ir, OptimizeConfig(iters=3000, seed=seed))
        results.append(
            anneal_route_loop(
                ir,
                OptimizeConfig(iters=1500, seed=seed, expansion=caps),
                route,
                loop=RouteLoopConfig(rounds=2, budget_s=900.0),
            )
        )
    r1, r2 = results
    assert r1 == r2, "RouteLoopResult diverged between identical seeded runs"
    # The named fields, said plainly (== above already covers them):
    assert r1.outcome.routed == r2.outcome.routed
    assert [m for m in r1.moves if m.accepted] == [m for m in r2.moves if m.accepted]
    assert conflict_digest_text(r1) == conflict_digest_text(r2)
    # The restored boards match too.
    assert ir1.outline == ir2.outline
    assert [ly["name"] for ly in ir1.stackup] == [ly["name"] for ly in ir2.stackup]
    assert (ir1.inst_x == ir2.inst_x).all()
    assert (ir1.inst_y == ir2.inst_y).all()
    assert (ir1.inst_rot == ir2.inst_rot).all()
    assert (ir1.seg_layer == ir2.seg_layer).all()
    print(
        f"\ndeterminism: routed {[r.routed for r in r1.rounds]}/"
        f"{r1.rounds[0].total} identical across two seed-{seed} runs"
    )
