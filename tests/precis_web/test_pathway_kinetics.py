"""``precis_web.pathway_kinetics`` — the web-side port of the catpath
report's kinetics trim + rule-based verdict (``autocatpath/report.py``).

The verdict thresholds are the contract: they must say the same thing about
a stored ``meta.results.kinetics`` record that the run report says about the
``kinetics.json`` side-car it was folded from. Pure unit tests — the panel's
HTML itself is built client-side by the vendored ``pathway-kinetics.js``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

import precis_web
from precis_web.pathway_kinetics import kinetics_payload, kinetics_verdict
from tests._node import needs_node


def _record(**over: Any) -> dict[str, Any]:
    """A small, healthy kinetics.solve record; tests override single keys."""
    rec: dict[str, Any] = {
        "conditions": {"temperature": 500.0, "pressures": {"NO": 0.05}},
        "product": "NH3",
        "tof": 3.2e-4,
        "span_eV": 1.1,
        "tof_span_limit": 8.0e-3,
        "mari_seed": "NO@fcc",
        "coverages": {"NO@fcc": 0.61, "H@fcc": 0.2, "*": 0.19},
        "steady_state": {"nullity": 1, "t_end": 1.0e6},
        "drc": {"X_RC": {"NO*->NOH*": 0.8, "NOH*->NH3*": 0.15}},
        "thermodynamic_drc": {"X_TRC": {"NO@fcc": -0.7}},
        "sensitivity": {
            "tof": {"p5": 1e-5, "p95": 2e-3},
            "n_samples": 200,
            "controlling": "NO*->NOH*",
        },
        "warnings": [],
        "transitions": [
            {
                "name": "NO(g)+* -> NO*",
                "from": "*",
                "to": "NO@fcc",
                "kind": "gas",
                "dG_eV": -0.5,
                "k_f": 1e5,
                "k_b": 1e2,
                "net_rate": 3e-4,
                "gas": "NO",
            },
            {
                "name": "NO* -> NOH*",
                "from": "NO@fcc",
                "to": "NOH@fcc",
                "kind": "step",
                "dG_eV": 0.3,
                "barrier_eV": 0.9,
                "k_f": 10.0,
                "k_b": 1.0,
                "net_rate": 3e-4,
            },
        ],
        "tof_bracket": {
            "tof_slow": 1e-5,
            "tof_fast": 5e-4,
            "load_bearing": [],
            "bounded_steps": ["NOH* -> NH3*"],
            "agree": True,
        },
    }
    rec.update(over)
    return rec


# ── verdict: the trust gate outranks the number ─────────────────────────


def test_verdict_disagreeing_bracket_is_not_determined_and_dead() -> None:
    v = kinetics_verdict(
        _record(
            tof_bracket={
                "tof_slow": 1e-9,
                "tof_fast": 2.0,
                "load_bearing": ["a", "b"],
                "bounded_steps": ["a", "b", "c"],
                "agree": False,
            }
        )
    )
    assert v["tone"] == "dead"
    # exact headline: both plural slots ("steps", "change" not "changes")
    assert v["headline"] == (
        "Not determined: 2 steps with no computed barrier change the answer."
    )
    # the bracket line names the missing steps, never the fallback text
    assert "Missing: a, b." in v["lines"][0]
    assert "provisional" in v["lines"][0]
    one = kinetics_verdict(_record(tof_bracket={"load_bearing": ["a"], "agree": False}))
    assert one["headline"] == (
        "Not determined: 1 step with no computed barrier changes the answer."
    )


def test_verdict_bracket_without_agree_key_stays_trusted() -> None:
    # an old record whose bracket predates the agree flag defaults to trusted
    v = kinetics_verdict(_record(tof_bracket={"tof_slow": 1e-9, "tof_fast": 2.0}))
    assert v["headline"].startswith("Turns over")
    assert v["tone"] == "ok"


def test_verdict_reads_magnitude_then_direction() -> None:
    assert kinetics_verdict(_record(tof=None))["headline"].startswith(
        "No turnover number"
    )
    noise = kinetics_verdict(_record(tof=1e-9))
    assert noise["headline"].startswith("Does not turn over")
    assert noise["tone"] == "warn"
    back = kinetics_verdict(_record(tof=-1e-3))
    assert back["headline"].startswith("Runs backwards")
    assert back["tone"] == "warn"
    ok = kinetics_verdict(_record(tof=5.0))
    assert ok["headline"].startswith("Turns over, active")
    assert ok["tone"] == "ok"
    assert kinetics_verdict(_record(tof=3.2e-4))["headline"].startswith(
        "Turns over, slow but finite"
    )


def test_verdict_names_the_dominant_coverage() -> None:
    lines = kinetics_verdict(_record())["lines"]
    assert any("saturated in NO@fcc" in x for x in lines)
    inhibited = kinetics_verdict(_record(coverages={"NH3@top": 0.8, "*": 0.2}))
    assert any("Product-inhibited" in x for x in inhibited["lines"])
    shared = kinetics_verdict(_record(coverages={"NO@fcc": 0.3, "*": 0.25}))
    assert any("No single intermediate dominates" in x for x in shared["lines"])


def test_verdict_rate_control_bands() -> None:
    strong = kinetics_verdict(_record())["lines"]
    assert any("Rate control sits on NO*->NOH*" in x for x in strong)
    ill = kinetics_verdict(_record(drc={"X_RC": {"s1": 5.0}}))
    assert any("UNUSABLE" in x for x in ill["lines"])
    shared = kinetics_verdict(_record(drc={"X_RC": {"s1": 0.3, "s2": 0.25}}))
    assert any("Rate control is shared" in x for x in shared["lines"])
    brake = kinetics_verdict(_record())["lines"]
    assert any("thermodynamic brake: NO@fcc" in x for x in brake)


def test_verdict_caveats_multiplicity_and_missing_pressure() -> None:
    # the REAL warning wordings, both vintages (report.py handles both via
    # the same regex): pre-0.18 "for the product X", current per-gas "for X;"
    old = kinetics_verdict(
        _record(
            steady_state={"nullity": 2, "t_end": 1.0e6},
            warnings=[
                "kinetics: no pressure stated for the product NH3; "
                "run again with pressures"
            ],
        )
    )
    assert any("2 absorbing components" in c for c in old["caveats"])
    assert any(
        "p(NH3) was never stated" in c and "its gas-exchange rates" in c
        for c in old["caveats"]
    )
    new = kinetics_verdict(
        _record(
            warnings=[
                "kinetics: no pressure stated for NH3; its gas exchange is "
                "priced at the 1 bar reference",
                "kinetics: no pressure stated for N2O; its gas exchange is "
                "priced at the 1 bar reference",
            ]
        )
    )
    assert any(
        "p(NH3, N2O) was never stated" in c and "those gas-exchange rates" in c
        for c in new["caveats"]
    )


def test_verdict_selectivity_side_products() -> None:
    v = kinetics_verdict(
        _record(production={"NH3": 3e-4, "N2O": 1e-4, "NO": -4e-4}, selectivity=0.75)
    )
    (line,) = [x for x in v["lines"] if "Not fully selective" in x]
    assert "N2O at 0.0001 /site/s" in line
    assert "NO" not in line.replace(
        "N2O", ""
    )  # consumption never counts as a side product
    assert "75% of net gas production is NH3" in line
    # fully selective (target only) -> no selectivity line
    clean = kinetics_verdict(_record(production={"NH3": 3e-4}, selectivity=1.0))
    assert not any("Not fully selective" in x for x in clean["lines"])


def test_verdict_product_species_parsed_from_decorated_state_names() -> None:
    # "NH3~n2@top" names the species NH3 (site/instance decorations stripped)
    v = kinetics_verdict(_record(coverages={"NH3~n2@top": 0.8, "*": 0.2}))
    assert any("Product-inhibited" in x for x in v["lines"])


def test_verdict_nonfinite_coverage_dropped_not_compared() -> None:
    # a drifted record with an inf coverage must not win the MARI slot
    v = kinetics_verdict(_record(coverages={"A@top": float("inf"), "H@fcc": 0.6}))
    assert any("saturated in H@fcc" in x for x in v["lines"])


def test_verdict_null_coverage_dropped_not_compared() -> None:
    # json_safe writes non-finite floats as null; a null coverage must not
    # reach max() (TypeError) nor count as a small number
    v = kinetics_verdict(_record(coverages={"NO@fcc": None, "H@fcc": 0.6}))
    assert any("saturated in H@fcc" in x for x in v["lines"])


# ── payload: a trim, not a transform ────────────────────────────────────


def test_payload_none_without_a_kinetics_record() -> None:
    assert kinetics_payload({}) is None
    assert kinetics_payload({"kinetics": {"warnings": []}}) is None
    assert kinetics_payload({"kinetics_error": "engine 0.4.1 lacks kinetics"}) is None


def test_payload_trims_and_keys_by_tier() -> None:
    rec = _record(coverages={f"s{i}": 0.1 for i in range(12)})
    out = kinetics_payload({"kinetics": rec})
    assert out is not None and set(out) == {"ml"}
    ml = out["ml"]
    # gr549757: no fixed cap — the payload is the full θ-desc list and the
    # page applies its display threshold
    assert len(ml["coverages"]) == 12
    assert ml["n_coverages"] == 12
    assert ml["drc"] == rec["drc"]["X_RC"]
    assert ml["trc"] == rec["thermodynamic_drc"]["X_TRC"]
    assert ml["bracket"]["agree"] is True
    assert ml["verdict"]["headline"].startswith("Turns over")
    # transitions keep exactly the keys the panel prints — the solver's
    # internal extras (gas, note, ...) are trimmed out
    assert set(ml["transitions"][0]) == {
        "name",
        "from",
        "to",
        "kind",
        "dG_eV",
        "barrier_eV",
        "k_f",
        "k_b",
        "net_rate",
    }
    dft = kinetics_payload({"kinetics": _record(tier="dft")})
    assert dft is not None and set(dft) == {"dft"}


def test_payload_carries_production_and_selectivity() -> None:
    rec = _record(production={"NH3": 3e-4, "N2O": 1e-4}, selectivity=0.75)
    out = kinetics_payload({"kinetics": rec})
    assert out is not None
    assert out["ml"]["production"] == rec["production"]
    assert out["ml"]["selectivity"] == 0.75


def test_payload_and_verdict_survive_drifted_shapes() -> None:
    """A stored record of any vintage/shape must degrade the panel, never
    raise (the route renders it on a live page, not at report-build time)."""
    junk = _record(
        coverages=["NO@fcc", 0.6],
        tof_bracket="disagree",
        sensitivity=None,
        drc="n/a",
        thermodynamic_drc=7,
        steady_state=[1],
        production="NH3",
        warnings="no pressure stated for NH3; x",
        transitions="none",
    )
    v = kinetics_verdict(junk)
    assert v["headline"].startswith("Turns over")
    assert v["caveats"] == []  # the bare-string warning must not iterate per char
    out = kinetics_payload({"kinetics": junk})
    assert out is not None
    ml = out["ml"]
    assert ml["coverages"] == [] and ml["drc"] == {} and ml["production"] == {}
    assert ml["bracket"] is None
    assert ml["warnings"] == [] and ml["transitions"] == []


# ── D8 reader flip: the v2 record renders the panel ────────────────────


def _v2_record() -> dict[str, Any]:
    """A D8 record on the current pin — the shape prod takes once the
    engine stops writing the v1 block."""
    return {
        "tof_chain": 1.0e-4,
        "tof_mf": 9.1e-3,
        "tof_span_limit": 8.0e-3,
        "coverages": {"fcc": {"NO": 0.61, "H": 0.2, "*": 0.19}},
        "drc": {"NO*->NOH*": 0.8},
        "trc": {"NO@fcc": -0.7},
        "selectivity": {"NH3": 0.9, "N2O": 0.1},
        "uncertainty": {
            "log_tof_p50": -2.0,
            "log_tof_p5": -5.0,
            "log_tof_p95": -1.0,
            "n_samples": 200,
            "controlling_step": {"NO*->NOH*": 0.6},
        },
        "warnings": ["kinetics: no barrier carries a spread"],
        "environment": {
            "T": 500.0,
            "gases": {"inputs": {"NO": 0.05}, "outputs": {"NH3": 1e-3}},
        },
    }


def test_payload_renders_a_v2_only_record() -> None:
    """No ``kinetics`` block at all: the record's shared fields fold into
    the trim shape — ranked MF TOF, flat DRC/TRC back under X_RC/X_TRC,
    per-class coverages as ``token@class``, the log band inverted to a
    linear sensitivity band, the resolved environment as conditions."""
    out = kinetics_payload({"kinetics_v2": _v2_record(), "score": {"target": "NH3"}})
    assert out is not None and set(out) == {"ml"}
    ml = out["ml"]
    assert ml["tof"] == 9.1e-3 and ml["tof_span_limit"] == 8.0e-3
    assert ml["drc"] == {"NO*->NOH*": 0.8}
    assert ml["trc"] == {"NO@fcc": -0.7}
    assert ml["coverages"] == [["NO@fcc", 0.61], ["H@fcc", 0.2], ["*@fcc", 0.19]]
    assert ml["coverage_classes"] == [
        {"cls": "fcc", "states": [["NO", 0.61], ["H", 0.2], ["*", 0.19]]}
    ]
    assert ml["n_coverages"] == 3
    assert ml["product"] == "NH3" and ml["selectivity"] == 0.9
    assert ml["sensitivity"]["tof"]["median"] == pytest.approx(1e-2)
    assert ml["sensitivity"]["n_samples"] == 200
    assert ml["conditions"] == {
        "temperature": 500.0,
        "pressures": {"NO": 0.05, "NH3": 1e-3},
    }
    assert ml["warnings"] == ["kinetics: no barrier carries a spread"]
    assert ml["verdict"]["headline"].startswith("Turns over")
    # v1-only content absent on the record — never fabricated
    assert ml["bracket"] is None and ml["transitions"] == []


def test_payload_v2_tier_read_off_bep_flux_share() -> None:
    """gr514393: the v2 record carries no ``tier`` — what v1 split into
    ``kinetics.dft.json`` vs ``kinetics.json`` the record reports as
    ``bep_flux_share``. Zero share is the v1 ``dft`` case; any share is
    ``ml``; a record lacking the field keeps the v1 block's answer."""
    dft = kinetics_payload(
        {
            "kinetics_v2": {**_v2_record(), "bep_flux_share": 0.0},
            "score": {"target": "NH3"},
        }
    )
    assert dft is not None and set(dft) == {"dft"}
    ml = kinetics_payload(
        {
            "kinetics_v2": {**_v2_record(), "bep_flux_share": 0.3},
            "score": {"target": "NH3"},
        }
    )
    assert ml is not None and set(ml) == {"ml"}
    # v2 without the field (older engine): the v1 block's tier survives
    both = kinetics_payload(
        {"kinetics": _record(tier="dft"), "kinetics_v2": _v2_record()}
    )
    assert both is not None and set(both) == {"dft"}


