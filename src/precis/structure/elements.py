"""Element data for the atomistic IR (ADR 0043).

A small, curated table — covalent radii (for bond detection + the
overlap/validator gate) and a nominal maximum valence (for the
over-coordination check). The v1 palette is Pd / Cu / C / H (ADR 0043 §3);
a handful of common neighbours are included so molecule mode is usable, and
unknown elements fall back to a permissive default rather than erroring — the
IR holds *any* element; only the relaxer is palette-restricted in v1.

Covalent radii are the single-bond values of Cordero et al. (2008), in
ångström. ``max_valence`` is ``None`` for metals, where coordination is not
valence-bounded.
"""

from __future__ import annotations

# Covalent radius (Å), single-bond (Cordero 2008). Extend as the palette grows.
_COVALENT_RADIUS: dict[str, float] = {
    "H": 0.31,
    "He": 0.28,
    "B": 0.84,
    "C": 0.76,
    "N": 0.71,
    "O": 0.66,
    "F": 0.57,
    "Si": 1.11,
    "P": 1.07,
    "S": 1.05,
    "Cl": 1.02,
    "Ni": 1.24,
    "Cu": 1.32,
    "Pd": 1.39,
    "Pt": 1.36,
    "Au": 1.36,
}

#: Fallback covalent radius for elements not in the table (permissive).
DEFAULT_RADIUS = 1.50

#: Nominal max coordination for covalent main-group atoms; ``None`` = metal.
_MAX_VALENCE: dict[str, int | None] = {
    "H": 1,
    "B": 4,
    "C": 4,
    "N": 4,
    "O": 2,
    "F": 1,
    "Si": 4,
    "P": 5,
    "S": 6,
    "Cl": 1,
    "Ni": None,
    "Cu": None,
    "Pd": None,
    "Pt": None,
    "Au": None,
}


def covalent_radius(element: str) -> float:
    """Single-bond covalent radius in Å (``DEFAULT_RADIUS`` if unknown)."""
    return _COVALENT_RADIUS.get(element, DEFAULT_RADIUS)


def max_valence(element: str) -> int | None:
    """Nominal maximum coordination for a covalent element; ``None`` for metals."""
    return _MAX_VALENCE.get(element)


def is_known(element: str) -> bool:
    """True if the element is in the curated radius table."""
    return element in _COVALENT_RADIUS


def bond_cutoff(a: str, b: str, tolerance: float = 1.2) -> float:
    """Distance below which atoms ``a``/``b`` are treated as bonded.

    The sum of covalent radii times a slack ``tolerance`` (default 1.2 — the
    common CrystalNN-style fudge that catches stretched bonds without bridging
    second neighbours).
    """
    return (covalent_radius(a) + covalent_radius(b)) * tolerance
