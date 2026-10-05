"""Offline T0 source, balance and public MCP energetics regressions."""

from __future__ import annotations

import asyncio
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from precis.errors import BadInput
from precis.handlers._rxn_energetics import render_energetics
from precis.runtime import PrecisRuntime
from precis.thermo import F_CONST, parse_equation, pathway_ledger, reaction_energetics
from precis.thermo.data import _evaluate, _records

NO_TO_NH3 = "NO + 5/2 H2 -> NH3 + H2O"
REFERENCE = json.loads(
    (Path(__file__).parent / "fixtures/thermo/nasa7-reference.json").read_text(
        encoding="utf-8"
    )
)


def _kj(v: float | None) -> float:
    assert v is not None
    return v / 1000


def test_no_to_nh3_matches_independent_acceptance() -> None:
    r = reaction_energetics(NO_TO_NH3, T=298.15, n_electrons=5)
    assert _kj(r.dH) == pytest.approx(-378.5, abs=1)
    assert _kj(r.dG) == pytest.approx(-332, abs=1)
    assert r.dG is not None
    assert pytest.approx(-r.dG / (5 * F_CONST)) == r.E
    assert pytest.approx(0.69, abs=0.01) == r.E
    assert not r.unavailable
    assert r.uphill == (False, "ΔG")
    assert {sp.data.source for sp in r.species} == {"nasa7-fit"}
    nh3 = next(sp for sp in r.species if sp.data.formula == "NH3")
    assert _kj(nh3.data.dGf) == pytest.approx(-16.4, abs=0.7)


@pytest.mark.parametrize(
    "equation,target",
    [
        ("N2 -> 2 N", 945),
        ("H2 -> 2 H", 436),
        ("O2 -> 2 O", 498),
        ("H2O -> 2 H + O", 927),
        ("NH3 -> N + 3 H", 1172),
    ],
)
def test_atomization_acceptance(equation: str, target: float) -> None:
    r = reaction_energetics(equation)
    assert not r.unavailable
    assert _kj(r.dH) == pytest.approx(target, abs=2)
    assert r.dH is not None and r.dH > 0


def test_oh_atomization_matches_accepted_nasa_tpis78_source() -> None:
    """Reto/delegate ruling supersedes the rough ATcT 430±2 target."""
    r = reaction_energetics("OH -> O + H")
    assert not r.unavailable
    assert _kj(r.dH) == pytest.approx(427.8239465937423, rel=1e-11, abs=1e-8)
    oh = r.species[0].data
    assert "NASA-TM-4513" in oh.tables and "TPIS78" in oh.tables


@pytest.mark.parametrize("key", sorted(REFERENCE["values"]))
def test_frozen_source_oracle(key: str) -> None:
    """Frozen numbers came from original files evaluated with Cantera 3.2.0,
    without Precis imports; tolerances only cover arithmetic roundoff."""
    record = _records()[key]
    source = REFERENCE["source_hashes"][record["source_file"]]
    assert source["sha256"] == record["source_sha256"]
    for t, props in REFERENCE["values"][key].items():
        h, s, cp = _evaluate(record, float(t))
        assert h == pytest.approx(props["H"], rel=1e-11, abs=1e-6)
        assert s == pytest.approx(props["S"], rel=1e-11, abs=1e-8)
        assert cp == pytest.approx(props["Cp"], rel=1e-11, abs=1e-8)


@pytest.mark.parametrize("key", sorted(REFERENCE["values"]))
def test_fit_boundaries_and_continuity(key: str) -> None:
    record = _records()[key]
    lo, hi = record["ranges"][0], record["ranges"][-1]
    for t in [lo, hi]:
        h, s, cp = _evaluate(record, t)
        assert cp > 0 and s > 0
    for t in [lo - 0.001, hi + 0.001]:
        with pytest.raises(BadInput, match="out of range"):
            _evaluate(record, t)
    for join in record["ranges"][1:-1]:
        left = _evaluate(record, join - 1e-6)
        right = _evaluate(record, join + 1e-6)
        assert left == pytest.approx(right, rel=1e-4, abs=0.1)