def test_payload_v2_fields_outrank_v1_but_v1_only_fields_survive() -> None:
    """Both blocks present: the ranked (MF) numbers win; the transitions
    table, bracket, production and tier stay from the v1 block the record
    cannot carry."""
    rec = _record(tof=3.2e-4)
    v2 = {"tof_mf": 7.0, "drc": {"mf_step": 1.0}, "warnings": []}
    out = kinetics_payload({"kinetics": rec, "kinetics_v2": v2})
    assert out is not None
    ml = out["ml"]
    assert ml["tof"] == 7.0
    assert ml["drc"] == {"mf_step": 1.0}
    # the trim keeps the panel's key subset off the v1 transition rows
    assert [t["name"] for t in ml["transitions"]] == [
        t["name"] for t in rec["transitions"]
    ]
    assert ml["bracket"]["agree"] is True
    assert ml["warnings"] == rec["warnings"]  # empty v2 list never erases
    assert ml["verdict"]["headline"].startswith("Turns over")
    # a kmc median outranks the MF TOF the same way the scalars rank it
    kmc = {
        **v2,
        "tof_kmc": {"median": 5.5, "p5": 5.0, "p95": 6.0},
    }
    out2 = kinetics_payload({"kinetics": rec, "kinetics_v2": kmc})
    assert out2 is not None and out2["ml"]["tof"] == 5.5


