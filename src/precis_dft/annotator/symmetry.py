"""Symmetry analysis via spglib.

Computes spacegroup, point group, Wyckoff positions, and the
equivalence classes the LLM uses for ``substitute(equivalence_class
=..., fraction=...)``-style ops.

Equivalence classes are named by the integer ID returned by
spglib's ``get_symmetry_dataset()`` and rendered with the element
prefix for readability (``Pt_eq_0``, ``Pt_eq_1``, ``Ni_eq_0``,
...). The naming reads better than raw orbit indices and keeps
classes distinguishable across multi-species structures.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ase import Atoms

#: Symmetry-detection tolerance in Å. spglib default is 1e-5 (too tight
#: for relaxed structures); a few hundredths of an Å is the typical
#: choice for DFT-relaxed slabs.
DEFAULT_SYMPREC = 1e-3


def _to_spglib_cell(atoms: Atoms) -> tuple[Any, Any, Any]:
    """Convert an ASE Atoms object to the spglib cell tuple shape."""
    return (
        atoms.cell.array,
        atoms.get_scaled_positions(),
        atoms.get_atomic_numbers(),
    )


def analyse(atoms: Atoms, symprec: float = DEFAULT_SYMPREC) -> dict[str, Any]:
    """Return spacegroup/point-group/Wyckoff info + equivalence classes.

    Schema::

        {
          "spacegroup_symbol":   "Pm-3m",
          "spacegroup_number":   221,
          "point_group":         "m-3m",
          "wyckoff_letters":     ["a", "b", ...] indexed by atom,
          "equivalence_classes": {
            "Pt_eq_0": [0, 3, 6, 9, ...],
            "Ni_eq_0": [12, 13, ...],
          },
        }

    Empty / failed symmetry analysis returns a degenerate dict with
    one equivalence class per atom; the LLM can still proceed, just
    without symmetry-aware enumeration.
    """
    try:
        import spglib
    except ImportError:  # pragma: no cover — spglib is a hard dep
        return _degenerate(atoms)

    cell = _to_spglib_cell(atoms)
    try:
        sgnum = spglib.get_spacegroup(cell, symprec=symprec)
        dataset = spglib.get_symmetry_dataset(cell, symprec=symprec)
    except Exception:
        return _degenerate(atoms)

    if dataset is None:
        return _degenerate(atoms)

    # `dataset` can be a SpaceGroupType or a dict depending on spglib
    # version. Accept either by attribute-or-key access.
    def _g(key: str) -> Any:
        if hasattr(dataset, key):
            return getattr(dataset, key)
        return dataset.get(key)

    spacegroup_symbol = (sgnum or "P1 (1)").rsplit(" ", 1)[0]
    # ``_g`` returns numpy arrays for some keys (e.g. equivalent_atoms);
    # ``arr or fallback`` raises a truth-value-ambiguity error, so
    # check for None explicitly and use the fallback when missing.
    sg_number_raw = _g("number")
    spacegroup_number = int(sg_number_raw) if sg_number_raw is not None else 1
    point_group_raw = _g("pointgroup")
    pointgroup_symbol = point_group_raw if point_group_raw is not None else "1"
    wyckoffs_raw = _g("wyckoffs")
    wyckoffs = list(wyckoffs_raw) if wyckoffs_raw is not None else ["a"] * len(atoms)
    eq_raw = _g("equivalent_atoms")
    equivalent_atoms = list(eq_raw) if eq_raw is not None else list(range(len(atoms)))

    classes = _group_equivalences(atoms, equivalent_atoms)
    return {
        "spacegroup_symbol": spacegroup_symbol,
        "spacegroup_number": spacegroup_number,
        "point_group": pointgroup_symbol,
        "wyckoff_letters": wyckoffs,
        "equivalence_classes": classes,
    }


def _group_equivalences(
    atoms: Atoms, equivalent_atoms: list[int]
) -> dict[str, list[int]]:
    """Group atoms by spglib's orbit + element, name by per-element rank."""
    by_orbit: dict[tuple[str, int], list[int]] = defaultdict(list)
    symbols = atoms.get_chemical_symbols()
    for i, orbit in enumerate(equivalent_atoms):
        by_orbit[(symbols[i], int(orbit))].append(i)

    # Per-element rank: within element X, the orbits are renamed
    # 0..N in the order their first member appears.
    rank_per_element: dict[str, int] = defaultdict(int)
    name_of: dict[tuple[str, int], str] = {}
    for (element, orbit), _members in sorted(
        by_orbit.items(), key=lambda kv: (kv[0][0], min(kv[1]))
    ):
        name = f"{element}_eq_{rank_per_element[element]}"
        rank_per_element[element] += 1
        name_of[(element, orbit)] = name

    return {name_of[k]: sorted(v) for k, v in by_orbit.items()}


def _degenerate(atoms: Atoms) -> dict[str, Any]:
    """Fallback when symmetry analysis fails — one class per atom."""
    symbols = atoms.get_chemical_symbols()
    rank: dict[str, int] = defaultdict(int)
    classes: dict[str, list[int]] = {}
    for i, sym in enumerate(symbols):
        name = f"{sym}_eq_{rank[sym]}"
        rank[sym] += 1
        classes[name] = [i]
    return {
        "spacegroup_symbol": "P1",
        "spacegroup_number": 1,
        "point_group": "1",
        "wyckoff_letters": ["a"] * len(atoms),
        "equivalence_classes": classes,
    }


__all__ = ["DEFAULT_SYMPREC", "analyse"]
