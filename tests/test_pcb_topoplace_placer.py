"""Slice 1a: the incremental proxy (parity with a full recompute and
evals/s), the placer (vs ``autoplace`` at equal evals), the archive's
diversity invariant, and seed determinism — spec §1a's own tests."""

from __future__ import annotations

import time

import numpy as np
import pytest

from precis.pcb.place import autoplace
from precis.pcb.topoplace import (
    ObjectiveShares,
    ProxyState,
    evaluate,
    placer,
    topo_place,
)
from precis.pcb.topoplace.archive import mean_displacement, rotations_differ
from precis.pcb.topoplace.legalize import LegalizeResult
from precis.pcb.topoplace.shove import ShoveResult
from tests.fixtures.pcb.topo_example_a import example_a
from tests.fixtures.pcb.topo_synth import synth_design

W = ObjectiveShares()


def test_try_move_commit_matches_full_recompute_1000_moves() -> None:
    """Incremental == full recompute, exactly, over 1000 seeded random
    moves (translate / rotate / flip, commit-or-revert) on the
    100-part synthetic board."""
    d, _inst, _nets = synth_design(seed=0)
    proxy = ProxyState(d, d.pose0, W)
    rng = np.random.default_rng(2)
    movable = np.flatnonzero(~d.fixed)
    for i in range(1000):
        p = int(movable[rng.integers(len(movable))])
        row = proxy.pose[p].copy()
        kind = rng.random()
        if kind < 0.8:
            row[0] += rng.normal(0.0, 3.0)
            row[1] += rng.normal(0.0, 3.0)
        elif kind < 0.9:
            row[2] = (row[2] + float(rng.integers(1, 4))) % 4
        else:
            row[3] = 1.0 - row[3]
        proxy.try_move(p, row)
        (proxy.commit if rng.random() < 0.5 else proxy.revert)()
        if i % 25 == 0:
            c, length = evaluate(d, proxy.pose)
            assert c == proxy.crossings, i
            assert length == proxy.length, i
    c, length = evaluate(d, proxy.pose)
    assert c == proxy.crossings
    assert length == proxy.length


def test_proxy_throughput_floor_and_printed_rate() -> None:
    """Proxy evals/s on the 100-part synthetic, printed every run —
    hard floor 500/s (spec's CI floor; target ≥2000/s on the dev Mac)."""
    d, _inst, _nets = synth_design(seed=0)
    proxy = ProxyState(d, d.pose0, W)
    rng = np.random.default_rng(1)
    movable = np.flatnonzero(~d.fixed)
    n_evals = 4000
    t0 = time.perf_counter()
    for _ in range(n_evals):
        p = int(movable[rng.integers(len(movable))])
        row = proxy.pose[p].copy()
        row[0] += rng.normal(0.0, 3.0)
        row[1] += rng.normal(0.0, 3.0)
        (proxy.commit if proxy.try_move(p, row) < 0 else proxy.revert)()
    rate = n_evals / (time.perf_counter() - t0)
    print(f"\nproxy throughput: {rate:.0f} evals/s (floor 500, target 2000)")
    assert rate >= 500.0


def test_spread_proxy_term_parity_and_direction() -> None:
    """The loop's board-fill term (gr515107): staged/commit tracks the
    full recompute, clumping raises it, dispersal lowers it, and parts
    sharing a net never repel each other."""
    from precis.pcb.topoplace.proxy import _part_shared, spread_total

    d, _inst, _nets = synth_design(seed=0)
    w = ObjectiveShares(crossings=0.5, ratsnest=0.1, overlap=0.2, spread=0.2)
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.spread == pytest.approx(spread_total(d, d.pose0))
    rng = np.random.default_rng(3)
    movable = np.flatnonzero(~d.fixed)
    for i in range(300):
        p = int(movable[rng.integers(len(movable))])
        row = proxy.pose[p].copy()
        row[0] += rng.normal(0.0, 3.0)
        row[1] += rng.normal(0.0, 3.0)
        if rng.random() < 0.1:
            row[3] = 1.0 - row[3]
        proxy.try_move(p, row)
        (proxy.commit if rng.random() < 0.5 else proxy.revert)()
        if i % 25 == 0:
            assert proxy.spread == pytest.approx(spread_total(d, proxy.pose)), i
    assert proxy.spread == pytest.approx(spread_total(d, proxy.pose))

    # direction: every movable part clumped on one point is the
    # repulsion maximum — strictly worse than any real pose.
    clump = proxy.pose.copy()
    clump[movable, :2] = clump[movable[0], :2]
    assert spread_total(d, clump) > spread_total(d, proxy.pose)

    # exemption: two parts on the same net contribute nothing even at
    # distance 0 — ``_part_shared`` must mark them.
    for pins in d.net_pins:
        members = np.unique(d.pin_part[pins])
        assert _part_shared(d)[np.ix_(members, members)].all()


