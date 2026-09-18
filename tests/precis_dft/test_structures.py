"""POSCAR serialization round-trips and structure ids are stable."""

from __future__ import annotations

from ase import Atoms
from ase.build import bulk, fcc111

from precis_dft.structures import (
    atoms_from_poscar,
    canonical_poscar,
    sha256_of,
    structure_id_for,
)


def test_canonical_poscar_round_trips_bulk() -> None:
    atoms = bulk("Pt", "fcc", a=3.92)
    text = canonical_poscar(atoms)
    parsed = atoms_from_poscar(text)
    assert len(parsed) == len(atoms)
    assert parsed.get_chemical_symbols() == atoms.get_chemical_symbols()


def test_canonical_poscar_round_trips_slab() -> None:
    slab = fcc111("Pt", size=(2, 2, 3), vacuum=10.0)
    text = canonical_poscar(slab)
    parsed = atoms_from_poscar(text)
    assert len(parsed) == 12  # 2*2*3
    assert parsed.get_chemical_symbols() == ["Pt"] * 12


def test_structure_id_is_stable() -> None:
    """Two constructions of the same structure produce the same id."""
    a = bulk("Pt", "fcc", a=3.92)
    b = bulk("Pt", "fcc", a=3.92)
    assert structure_id_for(a) == structure_id_for(b)


def test_structure_id_differs_for_different_structures() -> None:
    pt = bulk("Pt", "fcc", a=3.92)
    ni = bulk("Ni", "fcc", a=3.92)
    assert structure_id_for(pt) != structure_id_for(ni)


def test_sha256_of_deterministic() -> None:
    assert sha256_of("hello") == sha256_of("hello")
    assert sha256_of("hello") != sha256_of("world")


def test_empty_structure_id_doesnt_crash() -> None:
    empty = Atoms(cell=[10, 10, 10], pbc=True)
    # We don't promise the empty id is *useful*, only that it doesn't raise.
    assert structure_id_for(empty).startswith("structure:")