def _failed_mf_record() -> dict[str, Any]:
    """Shaped like prod pathway 527720: MF Newton failed, only the chain
    TOF (negative) survives, legacy environment, no NH3 selectivity."""
    return {
        "tof_mf": None,
        "tof_kmc": None,
        "tof_chain": -6.77e-9,
        "coverages": None,
        "drc": {},
        "bep_flux_share": 1.0,
        "environment": {"legacy": True},
        "selectivity": {"H2O": 1.0},
        "warnings": [
            "kinetics: mean-field MKM failed (MKMError: mkm: Newton did not converge)",
            "kinetics: DRC is undefined -- the steady-state TOF is not positive",
        ],
    }


def test_payload_reports_a_failed_mf_solve() -> None:
    out = kinetics_payload(
        {
            "kinetics": _record(coverages={"X*": 0.9}),
            "kinetics_v2": _failed_mf_record(),
            "score": {"target": "NH3"},
        }
    )
    assert out is not None
    d = next(iter(out.values()))
    s = d["v2_solve"]
    assert s["rung"] == "chain"
    assert s["mf_failure"] == "Newton did not converge"
    assert s["headline"].startswith(
        "mean-field solve failed: Newton did not converge — showing chain estimate"
    )
    assert s["positive"] is False
    assert "no positive steady state" in s["headline"]
    assert s["legacy"] is True
    assert s["bep_flux_share"] == 1.0
    assert s["selectivity_target"] == "NH3"
    assert s["selectivity_known"] is False
    assert d["coverages"] == []  # v1 coverages do not ride beside a failed MF


