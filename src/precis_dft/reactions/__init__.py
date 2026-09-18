"""Reaction-network loading + CHE-based step-ΔG / overpotential calc.

Two surfaces:

- :func:`load_library` reads every YAML under
  ``precis_dft/data/reaction_networks/`` and returns a dict keyed by
  the network ``id`` (e.g. ``reaction:oer_4step_acid``). The
  ``reaction_network`` handler caches the load at construction time.
- :func:`evaluate` takes a network and a per-species free-energy
  table and computes the step ΔGs at given conditions (T, pH, U_RHE).
  Returns the full record that becomes a ``reaction_eval`` chunk on
  the material.

The CHE framework: for a proton-coupled electron transfer step
``A → B + H+ + e-``, the free energy change at applied potential U
(vs. RHE) is ``ΔG(U) = ΔG(U=0) - n_e * U``. ``ΔG(U=0)`` comes from
the ZPE+TS-corrected free energies of the adsorbed species, with
``μ(H+ + e-) = 0.5 * μ(H2)`` at standard conditions. The
:func:`evaluate` function works directly with the
already-CHE-referenced per-species free energies the caller
supplies — i.e. ``G[*OH]`` is the bound-OH ΔG vs. ``* + 0.5 H2O -
0.5 H2`` at standard conditions.

The overpotential is reported as a thermodynamic limit (the
limiting step's ΔG / e minus the equilibrium potential), not a
kinetic overpotential. Honest about this in :data:`evaluate`'s
return.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import math
from typing import Any

import yaml

#: Equilibrium potentials (V vs. RHE) for the canonical reactions
#: shipped in the library. Used by :func:`evaluate` to compute the
#: thermodynamic overpotential ``η = max(ΔG_i) / n_e_per_step - U_eq``.
_U_EQ: dict[str, float] = {
    "reaction:oer_4step_acid": 1.23,
    "reaction:oer_4step_alkaline": 0.40,
    "reaction:oer_lom": 1.23,
    "reaction:oer_dual_site": 1.23,
    "reaction:orr_4step_associative": 1.23,
    "reaction:orr_4step_dissociative": 1.23,
    "reaction:her_volmer_heyrovsky": 0.00,
    "reaction:her_volmer_tafel": 0.00,
}

#: Gas constant times standard conditions, used for pH corrections.
#: ``kT * ln(10) ≈ 0.0592 V/pH unit`` at 298 K.
_KT_LN10_298 = 0.0592


def load_library() -> dict[str, dict[str, Any]]:
    """Return the bundled reaction-network library keyed by id.

    Reads every YAML file shipped under
    ``precis_dft/data/reaction_networks/``. YAML failures and
    missing ``id`` keys are logged-and-skipped — a malformed entry
    must not block the library load.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        package = importlib.resources.files("precis_dft.data.reaction_networks")
    except (ModuleNotFoundError, AttributeError):
        return out
    for entry in package.iterdir():
        if not entry.is_file() or not entry.name.endswith(".yaml"):
            continue
        try:
            with entry.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and "id" in doc:
            out[str(doc["id"])] = doc
    return out


def _u_equilibrium(network_id: str) -> float:
    """Return the equilibrium potential for a network, in V vs. RHE.

    Unknown networks default to 0 V — the user can override via the
    ``conditions`` dict on :func:`evaluate`.
    """
    return _U_EQ.get(network_id, 0.0)