def test_temperature_dependence_and_reference_convention() -> None:
    low = reaction_energetics(NO_TO_NH3)
    high = reaction_energetics(NO_TO_NH3, T=1500)
    assert low.dH != high.dH and low.dS != high.dS
    assert high.dH is not None and high.dS is not None
    assert high.dG is not None and low.dG is not None
    assert high.dG == pytest.approx(high.dH - 1500 * high.dS)
    assert high.dG > low.dG
    for t in [298.15, 1000, 1500]:
        r = reaction_energetics("H2 + N2 + O2 -> H2 + N2 + O2", T=t)
        assert r.dH == pytest.approx(0, abs=1e-8)
        assert r.dG == pytest.approx(0, abs=1e-8)
        assert all(sp.data.dHf == 0 and sp.data.dGf == 0 for sp in r.species)


def test_radical_subset_has_published_source_and_explicit_academic_notice() -> None:
    r = reaction_energetics("NO + 3/2 H2 -> NH2OH")
    nh2oh = r.species[-1].data
    assert nh2oh.S is not None and nh2oh.dHf is not None
    assert "ATcT/A" in nh2oh.tables and "TPIS89" not in nh2oh.tables
    assert _kj(nh2oh.dHf) == pytest.approx(-43.949749565546114, abs=1e-8)
    assert not r.unavailable
    for key in ["H2NO", "HNOH", "NH2OH", "HNO", "NH2"]:
        record = _records()[key]
        assert record["paper_doi"] == "10.1016/j.pecs.2018.01.002"
        assert record["original_species_id"] == key
        assert record["original_thermo_reference"]
        assert "no explicit licence grant" in record["permission"]
        assert "academic use, cited" in record["permission"]
        result = reaction_energetics(f"{key} -> {key}").species[0].data
        assert result.charge == 0
        assert "Glarborg et al. (2018)" in result.tables
        assert "no explicit author licence grant" in result.tables
    hnoh = reaction_energetics("H2NO -> HNOH").species[-1].data
    assert hnoh.name is not None and "trans & Equ" in hnoh.name
    assert "not a separately resolved pure-trans or cis fit" in hnoh.note
    assert hnoh.cas is None
    assert _records()["NH2"]["ranges"] == [200, 1000, 3000]
    with pytest.raises(BadInput, match="out of range"):
        reaction_energetics("NH2 -> NH2", T=3000.001)


def test_companion_mechanism_pin_distinguishes_raw_and_transformed_bytes() -> None:
    dataset = json.loads(
        (Path(__file__).parents[1] / "src/precis/thermo/nasa7.json").read_text(
            encoding="utf-8"
        )
    )
    source = dataset["sources"]["Glarborg2018/thermo.dat"]
    assert source == REFERENCE["source_hashes"]["Glarborg2018/thermo.dat"]
    assert source["mechanism_sha256"] == (
        "e90b07e855783551ce1bb3a15df9dac301062972ffe3a2dcc946bf12c8f05f3a"
    )
    assert source["mechanism_raw_bytes"] == 323890
    assert source["mechanism_transformed_sha256"] == (
        "aea1819ad65906306a1e27a83a7ffd9315a41dcb13efc2b0777119ba82724e50"
    )
    assert "CP1252" in source["mechanism_hash_transformation"]
    assert "UTF-8" in source["mechanism_hash_transformation"]
    assert "CRLF retained" in source["mechanism_hash_transformation"]
    assert source["mechanism_url"].endswith("/mech.dat")
    for phrase in [
        "adopted 1 bar interpretation",
        "original reference pressure unverified",
        "entropy constants unadjusted",
    ]:
        assert phrase in dataset["standard_pressure_note"]


@pytest.mark.parametrize(
    "equation",
    ["HNO + 1/2 H2 -> H2NO", "H2NO -> HNOH", "NO + 3/2 H2 -> NH2OH", "NH2 -> NH2"],
)
def test_radical_data_and_result_notes_disclose_adopted_pressure(equation: str) -> None:
    r = reaction_energetics(equation)
    assert not r.unavailable
    rendered = render_energetics(equation).body
    for phrase in [
        "adopted 1 bar interpretation",
        "original reference pressure unverified",
        "entropy constants unadjusted",
    ]:
        assert phrase in " ".join(r.notes) and phrase in rendered
        for species in r.species:
            record = _records()[species.data.formula]
            if record.get("paper_doi"):
                assert phrase in species.data.note and phrase in species.data.tables
                assert phrase in record["standard_pressure_note"]


