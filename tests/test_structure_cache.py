"""Run-cube cache-key unit tests (ADR 0043 §23.16).

Pure, DB-free: the content address is label- and bond-independent, the cache
key is sensitive to fidelity / model / params / code version, and a relaxed
geometry serialised under one design's canonical order applies correctly to a
*different* design that shares the input geometry (the cross-design hit).
"""

from __future__ import annotations

import numpy as np

from precis.structure import cache
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene


def _scene(atoms: list[tuple[str, str, list[float]]]) -> Scene:
    cell = Cell(np.eye(3) * 10.0, (True, True, True))
    sc = Scene(cell=cell)
    for label, element, frac in atoms:
        sc.atoms[label] = Atom(label=label, element=element, frac=np.array(frac))
    return sc


def test_structure_sha_is_label_and_order_independent() -> None:
    a = _scene([("aPd1", "Pd", [0.0, 0.0, 0.0]), ("aO1", "O", [0.3, 0.0, 0.0])])
    # same geometry, different labels + reversed insertion order
    b = _scene([("aO9", "O", [0.3, 0.0, 0.0]), ("aPd7", "Pd", [0.0, 0.0, 0.0])])
    assert cache.structure_sha(a) == cache.structure_sha(b)


def test_structure_sha_is_bond_independent() -> None:
    from precis.structure.scene import Bond

    a = _scene([("aPd1", "Pd", [0.0, 0.0, 0.0]), ("aPd2", "Pd", [0.26, 0.0, 0.0])])
    b = _scene([("aPd1", "Pd", [0.0, 0.0, 0.0]), ("aPd2", "Pd", [0.26, 0.0, 0.0])])
    b.bonds.append(Bond(i="aPd1", j="aPd2", order=2.0))
    assert cache.structure_sha(a) == cache.structure_sha(b)


def test_structure_sha_changes_on_geometry() -> None:
    a = _scene([("aPd1", "Pd", [0.0, 0.0, 0.0]), ("aPd2", "Pd", [0.26, 0.0, 0.0])])
    b = _scene([("aPd1", "Pd", [0.0, 0.0, 0.0]), ("aPd2", "Pd", [0.30, 0.0, 0.0])])
    assert cache.structure_sha(a) != cache.structure_sha(b)


def test_cache_key_sensitive_to_fidelity_model_params_version() -> None:
    sc = _scene([("aPd1", "Pd", [0.0, 0.0, 0.0])])
    base = cache.run_cache_key(
        sc, fidelity="ml", model="mace_mp", params={"steps": 200}
    )
    assert base == cache.run_cache_key(
        sc, fidelity="ml", model="mace_mp", params={"steps": 200}
    )
    assert base != cache.run_cache_key(sc, fidelity="dft-fast", model="mace_mp")
    assert base != cache.run_cache_key(sc, fidelity="ml", model="chgnet")
    assert base != cache.run_cache_key(
        sc, fidelity="ml", model="mace_mp", params={"steps": 50}
    )
    assert base != cache.run_cache_key(
        sc, fidelity="ml", model="mace_mp", params={"steps": 200}, code_version="999"
    )


def test_serialize_apply_round_trips_across_designs() -> None:
    # Design A: relax moves aPd2 from 0.26 → 0.24 (toward equilibrium).
    a = _scene([("aPd1", "Pd", [0.0, 0.0, 0.0]), ("aPd2", "Pd", [0.26, 0.0, 0.0])])
    order = cache.canonical_order(a)  # captured on the INPUT geometry
    a.atoms["aPd2"].frac = np.array([0.24, 0.0, 0.0])
    geom = cache.serialize_geometry(a, order)

    # Design B: same input geometry, different labels + reversed order.
    b = _scene([("zZZ", "Pd", [0.26, 0.0, 0.0]), ("aaa", "Pd", [0.0, 0.0, 0.0])])
    assert cache.structure_sha(b) == cache.structure_sha(
        _scene([("aPd1", "Pd", [0.0, 0.0, 0.0]), ("aPd2", "Pd", [0.26, 0.0, 0.0])])
    )
    cache.apply_geometry(b, geom)
    # the atom that was at 0.26 in B is now relaxed to 0.24, the origin atom stays.
    moved = {round(float(at.frac[0]), 4) for at in b.atoms.values()}
    assert moved == {0.0, 0.24}


def test_apply_geometry_count_mismatch_is_noop() -> None:
    a = _scene([("aPd1", "Pd", [0.0, 0.0, 0.0]), ("aPd2", "Pd", [0.26, 0.0, 0.0])])
    geom = {"frac": [[0.1, 0.0, 0.0]], "lattice": None}  # only 1 atom
    cache.apply_geometry(a, geom)  # 1 != 2 → left unchanged
    assert round(float(a.atoms["aPd2"].frac[0]), 4) == 0.26