def conditions_hash(conditions: dict[str, Any]) -> str:
    """Return a stable hex digest for a conditions dict.

    Used to key ``derived.eads[...]`` and ``derived.reactions[...]``
    on the (scheme, conditions_hash) tuple so multiple evaluations
    can coexist on the same material.
    """
    serialised = json.dumps(conditions, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()[:16]


def evaluate(
    network: dict[str, Any],
    *,
    free_energies: dict[str, float],
    conditions: dict[str, Any] | None = None,
    u_equilibrium: float | None = None,
) -> dict[str, Any]:
    """Compute step ΔGs + overpotential for ``network`` on a material.

    ``free_energies`` maps each adsorbed species id (``'*OH'``,
    ``'*O'``, ``'*OOH'``, ``'*H'``, …) to its ZPE+TS-corrected
    free energy in the CHE frame at standard conditions, in eV.
    The clean-slate ``'*'`` is implicitly 0 (the reference).

    Returns the record that the material handler will store as a
    ``reaction_eval`` chunk::

        {
          "network_id":              "reaction:oer_4step_acid",
          "conditions":              {...},
          "conditions_hash":         "<hex>",
          "step_dG":                 [ΔG_1, ΔG_2, ΔG_3, ΔG_4],  # at U=0
          "step_dG_applied":         [...],  # at U_applied
          "rate_determining_step":   int,
          "overpotential":           float,  # ≥ 0
          "overpotential_kind":      "thermodynamic_limit",
          "limiting_step_dG":        float,
          "u_equilibrium":           float,
          "u_applied":               float,
          "missing_intermediates":   [list of species this network
                                       needed but ``free_energies``
                                       didn't carry],
        }
    """
    conditions = dict(conditions or {})
    conditions.setdefault("T", 298.0)
    conditions.setdefault("pH", 0.0)
    conditions.setdefault("U_RHE", 0.0)

    network_id = str(network.get("id", ""))
    u_eq = (
        float(u_equilibrium)
        if u_equilibrium is not None
        else _u_equilibrium(network_id)
    )
    u_applied = float(conditions["U_RHE"])

    steps = list(network.get("steps", []))
    missing: list[str] = []

    # Build the free-energy lookup with implicit clean slate = 0.
    G: dict[str, float] = dict(free_energies)
    G.setdefault("*", 0.0)

    step_dG: list[float] = []
    step_dG_applied: list[float] = []
    step_n_e: list[int] = []

    for step in steps:
        n_e = int(step.get("n_e", 0))
        step_n_e.append(n_e)

        dG = _step_delta_g(step, G, missing)
        step_dG.append(dG)
        # ΔG(U) = ΔG(U=0) - n_e * U (per CHE convention; reduction
        # of H+ + e- to 0.5 H2 shifts the proton-electron pair
        # chemical potential by -eU).
        step_dG_applied.append(dG - n_e * u_applied)

    # Rate-determining step (RDS) under CHE is the step with the
    # largest ΔG at U=0 (most thermodynamically uphill). The
    # thermodynamic overpotential is the gap between RDS and the
    # equilibrium potential — the U at which every step has
    # ΔG ≤ 0 is U_lim, and η = U_lim - U_eq.
    if step_dG and any(n > 0 for n in step_n_e):
        # U_lim is the smallest U at which every PCET step has
        # ΔG(U) ≤ 0 — i.e. U_lim = max(ΔG_i / n_e_i) over PCET steps.
        per_step_u = [
            dG / n_e for dG, n_e in zip(step_dG, step_n_e, strict=False) if n_e > 0
        ]
        u_limiting = max(per_step_u) if per_step_u else 0.0
        # The RDS is the step that defines U_lim.
        rds = next(
            i
            for i, (dG, n_e) in enumerate(zip(step_dG, step_n_e, strict=False))
            if n_e > 0 and math.isclose(dG / n_e, u_limiting, abs_tol=1e-9)
        )
        # For oxidation reactions (OER), η = U_lim - U_eq; for
        # reduction (ORR, HER), η = U_eq - U_lim. The sign of
        # ``u_eq`` plus the network conventions decide which.
        # We treat the network as oxidation when u_eq > 0 (OER, OER
        # alkaline; equilibrium > 0). For HER (u_eq = 0) and ORR
        # (also written with u_eq = 1.23 as oxidation-of-water),
        # the convention here is "magnitude of the gap": always
        # nonnegative.
        eta = abs(u_limiting - u_eq)
    else:
        u_limiting = 0.0
        rds = -1
        eta = 0.0

    return {
        "network_id": network_id,
        "conditions": conditions,
        "conditions_hash": conditions_hash(conditions),
        "step_dG": step_dG,
        "step_dG_applied": step_dG_applied,
        "rate_determining_step": rds,
        "overpotential": eta,
        "overpotential_kind": "thermodynamic_limit",
        "limiting_step_dG": step_dG[rds] if rds >= 0 else 0.0,
        "u_equilibrium": u_eq,
        "u_applied": u_applied,
        "u_limiting": u_limiting,
        "missing_intermediates": missing,
    }


# ── Internals ─────────────────────────────────────────────────────


def _step_delta_g(
    step: dict[str, Any],
    G: dict[str, float],
    missing: list[str],
) -> float:
    """Compute ΔG for one elementary step at U=0.

    The CHE convention: every electron-proton pair on the product
    side cancels with ``0.5 * G(H2)`` at standard conditions, so
    we leave them out of the sum. Gas-phase species (H2O, O2, N2,
    CO2, …) are taken at standard chemical potential and
    cancel out when the user's free_energies are CHE-referenced —
    the convention is that ``G(*OH) = G_DFT(*OH) - G_DFT(*) -
    G_gas(H2O) + 0.5 G_gas(H2) + ZPE - TS`` already, so the step
    ΔGs reduce to differences of the supplied G values.

    Concretely, for OER step 1 (``H2O + * → *OH + H+ + e-``):
    ΔG = G(*OH) - G(*).
    """
    reactants = step.get("reactants", {}) or {}
    products = step.get("products", {}) or {}

    def _net(side: dict[str, Any]) -> float:
        total = 0.0
        for species, coef in side.items():
            if species in (
                "H2O",
                "O2",
                "Hp",
                "H+",
                "OHm",
                "OH-",
                "e",
                "e-",
                "N2",
                "CO2",
                "H2",
                "CO",
                "CH3OH",
                "C2H4",
                "NH3",
                "CH4",
            ):
                # Gas-phase / proton-electron contributions cancel
                # under the CHE convention applied to the user's G
                # values. Skip them here.
                continue
            if species not in G:
                # Adsorbed intermediate the user didn't supply.
                if species not in missing:
                    missing.append(species)
                continue
            total += float(coef) * G[species]
        return total

    return _net(products) - _net(reactants)


__all__ = ["conditions_hash", "evaluate", "load_library"]
