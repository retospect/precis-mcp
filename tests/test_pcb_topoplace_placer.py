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