def test_placer_beats_autoplace_at_equal_evals_median() -> None:
    """On the synthetic board, best proxy crossings ≤ autoplace's at
    equal eval count — printed per seed, hard on the median."""
    effort = 6000
    ours: list[int] = []
    theirs: list[int] = []
    for seed in range(5):
        d, inst, nets = synth_design(seed=seed)
        res = topo_place(d, objective=W, effort=effort, seed=seed)
        ap = autoplace(inst, nets, iters=effort, seed=seed)
        # autoplace's best pose read under the SAME pin-level metric —
        # centroid airwires hide the fan-in crossings two real pins
        # exhibit, so the comparison must happen inside this metric.
        ap_pose = d.pose0.copy()
        for i, row in enumerate(inst):
            ap_pose[i, 0], ap_pose[i, 1] = ap.positions[row["refdes"]]
        ac, _al = evaluate(d, ap_pose)
        ours.append(res.crossings)
        theirs.append(ac)
        print(
            f"\nseed {seed}: topo={res.crossings} "
            f"autoplace={ac} (centroid {ap.crossings_after})"
        )
    assert float(np.median(ours)) <= float(np.median(theirs))


def test_archive_diversity_invariant_and_size() -> None:
    """After a run: no two elites violate the diversity rule
    (≥ d_min displacement or ≥ 10 % rotations), count ≤ archive_size."""
    d, _inst, _nets = synth_design(seed=0)
    res = topo_place(d, objective=W, effort=4000, seed=0)
    elites = res.elites
    assert len(elites) <= 8
    for i, a in enumerate(elites):
        for b in elites[i + 1 :]:
            assert (
                mean_displacement(a.pose, b.pose) >= 2.0
                or rotations_differ(a.pose, b.pose) >= 0.1
            )


def test_determinism_same_seed_identical_elites_json() -> None:
    d, _inst, _nets = synth_design(seed=0)
    a = topo_place(d, objective=W, effort=3000, seed=7)
    b = topo_place(d, objective=W, effort=3000, seed=7)
    assert a.elites_json() == b.elites_json()
    assert (a.best_pose == b.best_pose).all()


# ── the shove↔legalize post-pass loop (_settle_pose) ────────────────


def test_settle_pose_zero_move_pass_exits_immediately() -> None:
    """example_a's authored pose is legal: one violation-free shove (0
    rounds) plus one no-movement legalize sweep and the loop is done —
    its normal exit is the fixed point, nowhere near the cap."""
    d = example_a()
    pose, shoved, legal = placer._settle_pose(d, d.pose0)
    np.testing.assert_array_equal(pose, d.pose0)
    assert shoved.rounds == 0 and shoved.moved_mm == 0.0
    assert legal.rounds == 1 and legal.resolved


def test_settle_pose_capped_at_post_passes_on_a_fight(monkeypatch) -> None:
    """Movement that keeps shrinking the residual count but never
    finishes runs the full ``_POST_PASSES`` — the cap is the only thing
    that stops an endless shove↔legalize fight (a cap, not a runaway
    guard: it is 50, not 4)."""
    calls = {"s": 0}

    def fake_shove(d, pose):
        calls["s"] += 1
        p = pose.copy()
        p[0, 0] += 0.01
        return ShoveResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.01,
            lp_optimum_mm=0.01,
            min_tri_area_mm2=1.0,
            remaining=((0, 1, 0.5),) * max(1, 60 - calls["s"]),
        )

    def fake_legalize(d, pose):
        p = pose.copy()
        p[0, 1] += 0.01
        return LegalizeResult(
            pose=p, resolved=False, rounds=1, moved_mm=0.01, remaining=()
        )

    monkeypatch.setattr("precis.pcb.topoplace.shove.shove_pose", fake_shove)
    monkeypatch.setattr("precis.pcb.topoplace.legalize.legalize_pose", fake_legalize)
    d = example_a()
    placer._settle_pose(d, d.pose0)
    assert calls["s"] == placer._POST_PASSES == 50


