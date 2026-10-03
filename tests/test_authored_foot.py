"""The authored-foot planner (docs/backlog/hexfold-ideal-surface-then-tile.md,
S3): k by measurement, bars on every row."""

from __future__ import annotations

import numpy as np
import pytest

from hexfold.build import build
from precis_se.atomic.generators import authored_foot as af


@pytest.mark.parametrize(
    ("n", "k"), [(6, 4), (9, 6), (12, 6), (15, 8), (18, 8), (24, 10)]
)
def test_k_min_is_the_narrowest_frustum_that_builds(n: int, k: int) -> None:
    assert af.k_min(n) == k
    net = build(af.foot_text(n, k), strict=False)
    assert not net.report.errors()
    # six heptagons: three at the sheet, three at the tube
    assert sorted(len(r) for r in net.rings if len(r) != 6) == [7] * 6


def test_foot_text_refuses_a_frustum_too_narrow_to_build() -> None:
    with pytest.raises(ValueError, match="k must be even"):
        af.foot_text(12, 4)
    with pytest.raises(ValueError, match="multiple of 3"):
        af.k_min(7)


def test_rings_are_cycles_so_ring_ideals_land_on_real_corners() -> None:
    net = build(af.foot_text(12, 6), strict=False)
    bonded = {frozenset((i, j)) for i, j, *_ in net.bonds}
    for rg in net.rings:
        assert all(frozenset((rg[t - 1], rg[t])) in bonded for t in range(len(rg)))


def test_angle_stats_are_zero_on_a_flat_sheet_and_see_a_pucker() -> None:
    net = build("hexfold 0.1\norigin s\ns: sheet(8, 8)\n", strict=False)
    pos = np.asarray(net.seed3, dtype=float)
    rms, amax, pyr = af.angle_stats(pos, net.bonds, net.rings)
    # the seed is near-ideal, not exact: a fraction of a degree everywhere
    assert rms < 0.1 and amax < 0.5 and abs(pyr) < 0.5
    lifted = pos.copy()
    inner = max(
        range(len(pos)), key=lambda a: sum(1 for i, j, *_ in net.bonds if a in (i, j))
    )
    lifted[inner, 2] += 0.5
    assert af.angle_stats(lifted, net.bonds, net.rings)[2] > 5.0


def _row(k: int, **kw: float) -> af.FootRow:
    base = dict(
        fillet_mean=0.02,
        fillet_p95=0.04,
        fillet_max=0.05,
        bond_min=1.40,
        bond_max=1.48,
        angle_rms=1.5,
        angle_max=15.0,
        pyramid_max=2.0,
        rim_r=7.6,
    )
    base.update(kw)
    misses = tuple(
        name
        for name, bad in (
            ("fillet mean", base["fillet_mean"] > af.MEAN_MAX_A),
            ("fillet max", base["fillet_max"] > af.DEV_MAX_A),
            ("bond min", base["bond_min"] < af.BOND_MIN_A),
            ("bond max", base["bond_max"] > af.BOND_MAX_A),
        )
        if bad
    )
    return af.FootRow(k=k, k_tether=1.0, passes=7, misses=misses, **base)


def test_excess_is_zero_inside_the_bars_and_sums_the_overshoot() -> None:
    assert _row(6).meets and _row(6).excess == 0.0
    r = _row(8, bond_min=1.355, fillet_max=0.4)
    assert r.misses == ("fillet max", "bond min")
    assert r.excess == pytest.approx(0.1 + 0.005)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("n", "radius", "k"), [(12, 8.0, 6), (6, 3.0, 4), (24, 8.0, 10)]
)
def test_plan_reproduces_the_s3_sweep(n: int, radius: float, k: int) -> None:
    # the regression anchors the planner verdict set (the pillar is (6,0)
    # R=3, the pill (24,0)): the narrowest frustum meets every bar where
    # k = k_min + 2 misses the bond floor
    plan = af.plan_foot(n, radius, candidates=(k, k + 2))
    assert plan.k == k and plan.meets
    wide = next(r for r in plan.rows if r.k == k + 2)
    assert not wide.meets and "bond min" in wide.misses
    c = plan.chosen
    assert c.pyramid_max < 12.0  # under C60's
    assert plan.positions.shape == (len(build(plan.text, strict=False).atoms), 3)