def test_payload_healthy_v2_solve_names_its_rung() -> None:
    out = kinetics_payload({"kinetics_v2": _v2_record(), "score": {"target": "NH3"}})
    assert out is not None
    s = next(iter(out.values()))["v2_solve"]
    assert s["rung"] == "mf"
    assert s["mf_failure"] is None
    assert s["positive"] is True
    assert s["headline"] == "TOF from the mf rung"
    assert s["unsettled"] is False and s["failed_observables"] == []


def _solve(**status: Any) -> dict[str, Any]:
    rec = _v2_record()
    rec["mf_status"] = {
        "ok": True,
        "stage": "steady",
        "reason": None,
        "state_err": 0.0,
        "failed_observables": [],
        **status,
    }
    out = kinetics_payload({"kinetics_v2": rec, "score": {"target": "NH3"}})
    assert out is not None
    return next(iter(out.values()))["v2_solve"]


def test_unsettled_state_is_surfaced_and_not_the_green_box() -> None:
    s = _solve(state_err=0.42)
    assert s["unsettled"] is True and s["state_err"] == 0.42
    assert "state not fully settled (ln-coverage step 0.42)" in s["headline"]
    assert "order-of-magnitude" in s["headline"]
    assert s["mf_failure"] is None and s["positive"] is True  # not "dead" either
    # settled to round-off: nothing to say
    assert _solve(state_err=1e-12)["unsettled"] is False
    assert _solve(state_err=None)["unsettled"] is False


def test_failed_observables_come_from_mf_status() -> None:
    s = _solve(
        failed_observables=[
            {"name": "drc", "reason": "singular Jacobian"},
            {"name": "orders"},
            {"reason": "nameless"},
        ]
    )
    assert s["failed_observables"] == [
        {"name": "drc", "reason": "singular Jacobian"},
        {"name": "orders", "reason": ""},
    ]


def test_mf_status_beats_the_warning_and_the_warning_is_the_fallback() -> None:
    warn = ["kinetics: mean-field MKM failed (MKMError: mkm: from the warning)"]
    rec = _failed_mf_record()
    rec["warnings"] = warn + ["kinetics: mean-field observable trc failed (no solve)"]
    rec["mf_status"] = {
        "ok": False,
        "stage": "newton",
        "reason": "MKMError: mkm: from the status",
        "state_err": None,
        "failed_observables": [],
    }
    out = kinetics_payload({"kinetics_v2": rec, "score": {"target": "NH3"}})
    assert out is not None
    s = next(iter(out.values()))["v2_solve"]
    assert s["mf_failure"] == "from the status" and s["mf_stage"] == "newton"
    assert s["failed_observables"] == []  # the status is authoritative
    # a stored record without mf_status: both come from the warnings
    del rec["mf_status"]
    out = kinetics_payload({"kinetics_v2": rec, "score": {"target": "NH3"}})
    assert out is not None
    s = next(iter(out.values()))["v2_solve"]
    assert s["mf_failure"] == "from the warning"
    assert s["failed_observables"] == [{"name": "trc", "reason": "no solve"}]


