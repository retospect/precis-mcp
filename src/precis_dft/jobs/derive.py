"""Pure-numerical post-processing of finished DFT calculations.

These functions take the raw scalars an SCF / vib calc already
carries (total energies, harmonic frequencies) and turn them into
the derived quantities the chemistry layer reads back:

- :func:`adsorption_energy` — E_ads of a bound slab against a named
  gas-phase reference scheme. No cluster needed; pure arithmetic on
  three (or four) total energies.
- :func:`harmonic_free_energy` — ZPE + TS correction from a vib
  calc's frequencies via :class:`ase.thermochemistry.HarmonicThermo`,
  floored at 50 cm⁻¹ so soft / imaginary modes don't blow up the
  entropy.

Both return a record dict the ``derive_*`` job_type shims persist
into the SCF calc's ``meta.derived`` (keyed so multiple schemes /
conditions coexist). The dband / bader / workfunction derivations
need real GPAW output files (PDOS, density, electrostatic
potential) and stay blocked on the cluster — they're not here.
"""

from __future__ import annotations

import re
from typing import Any

from ase.thermochemistry import HarmonicThermo
from ase.units import invcm

from precis_dft import reactions

# Minimum frequency (cm⁻¹) any mode is floored to before it enters
# the partition function. Soft and (numerically) imaginary modes
# otherwise dominate the vibrational entropy. 50 cm⁻¹ is the common
# catalysis convention (Nørskov group).
FREQ_FLOOR_CM1 = 50.0

# Gas-phase species each reference scheme needs in ``gas_energies``.
SCHEME_REFERENCES: dict[str, tuple[str, ...]] = {
    "norskov_water_h2": ("h2o", "h2"),
    "che_explicit": ("h2o", "h2"),
    "molecular_o2": ("o2", "h2"),
}


def _parse_formula(species: str) -> dict[str, int]:
    """Parse ``'*OOH'`` / ``'OH'`` / ``'*H'`` into ``{element: count}``.

    The leading adsorption marker ``*`` is stripped; bare element
    symbols count as one.
    """
    counts: dict[str, int] = {}
    for element, num in re.findall(r"([A-Z][a-z]?)(\d*)", species.lstrip("*")):
        if not element:
            continue
        counts[element] = counts.get(element, 0) + (int(num) if num else 1)
    return counts


def _reference_energy(
    composition: dict[str, int],
    scheme: str,
    gas_energies: dict[str, float],
) -> float:
    """Total energy of the gas-phase reference for ``composition``.

    Only O / H adsorbates are supported (OER / ORR / HER); anything
    with another element raises ``ValueError`` so the caller can
    report it rather than silently mis-reference.
    """
    n_o = composition.get("O", 0)
    n_h = composition.get("H", 0)
    other = {el: n for el, n in composition.items() if el not in ("O", "H")}
    if other:
        raise ValueError(
            f"scheme {scheme!r} only references O/H adsorbates; got extra {sorted(other)}"
        )

    if scheme in ("norskov_water_h2", "che_explicit"):
        # O ← H2O − H2 ; H ← ½H2  ⇒  n_O·E(H2O) + (½n_H − n_O)·E(H2)
        e_h2o = gas_energies["h2o"]
        e_h2 = gas_energies["h2"]
        return n_o * e_h2o + (0.5 * n_h - n_o) * e_h2
    if scheme == "molecular_o2":
        # O ← ½O2 ; H ← ½H2
        return 0.5 * n_o * gas_energies["o2"] + 0.5 * n_h * gas_energies["h2"]
    raise ValueError(f"unknown reference scheme {scheme!r}")