def test_nasa_only_result_keeps_its_verified_pressure_note() -> None:
    notes = " ".join(reaction_energetics("OH -> O + H").notes)
    assert "NASA-TM-4513 fits: standard pressure 1 bar" in notes
    assert "original reference pressure unverified" not in notes


def test_unavailable_radical_phase_still_discloses_pressure_assumption() -> None:
    r = reaction_energetics("NH2(l) -> NH2(l)")
    assert r.unavailable and r.dH is None and r.dG is None
    for phrase in [
        "adopted 1 bar interpretation",
        "original reference pressure unverified",
        "entropy constants unadjusted",
    ]:
        assert phrase in " ".join(r.notes)
        assert all(phrase in sp.data.note for sp in r.species)


@pytest.mark.parametrize(
    "equation",
    [key for key in REFERENCE["radical_reactions_298_15"] if "->" in key],
)
def test_radical_reactions_match_independent_source_ledger(equation: str) -> None:
    """Original upstream fits evaluated with Cantera, without Precis imports."""
    expected = REFERENCE["radical_reactions_298_15"][equation]
    r = reaction_energetics(equation, n_electrons=1)
    assert not r.unavailable
    assert _kj(r.dH) == pytest.approx(expected["dH_kJ_mol"], abs=1e-8)
    assert _kj(r.dG) == pytest.approx(expected["dG_kJ_mol"], abs=1e-8)
    assert r.dS == pytest.approx(expected["dS_J_mol_K"], abs=1e-8)
    assert pytest.approx(-expected["dG_kJ_mol"] * 1000 / F_CONST) == r.E


def test_neutral_oh_and_isomer_identities() -> None:
    radical = reaction_energetics("OH -> O + H").species[0].data
    assert radical.charge == 0 and radical.name == "hydroxyl radical"
    assert radical.cas == "3352-57-6"
    r = reaction_energetics("CH3OCH3 -> C2H5OH")
    assert [sp.data.name for sp in r.species] == ["dimethyl ether", "ethanol"]
    assert [sp.data.cas for sp in r.species] == ["115-10-6", "64-17-5"]
    assert abs(_kj(r.dH)) > 40
    unknown = reaction_energetics("C2H6O -> C2H6O")
    assert unknown.dH is None and unknown.dG is None
    assert all(sp.data.name is None and sp.data.cas is None for sp in unknown.species)
    assert "identity unavailable" in render_energetics("C2H6O -> C2H6O").body


def test_liquid_phase_and_unsupported_phase() -> None:
    r = reaction_energetics("H2O(l) -> H2O(g)")
    assert _kj(r.dH) == pytest.approx(44, abs=0.2)
    assert _kj(r.dG) == pytest.approx(8.6, abs=0.2)
    unavailable = reaction_energetics("NO + 5/2 H2 -> NH3(l) + H2O")
    assert unavailable.dH is None
    assert any("no approved l-phase fit" in u for u in unavailable.unavailable)


@pytest.mark.parametrize(
    "loose,balanced",
    [
        ("NO + H2 -> NH3 + H2O", NO_TO_NH3),
        ("OH + H2 -> H2O", "OH + 1/2 H2 -> H2O"),
        ("H2O -> H2 + O", "H2O -> H2 + O"),
        ("N2 -> N", "N2 -> 2 N"),
        ("H2 + O2 -> H2O", "H2 + 1/2 O2 -> H2O"),
    ],
)
def test_auto_balance(loose: str, balanced: str) -> None:
    r = reaction_energetics(loose)
    explicit = reaction_energetics(balanced)
    assert r.equation == balanced
    assert r.dH == explicit.dH and r.dG == explicit.dG
    if loose != balanced:
        assert "auto-balanced" in " ".join(r.notes)


