"""Element data for the atomistic IR.

A small, curated table — covalent radii (for bond detection + the
overlap/validator gate) and a nominal maximum valence (for the
over-coordination check). The v1 palette is Pd / Cu / C / H;
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
    "Br": 1.20,
    "I": 1.39,
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
    "Br": 1,
    "I": 1,
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


#: Effective valence budget for common charged main-group states (gr285775).
#: Deliberately NOT ``max_valence(element) + charge`` — that naive arithmetic
#: coincidentally matches a couple of these (O- = 2 - 1 = 1) but is wrong in
#: general (N+'s real budget is 4, not ``max_valence('N') + 1 == 5``): a
#: formal charge changes an atom's accessible orbitals/lone pairs, not a
#: fixed +/-1 slot on its neutral bond count. Keyed by ``(element, charge)``;
#: small and obviously extendable — add an entry rather than guessing.
_CHARGED_VALENCE: dict[tuple[str, int], int] = {
    ("N", 1): 4,  # ammonium / quaternary ammonium N+ (e.g. CBPQT(4+))
    ("O", -1): 1,  # alkoxide / carboxylate O-
    ("O", 1): 3,  # oxocarbenium-adjacent / protonated-ether O+
    ("C", -1): 3,  # carbanion
    ("C", 1): 3,  # carbocation
    ("S", 1): 3,  # sulfonium S+
    ("P", 1): 4,  # phosphonium P+
    ("F", -1): 0,  # halide anions: a bare anion carries no covalent bond
    ("Cl", -1): 0,
    ("Br", -1): 0,
    ("I", -1): 0,
}


def effective_valence(element: str, charge: int) -> tuple[int | None, bool]:
    """The valence budget rules 2/5 (``validate.py``) should use for an atom
    declaring ``charge`` — ``(budget, known)``.

    ``charge == 0`` always returns :func:`max_valence` (unchanged neutral
    behaviour), ``known=True``. A nonzero charge looks up
    :data:`_CHARGED_VALENCE`; a ``(element, charge)`` state with no entry
    falls back to the neutral :func:`max_valence` with ``known=False`` — the
    caller gets a usable number (never a hard failure) but should surface an
    advisory note (:mod:`vsepr`'s ``unmodeled_charge_state``) rather than
    silently trusting a guess at exotic charge-state chemistry.
    """
    if charge == 0:
        return max_valence(element), True
    entry = _CHARGED_VALENCE.get((element, charge))
    if entry is not None:
        return entry, True
    return max_valence(element), False


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
