"""``precis_dft.pourbaix_bulk`` — the bulk Pourbaix verdict engine.

Hand-built entries, no network. Formation free energies (298.15 K, kJ/mol,
converted to eV per formula unit) and their sources:

* Cu(s), P(s, white), Rh(s): 0 — the element reference states.
* Cu2O(s) −146.0, CuO(s) −129.7, Cu²⁺(aq) +65.49 — Wagman et al., "The NBS
  tables of chemical thermodynamic properties", J. Phys. Chem. Ref. Data 11,
  Suppl. 2 (1982).
* HCuO2⁻(aq) −258.5 — the tabulated value in Bard, Parsons & Jordan,
  *Standard Potentials in Aqueous Solution* (IUPAC/Dekker, 1985).
* H2PO4⁻(aq) −1130.28, HPO4²⁻(aq) −1089.15, PO4³⁻(aq) −1018.7 — Wagman et
  al. 1982.
* Cu3P(s) −50 — NOT a tabulated value: an assumed order of magnitude
  (≈ −0.13 eV/atom, the scale of DFT formation energies for late-transition-
  metal phosphides). The Cu3P test sweeps −100 … −10 kJ/mol and the verdict
  at its point is the same across that range, so the assertion does not
  hang on the number.

pymatgen supplies μ(H2O) and the Nernst prefactor itself.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pymatgen.analysis.phase_diagram import PDEntry
from pymatgen.analysis.pourbaix_diagram import PREFAC, IonEntry, PourbaixEntry
from pymatgen.core import Composition
from pymatgen.core.ion import Ion

from precis_dft import pourbaix_bulk as pb

_KJ_PER_MOL_TO_EV = 1.0 / 96.485


def _solid(formula: str, dgf_kj: float) -> Any:
    # PourbaixEntry documents PDEntry/IonEntry inputs but annotates only
    # ComputedEntry; route through Any.
    entry: Any = PDEntry(Composition(formula), dgf_kj * _KJ_PER_MOL_TO_EV)
    return PourbaixEntry(entry)


def _ion(formula: str, dgf_kj: float) -> Any:
    entry: Any = IonEntry(Ion.from_formula(formula), dgf_kj * _KJ_PER_MOL_TO_EV)
    return PourbaixEntry(entry)


def _cu() -> list[Any]:
    return [
        _solid("Cu", 0.0),
        _solid("Cu2O", -146.0),
        _solid("CuO", -129.7),
        _ion("Cu[2+]", 65.49),
        _ion("HCuO2[-]", -258.5),
    ]


def _p() -> list[Any]:
    return [
        _solid("P", 0.0),
        _ion("H2PO4[-]", -1130.28),
        _ion("HPO4[2-]", -1089.15),
        _ion("PO4[3-]", -1018.7),
    ]


def _cu3p(dgf_kj: float = -50.0) -> list[Any]:
    return [*_cu(), *_p(), _solid("Cu3P", dgf_kj)]


def _point(u: float, ph: float) -> dict[str, float]:
    return {"U_RHE": u, "pH": ph}


# ── acceptance: point verdicts ────────────────────────────────────────


def test_cu_at_ph7_cathodic_is_stable() -> None:
    r = pb.verdict(_cu(), {"Cu": 1}, [], _point(-0.2, 7))
    assert r["verdict"] == "stable"
    assert r["domain"] == ["Cu(s)"]
    assert r["dG_pbx_eV_atom"] == pytest.approx(0.0, abs=1e-9)
    assert r["matched_phase"] == {"formula": "Cu", "entry_id": None, "distance": 0.0}


def test_cu_at_ph1_anodic_is_dissolved() -> None:
    r = pb.verdict(_cu(), {"Cu": 1}, [], _point(0.6, 1))
    assert r["verdict"] == "dissolved"
    assert r["domain"] == ["Cu[+2]"]
    assert r["point"]["ions"] == ["Cu[+2]"]
    assert r["point"]["solids"] == []
    # Cu → Cu²⁺ at 1e-6 M: ΔG/Cu = 2·V_SHE − (ΔGf(Cu²⁺) + PREFAC·log10 1e-6)
    v = 0.6 - PREFAC * 1
    expected = 2 * v - (65.49 * _KJ_PER_MOL_TO_EV - 6 * PREFAC)
    assert r["dG_pbx_eV_atom"] == pytest.approx(expected, abs=1e-4)


def test_cu_at_ph13_anodic_is_oxidised() -> None:
    r = pb.verdict(_cu(), {"Cu": 1}, [], _point(0.6, 13))
    assert r["verdict"] == "oxidised"
    assert r["point"]["solids"] == ["Cu2O(s)"]
    # 2Cu + H2O → Cu2O + 2H⁺ + 2e⁻ is pH-neutral on the RHE scale:
    # ΔG/Cu = U_RHE − (ΔGf(Cu2O) − μ(H2O))/2, with μ(H2O) as pymatgen has it.
    from pymatgen.analysis.pourbaix_diagram import MU_H2O

    expected = 0.6 - (-146.0 * _KJ_PER_MOL_TO_EV - MU_H2O) / 2
    assert r["dG_pbx_eV_atom"] == pytest.approx(expected, abs=1e-4)
    assert r["dG_pbx_eV_atom"] > pb.DEFAULT_STABILITY_TOL


@pytest.mark.parametrize("dgf_cu3p", [-100.0, -50.0, -10.0])
def test_cu3p_at_cathodic_point_leaches_phosphate(dgf_cu3p: float) -> None:
    # U_RHE −0.2 V, pH 7 (qu202468's operating point): H2PO4⁻ is the stable
    # P species and Cu stays metallic — Cu3P → 3 Cu(s) + H2PO4⁻.
    r = pb.verdict(_cu3p(dgf_cu3p), {"Cu": 3, "P": 1}, [], _point(-0.2, 7))
    assert r["verdict"] == "leached"
    assert r["matched_phase"]["formula"] == "Cu3P"
    assert r["point"]["solids"] == ["Cu(s)"]
    assert r["point"]["ions"] == ["H2PO4[-1]"]
    assert r["dG_pbx_eV_atom"] > 0
    assert "Surviving solid(s): Cu(s); leached ion(s): H2PO4[-1]." in r["text"]


def test_cu3p_below_the_kept_combinations_is_its_own_domain() -> None:
    # At −0.4 V RHE a −100 kJ/mol Cu3P sits below Cu + phosphate. pymatgen's
    # multi-element diagram drops the lone Cu3P MultiEntry (its
    # process_multientry reads the identical reactant's coefficient), so the
    # engine restores it: the verdict is stable, not a phantom leach.
    r = pb.verdict(_cu3p(-100.0), {"Cu": 3, "P": 1}, [], _point(-0.4, 7))
    assert r["verdict"] == "stable"
    assert r["domain"] == ["Cu3P(s)"]
    assert r["dG_pbx_eV_atom"] == 0.0
    assert r["point"]["domain_restored"] is True
    # Where pymatgen's own domain stands, the flag stays down.
    leached = pb.verdict(_cu3p(-100.0), {"Cu": 3, "P": 1}, [], _point(-0.2, 7))
    assert leached["point"]["domain_restored"] is False


def test_cu2o_reduced_to_cu_is_transformed() -> None:
    r = pb.verdict(_cu(), {"Cu": 2, "O": 1}, [], _point(-0.2, 7))
    assert r["matched_phase"]["formula"] == "Cu2O"
    assert r["verdict"] == "transformed"
    assert r["point"]["solids"] == ["Cu(s)"]


def test_stability_tol_moves_the_stable_boundary() -> None:
    # At pH 13, +0.55 V Cu sits ~0.077 eV/atom above Cu2O.
    loose = pb.verdict(_cu(), {"Cu": 1}, [], _point(0.55, 13), tol=0.1)
    tight = pb.verdict(_cu(), {"Cu": 1}, [], _point(0.55, 13), tol=0.05)
    assert loose["dG_pbx_eV_atom"] == pytest.approx(0.0774, abs=1e-3)
    assert loose["verdict"] == "stable"
    assert loose["point"]["note"] == (
        "Cu is 0.077 eV/atom above Cu2O(s) (within stability_tol 0.1)"
    )
    assert tight["verdict"] == "oxidised"
    assert tight["basis"]["stability_tol"] == 0.05


# ── acceptance: window ────────────────────────────────────────────────


def test_window_spanning_dissolved_and_stable() -> None:
    window = {"U_RHE": [-0.2, 0.6], "pH": [1, 7]}
    r = pb.verdict(_cu(), {"Cu": 1}, [], _point(-0.2, 7), window)
    assert r["verdict"] == "stable"
    assert r["worst_in_window"] == "dissolved"
    assert r["dissolved_everywhere"] is False
    assert len(r["grid"]) == 25
    verdicts = {c["verdict"] for c in r["grid"]}
    assert {"stable", "dissolved"} <= verdicts


def test_window_dissolved_everywhere() -> None:
    window = {"U_RHE": [0.5, 0.7], "pH": [0, 2]}
    r = pb.verdict(_cu(), {"Cu": 1}, [], _point(0.6, 1), window, grid=3)
    assert len(r["grid"]) == 9
    assert r["worst_in_window"] == "dissolved"
    assert r["dissolved_everywhere"] is True


def test_window_axis_absent_stays_at_point() -> None:
    r = pb.verdict(_cu(), {"Cu": 1}, [], _point(-0.2, 7), {"U_RHE": [-0.4, 0.0]})
    assert len(r["grid"]) == 5
    assert {c["pH"] for c in r["grid"]} == {7.0}


def test_no_window_is_point_only() -> None:
    r = pb.verdict(_cu(), {"Cu": 1}, [], _point(0.6, 1))
    assert r["grid"] == []
    assert r["worst_in_window"] == "dissolved"
    assert r["basis"]["window"] is None
    assert "Point only (no window given)." in r["text"]


# ── acceptance: matching + dopants ────────────────────────────────────


def test_host_phase_mismatch_is_unmatched() -> None:
    # CuP is 0.5 (L1) from Cu3P, the nearest solid — past the 0.15 cut-off.
    r = pb.verdict(_cu3p(), {"Cu": 1, "P": 1}, [], _point(-0.2, 7), {"pH": [5, 9]})
    assert r["verdict"] == "unmatched"
    assert r["worst_in_window"] == "unmatched"
    assert r["dissolved_everywhere"] is False
    assert r["matched_phase"]["formula"] == "Cu3P"
    assert r["matched_phase"]["distance"] == pytest.approx(0.5)
    assert r["dG_pbx_eV_atom"] is None


def test_off_stoichiometric_slab_matches_nearest_phase() -> None:
    # A Cu3P(001) slab is rarely 3:1 — 26:10 is 0.056 from Cu3P.
    r = pb.verdict(_cu3p(), {"Cu": 26, "P": 10}, [], _point(-0.2, 7))
    assert r["matched_phase"]["formula"] == "Cu3P"
    assert r["matched_phase"]["distance"] == pytest.approx(
        2 * (0.75 - 26 / 36), abs=1e-6
    )
    assert r["verdict"] == "leached"


def test_pure_metal_host_matches_metal_not_its_oxides() -> None:
    # Cu, Cu2O and CuO are all distance 0 over non-O/H fractions; the
    # all-element tie-break picks the metal.
    m = pb.match_host_phase(_cu(), {"Cu": 1})
    assert m is not None and m["formula"] == "Cu"


def test_lowest_energy_polymorph_wins() -> None:
    entries = [*_cu(), _solid("Cu2O", -100.0)]
    m = pb.match_host_phase(entries, {"Cu": 2, "O": 1})
    assert m is not None
    assert m["entry"].entry.energy == pytest.approx(-146.0 * _KJ_PER_MOL_TO_EV)


def test_rh_dopant_in_cu_reported_separately() -> None:
    # Rh: only its element reference — this checks the dopant is reported
    # beside the host verdict, not Rh's aqueous chemistry.
    entries = [*_cu(), _solid("Rh", 0.0)]
    r = pb.verdict(entries, {"Cu": 35}, {"Rh": 1 / 36}, _point(-0.2, 7))
    assert r["verdict"] == "stable"
    assert r["matched_phase"]["formula"] == "Cu"
    assert r["verdict_source"] == "host_phase"
    assert r["flags"] == [pb.DOPANT_FLAG] == ["pourbaix:dopant-unassessed"]
    (rh,) = r["dopants"]
    assert rh["element"] == "Rh"
    assert rh["flag"] == "pourbaix:dopant-unassessed"
    assert rh["verdict_source"] == "elemental"
    assert rh["verdict"] == "stable"
    assert rh["host_atom_fraction"] == pytest.approx(1 / 36)
    assert "Dopant Rh not assessed in the alloy" in r["text"]


def test_dopant_without_entries_is_unmatched_not_stable() -> None:
    r = pb.verdict(_cu(), {"Cu": 35}, ["Rh"], _point(-0.2, 7))
    assert r["dopants"][0]["verdict"] == "unmatched"
    assert r["verdict"] == "stable"


# ── acceptance: the result carries its inputs ─────────────────────────


def test_result_carries_every_input() -> None:
    window = {"U_RHE": [-0.4, 0.0], "pH": [7, 10]}
    r = pb.verdict(
        _cu(),
        {"Cu": 1},
        [],
        _point(-0.2, 7),
        window,
        0.08,
        ion_conc_M=1e-5,
        grid=4,
        mp_version="2025.09.25",
        composition_path="ops",
    )
    b = r["basis"]
    assert b["U_RHE"] == -0.2
    assert b["pH"] == 7.0
    assert b["ion_conc_M"] == 1e-5
    assert b["window"] == window
    assert b["grid"] == 4
    assert b["stability_tol"] == 0.08
    assert b["mp_version"] == "2025.09.25"
    assert b["matched_phase"] == "Cu"
    assert b["match_distance"] == 0.0
    assert b["composition_path"] == "ops"
    assert b["prefac"] == PREFAC
    assert r["dG_pbx_eV_atom"] == pytest.approx(0.0, abs=1e-9)
    assert r["domain"] == ["Cu(s)"]
    assert r["source"] == "Materials Project 2025.09.25, CC-BY 4.0"
    assert pb.NECESSARY_NOT_SUFFICIENT in r["text"]
    assert pb.GAS_LIMIT in r["text"]
    assert "Data: Materials Project 2025.09.25, CC-BY 4.0." in r["text"]
    assert len(r["grid"]) == 16


@pytest.mark.parametrize("path", ["fresh", "cache_round_trip"])
def test_ion_concentration_reaches_the_diagram(path: str) -> None:
    # Cu/Cu²⁺ is +0.34 V SHE at 1 M and +0.16 V at 1e-6 M; +0.3 V RHE at
    # pH 1 is +0.24 V SHE, between the two. The flip must survive the job's
    # cache path too (entries serialised at whatever concentration they had).
    entries = _cu()
    if path == "cache_round_trip":
        entries = pb.entries_from_json(pb.entries_to_json(entries))
    dilute = pb.verdict(entries, {"Cu": 1}, [], _point(0.3, 1), ion_conc_M=1e-6)
    molar = pb.verdict(entries, {"Cu": 1}, [], _point(0.3, 1), ion_conc_M=1.0)
    assert dilute["verdict"] == "dissolved"
    assert dilute["domain"] == ["Cu[+2]"]
    # Cu → Cu²⁺ at 1e-6 M: ΔG/Cu = 2·V_SHE − (ΔGf(Cu²⁺) + PREFAC·log10 1e-6)
    v = 0.3 - PREFAC * 1
    expected = 2 * v - (65.49 * _KJ_PER_MOL_TO_EV - 6 * PREFAC)
    assert dilute["dG_pbx_eV_atom"] == pytest.approx(expected, abs=1e-4)
    assert molar["verdict"] == "stable"
    assert molar["domain"] == ["Cu(s)"]
    assert molar["basis"]["ion_conc_M"] == 1.0


def test_symmetric_tolerance_at_the_cu_cu2_line() -> None:
    # 1e-6 M Cu/Cu²⁺ at pH 1 sits at +0.221 V RHE. Just past it (+0.26 V)
    # Cu²⁺ is lower but by less than the tolerance → stable, noted; further
    # in (+0.40 V) the margin exceeds it → dissolved.
    near = pb.verdict(_cu(), {"Cu": 1}, [], _point(0.26, 1))
    far = pb.verdict(_cu(), {"Cu": 1}, [], _point(0.40, 1))
    assert 0 < near["dG_pbx_eV_atom"] <= pb.DEFAULT_STABILITY_TOL
    assert near["domain"] == ["Cu[+2]"]
    assert near["verdict"] == "stable"
    assert "Cu[+2]" in near["point"]["note"]
    assert f"{near['dG_pbx_eV_atom']:.3f} eV/atom above Cu[+2]" in near["point"]["note"]
    assert far["dG_pbx_eV_atom"] > pb.DEFAULT_STABILITY_TOL
    assert far["verdict"] == "dissolved"
    assert "note" not in far["point"]


def test_matched_cuo_reduced_to_cu2o_is_transformed() -> None:
    # pH 13, +0.6 V RHE: the domain is Cu2O. From CuO that is a loss of O
    # per Cu (1 → 0.5) → transformed; from Cu a gain (0 → 0.5) → oxidised.
    # CuO sits ~0.02 eV/atom above Cu2O here, so the tolerance is tightened.
    from_cuo = pb.verdict(_cu(), {"Cu": 1, "O": 1}, [], _point(0.6, 13), tol=0.01)
    from_cu = pb.verdict(_cu(), {"Cu": 1}, [], _point(0.6, 13), tol=0.01)
    assert from_cuo["matched_phase"]["formula"] == "CuO"
    assert from_cuo["dG_pbx_eV_atom"] > 0.01
    assert from_cuo["point"]["solids"] == ["Cu2O(s)"]
    assert from_cuo["verdict"] == "transformed"
    assert "Solid(s) at the point: Cu2O(s)." in from_cuo["text"]
    assert from_cu["point"]["solids"] == ["Cu2O(s)"]
    assert from_cu["verdict"] == "oxidised"


class _StubDiagram:
    """A diagram answering a fixed ΔG and domain — drives the restore guard
    without depending on where pymatgen's hull lands."""

    def __init__(self, dg: float, domain: Any) -> None:
        self.dg, self.domain = dg, domain

    def get_decomposition_energy(self, entry: Any, pH: float, V: float) -> float:
        return self.dg

    def get_stable_entry(self, pH: float, V: float) -> Any:
        return self.domain