def test_settle_pose_stops_when_conflicts_stop_improving(monkeypatch) -> None:
    """A pass that moves parts but leaves the residual count unchanged
    is a fight, not progress — the loop breaks after it, far under the
    cap, and the merged result still accumulates every pass's fields."""
    calls = {"s": 0}

    def fake_shove(d, pose):
        calls["s"] += 1
        p = pose.copy()
        p[0, 0] += 0.01
        return ShoveResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.01,
            lp_optimum_mm=0.01,
            min_tri_area_mm2=1.0,
            reason="infeasible" if calls["s"] == 1 else "",
            remaining=((0, 1, 0.5),),
        )

    def fake_legalize(d, pose):
        p = pose.copy()
        p[0, 1] += 0.01
        return LegalizeResult(
            pose=p, resolved=False, rounds=1, moved_mm=0.01, remaining=()
        )

    monkeypatch.setattr("precis.pcb.topoplace.shove.shove_pose", fake_shove)
    monkeypatch.setattr("precis.pcb.topoplace.legalize.legalize_pose", fake_legalize)
    d = example_a()
    _pose, shoved, legal = placer._settle_pose(d, d.pose0)
    assert calls["s"] == 2
    # accumulation: two passes, not the last one alone
    assert shoved.rounds == 2 and shoved.moved_mm == pytest.approx(0.02)
    assert shoved.reason == "infeasible"
    assert legal.rounds == 2 and legal.moved_mm == pytest.approx(0.02)


def test_settle_pose_returns_the_best_pose_seen(monkeypatch) -> None:
    """A regressing pass does not keep the board: when pass 2 leaves
    MORE residuals than pass 1, the loop returns pass 1's pose — and
    the merged result reads its ``remaining`` off that pass while
    ``lp_optimum_mm`` stays the FIRST pass's objective and
    ``min_tri_area_mm2`` the worst any pass saw."""
    calls = {"s": 0}
    poses_out: list[np.ndarray] = []

    def fake_shove(d, pose):
        calls["s"] += 1
        p = pose.copy()
        p[0, 0] += 0.5
        return ShoveResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.5,
            lp_optimum_mm=0.5 if calls["s"] == 1 else 9.9,
            min_tri_area_mm2=1.0 if calls["s"] == 1 else 0.25,
            remaining=((0, 1, 0.5),) * (2 if calls["s"] == 1 else 4),
        )

    def fake_legalize(d, pose):
        p = pose.copy()
        p[0, 1] += 0.5
        poses_out.append(p)
        return LegalizeResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.5,
            remaining=((0, 1, 0.25),) if calls["s"] == 1 else ((0, 1, 0.25),) * 3,
        )

    monkeypatch.setattr("precis.pcb.topoplace.shove.shove_pose", fake_shove)
    monkeypatch.setattr("precis.pcb.topoplace.legalize.legalize_pose", fake_legalize)
    d = example_a()
    pose, shoved, legal = placer._settle_pose(d, d.pose0)
    # pass 1: 2+1 conflicts, pose P1; pass 2: 4+3 (no improvement) ->
    # break, and the returned pose is P1's, not P2's
    assert calls["s"] == 2
    np.testing.assert_array_equal(pose, poses_out[0])
    assert len(legal.remaining) == 1  # pass 1's residual, not pass 2's 3
    assert shoved.lp_optimum_mm == pytest.approx(0.5)  # first pass only
    assert shoved.min_tri_area_mm2 == pytest.approx(0.25)  # worst across
    assert shoved.rounds == 2 and shoved.moved_mm == pytest.approx(1.0)


