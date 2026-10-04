"""Authored tops (hexfold-toolkit cycle 1): ``top: "sphere"`` and a rounded
``top: "lid"`` in :func:`precis_se.atomic.generators.authored_foot.plan_top`
and the ``hexfold_scene`` generator.

The planner builds one candidate per (k, L) on a bare tube, so the grid
tests are slow-marked and share one plan per module (``lru_cache`` on the
planner).  The numbers in the comments are what the stick relax measured when
the cycle was built; the assertions are the bars and the bands, not those
numbers.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pytest

from hexfold.lattice import tube_radius
from hexfold.report import Finding, HexfoldError, Severity
from precis_se.atomic.generators import GENERATORS, GeneratorError
from precis_se.atomic.generators import authored_foot as af
from precis_se.atomic.generators.hexfold_scene import _top_findings

_SPHERE: dict[str, Any] = {
    "name": "q",
    "at": [15, 15],
    "n": 12,
    "radius": 5.0,
    "tube_len": 3,
    "top": "sphere",
}


# a sphere off the plan table (a non-default fillet): planned live, so it
# counts toward the scene's cost ceilings
_LIVE_SPHERE: dict[str, Any] = {**_SPHERE, "top_fillet": 3.5}


def _scene(**feature: Any) -> Any:
    return GENERATORS["hexfold_scene"](
        {"sheet": [30, 30], "features": [{**_SPHERE, **feature}]}
    )


# ── refusals, by name (fast) ───────────────────────────────────────────


@pytest.mark.parametrize("n", [6, 9, 15, 18 + 1])
def test_a_sphere_on_a_bad_n_is_refused_with_the_reason(n: int) -> None:
    with pytest.raises(GeneratorError, match=r"sphere top needs n a multiple of 6"):
        _scene(n=n)


def test_an_unknown_top_is_refused_by_name() -> None:
    with pytest.raises(GeneratorError, match=r"top must be one of.*'dome'"):
        _scene(top="dome")


@pytest.mark.parametrize(
    ("feature", "match"),
    [
        ({"top": "lid", "top_R": 9.0}, "top_R belongs to top 'sphere'"),
        ({"top": "open", "top_fillet": 2.0}, "top_fillet belongs to top"),
        ({"top": "lid", "top_fillet": 5.0}, "more than the tube radius"),
        ({"top_fillet": -1.0}, "top_fillet must be positive"),
    ],
)
def test_misplaced_top_keywords_are_refused(
    feature: dict[str, Any], match: str
) -> None:
    with pytest.raises(GeneratorError, match=match):
        _scene(**feature)


def test_the_planner_refuses_what_it_cannot_tile() -> None:
    with pytest.raises(ValueError, match="multiple of 6"):
        af.plan_top(9, "sphere")
    with pytest.raises(ValueError, match="needs top_fillet"):
        af.plan_top(12, "lid")
    with pytest.raises(ValueError, match="kind must be"):
        af.plan_top(12, "ball")


def test_a_sphere_is_never_planned_with_a_bulge_of_length_0() -> None:
    # L=0 tore the net at every setting in the probe (10 ERROR, 112-252 pairs)
    for n in (12, 18, 24):
        cands = af._candidates(n, "sphere")
        assert cands and min(length for _k, length, _m in cands) >= 1
        r = n // 6 - 1
        assert {k for k, _l, _m in cands} == {r + 3, r + 4, r + 5}
        assert {length for _k, length, _m in cands} == {1, 2, 3}
    assert {k for k, _l, _m in af._candidates(12, "lid")} == {0}


# ── the fillet floor (fast) ─────────────────────────────────────────────


def test_r_min_fillet_counts_both_curvatures() -> None:
    # singly curved: kappa_mean = 1/(2 R_t) <= 12/(11.6*3.55) -> R_t >= 1.72
    assert af.r_min_fillet(1e9) == pytest.approx(1.716, abs=0.005)
    # a (12,0) tube adds the hoop curvature 1/4.70: the bound rises to 2.70
    rt = tube_radius(12, 0)
    assert af.r_min_fillet(rt) == pytest.approx(2.70, abs=0.02)
    assert af.r_min_fillet(rt) > af.r_min_fillet(1e9)
    # a hoop curvature past 2*kappa_max leaves no fillet radius that helps
    assert af.r_min_fillet(1.0) == float("inf")


def test_the_default_fillet_is_capped_by_the_room_and_floored_at_2_angstrom() -> None:
    rt = tube_radius(12, 0)
    # 1.5 * 2.70 = 4.05, and the radial step R - r (4.3) leaves room
    assert af.default_fillet(rt, 9.04) == pytest.approx(4.05, abs=0.03)
    # a step of 3.24 A is the cap
    assert af.default_fillet(rt, rt + 3.24) == pytest.approx(3.24)
    # never under 2 A, even where the room is smaller
    assert af.default_fillet(rt, rt + 0.5) == af.FILLET_FLOOR_A


# ── the choice order (fast, synthetic candidates) ──────────────────────


def _row(k: int, length: int, *, rel: float, r: float = 9.0, **kw: Any) -> af.TopRow:
    base: dict[str, Any] = dict(
        k=k,
        length=length,
        dome_rows=0,
        drop=2.0,
        R=r,
        fillet=3.0,
        atoms=400,
        dev_p95=0.05,
        dev_max=0.06,
        theta_p_max=5.0,
        bond_max=1.5,
        pairs=0,
        errors=(),
        relaxed_p95=rel,
        relaxed_dz=1.0,
        passes=7,
        misses=(),
    )
    base.update(kw)
    return af.TopRow(**base)


def test_the_choice_prefers_smallest_relaxed_p95_then_narrowest_k_then_shortest_l() -> (
    None
):
    rows = [
        _row(5, 2, rel=0.40),
        _row(4, 3, rel=0.80),
        _row(4, 2, rel=0.80),
        _row(6, 1, rel=0.40),
        # the smallest relaxed p95 of all, but it misses a bar
        _row(4, 1, rel=0.10, misses=("theta_p",), theta_p_max=13.0),
    ]
    best = af.pick_top(rows)
    assert (best.k, best.length) == (5, 2)  # 0.40 ties with (6,1); narrower k wins
    # equal relaxed p95 and k: the shorter bulge wins
    assert af.pick_top([_row(4, 3, rel=0.5), _row(4, 2, rel=0.5)]).length == 2
    # a gap under 0.01 A is noise and falls through to k
    assert af.pick_top([_row(5, 2, rel=0.501), _row(4, 3, rel=0.504)]).k == 4


def test_the_p95_tie_is_a_tolerance_not_a_rounding_bucket() -> None:
    # 0.496 and 0.504 sit in one 0.01 bucket and 0.494 and 0.503 in two; the
    # rule is "within 0.01 A of the minimum", so a gap of 0.009 is a tie
    # wherever it falls and the narrower k wins
    assert af.pick_top([_row(6, 2, rel=0.496), _row(4, 2, rel=0.504)]).k == 4
    assert af.pick_top([_row(6, 2, rel=0.494), _row(4, 2, rel=0.503)]).k == 4
    # a gap of 0.013 is a real difference whichever side of a bucket it is on
    assert af.pick_top([_row(6, 2, rel=0.490), _row(4, 2, rel=0.503)]).k == 6
    # the tie is measured from the minimum, not chained: 0.40, 0.405, 0.411
    rows = [_row(6, 2, rel=0.40), _row(5, 2, rel=0.405), _row(4, 2, rel=0.411)]
    assert af.pick_top(rows).k == 5
    # then shortest L, then fewest dome rows
    assert af.pick_top([_row(4, 3, rel=0.40), _row(4, 2, rel=0.405)]).length == 2
    tied = [dataclasses.replace(_row(0, 0, rel=0.5), dome_rows=m) for m in (4, 3, 5)]
    assert af.pick_top(tied).dome_rows == 3


def test_an_authored_r_picks_the_nearest_area_matched_candidate() -> None:
    rows = [
        _row(4, 2, rel=0.9, r=9.04),
        _row(4, 3, rel=0.5, r=10.07),
        _row(5, 3, rel=0.7, r=11.77),
    ]
    assert af.pick_top(rows).R == 10.07  # no request: the smallest relaxed p95
    assert af.pick_top(rows, authored_R=9.2).R == 9.04
    assert af.pick_top(rows, authored_R=11.5).R == 11.77


def test_when_no_candidate_meets_the_bars_the_fewest_misses_is_kept() -> None:
    rows = [
        _row(4, 1, rel=0.1, misses=("dev p95", "theta_p")),
        _row(4, 2, rel=0.9, misses=("pairs<1.34",), errors=("geom.clash",)),
        _row(5, 2, rel=0.9, misses=("theta_p",)),
    ]
    best = af.pick_top(rows)
    assert (best.k, best.length) == (5, 2) and not best.meets


# ── candidates that fail, and the refusals (fast) ──────────────────────


def test_one_candidate_that_will_not_build_is_a_miss_not_the_end_of_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(n: int, kind: str, k: int, length: int, **_kw: Any) -> af.TopRow:
        if k == 5:
            raise ValueError("cut.overlap at k=5")
        if k == 6:
            raise HexfoldError("compile failed at k=6")
        return _row(k, length, rel=0.9 - 0.1 * length)

    monkeypatch.setattr(af, "_measure_top", fake)
    plan = af.plan_top(12, "sphere", top_fillet=3.3)  # a key no other test caches
    assert len(plan.rows) == 9
    failed = [r for r in plan.rows if r.error]
    assert {r.k for r in failed} == {5, 6} and len(failed) == 6
    assert all(r.misses == ("build",) and not r.meets for r in failed)
    assert "cut.overlap at k=5" in failed[0].error
    assert "compile failed at k=6" in failed[-1].error
    assert (plan.k, plan.length) == (4, 3)  # the best of the rows that built


def test_a_plan_with_no_candidate_meeting_the_bars_is_refused_with_the_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(n: int, kind: str, k: int, length: int, **_kw: Any) -> af.TopRow:
        if k == 4:
            return _row(k, length, rel=0.5, misses=("theta_p",), theta_p_max=14.0)
        raise ValueError(f"no build at k={k}")

    monkeypatch.setattr(af, "_measure_top", fake)
    with pytest.raises(ValueError, match=r"no sphere top candidate.*theta_p.*no build"):
        af.plan_top(12, "sphere", top_fillet=3.4)


def test_an_explicit_zero_is_refused_not_read_as_absent() -> None:
    with pytest.raises(ValueError, match="top_fillet must be positive; got 0.0"):
        af.plan_top(12, "sphere", top_fillet=0.0)
    with pytest.raises(ValueError, match="top_R must be positive; got 0.0"):
        af.plan_top(12, "sphere", top_R=0.0)
    with pytest.raises(GeneratorError, match="top_fillet must be positive"):
        _scene(top_fillet=0.0)


def test_a_sphere_fillet_larger_than_the_room_is_refused_by_name() -> None:
    # Judged against each candidate's own realised (area-matched) R: the room
    # is R - r_tube, at most ~8.8 A for n=12, so a 12 A fillet fits none.
    with pytest.raises(
        ValueError,
        match=r"no sphere top candidate.*top_fillet 12 A is more than the room",
    ):
        af.plan_top(12, "sphere", top_fillet=12.0)
    with pytest.raises(GeneratorError, match="top_fillet 12 A is more than the room"):
        _scene(top_fillet=12.0)


def test_scene_top_errors_count_only_this_features_top_instances() -> None:
    inst = np.array(["s", "pw", "pc", "qc", "qc", "q"])

    def err(code: str, atoms: list[int]) -> Finding:
        return Finding(
            code,
            Severity.ERROR,
            "x",
            where=str(atoms[0]),
            data=(("atoms", atoms),),
        )

    findings = [
        err("geom.clash", [2, 3]),  # p's lid against q's lid: both own it
        err("geom.seed_overlap", [4, 5]),  # q's lid and q's tube
        err("geom.clash", [0, 5]),  # sheet and q's tube: neither top
        Finding("geom.angle.dev", Severity.WARN, "w", where="3"),
    ]
    assert af._errors_on(findings, inst, ("pw", "pc")) == ("geom.clash",)
    assert af._errors_on(findings, inst, ("qc",)) == ("geom.clash", "geom.seed_overlap")
    assert af._errors_on(findings, inst, ("tc",)) == ()
    only_where = [Finding("geom.clash", Severity.ERROR, "x", where="1")]
    assert af._errors_on(only_where, inst, ("pw",)) == ("geom.clash",)
    assert af._errors_on(only_where, inst, ("qc",)) == ()


# ── the WARNs (fast, synthetic plan) ───────────────────────────────────


def _plan(row: af.TopRow, **kw: Any) -> af.TopPlan:
    base: dict[str, Any] = dict(
        kind="sphere",
        n=12,
        k=row.k,
        length=row.length,
        dome_rows=0,
        drop=row.drop,
        R=row.R,
        fillet=row.fillet,
        authored_R=None,
        authored_fillet=None,
        chosen=row,
        rows=(row,),
    )
    base.update(kw)
    return af.TopPlan(**base)


def test_r_mismatch_and_relaxed_shape_and_bar_warns_name_their_numbers() -> None:
    row = _row(4, 3, rel=0.92, r=10.07, relaxed_dz=0.9)
    codes = {f.code for f in _top_findings("q", _plan(row), row)}
    assert codes == {"scene.top.relaxed_shape", "scene.top.theta_p_band"}
    out = _top_findings("q", _plan(row, authored_R=9.0), row)
    mismatch = next(f for f in out if f.code == "scene.top.R_mismatch")
    assert "9" in mismatch.message and "10.07" in mismatch.message
    shape = next(f for f in out if f.code == "scene.top.relaxed_shape")
    assert "0.92" in shape.message and "tethered geometry stored" in shape.message
    # the number is a stick-model figure and a lower bound (reviewer's wording)
    assert "stick-model relax with the tether off" in shape.message
    assert "A lower bound" in shape.message and "MACE-MP and xTB" in shape.message
    assert "about 2 A at the pole" in shape.message
    assert "~0.9 A flatter at the pole" in shape.message
    # judged on the planner's bare trial tube, unlike scene.top.bar
    assert "planner's bare trial tube, no sheet" in shape.message
    assert "scene.top.bar uses the scene re-measurement" in shape.message
    # within 0.5 A of the request: no mismatch; inside the band: no shape WARN
    ok = _row(4, 2, rel=0.3, r=9.04)
    assert {f.code for f in _top_findings("q", _plan(ok, authored_R=9.3), ok)} == {
        "scene.top.theta_p_band"
    }
    bad = dataclasses.replace(ok, misses=("theta_p",), theta_p_max=13.2)
    assert {f.code for f in _top_findings("q", _plan(bad), bad)} == {
        "scene.top.bar",
        "scene.top.theta_p_band",
    }


# ── real builds (slow) ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sphere12() -> af.TopPlan:
    return af.plan_top(12, "sphere")


@pytest.mark.slow
def test_the_n12_sphere_meets_all_five_bars_and_records_its_plan(
    sphere12: af.TopPlan,
) -> None:
    p, row = sphere12, sphere12.chosen
    assert p.meets and row.misses == ()
    # the five bars, read off the chosen row
    assert row.errors == () and row.pairs == 0
    assert row.dev_p95 <= af.TOP_DEV_P95_A
    assert row.bond_max < af.TOP_BOND_MAX_A
    assert row.theta_p_max <= af.THETA_P_MAX_DEG
    # the plan: k in [r+3, r+5] with r = 1, L in 1..3, never 0
    assert 4 <= p.k <= 6 and 1 <= p.length <= 3
    assert tube_radius(12, 0) < p.R and p.fillet >= af.FILLET_FLOOR_A
    assert len(p.rows) == 9
    # the grid holds both clean and torn candidates; the torn ones were not chosen
    assert any(not r.meets for r in p.rows) and all(r.length >= 1 for r in p.rows)
    # chosen = smallest relaxed p95 among the rows that meet (to 0.01 A)
    meeting = [r for r in p.rows if r.meets]
    assert round(row.relaxed_p95, 2) == min(round(r.relaxed_p95, 2) for r in meeting)
    # R is area-matched: the stored fillet is the default for that R
    assert p.fillet == pytest.approx(
        af.default_fillet(tube_radius(12, 0), p.R), abs=0.1
    )


@pytest.mark.slow
def test_the_n12_sphere_relaxes_flatter_and_the_scene_says_so(
    sphere12: af.TopPlan,
) -> None:
    # The stick relax with the tether off leaves the washer sphere ~0.9 A (p95)
    # from its authored surface and drops the pole ~0.9 A (the scratch MACE
    # measured 0.8-1.1 A RMS and a 2 A pole drop; the stick model is softer).
    # Assert on what the stick relax measures, not on the band: if a better
    # relaxer ever brings this under 0.5 A the WARN must go with it.
    row = sphere12.chosen
    assert row.relaxed_p95 > 0.0 and row.relaxed_dz > 0.0
    assert sphere12.relaxed_ok == (row.relaxed_p95 <= af.RELAXED_P95_A)
    assert not sphere12.relaxed_ok, row.relaxed_p95  # measured 0.92 A when built
    block = _scene()
    topo = block.topology
    findings = topo["report"]["findings"]
    assert not [f for f in findings if f["severity"] == "ERROR"], findings
    shape = [f for f in findings if f["code"] == "scene.top.relaxed_shape"]
    assert len(shape) == 1 and "tethered geometry stored" in shape[0]["message"]
    assert shape[0]["message"] in block.provenance
    rec = topo["plan"]["top_plans"]["q"]
    assert rec["kind"] == "sphere" and rec["bars_met"] is True
    assert rec["k"] == sphere12.k and rec["L"] == sphere12.length
    assert rec["R"] == pytest.approx(sphere12.R) and rec["fillet"] == pytest.approx(
        sphere12.fillet
    )
    assert rec["relaxed_p95"] == pytest.approx(row.relaxed_p95)
    assert rec["relaxed_ok"] is False and len(rec["grid"]) == 9
    # the scene's own measurement of the same top meets the bars too
    assert rec["scene"]["dev_p95"] <= af.TOP_DEV_P95_A
    assert rec["scene"]["theta_p_max"] <= af.THETA_P_MAX_DEG
    # geom.summary now counts every ring corner past tolerance, uncapped
    summary = next(f for f in findings if f["code"] == "geom.summary")
    listed = [f for f in findings if f["code"] == "geom.angle.dev"]
    assert len(listed) == 10 and summary["data"]["angle_n_over"] > 10
    assert summary["data"]["relax"] == "tethered"
    # the foot row is not polluted by the top (no scene.bar WARN)
    assert not [f for f in findings if f["code"] == "scene.bar"], findings


@pytest.mark.slow
def test_an_authored_top_r_far_from_every_candidate_warns_r_mismatch() -> None:
    plan = af.plan_top(12, "sphere", top_R=20.0)
    # nearest candidate to 20 A is the largest meeting one, still far away
    assert plan.authored_R == 20.0 and abs(plan.R - 20.0) > 0.5
    assert max(r.R for r in plan.rows if r.meets) == plan.R
    out = _top_findings("q", plan, plan.chosen)
    assert "scene.top.R_mismatch" in {f.code for f in out}


@pytest.mark.slow
def test_a_rounded_lid_on_the_pill_meets_the_bars() -> None:
    rt = tube_radius(12, 0)
    p = af.plan_top(12, "lid", top_fillet=rt)
    row = p.chosen
    assert p.meets and row.errors == () and row.pairs == 0
    assert row.dev_p95 <= af.TOP_DEV_P95_A and row.bond_max < af.TOP_BOND_MAX_A
    # theta_p ~ 11.6 * 3.55 / 4.7 = 8.8 deg predicted; measured 9.0 deg
    assert row.theta_p_max <= af.THETA_P_MAX_DEG
    assert p.k == 0 and p.length == 0 and p.fillet == pytest.approx(rt)
    assert len(p.rows) == len(af._LID_DOME_ROWS)
    # the relaxed number is recorded; the band decides the WARN
    assert row.relaxed_p95 > 0.0
    block = _scene(top="lid", top_fillet=rt)
    findings = block.topology["report"]["findings"]
    assert not [f for f in findings if f["severity"] == "ERROR"], findings
    rec = block.topology["plan"]["top_plans"]["q"]
    assert rec["kind"] == "lid" and rec["bars_met"] is True
    shape = [f for f in findings if f["code"] == "scene.top.relaxed_shape"]
    assert bool(shape) == (not rec["relaxed_ok"])
    assert block.topology["scene"]["features"][0]["top_fillet"] == pytest.approx(rt)


@pytest.mark.slow
def test_lid_with_fillet_r_and_a_sphere_of_radius_r_describe_the_same_hemisphere() -> (
    None
):
    # Orchestrator, cycle-1 verdict: `top: lid, top_fillet = r` and `top:
    # sphere, top_R = r` (no neck) must agree on the realised R within 0.3 A,
    # or say why not.  They do not agree, for two reasons the test pins:
    #  * the lid's R is the hemisphere its atoms' area covers, and its dome
    #    start is discrete (whole tube atom rows), so the grid brackets r
    #    (4.47..5.92 A for r = 4.70) and the chosen row is 0.30 A off, not
    #    under it: bounded here by 0.4 A;
    #  * a sphere top of R = r has no neck, and the washer route cannot build
    #    one: its smallest candidate is a bulge of (6k,0) with k >= r+3, so no
    #    sphere candidate gets near r (smallest area-matched R ~ 7.9 A).
    rt = tube_radius(12, 0)
    lid = af.plan_top(12, "lid", top_fillet=rt)
    radii = sorted(r.R for r in lid.rows)
    assert radii[0] < rt < radii[-1]  # the grid brackets the hemisphere
    assert abs(lid.R - rt) < 0.4
    sphere = af.plan_top(12, "sphere", top_R=rt)
    assert min(r.R for r in sphere.rows) > rt + 3.0
    assert abs(sphere.R - rt) > 0.5  # R_mismatch fires: r is not buildable


@pytest.mark.slow
def test_the_n24_sphere_is_tried_and_reported() -> None:
    # n = 24 (r = 3, k in 6..8): tried, and the result is the test's record.
    # When built: 5 of 9 candidates meet the five bars; the chosen (k=6, L=3,
    # R ~13.8 A, fillet ~3.1 A) has tethered p95 0.04 A and theta_p 3.8 deg,
    # and relaxes 1.7 A (p95) from the surface, so the relaxed-shape WARN
    # fires as it does at n = 12.
    p = af.plan_top(24, "sphere")
    assert len(p.rows) == 9 and p.meets, [r.misses for r in p.rows]
    assert p.k in (6, 7, 8) and p.length >= 1
    assert p.relaxed_ok == (p.chosen.relaxed_p95 <= af.RELAXED_P95_A)
    assert p.fillet == pytest.approx(
        af.default_fillet(tube_radius(24, 0), p.R), abs=0.1
    )


def test_two_sphere_tops_in_one_call_are_refused_before_any_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_se.atomic.generators import hexfold_scene as mod

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("planning started")

    monkeypatch.setattr(mod, "plan_scene", boom)
    # tabled or live, two spheres are refused: the scene relax is the cost
    for first in (_SPHERE, _LIVE_SPHERE):
        second = {**first, "name": "r", "at": [5, 5]}
        with pytest.raises(
            GeneratorError,
            match=r"at most 1 top: 'sphere' per scene op.*Tabled or not.*476 s",
        ):
            GENERATORS["hexfold_scene"](
                {"sheet": [40, 30], "features": [first, second]}
            )
    # a rounded lid beside a sphere is not capped
    lid = {**_SPHERE, "name": "r", "at": [5, 5], "top": "lid", "top_fillet": 3.0}
    with pytest.raises(AssertionError, match="planning started"):
        GENERATORS["hexfold_scene"](
            {"sheet": [40, 30], "features": [_LIVE_SPHERE, lid]}
        )


def _calls_plan_scene(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace ``plan_scene`` with a recorder that stops the call there: the
    cost ceilings are checked in ``_normalize``, before any planning."""
    from precis_se.atomic.generators import hexfold_scene as mod

    seen: list[int] = []

    def stop(*_a: object, **_k: object) -> object:
        seen.append(1)
        raise AssertionError("planning started")

    monkeypatch.setattr(mod, "plan_scene", stop)
    return seen


