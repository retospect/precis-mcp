"""``mode='tc'`` — the cheap-tier phonon-mediated-Tc estimator (gr709908).

Two layers:

* :func:`precis.structure.tc.tc_estimate` — Hopfield–McMillan λ +
  Allen–Dynes T_c as a pure function, checked against the spec's known
  cases (Nb λ≈1.0 T_c≈9 K; MgB₂ λ≈0.7–0.9 T_c≈39 K inside its band;
  Al λ≈0.4 T_c≈1.2 K).
* ``put(kind='structure', id=<slug>, mode='tc', args={...})`` — the
  synchronous door that stamps ``meta['last_tc']``; the frontier's
  ``mechanism_evidence`` flag / λ/Tc measures and the tick's
  ``· tc: <verdict>`` trailer read it back, versioned like
  ``last_electronic`` (a superseded-geometry stamp claims nothing).
"""

from __future__ import annotations

import json

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.structure import StructureHandler
from precis.structure.tc import allen_dynes, tc_estimate

_NBN = json.dumps(
    {
        "cell": {"a": 12.0, "b": 12.0, "c": 30.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Nb", "frac": [0.0, 0.0, 0.5]},
            {"op": "add_atom", "element": "Nb", "frac": [0.5, 0.5, 0.55]},
            {"op": "add_atom", "element": "N", "frac": [0.0, 0.0, 0.62]},
            {"op": "add_atom", "element": "N", "frac": [0.5, 0.5, 0.45]},
        ],
    }
)


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


# ── the physics — spec's known cases ────────────────────────────────


class TestKnownCases:
    def test_nb(self) -> None:
        """bcc Nb: λ≈1.0, T_c≈9 K (measured 9.25 K)."""
        est = tc_estimate(n_ef=1.0, theta_d_k=275, composition={"Nb": 1})
        assert est.lambda_ep == pytest.approx(1.0, abs=0.2)
        assert est.verdict == "phonon-mediated plausible"
        # μ*=0.13 is the strong-screened convention Nb quotes at.
        assert est.tc_k_mu013 == pytest.approx(9.0, abs=4.0)
        assert est.omega_log_k == pytest.approx(0.75 * 275)

    def test_mgb2(self) -> None:
        """MgB₂: λ≈0.7–0.9; the measured 39 K sits inside the honest band
        (two-gap anisotropy is exactly what plain Allen–Dynes misses — the
        band, not the central value, is the claim)."""
        est = tc_estimate(n_ef=0.25, theta_d_k=700, composition={"Mg": 1, "B": 2})
        assert 0.6 < est.lambda_ep < 1.0
        assert est.tc_band_k[0] < 39.0 < est.tc_band_k[1]
        assert est.verdict == "phonon-mediated plausible"

    def test_al(self) -> None:
        """fcc Al: λ≈0.4, T_c≈1.2 K (measured 1.18 K)."""
        est = tc_estimate(n_ef=0.45, theta_d_k=428, composition={"Al": 1})
        assert est.lambda_ep == pytest.approx(0.4, abs=0.12)
        assert est.tc_k == pytest.approx(1.2, abs=0.9)

    def test_insulator_is_absent(self) -> None:
        """n_ef = 0 → no states at E_F, the phonon channel is closed:
        λ = T_c = 0 and the verdict says absent, never a silent mid."""
        est = tc_estimate(n_ef=0.0, theta_d_k=300, composition={"Nb": 1})
        assert est.lambda_ep == 0.0 and est.tc_k == 0.0
        assert est.verdict == "phonon-mediated absent"

    def test_weak_coupling(self) -> None:
        """A nonzero but tiny λ reads weak, not plausible."""
        est = tc_estimate(n_ef=0.08, theta_d_k=300, composition={"Cu": 1})
        assert 0 < est.lambda_ep < 0.3
        assert est.verdict == "phonon-mediated weak"

    def test_light_element_couples_harder_at_same_eta(self) -> None:
        """The per-species mass in M⟨ω²⟩ is load-bearing: the same η and
        DOS on an 11-amu boron couples far harder than on 93-amu niobium."""
        boron = tc_estimate(n_ef=1.0, theta_d_k=275, composition={"B": 1})
        niobium = tc_estimate(n_ef=1.0, theta_d_k=275, composition={"Nb": 1})
        assert boron.lambda_ep > niobium.lambda_ep

    def test_fallback_element_is_named(self) -> None:
        """An element outside the ⟨I²⟩ table still estimates — on the
        wide generic range — and names itself in i2_fallback."""
        est = tc_estimate(n_ef=1.0, theta_d_k=300, composition={"Xe": 1})
        assert est.i2_fallback == ("Xe",)
        assert est.lambda_ep > 0

    def test_i2_override(self) -> None:
        est = tc_estimate(
            n_ef=1.0,
            theta_d_k=275,
            composition={"Nb": 1},
            i2={"Nb": [6.0, 9.0]},
        )
        assert est.i2["Nb"] == (6.0, 9.0)
        assert est.lambda_band[0] < est.lambda_ep < est.lambda_band[1]

    def test_omega_log_overrides_theta_d(self) -> None:
        est = tc_estimate(
            n_ef=1.0, theta_d_k=999, omega_log_k=200, composition={"Nb": 1}
        )
        assert est.omega_log_k == pytest.approx(200.0)

    def test_input_validation(self) -> None:
        with pytest.raises(ValueError):
            tc_estimate(n_ef=-1.0, theta_d_k=275, composition={"Nb": 1})
        with pytest.raises(ValueError):
            tc_estimate(n_ef=1.0, composition={"Nb": 1})
        with pytest.raises(ValueError):
            tc_estimate(n_ef=1.0, theta_d_k=0, composition={"Nb": 1})
        with pytest.raises(ValueError):
            tc_estimate(n_ef=1.0, theta_d_k=275, composition={})

    def test_allen_dynes_denominator_guard(self) -> None:
        """λ <= μ*(1+0.62λ) → the exponential's denominator is
        non-positive; the honest answer is 0, not a negative-Tc lie."""
        assert allen_dynes(0.05, 300.0, 0.10) == 0.0
        assert allen_dynes(0.0, 300.0, 0.10) == 0.0