def test_balance_energy_electron_scaling_consistency() -> None:
    auto = reaction_energetics("NO + H2 -> NH3 + H2O", n_electrons=5)
    same = reaction_energetics(NO_TO_NH3, n_electrons=5)
    doubled = reaction_energetics("2 NO + 5 H2 -> 2 NH3 + 2 H2O", n_electrons=10)
    assert auto.dH == same.dH and auto.dG == same.dG and auto.E == same.E
    assert same.dH is not None and same.dG is not None
    assert doubled.dH == pytest.approx(2 * same.dH)
    assert doubled.dG == pytest.approx(2 * same.dG)
    assert pytest.approx(same.E) == doubled.E
    assert parse_equation("2NO + 5.0 H2 = 2NH3 + 2H2O").reactants[0].coef == 2
    assert parse_equation("NO + H2 -> NH3 + H2O").reactants[1].coef == Fraction(5, 2)


@pytest.mark.parametrize(
    "equation",
    [
        "NO + H2 -> NH3",
        "H2 -> O2",
        "NO -> HNO",
        "H2 -> H2 + O2",
        "H2 + H -> H2 + O2 + O",
    ],
)
def test_no_valid_positive_balance(equation: str) -> None:
    with pytest.raises(BadInput, match="no valid"):
        parse_equation(equation)


def test_nonunique_balance_requires_explicit_coefficients() -> None:
    with pytest.raises(BadInput, match="nonunique/underdetermined"):
        parse_equation("C + O2 -> CO + CO2")
    assert parse_equation("3 C + 2 O2 -> 2 CO + CO2").reactants[0].coef == 3


@pytest.mark.parametrize(
    "eq",
    [
        "",
        "NO",
        "NO -> ",
        "foo + bar -> baz",
        "Zz + H2 -> ZzH2",
        "NO + 0 H2 -> NO",
        "NO -> NO -> NO",
        "OH- -> O + H",
        "OH+ -> O + H",
        "H0 -> H",
    ],
)
def test_garbage_and_charged_formulas_are_bad_input(eq: str) -> None:
    with pytest.raises(BadInput):
        parse_equation(eq)


@pytest.mark.parametrize("parameter", ["T", "n_electrons"])
@pytest.mark.parametrize(
    "value", [0, -5, float("inf"), float("-inf"), float("nan"), "garbage"]
)
def test_bad_physical_parameters(parameter: str, value: float | str) -> None:
    # Deliberately invalid runtime inputs exercise the rejection contract.
    kwargs: dict[str, Any] = {parameter: value}
    with pytest.raises(BadInput):
        reaction_energetics(NO_TO_NH3, **kwargs)
    with pytest.raises(BadInput):
        render_energetics(NO_TO_NH3, **kwargs)


def test_pathway_cumulative_and_species_rows() -> None:
    equations = ["NO + 1/2 H2 -> HNO", "HNO + 2 H2 -> NH3 + H2O"]
    led = pathway_ledger(equations)
    overall = reaction_energetics(NO_TO_NH3)
    assert led.cum_dH[-1] == pytest.approx(overall.dH)
    assert led.cum_dG[-1] == pytest.approx(overall.dG)
    assert led.steps[0].uphill == (True, "ΔG")
    assert led.steps[1].uphill == (False, "ΔG")
    rendered = render_energetics(";".join(equations)).body
    assert "nitric oxide" in rendered and "nitroxyl" in rendered
    assert "nasa7-fit" in rendered and "cas" in rendered
    with pytest.raises(BadInput, match="single reaction only"):
        render_energetics(";".join(equations), n_electrons=5)
    with pytest.raises(BadInput):
        pathway_ledger([])


@pytest.mark.parametrize("intermediate", ["H2NO", "HNOH"])
@pytest.mark.parametrize("temperature", [298.15, 1500])
def test_actual_radical_pathway_with_partners_is_complete(
    intermediate: str, temperature: float
) -> None:
    equations = [
        "NO + 1/2 H2 -> HNO",
        f"HNO + 1/2 H2 -> {intermediate}",
        f"{intermediate} + 1/2 H2 -> NH2OH",
        "NH2OH + H2 -> NH3 + H2O",
    ]
    led = pathway_ledger(equations, T=temperature)
    assert all(not step.unavailable for step in led.steps)
    assert all(value is not None for value in led.cum_dH + led.cum_dG)
    overall = reaction_energetics(NO_TO_NH3, T=temperature)
    assert led.cum_dH[-1] == pytest.approx(overall.dH)
    assert led.cum_dG[-1] == pytest.approx(overall.dG)
    if temperature == 298.15:
        assert led.steps[0].uphill == (True, "ΔG")
        assert led.steps[1].uphill == (intermediate == "HNOH", "ΔG")
    rendered = render_energetics(";".join(equations), T=temperature).body
    assert "Glarborg et al. (2018)" in rendered and "hydroxylamine" in rendered
    assert (
        "trans & Equ" in rendered if intermediate == "HNOH" else "aminoxyl" in rendered
    )


