"""SPSO slice 8 Part A — ``window_objectives.attribution_probe``, the
per-corner orchestration of the opposed-corner sensitivity probe on the
Shapley attribution (``docs/backlog/surface-pourbaix-staircase-optimizer.md``,
slice 8 contract, stage 4).

DB-free. One real fixture end to end (a synthetic coverage payload through
``surface_pourbaix.sweep`` -> ``build_map`` -> ``window_measures`` /
``shapley_field``), plus spies on the seams the contract pins (no resweep at
corner 0, one ``wanted_key`` for all corners, the seed, the route pin).

The fixture, on a -0.5..0.5 V window (grid 51, step 0.02): the co-adsorbed
``NH2+H`` mixture (n = 3, the wanted termination) rests between about
-0.3 V, where ``NH2`` (n = 4) gives way below, and 0.24 V, where the
closed-shell ``NO`` takes over (H* never wins). The opposed corners move the
upper edge by about 0.028 V and the lower by 0.047 V either way (``+1``: the
mixture loses, its region shrinks; ``-1`` it grows). Three rungs forbid U
above 0.1, 0.2 and 0.25 V, so the window is the wanted region up to 0.1 V and
its area follows the lower edge.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from precis.quest import window_objectives as wo
from precis_pathway import staircase_masks as sm
from precis_pathway import surface_pourbaix as sp

AREA = 10.0
WANTED = ["NH2+H", 3]
U_OP = -0.1  # inside the mixture's region at every corner
RUNGS = ("A", "B", "C")  # forbid U > 0.1, 0.2, 0.25 V


def _pt(g: float, n: int = 1, counts: dict[str, int] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "n": n,
        "theta": 0.25 * n,
        "energy": -100.0 + g * AREA,
        "gamma": g,
        "converged": True,
        "detached": False,
    }
    if counts:
        d["counts"] = counts
    return d


def _payload(
    shift: float = 0.0, only: str | None = None, mixture: bool = True
) -> dict[str, Any]:
    """``shift`` lifts every gamma by that many eV/A^2 (a positive shift lets
    the clean surface rest above the adsorbates); ``only`` keeps one
    adsorbate; ``mixture=False`` drops the co-adsorbed configuration."""
    ads = {
        "H": {"site": "fcc", "points": [_pt(-0.02 + shift)]},
        "NO": {"site": "fcc", "points": [_pt(-0.08 + shift)]},
        "NH2": {"site": "fcc", "points": [_pt(-0.11 + shift, 4)]},
    }
    if only is not None:
        ads = {only: ads[only]}
    mixtures = (
        [
            {
                "species": ["NH2", "H"],
                "label": "NH2+H",
                "points": [_pt(-0.2 + shift, 3, {"NH2": 2, "H": 1})],
            }
        ]
        if mixture
        else []
    )
    return {
        "model": "mace:medium",
        "conditions": {"temperature": 298.15, "pressures": {}},
        "mu": {"H": -3.4, "NO": -12.0, "NH2": -8.3 + 2 * -3.4},
        "facets": [
            {
                "facet": "111",
                "area": AREA,
                "adsorbates": ads,
                "mixtures": mixtures,
            }
        ],
        "warnings": [],
    }


def _che(
    sigma_e_ev: float = 0.05, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    out = sp.sweep(
        {"mace:medium": payload if payload is not None else _payload()},
        u_window=(-0.5, 0.5),
        grid=51,
        sigma_e_ev=sigma_e_ev,
    )
    return json.loads(json.dumps(out))  # as the job stores it


def _job(che: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref_id": 7,
        "model": "mace:medium",
        "anchor_key": "a" * 64,
        "engine_version": "0.24.1",
        "corrections": None,
        "che_sweep": che,
    }


def _route(name: str, *rungs: tuple[str, float]) -> sm.Route:
    return sm.Route(name, tuple(sm.Rung(n, g0, 1) for n, g0 in rungs))


# viable for U <= -delta_g0 (eps 0, one electron): forbids U above 0.1/0.2/0.25
_WIDE = _route("wide", ("A", -0.1), ("B", -0.2), ("C", -0.25))
_NARROW = _route("narrow", ("A", -0.1), ("B", -0.2), ("C", -0.0))
_WIDE_TWIN = _route("wide-twin", ("A", -0.1), ("B", -0.2), ("C", -0.25))
# forbids U above 0.3/0.4/0.45 V: leaves a window inside the clean region
_CLEAN_WIDE = _route("clean-wide", ("A", -0.3), ("B", -0.4), ("C", -0.45))


def _sr(*pcets: sm.Route, her: sm.Route | None = None) -> Any:
    return SimpleNamespace(routes=list(pcets), pcet_routes=list(pcets), her=her)


def _probe(
    job: dict[str, Any] | None = None, sr: Any = None, u: float = U_OP, seed: int = 11
) -> wo.AttributionProbe:
    return wo.attribution_probe(
        job if job is not None else _job(_che()),
        sr if sr is not None else _sr(_WIDE),
        u,
        seed=seed,
    )


def test_end_to_end_three_corners_and_the_verdict() -> None:
    job = _job(_che())
    before = copy.deepcopy(job)
    res = _probe(job)
    assert job == before  # the input job (and its sweep) is never mutated

    assert res.wanted_key == WANTED
    assert res.route_index == 0 and res.route_name == "wide"
    assert res.rung_names == RUNGS
    assert sorted(res.window_by_corner) == sorted(res.phi_by_corner) == [-1, 0, 1]

    # the wanted region's lower edge sits at about -0.25 / -0.30 / -0.35 V
    # (corners +1 / 0 / -1): the window (wanted and rungs) grows strictly
    # from corner +1 to -1
    area = {c: res.window_by_corner[c]["window_area_VpH"] for c in (-1, 0, 1)}
    assert area[1] < area[0] < area[-1]
    for c in (-1, 0, 1):
        phi = res.phi_by_corner[c]
        assert phi is not None and len(phi) == 3
        assert all(v >= 0.0 for v in phi)
    # A forbids the most wanted area (alone from 0.1 V up), so it is the top rung
    phi0 = res.phi_by_corner[0]
    assert phi0 is not None and max(range(3), key=lambda r: phi0[r]) == 0

    want = sm.attribution_verdict(res.phi_by_corner, res.rung_names)
    assert res.verdict == want
    assert res.verdict.top == 0
    assert res.verdict.verdict in (sm.VERDICT_AMBIGUOUS, sm.VERDICT_NOT_AMBIGUOUS)


def test_corner_zero_is_the_stored_sweep_and_only_shifted_corners_resweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    che = _che()
    calls: list[list[sp.Anchor]] = []
    real = sp.resweep

    def spy(stored: dict[str, Any], anchors: list[sp.Anchor]) -> dict[str, Any]:
        assert stored is che  # always the stored sweep, never a shifted one
        calls.append(anchors)
        return real(stored, anchors)

    monkeypatch.setattr(sp, "resweep", spy)
    _probe(_job(che))
    assert len(calls) == 2  # corners -1 and +1; corner 0 does not resweep
    stored = {a["adsorbate"]: a["gamma0"] for a in che["anchors"]}
    lows, highs = sorted(
        calls, key=lambda c: next(a.gamma0 for a in c if a.adsorbate == "NH2+H")
    )
    # corner -1 (first after sorting on the wanted anchor): wanted gains,
    # others lose; corner +1 the reverse
    for a in lows:
        sign = -1 if a.adsorbate == "NH2+H" else +1
        assert a.gamma0 == pytest.approx(stored[a.adsorbate] + sign * a.sigma_gamma)
    for a in highs:
        sign = +1 if a.adsorbate == "NH2+H" else -1
        assert a.gamma0 == pytest.approx(stored[a.adsorbate] + sign * a.sigma_gamma)


def test_wanted_key_is_read_once_and_never_follows_the_shift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[Any] = []
    real_wanted = wo._wanted_key

    def wanted_spy(*a: Any, **k: Any) -> Any:
        reads.append(real_wanted(*a, **k))
        return reads[-1]

    seen: list[Any] = []
    real_rm = wo.resting_map_for

    def rm_spy(smod: Any, m: dict[str, Any], key: Any) -> Any:
        seen.append(key)
        return real_rm(smod, m, key)

    monkeypatch.setattr(wo, "_wanted_key", wanted_spy)
    monkeypatch.setattr(wo, "resting_map_for", rm_spy)
    # 0.22 V rests on the mixture at corner 0 (edge 0.24) but on NO at corner
    # +1 (edge 0.212): a per-corner re-read would swap the key there.
    res = _probe(u=0.22)
    assert len(reads) == 1 and reads[0] == WANTED
    assert seen == [WANTED, WANTED, WANTED]  # corner 0, then -1 and +1
    assert res.wanted_key == WANTED
    assert res.phi_by_corner[1] is not None
    assert res.window_by_corner[1]["window_area_VpH"] > 0.0


def test_seed_reaches_every_corners_shapley(monkeypatch: pytest.MonkeyPatch) -> None:
    seeds: list[Any] = []
    real = sm.shapley_field

    def spy(*a: Any, **k: Any) -> Any:
        seeds.append((k.get("seed"), k.get("her_margin")))
        return real(*a, **k)

    monkeypatch.setattr(sm, "shapley_field", spy)
    _probe(seed=1234)
    # window_measures' own shapley calls (seedless) are not the probe's
    probe_calls = [s for s in seeds if s[0] is not None]
    assert probe_calls == [(1234, 0.0)] * 3


def test_route_pin_takes_the_largest_window_area() -> None:
    # narrow's C rung (dg0 = 0) cuts the window at 0.0 V: the smaller area
    res = _probe(sr=_sr(_NARROW, _WIDE))
    assert res.route_index == 1 and res.route_name == "wide"
    res = _probe(sr=_sr(_WIDE, _NARROW))
    assert res.route_index == 0 and res.route_name == "wide"


def test_route_pin_ties_go_to_the_lowest_index() -> None:
    res = _probe(sr=_sr(_NARROW, _WIDE, _WIDE_TWIN))
    assert res.route_index == 1
    res = _probe(sr=_sr(_WIDE_TWIN, _NARROW, _WIDE))
    assert res.route_index == 0 and res.route_name == "wide-twin"


def test_route_pin_calls_window_measures_once_per_route_then_reuses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcets: list[str] = []
    real = wo.window_measures

    def spy(resting: Any, routes: Any, her: Any, **k: Any) -> Any:
        assert len(k["pcet_routes"]) == 1 and len(routes) == 1
        pcets.append(k["pcet_routes"][0].name)
        assert k.get("eps_eV") is None and k.get("her_margin_eV") == 0.0
        return real(resting, routes, her, **k)

    monkeypatch.setattr(wo, "window_measures", spy)
    res = _probe(sr=_sr(_NARROW, _WIDE))
    # corner 0 scores both routes; the two shifted corners reuse the pinned one
    assert pcets == ["narrow", "wide", "wide", "wide"]
    assert res.route_index == 1


def test_her_competitor_reaches_window_and_shapley() -> None:
    # HER's limiting step (U - 0.3) sits below the main route's (U - 0.1) at
    # every U, so with margin 0 the HER mask removes the whole window
    her = sm.Route("HER", (sm.Rung("H2", -0.3, 1, kind="her"),))
    plain = _probe()
    with_her = _probe(sr=_sr(_WIDE, her=her))
    assert plain.window_by_corner[0]["window_area_VpH"] > 0.0
    assert with_her.window_by_corner[0]["window_area_VpH"] == 0.0
    # HER is a mask on the baseline, not a rung of the Shapley game: the
    # rungs stay the route's, and with HER removing the whole baseline there
    # is no lost area for the rungs to share
    assert plain.rung_names == with_her.rung_names == RUNGS
    assert with_her.phi_by_corner[0] == (0.0, 0.0, 0.0)
    assert with_her.verdict.verdict == sm.VERDICT_NO_LOST_AREA


def test_empty_wanted_region_at_a_shifted_corner_is_ambiguous() -> None:
    # bars of ~1.4 eV/A^2: at +1 the mixture is pushed above the clean surface
    # and NO everywhere, so nothing rests on the wanted termination there
    res = _probe(_job(_che(sigma_e_ev=10.0)))
    assert res.phi_by_corner[0] is not None
    assert res.phi_by_corner[1] is None
    assert res.phi_by_corner[-1] is not None
    assert res.window_by_corner[1]["window_area_VpH"] == 0.0
    assert res.verdict.verdict == sm.VERDICT_AMBIGUOUS
    assert "empty" in res.verdict.reason and "+1" in res.verdict.reason
    assert res.verdict == sm.attribution_verdict(res.phi_by_corner, res.rung_names)


def test_a_mixture_with_shuffled_counts_is_still_the_wanted_termination() -> None:
    """After a jsonb round trip a mixture anchor's ``counts`` can come back
    in another key order, so ``Anchor.key`` no longer equals the scan's. The
    probe matches the wanted region on ``(adsorbate, n)`` and must still
    shift the mixture as the wanted anchor."""
    che = _che()
    shuffled = copy.deepcopy(che)
    for a in shuffled["anchors"]:
        if a.get("counts"):
            assert list(a["counts"]) == ["NH2", "H"]
            a["counts"] = dict(reversed(list(a["counts"].items())))
            assert list(a["counts"]) == ["H", "NH2"]
    applied = sp.applied_reservoirs(che, "mace:medium")
    orig_mix = next(
        sp.Anchor.from_dict(a, applied) for a in che["anchors"] if a.get("counts")
    )
    shuf_mix = next(
        sp.Anchor.from_dict(a, applied) for a in shuffled["anchors"] if a.get("counts")
    )
    assert shuf_mix.key != orig_mix.key  # the caveat is real here
    assert shuf_mix.slope == pytest.approx(orig_mix.slope)

    want = _probe(_job(che))
    got = _probe(_job(shuffled))
    assert got.wanted_key == want.wanted_key == WANTED
    assert got.phi_by_corner == want.phi_by_corner
    assert got.verdict == want.verdict
    # the mixture really moved: corner +1's window is not corner 0's
    area = [got.window_by_corner[c]["window_area_VpH"] for c in (1, 0)]
    assert area[0] < area[1]


def test_nothing_to_run_on_raises_with_a_next_step() -> None:
    job = _job(_che())
    with pytest.raises(ValueError, match="no che_sweep"):
        wo.attribution_probe({**job, "che_sweep": None}, _sr(_WIDE), U_OP, seed=0)
    with pytest.raises(ValueError, match="pcet_routes is empty"):
        wo.attribution_probe(job, _sr(), U_OP, seed=0)
    with pytest.raises(ValueError, match="no region"):
        wo.attribution_probe(_job({**_che(), "envelope": []}), _sr(_WIDE), U_OP, seed=0)


def _spy_corners(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[list[sp.Anchor]], list[Any]]:
    """Record the anchors each ``resweep`` call gets (corner -1, then +1) and
    every ``RestingMap`` the probe builds (corner 0, -1, +1)."""
    calls: list[list[sp.Anchor]] = []
    maps: list[Any] = []
    real_resweep, real_rm = sp.resweep, wo.resting_map_for

    def resweep_spy(stored: dict[str, Any], anchors: list[sp.Anchor]) -> Any:
        calls.append(anchors)
        return real_resweep(stored, anchors)

    def rm_spy(smod: Any, m: dict[str, Any], key: Any) -> Any:
        maps.append(real_rm(smod, m, key))
        return maps[-1]

    monkeypatch.setattr(sp, "resweep", resweep_spy)
    monkeypatch.setattr(wo, "resting_map_for", rm_spy)
    return calls, maps


def _assert_anchors_shifted(
    che: dict[str, Any],
    calls: list[list[sp.Anchor]],
    wanted: tuple[str, int] | None,
) -> None:
    """``calls`` = [corner -1, corner +1]. An anchor on the ``wanted``
    termination moves by ``+sign * sigma``, every other one (all of them when
    ``wanted`` is None, the clean surface) by ``-sign * sigma``."""
    stored = {(a["adsorbate"], a["n"]): a for a in che["anchors"]}
    assert len(calls) == 2
    for sign, anchors in zip((-1, 1), calls, strict=True):
        assert {(a.adsorbate, a.n) for a in anchors} == set(stored)  # every anchor
        for a in anchors:
            old = stored[(a.adsorbate, a.n)]
            assert a.sigma_gamma > 0.0
            move = (1 if (a.adsorbate, a.n) == wanted else -1) * sign * a.sigma_gamma
            assert a.gamma0 == pytest.approx(old["gamma0"] + move)


def _region_width(maps: list[Any]) -> dict[int, int]:
    """Wanted-region width in U grid points per corner (maps: 0, -1, +1)."""
    return {c: int(m.wanted[0].sum()) for c, m in zip((0, -1, 1), maps, strict=True)}


def test_clean_wanted_shifts_every_anchor_down_at_plus_one_and_the_region_shrinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # shift 0.1 lifts every gamma so the clean surface rests above ~0.2 V (the
    # mixture wins up to there); sigma 0.1 eV gives bars of ~0.014 eV/A^2. The
    # route forbids U above 0.3/0.4/0.45 V, so the window is clean-and-viable.
    che = _che(0.1, _payload(shift=0.1))
    calls, maps = _spy_corners(monkeypatch)
    res = _probe(_job(che), _sr(_CLEAN_WIDE), u=0.35)

    assert res.wanted_key is None
    assert len(che["anchors"]) == 4
    # no anchor matches the clean surface: -sigma at +1, +sigma at -1, for all
    _assert_anchors_shifted(che, calls, None)

    # Measures: the clean region's width in U grid points (from the map the
    # probe builds per corner) and window_area_VpH; both shrink at +1, grow at -1
    width = _region_width(maps)
    assert width[1] < width[0] < width[-1]
    area = {c: res.window_by_corner[c]["window_area_VpH"] for c in (-1, 0, 1)}
    assert area[1] < area[0] < area[-1]
    for c in (-1, 0, 1):
        phi = res.phi_by_corner[c]
        assert phi is not None and len(phi) == 3
    assert res.verdict == sm.attribution_verdict(res.phi_by_corner, res.rung_names)


@pytest.mark.parametrize(
    ("u", "wanted_key"),
    [
        pytest.param(-0.1, ["NH2", 4], id="op-point-rests-on-the-anchor"),
        pytest.param(0.3, None, id="op-point-rests-clean"),
    ],
)
def test_single_anchor_probe_runs_end_to_end_and_shifts_the_lone_anchor(
    monkeypatch: pytest.MonkeyPatch, u: float, wanted_key: Any
) -> None:
    # one anchor, NH2 (n = 4), rests below ~0.1375 V and the clean surface
    # above; the multi-rung route stays (rungs forbid U > 0.1/0.2/0.25 V)
    che = _che(0.1, _payload(only="NH2", mixture=False))
    assert [(a["adsorbate"], a["n"]) for a in che["anchors"]] == [("NH2", 4)]
    calls, maps = _spy_corners(monkeypatch)
    res = _probe(_job(che), u=u)

    assert res.wanted_key == wanted_key
    assert res.rung_names == RUNGS
    # wanted = the anchor: +sigma at +1 (it loses); wanted clean: the anchor
    # takes -sigma at +1 (it wins more, the clean surface loses)
    _assert_anchors_shifted(che, calls, None if wanted_key is None else ("NH2", 4))
    # either way the wanted region (width in U grid points, from the per-corner
    # map) shrinks at +1 and grows at -1
    width = _region_width(maps)
    assert width[1] < width[0] < width[-1]
    for c in (-1, 0, 1):
        phi = res.phi_by_corner[c]
        assert phi is not None and len(phi) == 3
    assert res.verdict == sm.attribution_verdict(res.phi_by_corner, res.rung_names)