def test_restore_ignores_numerical_noise() -> None:
    cu, cu2o = _solid("Cu", 0.0), _solid("Cu2O", -146.0)
    cell = pb._point_eval(
        _StubDiagram(-1e-9, cu2o), cu, frozenset({"Cu"}), 0.6, 13, 0.1
    )
    assert cell["domain_restored"] is False
    assert cell["domain"] == ["Cu2O(s)"]  # pymatgen's domain kept
    assert cell["verdict"] == "stable"
    # The noise margin prints as 0, never "-0.000 eV/atom above".
    assert "Cu is 0.000 eV/atom above" in cell["note"]


def test_restore_on_a_real_negative_is_flagged() -> None:
    cu, cu2o = _solid("Cu", 0.0), _solid("Cu2O", -146.0)
    cell = pb._point_eval(
        _StubDiagram(-0.05, cu2o), cu, frozenset({"Cu"}), 0.6, 13, 0.1
    )
    assert cell["domain_restored"] is True
    assert cell["domain"] == ["Cu(s)"]
    assert cell["dG_pbx_eV_atom"] == 0.0
    assert cell["verdict"] == "stable"
    assert "note" not in cell


# ── helpers ───────────────────────────────────────────────────────────


def test_v_she_uses_pymatgen_prefac() -> None:
    assert pb.prefac() == PREFAC
    assert pb.v_she(0.6, 13) == pytest.approx(0.6 - PREFAC * 13)