def test_settle_pose_pose_hash_breaks_an_oscillation(monkeypatch) -> None:
    """shove pushes +x, legalize undoes it: the pose revisits a seen
    hash and the loop breaks BEFORE the next shove — a 2-cycle the
    conflict count alone would miss (the residuals here keep
    shrinking)."""
    calls = {"s": 0}

    def fake_shove(d, pose):
        calls["s"] += 1
        p = pose.copy()
        p[0, 0] += 0.5
        return ShoveResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.5,
            lp_optimum_mm=0.5,
            min_tri_area_mm2=1.0,
            remaining=((0, 1, 0.5),) * (5 - calls["s"]),
        )

    def fake_legalize(d, pose):
        p = pose.copy()
        p[0, 0] -= 0.5
        return LegalizeResult(
            pose=p, resolved=False, rounds=1, moved_mm=0.5, remaining=()
        )

    monkeypatch.setattr("precis.pcb.topoplace.shove.shove_pose", fake_shove)
    monkeypatch.setattr("precis.pcb.topoplace.legalize.legalize_pose", fake_legalize)
    d = example_a()
    placer._settle_pose(d, d.pose0)
    assert calls["s"] == 1


def test_settle_pose_is_deterministic() -> None:
    """Same input pose → identical output: the stacked-pair example-A
    board settles to a byte-identical pose with identical accumulated
    fields on a repeat run."""
    d = example_a()
    pose = d.pose0.copy()
    pose[1] = pose[0].copy()
    a = placer._settle_pose(d, pose)
    b = placer._settle_pose(d, pose)
    np.testing.assert_array_equal(a[0], b[0])
    assert a[1].moved_mm == b[1].moved_mm
    assert a[2].moved_mm == b[2].moved_mm


def test_placement_sides_top_forbids_bottom_flips() -> None:
    """gr520370: sides='top' never lands a part on the bottom — the flip
    move is only offered to a part already down (to come up), and the
    per-proposal check keeps a top part from ever going down."""
    import dataclasses

    d, _inst, _nets = synth_design(seed=0)
    d = dataclasses.replace(d, sides="top")
    res = topo_place(d, objective=W, effort=3000, seed=1)
    assert (res.best_pose[:, 3] < 0.5).all()
    for e in res.elites:
        assert (e.pose[:, 3] < 0.5).all()