def test_js_never_renders_unsettled_as_ok() -> None:
    """The panel script picks the box tone from these flags; an unsettled or
    observable-losing solve must take the amber branch before 'ok'."""
    from pathlib import Path

    import precis_web

    js = (
        Path(precis_web.__file__).parent / "static" / "pathway-kinetics.js"
    ).read_text(encoding="utf-8")
    assert '(bad ? "dead" : caution ? "warn" : "ok")' in js
    assert "const caution = S.unsettled || FO.length > 0;" in js
    assert "state not fully settled: ln-coverage step" in js
    assert "failed observables: " in js


# ── gr549757: coverages ordered by θ, vacancies can't win the verdict ────


def test_coverages_sorted_by_theta_not_jsonb_order() -> None:
    """The stored dict arrives in jsonb's key order (length then bytes); the
    payload must sort by θ or the table shows the shortest-named states."""
    rec = _record(
        coverages={
            # jsonb-ordered: short keys first, sizes unrelated to order
            "*": 1e-9,
            "N": 0.348,
            "NH": 2e-6,
            "NO": 3e-7,
            "OH": 1e-8,
            "HNO": 0.056,
            "H2O": 4e-9,
            "HNOH": 0.347,
            "H2NO": 0.247,
        }
    )
    out = kinetics_payload({"kinetics": rec})
    assert out is not None
    assert out["ml"]["coverages"] == [
        ["N", 0.348],
        ["HNOH", 0.347],
        ["H2NO", 0.247],
        ["HNO", 0.056],
        ["NH", 2e-6],
        ["NO", 3e-7],
        ["OH", 1e-8],
        ["H2O", 4e-9],
        ["*", 1e-9],
    ]


def test_verdict_via_payload_never_names_a_vacancy() -> None:
    """gr549757: ``*@hcp`` at θ = 1 is an empty site, not the MARI — the
    verdict sees a vacancy-stripped table (the ported function itself stays
    verbatim for the golden parity gate)."""
    out = kinetics_payload(
        {"kinetics": _record(coverages={"*@hcp": 1.0, "N@fcc": 0.348})}
    )
    assert out is not None
    lines = out["ml"]["verdict"]["lines"]
    assert not any("*@hcp" in x for x in lines)
    assert any("No single intermediate dominates" in x for x in lines)


def test_coverage_classes_keep_each_class_together() -> None:
    """Each site class sums to ~1 independently — the payload keeps the v2
    per-class tables (θ-desc inside, biggest class first) so the page never
    pretends the flattened dict sums to 1."""
    v2 = {
        **_v2_record(),
        "coverages": {
            "hcp": {"*": 1.0, "O": 2e-4},
            "fcc": {"N": 0.348, "HNOH": 0.347, "H2NO": 0.247, "*": 0.05},
        },
    }
    out = kinetics_payload({"kinetics_v2": v2, "score": {"target": "NH3"}})
    assert out is not None
    ml = out["ml"]
    assert ml["coverage_classes"] == [
        {"cls": "hcp", "states": [["*", 1.0], ["O", 2e-4]]},
        {
            "cls": "fcc",
            "states": [["N", 0.348], ["HNOH", 0.347], ["H2NO", 0.247], ["*", 0.05]],
        },
    ]
    # the flat list is the same states, θ-desc across classes, @cls-suffixed
    assert ml["coverages"][0] == ["*@hcp", 1.0]
    assert ml["coverages"][1] == ["N@fcc", 0.348]


def test_rate_control_names_its_solve() -> None:
    """gr549763: the bars' numbers may come from a different solve than the
    warnings describe — the payload says which (``drc_mf or drc["X_RC"]``:
    a failed/empty MF observable folds the chain's dict forward verbatim)."""
    # v2-only record: the flat values are the MF solve's
    out = kinetics_payload({"kinetics_v2": _v2_record(), "score": {"target": "NH3"}})
    assert out is not None
    assert out["ml"]["drc_source"] == "mf"
    assert out["ml"]["trc_source"] == "mf"
    # both blocks: v2's flat dict equal to v1's X_RC means it IS the chain's
    rec = _record()
    v2 = {"tof_mf": 1e-3, "drc": dict(rec["drc"]["X_RC"]), "trc": {}}
    out = kinetics_payload({"kinetics": rec, "kinetics_v2": v2})
    assert out is not None
    assert out["ml"]["drc_source"] == "chain"
    assert out["ml"]["trc_source"] == "chain"
    # v1-only: the chain solve's own sensitivities
    out = kinetics_payload({"kinetics": rec})
    assert out is not None
    assert out["ml"]["drc_source"] == "chain"
    assert out["ml"]["trc_source"] == "chain"


# ── gr549759: the solve status flags a TOF inside solver noise ───────────


