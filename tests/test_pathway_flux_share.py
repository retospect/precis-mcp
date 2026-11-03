"""``precis_pathway.flux_share`` — the greedy net-flux decomposition that
attributes a stored kinetics record's target flux to the explorer's
enumerated routes (gr553482). Pure tests over hand-built transition
lists — no engine, no store.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis_pathway import flux_share


def _t(
    a: str,
    b: str,
    r: float,
    *,
    kind: str = "step",
    gas: str | None = None,
) -> dict[str, Any]:
    t: dict[str, Any] = {
        "name": f"{a}->{b}",
        "from": a,
        "to": b,
        "kind": kind,
        "net_rate": r,
    }
    if gas is not None:
        t["gas"] = gas
    return t


def _two_route_record(fa: float = 7.0, fb: float = 3.0, **over: Any) -> dict[str, Any]:
    """Two parallel routes off one shared step: R -> S carries both
    branches, S -fa-> A -fa-> NH3 and S -fb-> B -fb-> NH3; the NH3
    desorption closes each cycle back to R at their sum. TOF = fa+fb —
    the shared step carries everything, the branches split it fa:fb."""
    total = fa + fb
    rec: dict[str, Any] = {
        "product": "NH3",
        "root": "R",
        "tof": total,
        "conditions": {"temperature": 500.0, "pressures": {"N2": 1.0}},
        "transitions": [
            _t("R", "S", total),
            _t("S", "A", fa),
            _t("A", "NH3", fa),
            _t("S", "B", fb),
            _t("B", "NH3", fb),
            _t("NH3", "R", total, kind="desorption", gas="NH3"),
        ],
    }
    rec.update(over)
    return rec


_ROUTES = [["R", "S", "A", "NH3"], ["R", "S", "B", "NH3"]]


def test_decomposition_splits_shared_step_by_bottleneck() -> None:
    d = flux_share.flux_decomposition(_two_route_record())
    assert d is not None
    rates = sorted((p["rate"] for p in d["paths"]), reverse=True)
    assert rates == pytest.approx([7.0, 3.0])
    states = sorted(tuple(p["states"]) for p in d["paths"])
    assert states == [("R", "S", "A", "NH3", "R"), ("R", "S", "B", "NH3", "R")]
    # shares of the extracted paths sum to the TOF within tolerance
    assert math.fsum(rates) == pytest.approx(d["tof"])
    assert d["remainder"] == pytest.approx(0.0)


def test_decomposition_backward_gas_leg_produces_target() -> None:
    """A non-desorption gas exchange releases the product on its BACKWARD
    leg — net_rate < 0 here is the same 10 /site/s of NH3 leaving, per
    ``kinetics.production_rates``' attribution."""
    rec = _two_route_record()
    rec["transitions"][-1] = _t("R", "NH3", -10.0, kind="adsorption", gas="NH3")
    d = flux_share.flux_decomposition(rec)
    assert d is not None
    assert math.fsum(p["rate"] for p in d["paths"]) == pytest.approx(10.0)


def test_decomposition_unreachable_flux_stays_in_the_remainder() -> None:
    """A producing edge no root path can reach is real production the
    routes never account for — the remainder Reto asked to see."""
    rec = _two_route_record(tof=11.0)
    rec["transitions"].append(_t("Z", "R", 1.0, kind="desorption", gas="NH3"))
    d = flux_share.flux_decomposition(rec)
    assert d is not None
    assert math.fsum(p["rate"] for p in d["paths"]) == pytest.approx(10.0)
    assert d["remainder"] == pytest.approx(1.0)


def test_decomposition_none_without_a_usable_record() -> None:
    assert flux_share.flux_decomposition(None) is None
    assert flux_share.flux_decomposition({}) is None
    assert flux_share.flux_decomposition(_two_route_record(transitions="nope")) is None
    assert flux_share.flux_decomposition(_two_route_record(product=None)) is None
    assert flux_share.flux_decomposition(_two_route_record(root=None)) is None
    # non-positive / missing TOF: a solve running backwards or not
    # turning over has no target flux to attribute
    for tof in (0.0, -2.0, None, "nan", float("nan")):
        assert flux_share.flux_decomposition(_two_route_record(tof=tof)) is None