def adsorption_energy(
    *,
    e_bound: float,
    e_clean: float,
    species: str,
    scheme: str = "norskov_water_h2",
    gas_energies: dict[str, float],
    conditions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adsorption energy of ``species`` on a slab vs. a reference scheme.

    ``E_ads = E(slab+ads) − E(clean slab) − E_ref(species)``

    where ``E_ref`` is built from the gas-phase total energies in
    ``gas_energies`` (keys per :data:`SCHEME_REFERENCES`).

    For ``che_explicit`` the proton-coupled (H⁺ + e⁻) reference is
    shifted by ``−n_H · U_RHE`` (computational hydrogen electrode);
    the per-step electron count is *not* applied here — the reaction
    evaluator owns that — so this only folds the species' own H
    content at the stated potential.

    Returns a record with the value (eV), the scheme, a stable
    ``conditions_hash``, the storage ``key`` (``"<scheme>:<hash>"``),
    and the component energies for provenance.
    """
    if scheme not in SCHEME_REFERENCES:
        raise ValueError(f"unknown scheme {scheme!r}; have {sorted(SCHEME_REFERENCES)}")
    needed = SCHEME_REFERENCES[scheme]
    missing = [g for g in needed if g not in gas_energies]
    if missing:
        raise ValueError(f"scheme {scheme!r} needs gas energies {missing}")

    composition = _parse_formula(species)
    e_ref = _reference_energy(composition, scheme, gas_energies)
    value = e_bound - e_clean - e_ref

    conditions = dict(conditions or {})
    conditions.setdefault("T", 298.0)
    if scheme == "che_explicit":
        u_rhe = float(conditions.get("U_RHE", 0.0))
        n_h = composition.get("H", 0)
        value -= n_h * u_rhe

    chash = reactions.conditions_hash(conditions)
    return {
        "value": value,
        "scheme": scheme,
        "species": species,
        "conditions": conditions,
        "conditions_hash": chash,
        "key": f"{scheme}:{chash}",
        "components": {
            "E_bound": e_bound,
            "E_clean": e_clean,
            "E_ref": e_ref,
        },
    }


def _floor_frequencies(frequencies_cm1: list[float]) -> tuple[list[float], int]:
    """Floor (the magnitude of) each mode to :data:`FREQ_FLOOR_CM1`.

    Imaginary modes show up as negative frequencies; we take the
    magnitude before flooring. Returns the floored list and the
    count of modes that were actually raised.
    """
    floored: list[float] = []
    n_floored = 0
    for f in frequencies_cm1:
        mag = abs(float(f))
        if mag < FREQ_FLOOR_CM1:
            floored.append(FREQ_FLOOR_CM1)
            n_floored += 1
        else:
            floored.append(mag)
    return floored, n_floored


def harmonic_free_energy(
    *,
    e_tot: float,
    frequencies_cm1: list[float],
    conditions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gibbs free energy of an adsorbate in the harmonic approximation.

    Floors soft / imaginary modes at 50 cm⁻¹, then uses
    :class:`ase.thermochemistry.HarmonicThermo` for ZPE + thermal +
    −TS. For an adsorbate the pV term is negligible, so G ≈ the
    Helmholtz free energy reported by ASE.

    Returns a record with the free energy ``value`` (eV), its ``zpe``
    and ``minus_TS`` components, a ``conditions_hash`` key, and any
    ``warnings`` (e.g. missing solvation under electrochemical
    conditions).
    """
    if not frequencies_cm1:
        raise ValueError("harmonic_free_energy needs at least one frequency")

    conditions = dict(conditions or {})
    conditions.setdefault("T", 298.0)
    temperature = float(conditions["T"])

    floored, n_floored = _floor_frequencies(frequencies_cm1)
    vib_energies = [f * invcm for f in floored]

    thermo = HarmonicThermo(vib_energies=vib_energies, potentialenergy=e_tot)
    free_energy = thermo.get_helmholtz_energy(temperature, verbose=False)
    zpe = thermo.get_ZPE_correction()
    entropy = thermo.get_entropy(temperature, verbose=False)
    minus_ts = -temperature * entropy

    warnings: list[str] = []
    if conditions.get("electrochemical") and not conditions.get("solvation"):
        warnings.append("no_solvation_correction")
    if n_floored:
        warnings.append(f"floored_{n_floored}_modes_to_{int(FREQ_FLOOR_CM1)}cm-1")

    chash = reactions.conditions_hash(conditions)
    return {
        "value": free_energy,
        "zpe": zpe,
        "minus_TS": minus_ts,
        "E_tot": e_tot,
        "T": temperature,
        "n_modes": len(frequencies_cm1),
        "n_floored": n_floored,
        "conditions": conditions,
        "conditions_hash": chash,
        "warnings": warnings,
    }


__all__ = [
    "FREQ_FLOOR_CM1",
    "SCHEME_REFERENCES",
    "adsorption_energy",
    "harmonic_free_energy",
]
