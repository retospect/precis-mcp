"""Typed-op application: real ASE mutations on real structures."""

from __future__ import annotations

import pytest
from ase import Atoms
from ase.build import bulk, fcc111

from precis_dft.ops.apply import apply_ops

# ── Single mutation ops ───────────────────────────────────────────


class TestSetSpecies:
    def test_set_species_changes_one_atom(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2))
        n_pt = sum(1 for s in atoms.get_chemical_symbols() if s == "Pt")
        result = apply_ops(atoms, [{"set_species": {"site": 0, "element": "Ni"}}])
        assert isinstance(result, Atoms)
        symbols = result.get_chemical_symbols()
        assert symbols[0] == "Ni"
        assert sum(1 for s in symbols if s == "Pt") == n_pt - 1
        assert sum(1 for s in symbols if s == "Ni") == 1

    def test_set_species_rejects_bad_index(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92)
        with pytest.raises(ValueError, match="out of range"):
            apply_ops(atoms, [{"set_species": {"site": 999, "element": "Ni"}}])


class TestVacancy:
    def test_vacancy_removes_one_atom(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2))
        n_before = len(atoms)
        result = apply_ops(atoms, [{"vacancy": {"site": 0}}])
        assert isinstance(result, Atoms)
        assert len(result) == n_before - 1


class TestStrain:
    def test_biaxial_strain_expands_in_plane(self) -> None:
        atoms = fcc111("Pt", size=(2, 2, 4), vacuum=10.0)
        a, b, c = atoms.cell.lengths()
        result = apply_ops(
            atoms, [{"strain": {"component": "biaxial", "magnitude": 0.02}}]
        )
        assert isinstance(result, Atoms)
        a2, b2, c2 = result.cell.lengths()
        assert a2 == pytest.approx(a * 1.02, rel=1e-3)
        assert b2 == pytest.approx(b * 1.02, rel=1e-3)
        # Out-of-plane axis unchanged.
        assert c2 == pytest.approx(c, rel=1e-3)
        assert b == pytest.approx(a)  # consumes b for the linter


class TestSupercell:
    def test_supercell_222_quadruples_atoms(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92)
        n = len(atoms)
        result = apply_ops(atoms, [{"supercell": {"repeats": [2, 2, 2]}}])
        assert isinstance(result, Atoms)
        assert len(result) == n * 8


class TestConstrain:
    def test_constrain_layer_adds_fixatoms(self) -> None:
        slab = fcc111("Pt", size=(2, 2, 4), vacuum=10.0)
        result = apply_ops(slab, [{"constrain": {"layer": 0, "kind": "frozen"}}])
        assert isinstance(result, Atoms)
        assert len(result.constraints) >= 1


class TestDisplace:
    def test_displace_moves_one_atom(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92)
        before = atoms.get_positions()[0].copy()
        result = apply_ops(
            atoms, [{"displace": {"site": 0, "vector": [0.0, 0.0, 0.1]}}]
        )
        assert isinstance(result, Atoms)
        after = result.get_positions()[0]
        assert after[2] == pytest.approx(before[2] + 0.1, abs=1e-6)


class TestSetMagmom:
    def test_set_magmom_sets_initial_value(self) -> None:
        atoms = bulk("Ni", "fcc", a=3.52)
        result = apply_ops(atoms, [{"set_magmom": {"site": 0, "magmom": 0.6}}])
        assert isinstance(result, Atoms)
        magmoms = result.get_initial_magnetic_moments()
        assert magmoms[0] == pytest.approx(0.6)


# ── Combinatorial substitute ──────────────────────────────────────


class TestSubstituteCombinatorial:
    def test_substitute_first_picks_one_pattern(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2))
        result = apply_ops(
            atoms,
            [
                {
                    "substitute": {
                        "equivalence_class": "Pt",
                        "fraction": 0.125,
                        "element": "Ni",
                        "enumerate": "first",
                    }
                }
            ],
        )
        assert isinstance(result, Atoms)
        symbols = result.get_chemical_symbols()
        assert sum(1 for s in symbols if s == "Ni") == 1

    def test_substitute_enumerate_returns_siblings(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2))
        result = apply_ops(
            atoms,
            [
                {
                    "substitute": {
                        "equivalence_class": "Pt",
                        "fraction": 0.125,
                        "element": "Ni",
                        "enumerate": "all",
                        "max_siblings": 32,
                    }
                }
            ],
        )
        # Combinatorial returns a list.
        assert isinstance(result, list)
        # Eight Pt atoms, swap one — at most 8 siblings (symmetry
        # reduction not yet applied here, so we hit the full
        # combinatorial count clamped by max_siblings).
        assert 1 <= len(result) <= 8
        for sibling in result:
            assert sum(1 for s in sibling.get_chemical_symbols() if s == "Ni") == 1

    def test_substitute_terminates_chain(self) -> None:
        """An ``enumerate='all'`` op terminates the apply sequence;
        subsequent ops must raise."""
        atoms = bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2))
        with pytest.raises(ValueError, match="terminates the chain"):
            apply_ops(
                atoms,
                [
                    {
                        "substitute": {
                            "equivalence_class": "Pt",
                            "fraction": 0.125,
                            "element": "Ni",
                            "enumerate": "all",
                        }
                    },
                    {"vacancy": {"site": 0}},
                ],
            )


# ── Op chaining ────────────────────────────────────────────────────


class TestChaining:
    def test_strain_then_vacancy_chains(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2))
        n = len(atoms)
        result = apply_ops(
            atoms,
            [
                {"strain": {"component": "biaxial", "magnitude": 0.01}},
                {"vacancy": {"site": 0}},
            ],
        )
        assert isinstance(result, Atoms)
        assert len(result) == n - 1
        a = result.cell.lengths()[0]
        # Strain compounded into the chain.
        a_unrelaxed = atoms.cell.lengths()[0]
        assert a == pytest.approx(a_unrelaxed * 1.01, rel=1e-3)


# ── Validation ────────────────────────────────────────────────────


class TestValidation:
    def test_unknown_op_kind_raises(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92)
        with pytest.raises(ValueError, match=r"no recognised kind|unknown op"):
            apply_ops(atoms, [{"do_a_barrel_roll": {"force": 9000}}])
