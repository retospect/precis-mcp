"""``precis_web.pathway_kinetics`` — the web-side port of the catpath
report's kinetics trim + rule-based verdict (``autocatpath/report.py``).

The verdict thresholds are the contract: they must say the same thing about
a stored ``meta.results.kinetics`` record that the run report says about the
``kinetics.json`` side-car it was folded from. Pure unit tests — the panel's
HTML itself is built client-side by the vendored ``pathway-kinetics.js``.
"""

from __future__ import annotations

from typing import Any

from precis_web.pathway_kinetics import kinetics_payload, kinetics_verdict


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
    assert v["headline"].startswith("Not determined: 2 steps")
    assert "provisional" in v["lines"][0]


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
    assert len(ml["coverages"]) == 8
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
    assert ml["coverages"] == {} and ml["drc"] == {} and ml["production"] == {}
    assert ml["bracket"] is None
    assert ml["warnings"] == [] and ml["transitions"] == []