def _lid(i: int, fillet: float, n: int = 12) -> dict[str, Any]:
    return {
        "name": f"l{i}",
        "at": [5 + 4 * i, 5],
        "n": n,
        "radius": 5.0,
        "tube_len": 3,
        "top": "lid",
        "top_fillet": fillet,
    }


def _call(features: list[dict[str, Any]]) -> object:
    return GENERATORS["hexfold_scene"]({"sheet": [60, 30], "features": features})


def test_sphere_above_n12_is_refused_before_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _calls_plan_scene(monkeypatch)
    for sphere in (_SPHERE, _LIVE_SPHERE):  # tabled n=18 too: the relax is slow
        with pytest.raises(
            GeneratorError, match=r"n <= 12; got \['q \(n=18\)'\].*476 s"
        ):
            _call([{**sphere, "n": 18}])
    assert not seen


@pytest.mark.parametrize(
    ("features", "cost"),
    [
        ([_LIVE_SPHERE, _lid(1, 3.0)], 13),
        ([_LIVE_SPHERE, _lid(1, 3.0), _lid(2, 4.0)], 17),
        ([_lid(i, 2.0 + i) for i in range(4)], 16),
        ([_lid(i, 2.0 + i) for i in range(5)], 20),
    ],
    ids=["sphere+1lid", "sphere+2lids", "4lids", "5lids"],
)
def test_candidate_budget_counts_sphere_9_and_distinct_lids_4(
    monkeypatch: pytest.MonkeyPatch, features: list[dict[str, Any]], cost: int
) -> None:
    seen = _calls_plan_scene(monkeypatch)
    if cost <= 16:  # accepted: reaches planning
        with pytest.raises(AssertionError, match="planning started"):
            _call(features)
        assert seen
    else:
        with pytest.raises(
            GeneratorError, match=rf"budgets 16 candidate builds.*needs {cost}"
        ):
            _call(features)
        assert not seen