def test_near_noise_marks_the_rung_and_hardens_the_headline() -> None:
    """A TOF below the solver's noise floor has no resolved magnitude — the
    status must say so, not merely 'order-of-magnitude'."""
    s = _solve(state_err=0.42, tof_near_noise=True)
    assert s["near_noise"] is True
    assert "noise" in s["headline"]
    assert "order-of-magnitude" not in s["headline"]
    # the same flag read off a legacy record's warning text
    rec = _v2_record()
    rec["warnings"] = [
        "mkm: TOF 6.87e-53 is within 10x the balance residual noise "
        "(3.63e-38 /s); the sign and magnitude are not resolved"
    ]
    out = kinetics_payload({"kinetics_v2": rec, "score": {"target": "NH3"}})
    assert out is not None
    assert out["ml"]["v2_solve"]["near_noise"] is True
    # chain rung carries its own flag
    rec2: dict[str, Any] = {
        "tof_mf": None,
        "tof_chain": -5.17e-26,
        "tof_chain_near_noise": True,
        "warnings": [],
    }
    out = kinetics_payload({"kinetics_v2": rec2, "score": {"target": "NH3"}})
    assert out is not None
    s = out["ml"]["v2_solve"]
    assert s["rung"] == "chain" and s["near_noise"] is True
    # a clean record flags nothing
    assert _solve()["near_noise"] is False


# ── gr549759: every known engine warning has a help entry ────────────────

#: Representative instantiations of every warning the engine emits from
#: ``autocatpath/kinetics.py`` and ``autocatpath/mkm.py`` (the texts carry
#: values, so these are real strings, not templates). When a new engine
#: message reaches the panel it renders verbatim under "unrecognised" —
#: add it here AND to ``WARN_HELP`` in pathway-kinetics.js.
KNOWN_WARNINGS = [
    # the numbers can't be trusted
    "mkm: TOF 6.87e-53 is within 10x the balance residual noise "
    "(3.63e-38 /s); the sign and magnitude are not resolved",
    "kinetics: chain TOF -5.17e-26 is inside its cancellation noise "
    "(4.81e-25); sign and magnitude are not resolved -- read it as zero "
    "within noise, not as a rate",
    "mkm: steady state resolved only to |d ln theta| ~ 0.1 (a slow mode: "
    "near-singular balance Jacobian); coverages and TOF carry that "
    "relative uncertainty",
    "kinetics: mean-field MKM failed (MKMError: mkm: Newton did not converge)",
    "kinetics: mean-field observable ea_app failed "
    "(MKMError: mkm: Newton did not converge)",
    "kinetics: mean-field observable orders skipped (observable time budget spent)",
    "kinetics: mean-field observable orders failed for NH3, NO "
    "(MKMError: mkm: steady state not resolved tightly enough for a "
    "finite difference)",
    "kinetics: DRC is undefined -- the steady-state TOF is not positive",
    "kinetics: thermodynamic DRC is undefined -- the steady-state TOF is not positive",
    "kinetics: X_RC of NO*->NOH* is undefined (a perturbed TOF was not positive)",
    "kinetics: X_TRC of NOH is undefined (a perturbed TOF was not positive)",
    "kinetics: sum of X_RC = 1.234 differs from 1 by more than 0.05 -- "
    "the steady state may be unconverged or the network mis-specified",
    "kinetics: 328 of 500 correlated draws failed a core check or gave "
    "no positive TOF; excluded from the band",
    "kinetics: 40 of 500 Monte-Carlo draws gave no positive TOF; "
    "excluded from the band",
    "kinetics: GUARD -- excluded steps are load-bearing: at their "
    "optimistic bound the TOF(NH3) moves from 1e-9 to 2e-4 /site/s; "
    "missing barriers carrying flux: a->b -- TOF, coverages and rate "
    "control are unreliable until these barriers are computed",
    "kinetics: steady-state residual 1.2e-4 /s is large relative to the "
    "fastest rate constant; coverages and TOF may not be converged",
    "mkm: steady-state residual on 'N' is 1e-7 /s against a flux scale "
    "1e-5; coverages and TOF may not be converged",
    "kinetics: the graph has more than one absorbing component; the "
    "steady state depends on the initial coverages (reported from the "
    "ODE at t = 1000000 s)",
    "kinetics: states with no usable transition (excluded from the model): X, Y",
    "kinetics: step a->b endpoint missing an energy; excluded",
    "kinetics: step a->b has no computed barrier (screening/best-first); "
    "excluded -- never assumed barrierless",
    "kinetics: step a->b has no finite barrier/energy (failed NEBs "
    "aggregate to NaN); excluded -- never guessed",
    "kinetics: provider mlip cannot price step a->b by any rated source; excluded",
    "kinetics: link a -> b endpoint missing an energy; excluded",
    "kinetics: transition a->b has |dG| = 20.00 eV (773 kT); its reverse "
    "rate is outside double precision, so it is treated as one-way and "
    "its detailed balance is unverifiable -- check the endpoint energies "
    "before trusting any rate through it",
    "kinetics: no state releases NH3; the cycle does not close and the "
    "steady-state TOF will be ~0",
    "kinetics: kinetics_v2 record not written (TypeError: x)",
    "kinetics: CONSISTENCY equilibrium_flux failed: edge a->b",
    # assumptions, fixable by stating conditions
    "kinetics: no pressure stated for NH3; its gas exchange is priced at "
    "the 1 bar reference",
    "kinetics: no pressure stated for H2O; its gas exchange is priced at "
    "the 1 bar reference -- for a product gas this over-counts "
    "readsorption; state its real partial pressure for an honest "
    "selectivity",
    "kinetics: no pressure stated for the product NH3; run again with pressures",
    "sticking uncited: H2",
    "kinetics: environment.electrochemical.U_vs_RHE is set but "
    "electrochemical.provider is null — the potential is recorded but "
    "shifts nothing; set provider: 'che' to apply the CHE layer",
    "kinetics: thermo.mode none -- no gas reference carries kT ln p, so "
    "the environment's pressures reach only the Hertz-Knudsen prefactors",
    "kinetics: no thermo table data for NH3(g); its reference stays E-based",
    "kinetics: could not measure E(NH3(g)) (KeyError); its cycle closure "
    "cannot be priced",
    "kinetics: cannot price the closure NOH -> * + NOH(g) (unpriced "
    "gas); closure skipped",
    "kinetics: MARI guess 'X' is not a network state; starting from the clean surface",
    "kinetics: MARI fragment 'X' matches no network state; starting from "
    "the clean surface",
    "kinetics: DFT root '*' not in the run's nodes; state energies stay ML-tier",
    "kinetics: results.dft.json has no referenced 'rel' energies "
    "(re-run refine with gas_references on); state energies stay ML-tier",
    "kinetics: DFT tier requested but nothing in results.dft.json was "
    "applicable; result is ML-tier",
    "kinetics: product 'X' is not a closed-shell gas molecule; no cycle closure",
    "kinetics.tpd: requested adsorbate(s) the network does not desorb "
    "from a bare adsorbate state: NH3",
    # model limits, for information
    "kinetics: link H -> *~h2 exchanges more than one gas (H2+H2); "
    "treated as one barrierless event -- no collision-theory prefactor "
    "is defined for it (v1 limit)",
    "kinetics: link a -> b supplies an element with no reservoir "
    "molecule; treated as a barrierless surface link",
    "kinetics: landing state *~nh2oh sits +0.849 eV off its base * after "
    "ledger pricing (independent relaxations); each transition keeps its "
    "own dG",
    "kinetics: step a->b aggregated delta_e +0.080 eV differs from its "
    "endpoints' node difference +0.200 eV by more than 0.05 eV",
    "kinetics: no barrier carries a spread (single seed and model?); "
    "there is no distribution to sample -- the sensitivity of a single "
    "draw is the DRC itself",
    "kinetics: q_over_k unavailable -- the overall NH3 reaction has an "
    "unpriced gas reference",
    "kinetics: q_over_k -- the overall reaction leaves parked fragments "
    "(N) at infinite dilution; their reference energies are inside dG",
    "kinetics.sweep: net has no slab — site_area_A2 falls back to "
    "6.5 A^2; adsorption prefactors carry that assumption",
    "kinetics: tpd_corrected offsets applied (X +0.10 eV); the "
    "uncorrected energies stay on the base provider",
    "kinetics: electrochemical provider 'che' applied (U_vs_RHE -1.000 "
    "V): every state moves by n_eff*eU (the ledger's PCET count — "
    "supplied O/C carry their reference's embedded reservoir H), every "
    "TS by the omega rule, and the gas references are untouched",
    "kinetics: DFT tier -- 3 state energie(s) and 2 barrier(s) swapped "
    "in at std = 0; the Monte-Carlo band now spans only the remaining "
    "ML-tier spread",
    "kinetics: the target committor did not converge in 2000 iterations "
    "— bep_flux_share uses the partially-relaxed h",
    "kinetics: mean-field TOF departs from the chain by +0.50 decades "
    "(reported, not gated)",
    "kinetics: CONSISTENCY equilibrium_flux did not run (no equilibrium "
    "composition could be formed (x))",
]


