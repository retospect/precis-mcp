"""precis.thermo — tabulated reaction energetics (offline, no DB except the
one dispatch test)."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.runtime import PrecisRuntime
from precis.thermo import F_CONST, parse_equation, pathway_ledger, reaction_energetics

NO_TO_NH3 = "NO + 5/2 H2 -> NH3 + H2O"


def _kj(v: float | None) -> float:
    assert v is not None
    return v / 1000


def test_no_to_nh3_matches_tabulated_values() -> None:
    r = reaction_energetics(NO_TO_NH3, T=298.15, n_electrons=5)
    assert _kj(r.dH) == pytest.approx(-378.5, abs=1.0)
    assert _kj(r.dG) == pytest.approx(-332.0, abs=1.0)
    assert r.dG is not None
    assert pytest.approx(-r.dG / (5 * F_CONST)) == r.E
    assert pytest.approx(0.69, abs=0.01) == r.E
    assert not r.unavailable
    assert r.uphill == (False, "ΔG")
    assert {sp.data.source for sp in r.species} == {"tabulated"}
    # NH3 ΔGf° (literature -16.4 kJ/mol) via element-S° correction
    nh3 = next(sp for sp in r.species if sp.data.formula == "NH3")
    assert _kj(nh3.data.dGf) == pytest.approx(-16.4, abs=0.5)


def test_missing_entropy_makes_dg_unavailable_but_keeps_dh() -> None:
    r = reaction_energetics("NO + 3/2 H2 -> NH2OH")
    assert r.dH is not None
    # ATcT ΔHf°; the Joback group-contribution estimate (-161.8) is excluded
    assert all("JOBACK" not in sp.data.tables for sp in r.species)
    assert _kj(r.dH) == pytest.approx(-43.48 - 91.089, abs=0.05)
    assert r.dG is None
    assert r.dS is None
    assert any("NH2OH(g) S° unavailable" in u for u in r.unavailable)
    assert any("xtb-idealgas" in n for n in r.notes)
    # E° needs ΔG: stays None even when asked for
    assert reaction_energetics("NO + 3/2 H2 -> NH2OH", n_electrons=3).E is None


def test_radical_absent_from_tables_is_unavailable_not_guessed() -> None:
    r = reaction_energetics("HNO + 1/2 H2 -> H2NO")
    assert r.dH is None
    assert r.dG is None
    assert any("H2NO(g) ΔHf° not in the tabulated data" in u for u in r.unavailable)


def test_liquid_phase_hint_changes_dh() -> None:
    r = reaction_energetics("NO + 5/2 H2 -> NH3 + H2O(l)")
    assert _kj(r.dH) == pytest.approx(-422.0, abs=1.0)
    water = next(sp for sp in r.species if sp.data.formula == "H2O")
    assert water.data.phase == "l"


def test_phase_without_tabulated_data_is_unavailable_not_gas_fallback() -> None:
    r = reaction_energetics("NO + 5/2 H2 -> NH3(l) + H2O")
    assert r.dH is None
    assert any("NH3(l)" in u for u in r.unavailable)


def test_temperature_note_and_dg_uses_t() -> None:
    r298 = reaction_energetics(NO_TO_NH3)
    r400 = reaction_energetics(NO_TO_NH3, T=400.0)
    assert r298.dH == r400.dH
    assert r400.dG is not None and r298.dG is not None and r400.dS is not None
    assert r400.dG == pytest.approx(r400.dH - 400.0 * r400.dS)  # type: ignore[operator]
    assert r400.dG > r298.dG  # ΔS < 0, so ΔG rises with T
    assert any("no Cp correction" in n for n in r400.notes)
    assert not any("no Cp correction" in n for n in r298.notes)


def test_pathway_cumulative_equals_overall_and_flags_uphill() -> None:
    led = pathway_ledger(["NO + 1/2 H2 -> HNO", "HNO + 2 H2 -> NH3 + H2O"])
    overall = reaction_energetics(NO_TO_NH3)
    assert led.cum_dH[-1] == pytest.approx(overall.dH)
    assert led.cum_dG[-1] == pytest.approx(overall.dG)
    assert led.steps[0].uphill == (True, "ΔG")
    assert led.steps[1].uphill == (False, "ΔG")


def test_pathway_cumulative_unavailable_from_first_gap() -> None:
    led = pathway_ledger(
        ["NO + 1/2 H2 -> HNO", "HNO + 1/2 H2 -> H2NO", "H2NO + 3/2 H2 -> NH3 + H2O"]
    )
    assert led.cum_dH[0] is not None
    assert led.cum_dH[1] is None and led.cum_dH[2] is None
    assert led.steps[1].uphill is None


def test_pathway_uphill_judged_on_dh_when_dg_unavailable() -> None:
    led = pathway_ledger(["NH2OH -> NO + 3/2 H2", "NO + 3/2 H2 -> NH2OH"])
    assert led.steps[0].dG is None
    assert led.steps[0].uphill == (True, "ΔH")
    assert led.steps[1].uphill == (False, "ΔH")


@pytest.mark.parametrize("eq", ["NO + H2 -> NH3 + H2O"])
def test_unbalanced_names_the_element(eq: str) -> None:
    with pytest.raises(BadInput) as ei:
        parse_equation(eq)
    msg = str(ei.value)
    assert "not balanced" in msg
    assert "N" in msg and "H" in msg


def test_unbalanced_single_element() -> None:
    with pytest.raises(BadInput, match=r"O \(product side has 1 fewer"):
        parse_equation("H2 + O2 -> H2O")


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
    ],
)
def test_garbage_is_bad_input(eq: str) -> None:
    with pytest.raises(BadInput):
        parse_equation(eq)


def test_coefficient_forms_and_separators() -> None:
    for eq in (
        "2NO + 5H2 = 2NH3 + 2H2O",
        "2 NO + 5.0 H2 → 2 NH3 + 2 H2O",
        "NO + 2.5 H2 -> NH3 + H2O",
    ):
        assert parse_equation(eq).products


def test_bad_t_and_n_electrons() -> None:
    with pytest.raises(BadInput):
        reaction_energetics(NO_TO_NH3, T=-5)
    with pytest.raises(BadInput):
        reaction_energetics(NO_TO_NH3, n_electrons=0)


def test_get_dispatch_delivers_args_to_handler(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch(
        "get",
        {
            "kind": "rxn",
            "view": "energetics",
            "q": NO_TO_NH3,
            "__extras__": {"T": 400.0, "n_electrons": 5},
        },
    )
    assert "[error:" not in out, out
    assert "ΔG(400 K)" in out
    assert "E° = " in out and "n = 5" in out
    assert "no Cp correction" in out
    assert "tabulated" in out


def test_get_dispatch_pathway_via_semicolons(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch(
        "get",
        {
            "kind": "rxn",
            "view": "energetics",
            "q": "NO + 1/2 H2 -> HNO; HNO + 2 H2 -> NH3 + H2O",
        },
    )
    assert "[error:" not in out, out
    assert "pathway energetics" in out
    assert "yes (ΔG)" in out


def test_get_dispatch_unbalanced_is_bad_input(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch(
        "get",
        {"kind": "rxn", "view": "energetics", "q": "NO + H2 -> NH3"},
    )
    assert "[error:BadInput]" in out