_PILLAR = af.SceneFeature("t", (13, 15), 6, 3.0, 5, "ball")
_BUMP = af.SceneFeature("q", (20, 6), 12, 5.0, 1, "lid")


def test_scene_text_builds_one_3_plus_3_foot_per_feature() -> None:
    ks = {"t": af.k_min(6), "q": af.k_min(12)}
    net = build(af.scene_text((30, 24), (_PILLAR, _BUMP), ks), strict=False)
    assert not net.report.errors()
    inst = [a.instance for a in net.atoms]

    def seam(*names: str) -> list[int]:
        return sorted(
            len(r)
            for r in net.rings
            if len(r) != 6 and {inst[a] for a in r} == set(names)
        )

    for f in ("t", "q"):  # three heptagons at the sheet, three at the tube
        assert seam("s", f"{f}f") == [7, 7, 7]
        assert seam(f"{f}f", f) == [7, 7, 7]
    assert {"s", "t", "tf", "tc", "q", "qf", "qc"} <= set(inst)


def test_scene_text_refuses_tops_the_build_cannot_seat() -> None:
    def one(n: int, top: str) -> tuple[af.SceneFeature, ...]:
        return (af.SceneFeature("p", (9, 9), n, 5.0, 2, top),)

    with pytest.raises(ValueError, match="ball top fuses onto"):
        af.scene_text((30, 24), one(12, "ball"), {"p": 6})
    with pytest.raises(ValueError, match="flat lid"):
        af.scene_text((30, 24), one(9, "lid"), {"p": 6})
    with pytest.raises(ValueError, match="top must be one of"):
        af.scene_text((30, 24), one(6, "dome"), {"p": 4})
    with pytest.raises(ValueError, match="distinct and not"):
        af.plan_scene((30, 24), (_PILLAR, _PILLAR))


def test_a_hole_cell_whose_seam_gains_5_7_pairs_is_refused_before_relaxing() -> None:
    # at some cells the sheet seam mints three extra 5-7 pairs (seam-phase
    # fault); the scene would then judge a census nobody planned
    bad = af.SceneFeature("t", (9, 12), 6, 3.0, 3, "open")
    with pytest.raises(
        ValueError, match=r"seam at cell \(9, 12\) has rings \[5, 5, 5, 7"
    ):
        af.plan_scene((30, 24), (bad,))


def test_a_fallback_k_is_judged_in_the_rebuilt_scene_and_a_miss_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # orchestrator, S4 verdict: the fallback winner is re-judged IN the
    # scene, and a winner that still misses there is reported, not looped on
    calls: list[dict[str, int]] = []

    def fake_relax(
        sheet: tuple[int, int],
        features: tuple[af.SceneFeature, ...],
        ks: dict[str, int],
        extra: str,
        k_tether: float,
    ) -> af.ScenePlan:
        calls.append(dict(ks))
        return af.ScenePlan(
            text="",
            ks=dict(ks),
            positions=np.zeros((0, 3)),
            instances=(),
            rows={f.name: _row(ks[f.name], bond_min=1.30) for f in features},
            tops={},
            findings=(),
            passes=1,
        )

    monkeypatch.setattr(af, "_relax_scene", fake_relax)
    monkeypatch.setattr(af, "_planned_k", lambda n, radius, k_tether: 8)
    plan = af.plan_scene((30, 24), (_BUMP,))
    assert calls == [{"q": 6}, {"q": 8}]  # k_min, then the fallback, once
    assert plan.ks == {"q": 8} and not plan.meets
    assert plan.rows["q"].misses == ("bond min",)


@pytest.mark.slow
def test_pillar_and_bump_scene_meets_the_s4_bars() -> None:
    # S4 acceptance on a small sheet: no ERROR finding (geom.clash,
    # geom.seed_overlap), every foot inside the S3 bars, judged on the
    # tethered coordinates and labelled so
    plan = af.plan_scene((30, 24), (_PILLAR, _BUMP))
    assert plan.errors == () and plan.meets, (plan.errors, plan.rows)
    assert plan.ks == {"t": 4, "q": 6}
    for row in plan.rows.values():
        assert row.pyramid_max < 12.0  # under C60's
    summary = next(dict(f.data) for f in plan.findings if f.code == "geom.summary")
    assert summary["relax"] == "tethered" and summary["clash_min"] >= 1.0
    assert set(plan.tops) == {"t", "q"}