def _warn_help_patterns() -> list[re.Pattern[str]]:
    """The ``re`` strings out of ``WARN_HELP`` in pathway-kinetics.js, as
    Python regexes (the table is held to a syntax both engines share)."""
    from pathlib import Path

    import precis_web

    js = (
        Path(precis_web.__file__).parent / "static" / "pathway-kinetics.js"
    ).read_text(encoding="utf-8")
    block = js[js.index("const WARN_HELP") : js.index("const WARN_RES")]
    return [
        re.compile(p.replace("\\\\", "\\"))
        for p in re.findall(r'\bre:\s*"((?:[^"\\]|\\.)*)"', block)
    ]


def test_every_known_engine_warning_has_a_help_entry() -> None:
    """Each known engine message matches at least one WARN_HELP regex, and
    every regex is exercised — a pattern that matches nothing is dead code
    and a message that matches nothing renders raw."""
    pats = _warn_help_patterns()
    assert pats, "WARN_HELP table not found in pathway-kinetics.js"
    hit = [0] * len(pats)
    misses = []
    for w in KNOWN_WARNINGS:
        matched = [i for i, p in enumerate(pats) if p.search(w)]
        if not matched:
            misses.append(w)
        for i in matched:
            hit[i] += 1
    assert misses == []
    dead = [pats[i].pattern for i, n in enumerate(hit) if n == 0]
    assert dead == []