def test_route_flux_shares_sorted_and_dominant() -> None:
    """The picker's payload: every route's share + log10 rate, the
    dominant one flagged, display order by yield — route order in the
    payload follows the input enumeration, ``order`` carries the sort."""
    routes = [_ROUTES[1], _ROUTES[0]]  # the small-share branch enumerated first
    rf = flux_share.route_flux(_two_route_record(), routes)
    assert rf is not None
    a, b = rf["routes"]
    assert a["share"] == pytest.approx(0.3) and not a["dominant"]
    assert b["share"] == pytest.approx(0.7) and b["dominant"]
    assert b["log10"] == pytest.approx(math.log10(7.0))
    assert b["rate"] == pytest.approx(7.0)
    assert rf["order"] == [1, 0]
    assert rf["remainder_share"] == pytest.approx(0.0)
    assert rf["note"] == flux_share.SHARE_NOTE
    assert rf["product"] == "NH3" and rf["tof"] == pytest.approx(10.0)


def test_route_flux_zero_share_dash_and_excluded_step() -> None:
    """Modelled-but-zero -> no share (the picker's dash); a route crossing
    an edge with NO transition between its endpoints was never priced ->
    ``excluded``."""
    rec = _two_route_record()
    rec["transitions"] += [_t("S", "C", 0.0), _t("C", "NH3", 0.0)]
    routes = [
        ["R", "S", "A", "NH3"],  # 0.7 — dominant
        ["R", "S", "C", "NH3"],  # modelled, zero flux -> dash
        ["R", "X", "NH3"],  # R->X unmodelled -> excluded
    ]
    rf = flux_share.route_flux(rec, routes)
    assert rf is not None
    a, c, x = rf["routes"]
    assert a["dominant"] and a["share"] == pytest.approx(0.7)
    assert c["share"] is None and c["excluded"] is False
    assert x["share"] is None and x["excluded"] is True
    assert rf["order"] == [0, 1, 2]


def test_route_flux_folds_history_suffixes() -> None:
    """``meta.graph`` node ids may keep a ``~history`` landing suffix the
    kinetics record strips — mapping must fold it, not lose the route."""
    rf = flux_share.route_flux(_two_route_record(), [["R", "S", "A~n2", "NH3"]])
    assert rf is not None
    assert rf["routes"][0]["share"] == pytest.approx(0.7)


def test_route_flux_none_without_kinetics() -> None:
    """A pathway with no usable kinetics record renders the picker exactly
    as before — ``None``, not an all-dashes payload."""
    assert flux_share.route_flux(None, _ROUTES) is None
    assert flux_share.route_flux({}, _ROUTES) is None
    assert flux_share.route_flux(_two_route_record(tof=0.0), _ROUTES) is None


def test_route_flux_unmatched_paths_land_in_the_remainder() -> None:
    """An extracted path matching no enumerated route is unattributed
    flux — it must land in the remainder, not vanish (review: it used to
    drop silently, leaving shares that never summed to 100%)."""
    rf = flux_share.route_flux(
        _two_route_record(fa=6.0, fb=4.0), [["R", "S", "A", "NH3"]]
    )
    assert rf is not None
    assert rf["routes"][0]["share"] == pytest.approx(0.6)
    assert rf["remainder_share"] == pytest.approx(0.4)