def test_identical_lids_count_once_toward_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _calls_plan_scene(monkeypatch)
    same = [_lid(i, 3.0) for i in range(6)]  # one distinct lid: 4
    with pytest.raises(AssertionError, match="planning started"):
        _call([_LIVE_SPHERE, *same])
    assert seen


# ── the per-n plan table ───────────────────────────────────────────────


def test_the_plan_table_matches_this_hexfold_and_holds_every_key() -> None:
    # A stale table (another hexfold version) is ignored at run time and every
    # top plans live; this test is what says "regenerate":
    #   uv run --with numba python -m precis_se.atomic.generators.authored_foot
    import json

    from hexfold import __version__ as hexfold_version

    doc = json.loads(af.TOP_TABLE_PATH.read_text(encoding="utf-8"))
    assert doc["hexfold"] == hexfold_version, "regenerate the plan table"
    assert doc["k_tether"] == 1.0 and doc["theta_p_max"] == af.THETA_P_MAX_DEG
    for kind, n, fillet in af.table_keys():
        assert af.top_tabled(n, kind, fillet), (kind, n, fillet)
        rows = af._tabled_rows(n, kind, fillet, 1.0, af.THETA_P_MAX_DEG)
        assert rows is not None
        assert len(rows) == len(af._candidates(n, kind))
        assert af.pick_top(rows).meets, (kind, n, [r.misses for r in rows])