def test_unknown_identity_still_withholds_dependent_pathway_totals() -> None:
    led = pathway_ledger(["NO + 1/2 H2 -> HNO", "HNO -> NOH", NO_TO_NH3])
    assert led.cum_dH[0] is not None and led.cum_dG[0] is not None
    assert led.cum_dH[1:] == [None, None]
    assert led.cum_dG[1:] == [None, None]
    assert led.steps[-1].dH is not None and led.steps[-1].dG is not None


def test_import_is_lazy() -> None:
    script = "import sys; import precis.thermo; assert not any(x in sys.modules for x in ['sympy','ase','numpy','scipy','chemicals','cantera']); from precis.thermo.data import _records; assert _records.cache_info().currsize == 0"
    subprocess.run([__import__("sys").executable, "-c", script], check=True)


def test_actual_public_get_and_fastmcp_schema(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.server.fastmcp import FastMCP

    from precis.tools import core

    monkeypatch.setattr(core, "_dispatch", runtime_with_store.dispatch)
    server = FastMCP("thermo-public-regression")
    server.add_tool(core.get)
    schema = asyncio.run(server.list_tools())[0].inputSchema["properties"]
    assert "q" in schema and "args" in schema
    # FastMCP parses the actual argument schema before calling tools.core.get,
    # which then forwards extras into the real runtime and handler.
    result = asyncio.run(
        server.call_tool(
            "get",
            {
                "kind": "rxn",
                "view": "energetics",
                "q": "NO + H2 -> NH3 + H2O",
                "args": {"T": 400, "n_electrons": 5},
            },
        )
    )
    text = str(result)
    assert "NO + 5/2 H2 -> NH3 + H2O" in text
    assert "ΔG(400 K)" in text and "E° = " in text
    assert "nitric oxide" in text and "nasa7-fit" in text
    assert "[error:" not in text
    for intermediate in ["H2NO", "HNOH"]:
        pathway = (
            f"NO+1/2 H2->HNO; HNO+1/2 H2->{intermediate}; "
            f"{intermediate}+1/2 H2->NH2OH; NH2OH+H2->NH3+H2O"
        )
        result = asyncio.run(
            server.call_tool(
                "get",
                {
                    "kind": "rxn",
                    "view": "energetics",
                    "q": pathway,
                    "args": {"T": 298.15},
                },
            )
        )
        rendered = str(result)
        assert "[error:" not in rendered and "pathway energetics" in rendered
        assert "Glarborg et al. (2018)" in rendered
        assert (
            "hydroxylamine" in rendered
            and "no explicit author licence grant" in rendered
        )
        assert "−332.6" in rendered or "-332.6" in rendered
    for parameter in ("T", "n_electrons"):
        for value in (0, float("inf"), float("nan")):
            bad = core.get(
                kind="rxn", view="energetics", q=NO_TO_NH3, args={parameter: value}
            )
            assert "BadInput" in str(bad) and "finite" in str(bad)
    bad_pathway = core.get(
        kind="rxn",
        view="energetics",
        q="NO + 1/2 H2 -> HNO; HNO + 2 H2 -> NH3 + H2O",
        args={"n_electrons": 5},
    )
    assert "BadInput" in str(bad_pathway) and "single reaction only" in str(bad_pathway)


def test_get_dispatch_pathway_via_semicolons(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch(
        "get",
        {
            "kind": "rxn",
            "view": "energetics",
            "q": "NO + 1/2 H2 -> HNO; HNO + 2 H2 -> NH3 + H2O",
        },
    )
    assert "[error:" not in out
    assert "pathway energetics" in out and "yes (ΔG)" in out
    assert "nitroxyl" in out and "NASA-TM-4513" in out


def test_get_dispatch_impossible_is_bad_input(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch(
        "get", {"kind": "rxn", "view": "energetics", "q": "NO + H2 -> NH3"}
    )
    assert "[error:BadInput]" in out
