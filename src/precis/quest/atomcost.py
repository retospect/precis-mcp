"""Atom cost — the frontier's economic axis (slice B, kinetics cutover).

A catalyst candidate's *activity* (``log_tof``, slice A) is only half of the
"is dear-but-active worth it" question — the other half is what the material
would cost to make at scale. :func:`atom_cost` answers that from composition
alone (no simulation needed): the mass-weighted average USD/kg of the
elements present, log10'd so it plots on the same kind of axis as
``log_tof`` and so a candidate's rank barely moves on a within-decade price
wobble.

:data:`ELEMENT_USD_PER_KG` is a **static, order-of-magnitude** table (roughly
2024 spot prices) — not a live feed, not element-form-specific (bulk metal
vs. nanoparticle vs. salt precursor all differ, sometimes by a factor of a
few), not tracking any particular index. Pareto ranking on this axis is
*order-based*, so getting the right **decade** (Fe cents/kg vs. Rh
$100k/kg) is what matters, not the third significant figure. Extend the
table freely as new elements show up in catalyst quests; an element missing
from it is never fabricated a price — see :func:`atom_cost`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

#: Approximate atomic weight (g/mol), standard periodic-table values,
#: elements 1 (H) through 86 (Rn) — enough to weigh any composition a
#: catalyst quest's `structure` ops (`slab`/`add_atom`/`set_element`) could
#: plausibly produce. Independent of :data:`ELEMENT_USD_PER_KG`: an element
#: can be weighed here without being priced there (its mass still counts
#: toward the "how much of the composition is unpriced" gate in
#: :func:`atom_cost`).
_ATOMIC_MASS: dict[str, float] = {
    "H": 1.008,
    "He": 4.003,
    "Li": 6.940,
    "Be": 9.012,
    "B": 10.810,
    "C": 12.011,
    "N": 14.007,
    "O": 15.999,
    "F": 18.998,
    "Ne": 20.180,
    "Na": 22.990,
    "Mg": 24.305,
    "Al": 26.982,
    "Si": 28.085,
    "P": 30.974,
    "S": 32.060,
    "Cl": 35.450,
    "Ar": 39.948,
    "K": 39.098,
    "Ca": 40.078,
    "Sc": 44.956,
    "Ti": 47.867,
    "V": 50.941,
    "Cr": 51.996,
    "Mn": 54.938,
    "Fe": 55.845,
    "Co": 58.933,
    "Ni": 58.693,
    "Cu": 63.546,
    "Zn": 65.380,
    "Ga": 69.723,
    "Ge": 72.630,
    "As": 74.922,
    "Se": 78.971,
    "Br": 79.904,
    "Kr": 83.798,
    "Rb": 85.468,
    "Sr": 87.620,
    "Y": 88.906,
    "Zr": 91.224,
    "Nb": 92.906,
    "Mo": 95.950,
    "Tc": 97.907,
    "Ru": 101.070,
    "Rh": 102.906,
    "Pd": 106.420,
    "Ag": 107.868,
    "Cd": 112.414,
    "In": 114.818,
    "Sn": 118.710,
    "Sb": 121.760,
    "Te": 127.600,
    "I": 126.904,
    "Xe": 131.293,
    "Cs": 132.905,
    "Ba": 137.327,
    "La": 138.905,
    "Ce": 140.116,
    "Pr": 140.908,
    "Nd": 144.242,
    "Pm": 144.913,
    "Sm": 150.360,
    "Eu": 151.964,
    "Gd": 157.250,
    "Tb": 158.925,
    "Dy": 162.500,
    "Ho": 164.930,
    "Er": 167.259,
    "Tm": 168.934,
    "Yb": 173.054,
    "Lu": 174.967,
    "Hf": 178.490,
    "Ta": 180.948,
    "W": 183.840,
    "Re": 186.207,
    "Os": 190.230,
    "Ir": 192.217,
    "Pt": 195.084,
    "Au": 196.967,
    "Hg": 200.592,
    "Tl": 204.380,
    "Pb": 207.200,
    "Bi": 208.980,
    "Po": 208.982,
    "At": 209.987,
    "Rn": 222.018,
}

#: Approximate 2024 market price, USD/kg, per element — see the module
#: docstring: static, order-of-magnitude only. Covers the plausible catalyst
#: space: common structural/base metals, the transition metals catalysis
#: quests actually dope with, the precious-metal group (where cost swings a
#: candidate's ranking the most), a handful of lanthanides/other metals that
#: show up as dopants, and the light non-metal framework/adsorbate atoms
#: (N/H/O/C/S/P) — priced nominally near $1/kg rather than omitted, since a
#: mass-weighted metal price barely moves from a framework atom either way
#: and an all-adsorbate composition should not come back unpriced just
#: because no metal host was named.
ELEMENT_USD_PER_KG: dict[str, float] = {
    # light framework / adsorbate atoms — nominal, not a real commodity price
    "H": 1.0,
    "C": 1.0,
    "N": 1.0,
    "O": 1.0,
    "S": 1.0,
    "P": 1.0,
    # common structural / base metals
    "Fe": 0.1,
    "Al": 2.5,
    "Mn": 2.0,
    "Zn": 3.0,
    "Cr": 10.0,
    "Cu": 9.0,
    "Ti": 11.0,
    "Sn": 25.0,
    "Pb": 2.0,
    "Ni": 15.0,
    # catalysis-relevant transition metals
    "Co": 35.0,
    "Zr": 40.0,
    "Mo": 40.0,
    "W": 35.0,
    "V": 350.0,
    "Nb": 70.0,
    "Ta": 300.0,
    "Re": 1500.0,
    # precious metals — this is where cost dominates a ranking
    "Ag": 800.0,
    "Au": 65000.0,
    "Pt": 30000.0,
    "Pd": 35000.0,
    "Rh": 140000.0,
    "Ir": 150000.0,
    "Ru": 15000.0,
    "Os": 12000.0,
    # lanthanides + other dopant-plausible metals
    "La": 5.0,
    "Ce": 5.0,
    "Y": 35.0,
    "Sc": 3500.0,
    "Ga": 300.0,
    "In": 250.0,
    "Bi": 20.0,
    "Cd": 3.0,
}

#: Below this fraction of the composition's total mass being priced,
#: :func:`atom_cost` returns ``None`` rather than a value computed almost
#: entirely off a handful of priced trace atoms.
_MIN_PRICED_MASS_FRACTION = 0.5


def _positive_counts(counts: Mapping[str, int | float]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for element, n in counts.items():
        if not isinstance(n, (int, float)) or isinstance(n, bool):
            continue
        if n > 0:
            out.append((element, float(n)))
    return out


def atom_cost(counts: Mapping[str, int | float]) -> float | None:
    """log10 of the mass-weighted average USD/kg over ``counts`` (element ->
    atom count), or ``None`` when the price is not meaningfully knowable.

    ``log10( Σ(n_i·m_i·p_i) / Σ(n_i·m_i) )`` over the elements that carry
    both a known atomic mass and a known price — an element with no entry in
    :data:`ELEMENT_USD_PER_KG` is skipped from that sum entirely (never
    assigned a fabricated price), but its mass still counts toward the
    denominator of the *coverage* check: if the elements with a known price
    account for less than :data:`_MIN_PRICED_MASS_FRACTION` of the
    composition's total known mass, the price is dominated by guesswork and
    this returns ``None`` rather than a number nobody should trust. An
    element with no known atomic mass either (outside the table's periodic
    range) is dropped from both sums — it can be neither weighed nor priced.
    """
    priced_mass = 0.0
    cost_mass = 0.0
    total_mass = 0.0
    for element, n in _positive_counts(counts):
        mass = _ATOMIC_MASS.get(element)
        if mass is None:
            continue
        m = n * mass
        total_mass += m
        price = ELEMENT_USD_PER_KG.get(element)
        if price is None:
            continue
        priced_mass += m
        cost_mass += m * price
    if total_mass <= 0 or priced_mass <= 0:
        return None
    if priced_mass / total_mass < _MIN_PRICED_MASS_FRACTION:
        return None
    return math.log10(cost_mass / priced_mass)


def dearest(counts: Mapping[str, int | float]) -> str | None:
    """``"<element> ≈ $<price>/kg"`` for the priciest priced element present
    in ``counts`` — by USD/kg alone, regardless of how small its share of the
    composition is (a trace of Rh is still the naming-worthy reason the
    candidate is expensive). ``None`` when no present element has a known
    price."""
    best: tuple[str, float] | None = None
    for element, _n in _positive_counts(counts):
        price = ELEMENT_USD_PER_KG.get(element)
        if price is None:
            continue
        if best is None or price > best[1]:
            best = (element, price)
    if best is None:
        return None
    element, price = best
    return f"{element} ≈ ${price:g}/kg"


__all__ = ["ELEMENT_USD_PER_KG", "atom_cost", "dearest"]