def test_thermodynamic_drc_warning_takes_its_own_entry() -> None:
    """``thermodynamic DRC is undefined`` contains ``DRC is undefined`` —
    first-match order in WARN_HELP must hand it to the TRC entry."""
    pats = _warn_help_patterns()
    first = next(
        i
        for i, p in enumerate(pats)
        if p.search(
            "kinetics: thermodynamic DRC is undefined -- "
            "the steady-state TOF is not positive"
        )
    )
    assert pats[first].pattern == "thermodynamic DRC is undefined"


# ── gr549763: rate-control bars — legend, number on the fill, → names ────

#: Runs the vendored panel script under node against a stub DOM: the script
#: builds the whole panel into ``#kinetics``'s innerHTML off the
#: ``#kinetics-data`` JSON, so the rendered markup is assertable without a
#: browser. Same stub-DOM probe pattern as test_pathway_detail's
#: ``_NODE_URL_JS``.
_NODE_PANEL_JS = r"""
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf-8');
const payload = fs.readFileSync(process.argv[3], 'utf-8');
const host = {innerHTML: ''};
global.document = {
  getElementById: (id) => {
    if (id === 'kinetics') return host;
    if (id === 'kinetics-data') return {textContent: payload};
    return null;
  },
};
(0, eval)(script);
console.log(JSON.stringify({html: host.innerHTML}));
"""


def _render_panel(tmp_path: Path, payload: dict[str, Any]) -> str:
    """The innerHTML the panel script builds for ``payload`` (needs node)."""
    js = Path(precis_web.__file__).parent / "static" / "pathway-kinetics.js"
    (tmp_path / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    driver = tmp_path / "panel_driver.js"
    driver.write_text(_NODE_PANEL_JS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(driver), str(js), str(tmp_path / "payload.json")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout)["html"]


@needs_node
def test_rate_control_legend_explains_each_colour_and_sign(
    tmp_path: Path,
) -> None:
    """Each section says in plain words what its two colours mean — the
    swatch alone told the reader nothing."""
    out = kinetics_payload({"kinetics": _record()})
    assert out is not None
    html = _render_panel(tmp_path, out)
    # X_RC: blue = speeds the cycle, red = slows it
    assert "positive: lowering this step&#39;s barrier speeds the cycle" in html
    assert "negative: making the step faster slows the cycle" in html
    # X_TRC: green = speeds, red = the poison
    assert "positive: binding this state harder speeds the cycle" in html
    assert "a poison" in html and "stabilising it slows the cycle" in html
    # the swatches use the same colours the fills do
    assert "<span class='ksw' style='background:#2b6cb0'></span>" in html
    assert "<span class='ksw' style='background:#2f855a'></span>" in html


@needs_node
def test_rate_control_number_sits_on_the_coloured_fill(tmp_path: Path) -> None:
    """The value rides on the fill right after the step name — the old
    layout parked it past a 220 px track the eye had to cross."""
    out = kinetics_payload({"kinetics": _record()})
    assert out is not None
    html = _render_panel(tmp_path, out)
    assert "ktrack" not in html
    m = re.search(
        r"<span class='kname'>NO\*→NOH\*</span>"
        r"<span class='kfill' style='min-width:(\d+)px;background:#2b6cb0'>"
        r"<span class='kval'>([+-]?\d+\.\d+)</span>",
        html,
    )
    assert m is not None
    assert m.group(2) == "+0.80"


@needs_node
def test_out_of_range_bar_clamps_but_keeps_the_true_number(
    tmp_path: Path,
) -> None:
    """|X| beyond the 220 px scale used to draw the same full-width bar as
    everything else; now the fill clamps, the caption says the scale is
    relative, the section flags itself, and the printed number stays true."""
    out = kinetics_payload(
        {"kinetics": _record(drc={"X_RC": {"NO*->NOH*": -249.94, "a->b": 0.5}})}
    )
    assert out is not None
    html = _render_panel(tmp_path, out)
    m = re.search(
        r"<span class='kname'>NO\*→NOH\*</span>"
        r"<span class='kfill' style='min-width:(\d+)px;background:#a33'>"
        r"<span class='kval'>([+-]?\d+\.\d+)</span>",
        html,
    )
    assert m is not None
    assert int(m.group(1)) <= 220  # the bar is clamped …
    assert m.group(2) == "-249.94"  # … the number is not
    assert "scaled to the largest shown" in html
    assert "unusual X_RC values" in html
    # a small value still draws SOMETHING: the fill floors at the number's
    # width rather than vanishing (min-width 0 + the kval chip)
    assert "floored at the number's width" in html


@needs_node
def test_step_names_render_real_arrows_but_keep_raw_keys(
    tmp_path: Path,
) -> None:
    """``->`` displays as → wherever the panel prints a step name — bars,
    the rate-constant table, the steps table, the verdict — while the
    payload's data keys stay the raw stored names."""
    out = kinetics_payload({"kinetics": _record()})
    assert out is not None
    assert "NO*->NOH*" in out["ml"]["drc"]  # the data key is raw
    html = _render_panel(tmp_path, out)
    # bars
    assert "NO*→NOH*" in html
    # the numbered rate-constant table and the per-step table
    assert "NO(g)+* → NO*" in html
    assert "NO* → NOH*" in html
    # the verdict sentence that embeds the same step name
    assert "Rate control sits on NO*→NOH*" in html
    # nothing raw survives anywhere in the rendered panel
    assert "->" not in html