def test_route_flux_gross_over_tof_shares_of_decomposed_flux() -> None:
    """Re-adsorption: 12 desorbs, 2 readsorbs -> net TOF 10 but gross
    decomposition 12. Shares are then fractions of the decomposed flux —
    the note says so — and none reads above 100%."""
    rec = _two_route_record(fa=6.0, fb=4.0)
    rec["transitions"][-1]["net_rate"] = 12.0
    rec["transitions"].append(_t("R", "NH3", 2.0, kind="adsorption", gas="NH3"))
    routes = [*_ROUTES, ["R", "NH3"]]
    rf = flux_share.route_flux(rec, routes)
    assert rf is not None
    assert rf["gross"] is True
    assert "decomposed flux" in rf["note"] and "re-adsorption" in rf["note"]
    shares = [r["share"] for r in rf["routes"]]
    assert all(s is not None and s <= 1.0 for s in shares)
    # the 2-unit R->NH3->R circulation maps to the direct desorption route
    assert shares == pytest.approx([0.5, 1 / 3, 1 / 6])
    assert math.fsum(shares) == pytest.approx(1.0)
    assert rf["remainder_share"] == pytest.approx(0.0)


def test_route_flux_float_noise_over_tof_is_not_gross() -> None:
    """``math.fsum`` of extracted rates can land ~1 ulp over the stored
    tof on non-binary values — that is round-off, not re-adsorption, and
    must not flip the note to the decomposed-flux wording."""
    rf = flux_share.route_flux(_two_route_record(fa=0.1, fb=0.2, tof=0.3), _ROUTES)
    assert rf is not None
    # extracted fsum is 0.30000000000000004 — a hair over tof=0.3
    assert rf["gross"] is False
    assert rf["note"] == flux_share.SHARE_NOTE
    shares = [r["share"] for r in rf["routes"]]
    assert shares == pytest.approx([0.1 / 0.3, 0.2 / 0.3])


def test_decomposition_ignores_side_cycles() -> None:
    """Circulation inside the network (a 2-cycle between A and B) is real
    net flux but sits on no maximal root->target path — the split is
    unchanged, nothing hangs."""
    rec = _two_route_record()
    rec["transitions"] += [_t("A", "B", 5.0), _t("B", "A", 2.0)]
    d = flux_share.flux_decomposition(rec)
    assert d is not None
    assert sorted((p["rate"] for p in d["paths"]), reverse=True) == pytest.approx(
        [7.0, 3.0]
    )


def test_decomposition_two_target_desorption_sites() -> None:
    """Two states each releasing the product — the widest producer is
    extracted first, and each lands on its own route."""
    rec = {
        "product": "NH3",
        "root": "R",
        "tof": 10.0,
        "transitions": [
            _t("R", "S", 10.0),
            _t("S", "A", 6.0),
            _t("A", "P1", 6.0),
            _t("S", "B", 4.0),
            _t("B", "P2", 4.0),
            _t("P1", "R", 6.0, kind="desorption", gas="NH3"),
            _t("P2", "R", 4.0, kind="desorption", gas="NH3"),
        ],
    }
    d = flux_share.flux_decomposition(rec)
    assert d is not None
    assert [p["rate"] for p in d["paths"]] == pytest.approx([6.0, 4.0])
    assert d["paths"][0]["states"] == ["R", "S", "A", "P1", "R"]
    assert d["paths"][1]["states"] == ["R", "S", "B", "P2", "R"]
    rf = flux_share.route_flux(rec, [["R", "S", "A", "P1"], ["R", "S", "B", "P2"]])
    assert rf is not None
    assert [r["share"] for r in rf["routes"]] == pytest.approx([0.6, 0.4])
    assert rf["routes"][0]["dominant"]


def test_decomposition_max_paths_and_rel_tol_stops() -> None:
    """Both stop conditions hold the tail: ``max_paths`` caps the count,
    ``rel_tol`` stops before draining below the fraction of TOF."""
    d = flux_share.flux_decomposition(_two_route_record(), max_paths=1)
    assert d is not None
    assert [p["rate"] for p in d["paths"]] == pytest.approx([7.0])
    assert d["remainder"] == pytest.approx(3.0)
    d = flux_share.flux_decomposition(_two_route_record(), rel_tol=0.5)
    assert d is not None
    assert [p["rate"] for p in d["paths"]] == pytest.approx([7.0])
    assert d["remainder"] == pytest.approx(3.0)