def test_fixed_side_strips_the_back_side_freedom() -> None:
    """gr520370's per-part lock: ``fixed_side`` on an instance denies the
    BACK_SIDE freedom — the placer can't flip it, whatever the board's
    placement_sides says."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import FREEDOM_BACK_SIDE, design_from_ir

    ir = from_graph(
        {
            "instances": [
                {"refdes": "A", "pins": [{"name": "1"}], "fixed_side": True},
                {"refdes": "B", "pins": [{"name": "1"}]},
            ],
            "nets": [
                {
                    "name": "N1",
                    "members": [
                        {"refdes": "A", "pin": "1"},
                        {"refdes": "B", "pin": "1"},
                    ],
                }
            ],
        }
    )
    td = design_from_ir(ir, {})
    assert not (td.freedoms[0] & FREEDOM_BACK_SIDE)
    assert td.freedoms[1] & FREEDOM_BACK_SIDE
    td_top = design_from_ir(ir, {}, placement_sides="top")
    assert td_top.sides == "top"


def test_authored_proximity_measure_pulls_parts_together() -> None:
    """gr520546: an authored ``near()``/proximity measure is priced in
    the proxy objective. U1 and U2 start ~20 mm apart with NO net
    between them — nothing but the measure can pull them inside its
    2 mm bound."""
    import dataclasses

    from precis.pcb.topoplace.design import TopoMeasure

    d, _inst, _nets = synth_design(seed=0, n_parts=4, n_nets=0, n_multinet=0, n_fixed=0)
    d = dataclasses.replace(
        d,
        measures=(
            TopoMeasure(ia=0, ib=1, bound="upper", goal_mm=2.0, weight=1.0, hard=True),
        ),
    )
    dist0 = float(np.hypot(*(d.pose0[0, :2] - d.pose0[1, :2])))
    assert dist0 > 10.0
    res = topo_place(
        d,
        objective=ObjectiveShares(
            crossings=0.0, ratsnest=0.0, overlap=0.1, measures=1.0
        ),
        effort=2000,
        seed=1,
    )
    dist1 = float(np.hypot(*(res.best_pose[0, :2] - res.best_pose[1, :2])))
    assert dist1 <= 2.0 + 0.5  # inside the bound, up to the MTV epsilon


def test_rigid_group_keeps_authored_offsets_through_the_anneal() -> None:
    """gr520546: an authored ``group``/``group_offset`` pair is ONE rigid
    body. A net pulls the group across the board while free parts sit
    in the way — translate, rotate, shove and legalize must all keep
    the authored member offset and relative rotation intact."""
    import dataclasses

    from precis.pcb.topoplace.design import qrot

    d, _inst, _nets = synth_design(seed=0, n_parts=6, n_nets=4, n_multinet=0, n_fixed=0)
    # U1/U2 are one assembly: U2 sits +12 mm x off the group's anchor,
    # same rotation.
    group_of = np.full(len(d.parts), -1, dtype=np.int32)
    group_of[0] = group_of[1] = 0
    group_off = np.zeros((len(d.parts), 3), dtype=np.float64)
    group_off[1] = (12.0, 0.0, 0.0)
    pose0 = d.pose0.copy()
    pose0[0] = (10.0, 10.0, 0.0, 0.0)
    pose0[1] = (22.0, 10.0, 0.0, 0.0)  # the authored shape at arot 0
    d = dataclasses.replace(
        d,
        group_of=group_of,
        group_members=((0, 1),),
        group_off=group_off,
        pose0=pose0,
    )
    res = topo_place(d, objective=ObjectiveShares(), effort=3000, seed=3)
    dx = res.best_pose[1, :2] - res.best_pose[0, :2]
    arot_q = round(res.best_pose[0, 2]) % 4
    np.testing.assert_allclose(qrot(dx, -arot_q), (12.0, 0.0), atol=1e-6)
    assert round(res.best_pose[0, 2]) % 4 == round(res.best_pose[1, 2]) % 4


def test_fixed_group_member_pins_the_whole_group() -> None:
    """gr520546: a fixed member pins the group — every member's pose0
    is rebuilt rigidly off the pinned member's anchor (J2's authored
    xy is overridden by its ``group_offset``), and all members land in
    ``d.fixed`` so no mover touches them."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    ir = from_graph(
        {
            "instances": [
                {
                    "refdes": "J1",
                    "pins": [{"name": "1"}],
                    "x": 5.0,
                    "y": 5.0,
                    "rot": 0.0,
                    "fixed": "xy",
                    "group": "hdr",
                    "group_offset": {"x": 0.0, "y": 0.0, "rot": 0.0},
                },
                {
                    "refdes": "J2",
                    "pins": [{"name": "1"}],
                    "x": 40.0,
                    "y": 30.0,  # authored xy IGNORED: the group shape wins
                    "group": "hdr",
                    "group_offset": {"x": 15.24, "y": 0.0, "rot": 0.0},
                },
                {
                    "refdes": "R1",
                    "pins": [{"name": "1"}],
                    "x": 20.0,
                    "y": 8.0,
                },
            ],
            "nets": [],
        }
    )
    td = design_from_ir(ir, {})
    assert td.fixed[0] and td.fixed[1] and not td.fixed[2]
    assert td.group_members == ((0, 1),)
    np.testing.assert_allclose(td.pose0[0, :2], (5.0, 5.0))
    np.testing.assert_allclose(td.pose0[1, :2], (20.24, 5.0))
    assert td.pose0[1, 2] == 0.0