def test_a_tabled_top_builds_no_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("a candidate was built")

    monkeypatch.setattr(af, "_measure_top", boom)
    p = af.plan_top(24, "sphere")
    assert p.source == "table" and p.meets
    lid = af.plan_top(12, "lid", top_fillet=af.table_lid_fillet(12))
    assert lid.source == "table" and lid.fillet == af.table_lid_fillet(12)
    # an authored top_R picks among the tabled rows, still without a build
    near = af.plan_top(12, "sphere", top_R=12.0)
    assert near.source == "table" and near.authored_R == 12.0
    # off the table (another fillet, another tether): planned live
    assert not af.top_tabled(12, "lid", 3.0)
    assert not af.top_tabled(12, "sphere", None, k_tether=2.0)


def test_tabled_tops_do_not_count_toward_the_scene_ceilings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _calls_plan_scene(monkeypatch)
    # three distinct live lids (12) beside a tabled sphere and a tabled lid:
    # 12 <= 16, where a live sphere would make it 21
    live_lids = [_lid(i, 2.0 + i) for i in range(3)]
    hemi = _lid(5, af.table_lid_fillet(12))
    with pytest.raises(AssertionError, match="planning started"):
        _call([_SPHERE, hemi, *live_lids])
    assert seen
    with pytest.raises(GeneratorError, match=r"needs 21"):
        _call([_LIVE_SPHERE, hemi, *live_lids])


@pytest.mark.slow
@pytest.mark.parametrize(
    ("kind", "n", "fillet"),
    [("lid", 12, af.table_lid_fillet(12)), ("sphere", 12, None)],
    ids=["lid12", "sphere12"],
)
def test_the_tabled_rows_are_what_the_planner_measures_today(
    kind: str, n: int, fillet: float | None
) -> None:
    # The version check misses a planner change landed without a hexfold
    # bump; re-planning the two n=12 keys live catches one on either path.
    live = af._top_grid(n, kind, fillet, 1.0, af.THETA_P_MAX_DEG)
    table = af._tabled_rows(n, kind, fillet, 1.0, af.THETA_P_MAX_DEG)
    assert table is not None and len(live) == len(table)
    for a, b in zip(live, table, strict=True):
        da, db = dataclasses.asdict(a), dataclasses.asdict(b)
        for key, va in da.items():
            if isinstance(va, float):
                assert va == pytest.approx(db[key], abs=1e-3), (key, a, b)
            else:
                assert va == db[key], (key, a, b)