# ── the door + the stamped evidence ─────────────────────────────────


class TestPutTc:
    def test_stamp_and_receipt(self, structure) -> None:
        structure.put(id="nbn_slab", text=_NBN)
        resp = structure.put(
            id="nbn_slab",
            mode="tc",
            args={"n_ef": 1.0, "theta_d": 275},
        )
        assert "lambda_ep" in resp.body and "verdict" in resp.body
        ref = structure.store.get_ref(kind="structure", id="nbn_slab")
        last = ref.meta["last_tc"]
        assert last["on_version"] == int(ref.meta["version"])
        assert last["lambda_ep"] > 0
        assert last["tc_k"] > 0
        assert last["omega_log_k"] == pytest.approx(0.75 * 275)
        assert last["n_ef"] == pytest.approx(1.0)
        assert "phonon-mediated" in last["verdict"]

    def test_frontier_reads_the_stamp(self, structure, store) -> None:
        """The gripe's ask: an energy-only frontier row gains λ/Tc
        measures and 'mechanism evidence: present'."""
        from precis.quest.frontier import _candidate_from_structure

        structure.put(id="nbn_slab", text=_NBN)
        ref = structure.store.get_ref(kind="structure", id="nbn_slab")
        c0 = _candidate_from_structure(store, ref)
        assert c0.flags["mechanism_evidence"] == "absent"
        assert "lambda_ep" not in c0.measures

        structure.put(id="nbn_slab", mode="tc", args={"n_ef": 1.0, "theta_d": 275})
        ref = structure.store.get_ref(kind="structure", id="nbn_slab")
        c = _candidate_from_structure(store, ref)
        assert c.flags["mechanism_evidence"] == "present"
        assert c.flags["tc_verdict"] == "phonon-mediated plausible"
        assert c.measures["lambda_ep"] > 0
        assert c.measures["tc_k"] > 0
        assert c.measures["omega_log_k"] == pytest.approx(0.75 * 275)

    def test_stale_version_stamp_claims_nothing(self, structure, store) -> None:
        """A stamp describing superseded geometry must not keep reporting
        evidence — same contract as last_electronic."""
        from precis.quest.frontier import _candidate_from_structure

        structure.put(id="nbn_slab", text=_NBN)
        structure.put(id="nbn_slab", mode="tc", args={"n_ef": 1.0, "theta_d": 275})
        # Re-put bumps the version — the estimate now describes old geometry.
        structure.put(id="nbn_slab", text=_NBN)
        ref = structure.store.get_ref(kind="structure", id="nbn_slab")
        c = _candidate_from_structure(store, ref)
        assert c.flags["mechanism_evidence"] == "absent"
        assert "tc_verdict" not in c.flags
        assert "lambda_ep" not in c.measures

    def test_frontier_summary_tc_tail(self, store) -> None:
        from precis.quest import tick as quest_tick
        from precis.quest.frontier import Candidate, FrontierResult

        fr = FrontierResult(
            objectives=[("energy", "min")],
            frontier=[
                Candidate(
                    ref_id=1,
                    handle="st1",
                    name="nbn-slab",
                    measures={"energy": -1.2, "lambda_ep": 0.9, "tc_k": 10.1},
                    converged=True,
                    flags={
                        "mechanism_evidence": "present",
                        "tc_verdict": "phonon-mediated plausible",
                    },
                )
            ],
        )
        out = quest_tick._frontier_summary(store, 0, fr=fr)
        assert "mechanism evidence: present" in out
        assert "tc: phonon-mediated plausible" in out

    def test_no_quest_logbook_when_unserved(self, structure) -> None:
        """A structure serving no quest still stamps — the receipt just
        has no logbook line to report."""
        structure.put(id="nbn_slab", text=_NBN)
        resp = structure.put(
            id="nbn_slab", mode="tc", args={"n_ef": 1.0, "theta_d": 275}
        )
        assert "quest logbook" not in resp.body

    def test_input_refusals(self, structure) -> None:
        structure.put(id="nbn_slab", text=_NBN)
        with pytest.raises(BadInput):
            structure.put(mode="tc", args={"n_ef": 1.0, "theta_d": 275})
        with pytest.raises(BadInput):
            structure.put(id="nbn_slab", mode="tc", args={"theta_d": 275})
        with pytest.raises(BadInput):
            structure.put(id="nbn_slab", mode="tc", args={"n_ef": 1.0})
        with pytest.raises(BadInput):
            structure.put(
                id="nbn_slab",
                mode="tc",
                args={"n_ef": -1.0, "theta_d": 275},
            )
        with pytest.raises(BadInput):
            structure.put(
                id="nbn_slab",
                mode="tc",
                args={"n_ef": 1.0, "theta_d": 0},
            )
        with pytest.raises(BadInput):
            structure.put(
                id="nbn_slab",
                mode="tc",
                args={"n_ef": 1.0, "theta_d": 275, "bogus": 1},
            )
        with pytest.raises(BadInput):
            structure.put(
                id="nbn_slab",
                mode="tc",
                args={"n_ef": 1.0, "theta_d": 275, "i2": "Nb"},
            )

    def test_omega_log_input(self, structure) -> None:
        structure.put(id="nbn_slab", text=_NBN)
        structure.put(
            id="nbn_slab",
            mode="tc",
            args={"n_ef": 1.0, "omega_log": 200},
        )
        ref = structure.store.get_ref(kind="structure", id="nbn_slab")
        assert ref.meta["last_tc"]["omega_log_k"] == pytest.approx(200.0)

    def test_rerun_overwrites_same_version(self, structure) -> None:
        """Idempotent: re-estimating on the same version rewrites the
        stamp rather than accumulating."""
        structure.put(id="nbn_slab", text=_NBN)
        structure.put(id="nbn_slab", mode="tc", args={"n_ef": 1.0, "theta_d": 275})
        structure.put(id="nbn_slab", mode="tc", args={"n_ef": 0.5, "theta_d": 275})
        ref = structure.store.get_ref(kind="structure", id="nbn_slab")
        assert ref.meta["last_tc"]["n_ef"] == pytest.approx(0.5)