def test_worst_order() -> None:
    assert pb.worst(["stable", "oxidised", "unmatched"]) == "oxidised"
    assert pb.worst(["transformed", "leached"]) == "leached"
    assert pb.worst(["stable", "dissolved"]) == "dissolved"
    assert pb.worst([]) is None
    assert pb.VERDICT_ORDER == (
        "dissolved",
        "leached",
        "transformed",
        "oxidised",
        "unmatched",
        "stable",
    )


def test_chemsys_drops_o_and_h() -> None:
    assert pb.chemsys({"P", "Cu", "O", "H"}) == "Cu-P"


def test_chemsys_over_three_elements_refused() -> None:
    with pytest.raises(ValueError, match="at most 3"):
        pb.verdict(_cu(), {"Cu": 1, "Ni": 1, "Pd": 1, "Pt": 1}, [], _point(0, 7))


def test_empty_host_refused() -> None:
    with pytest.raises(ValueError, match="no non-O/H element"):
        pb.verdict(_cu(), {"O": 1}, [], _point(0, 7))


def test_entries_json_round_trip_reproduces_verdict() -> None:
    entries = _cu3p()
    back = pb.entries_from_json(pb.entries_to_json(entries))
    assert [e.name for e in back] == [e.name for e in entries]
    window = {"U_RHE": [-0.4, 0.2], "pH": [5, 9]}
    a = pb.verdict(entries, {"Cu": 3, "P": 1}, [], _point(-0.2, 7), window)
    b = pb.verdict(back, {"Cu": 3, "P": 1}, [], _point(-0.2, 7), window)
    assert a["grid"] == b["grid"]
    assert a["point"] == b["point"]


def test_module_imports_nothing_from_precis() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(pb.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert "precis" not in roots
    assert roots <= {
        "__future__",
        "math",
        "collections",
        "typing",
        "pymatgen",
        "mp_api",
    }


# ── opt-in live check against the Materials Project ──────────────────


@pytest.mark.mp_live
def test_live_mp_cu_cases(record_property: Any) -> None:
    key = os.environ.get("PRECIS_MP_API_KEY", "").strip()
    if not key:
        pytest.fail("-m mp_live needs PRECIS_MP_API_KEY in the environment")
    version = pb.database_version(key)
    record_property("mp_version", version)
    print(f"Materials Project database version: {version}")
    entries = pb.fetch_entries(key, "Cu")
    cases = {(-0.2, 7): "stable", (0.6, 1): "dissolved", (0.6, 13): "oxidised"}
    got = {
        pt: pb.verdict(entries, {"Cu": 1}, [], _point(*pt), mp_version=version)[
            "verdict"
        ]
        for pt in cases
    }
    assert got == cases, f"MP {version}"