def test_group_without_offsets_or_poses_stays_independent() -> None:
    """GRIPE_FIX (gr520546 review): a group is rigid only when its shape
    is recoverable — every member carrying an authored ``group_offset``
    or a finite pose0. A fresh board's group (no offsets, nothing
    placed) otherwise collapsed every member onto the anchor's xy —
    a permanent stack the legalizer, shove LP and ``_part_hit`` all skip
    (same-group pairs), invisible to every gate. The members place
    independently and the run warns once, naming the group."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    ir = from_graph(
        {
            "instances": [
                {"refdes": r, "pins": [{"name": "1"}], "group": "hdr"}
                for r in ("J1", "J2", "J3")
            ],
            "nets": [],
        }
    )
    td = design_from_ir(ir, {})
    assert td.group_members == ()
    assert (td.group_of == -1).all()
    assert len(td.warnings) == 1
    w = td.warnings[0]
    assert "'hdr'" in w and "group_offset" in w and "place once" in w


def test_group_of_posed_members_stays_rigid() -> None:
    """The gate's keep side: no ``group_offset`` authored anywhere, but
    every member placed — the pose0-implied shape still makes one rigid
    body, exactly as before (only a member with NEITHER source of shape
    demotes the group)."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    ir = from_graph(
        {
            "instances": [
                {
                    "refdes": "J1",
                    "pins": [{"name": "1"}],
                    "x": 5.0,
                    "y": 5.0,
                    "group": "hdr",
                },
                {
                    "refdes": "J2",
                    "pins": [{"name": "1"}],
                    "x": 15.0,
                    "y": 5.0,
                    "group": "hdr",
                },
                {
                    "refdes": "J3",
                    "pins": [{"name": "1"}],
                    "x": 25.0,
                    "y": 5.0,
                    "group": "hdr",
                },
            ],
            "nets": [],
        }
    )
    td = design_from_ir(ir, {})
    assert td.group_members == ((0, 1, 2),)
    assert td.warnings == ()
    np.testing.assert_allclose(td.group_off[1, :2], (10.0, 0.0), atol=1e-9)
    np.testing.assert_allclose(td.group_off[2, :2], (20.0, 0.0), atol=1e-9)


def test_partly_poseless_group_stays_independent() -> None:
    """One placed member does NOT rescue a group whose other members
    carry neither offset nor pose: before the gate, the poseless
    members' nonfinite rel read as offset (0,0) and the normalisation
    stacked them onto the anchor's xy."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    ir = from_graph(
        {
            "instances": [
                {
                    "refdes": "J1",
                    "pins": [{"name": "1"}],
                    "x": 5.0,
                    "y": 5.0,
                    "group": "hdr",
                },
                {"refdes": "J2", "pins": [{"name": "1"}], "group": "hdr"},
                {"refdes": "J3", "pins": [{"name": "1"}], "group": "hdr"},
            ],
            "nets": [],
        }
    )
    td = design_from_ir(ir, {})
    assert td.group_members == ()
    assert (td.group_of == -1).all()
    assert len(td.warnings) == 1
    assert "'hdr'" in td.warnings[0]
    # the placed member's pose is untouched — no anchor normalisation ran
    np.testing.assert_allclose(td.pose0[0, :2], (5.0, 5.0))


def test_ungrouped_run_matches_main_golden_pose_bytewise() -> None:
    """gr520546 review (SHOULD-FIX 3): with no authored groups a seeded
    run reproduces origin/main's ``best_pose`` byte for byte — the
    probe/translate movers stage ``clip(pose+delta)`` again and
    ``_scatter`` seeds a fixed part's NaN pose once more (pre-revert it
    stayed NaN and ``_settle_pose`` crashed inside shapely on this very
    board). Expected captured from origin/main's ``topo_place`` on this
    design: synth seed 0, 8 parts / 4 nets / 1 fixed (part 0's pose0 xy
    blanked), effort 2000, placer seed 7."""
    d, _inst, _nets = synth_design(seed=0, n_parts=8, n_nets=4, n_multinet=0, n_fixed=1)
    d.pose0[0, :2] = np.nan
    res = topo_place(d, objective=W, effort=2000, seed=7)
    expected = np.asarray(
        [
            [62.259355727257365, 88.9269524950184, 0.0, 0.0],
            [65.67894204934903, 97.3579915676985, 0.0, 1.0],
            [61.750907396549245, 89.30112262516882, 1.0, 1.0],
            [1.0528827910690437, 74.77661530325685, 0.0, 0.0],
            [59.92735564922514, 10.684827235968266, 1.0, 0.0],
            [1.083642436802397, 74.82382768731681, 2.0, 1.0],
            [1.9086012340484362, 73.02282868814692, 1.0, 1.0],
            [60.33448286642105, 11.092021830309974, 0.0, 1.0],
        ]
    )
    np.testing.assert_array_equal(res.best_pose, expected)
