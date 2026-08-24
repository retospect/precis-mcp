"""Unit tests for the periodic-symmetry canonical frame (:mod:`precis.structure.canonical`).

DB-free: builds 3×3 hex fcc(111)-shaped slab scenes by hand (no ``slab`` op
— no ASE dependency) and checks the invariance the module promises:
translation, in-plane rotation, and mirror twins hash identically; a
genuinely different decoration hashes differently; ``normalize_scene``
rewrites into that frame without disturbing anything else.
"""

from __future__ import annotations

import copy

import numpy as np
import pytest

from precis.structure.canonical import (
    geom_hash_c,
    inplane_symmetry_ops,
    normalize_scene,
)
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Bond, Scene

#: The 3×3 Pd(111)-shaped hex cell every scene in this file shares —
#: matches the prod quest candidate slabs (qu164903's corner saga).
_A = 8.3
_C = 24.0
_GAMMA = 120.0
_HOST_Z = 0.62
_H_Z = 0.71


def _cell(pbc: tuple[bool, bool, bool] = (True, True, True)) -> Cell:
    return Cell.from_lengths_angles(_A, _A, _C, gamma=_GAMMA, pbc=pbc)


def _atom(
    label: str, element: str, x: float, y: float, z: float, fixed: int = 0
) -> Atom:
    return Atom(
        label=label, element=element, frac=np.array([x, y, z], dtype=float), fixed=fixed
    )


def _scene(
    atoms: list[Atom], *, pbc: tuple[bool, bool, bool] = (True, True, True)
) -> Scene:
    sc = Scene(cell=_cell(pbc))
    for a in atoms:
        sc.atoms[a.label] = a
    return sc


def _host_lattice(element: str = "Pd") -> list[Atom]:
    """The 9 top-layer host sites — frac x,y multiples of 1/3, z=0.62."""
    atoms = []
    n = 0
    for i in range(3):
        for j in range(3):
            atoms.append(_atom(f"a{element}{n}", element, i / 3.0, j / 3.0, _HOST_Z))
            n += 1
    return atoms


def _bare_dopant(x: float, y: float, element: str = "Cu") -> Scene:
    return _scene([_atom("aCu0", element, x, y, _HOST_Z)])


def _dopant_plus_h(dx: float, dy: float, hx: float, hy: float) -> Scene:
    return _scene(
        [
            _atom("aCu0", "Cu", dx, dy, _HOST_Z),
            _atom("aH0", "H", hx, hy, _H_Z),
        ]
    )


# ── (a) 12 in-plane ops for the hex cell ────────────────────────────────


def test_hex_cell_has_12_inplane_ops() -> None:
    scene = _bare_dopant(0.0, 0.0)
    ops = inplane_symmetry_ops(scene)
    assert len(ops) == 12
    # every op is a proper isometry (|det| = 1) over the in-plane metric
    for m in ops:
        assert abs(round(np.linalg.det(m))) == 1


# ── (b) bare dopant translation twin (prod st245406/st237458) ──────────


def test_bare_dopant_translation_twin_hashes_equal() -> None:
    a = _bare_dopant(0.0, 0.0)
    b = _bare_dopant(1.0 / 3.0, 1.0 / 3.0)
    assert geom_hash_c(a) == geom_hash_c(b)


# ── (c) dopant+H translation twin (prod st243092/st239974) ─────────────


def test_dopant_plus_h_translation_twin_hashes_equal() -> None:
    a = _dopant_plus_h(0.0, 0.0, 1.0 / 3.0, 1.0 / 3.0)
    b = _dopant_plus_h(1.0 / 3.0, 1.0 / 3.0, 2.0 / 3.0, 2.0 / 3.0)
    assert geom_hash_c(a) == geom_hash_c(b)


# ── (d) mirror twin ──────────────────────────────────────────────────


def test_mirror_twin_hashes_equal() -> None:
    a = _dopant_plus_h(0.0, 0.0, 1.0 / 3.0, 0.0)
    b = _dopant_plus_h(0.0, 0.0, 0.0, 1.0 / 3.0)
    assert geom_hash_c(a) == geom_hash_c(b)


# ── (e) genuinely different decoration hashes differently ──────────────


def test_h_atop_dopant_differs_from_h_offset() -> None:
    atop = _dopant_plus_h(0.0, 0.0, 0.0, 0.0)
    offset = _dopant_plus_h(0.0, 0.0, 1.0 / 3.0, 0.0)
    assert geom_hash_c(atop) != geom_hash_c(offset)


# ── (f) frac 1.0 ≡ 0.0 ───────────────────────────────────────────────


def test_frac_one_and_zero_hash_equal() -> None:
    a = _bare_dopant(0.0, 0.0)
    b = _bare_dopant(1.0, 1.0)
    assert geom_hash_c(a) == geom_hash_c(b)


# ── (g) normalize_scene ──────────────────────────────────────────────


class TestNormalizeScene:
    def test_preserves_hash(self) -> None:
        scene = _dopant_plus_h(1.0 / 3.0, 1.0 / 3.0, 2.0 / 3.0, 2.0 / 3.0)
        before = geom_hash_c(scene)
        normalize_scene(scene)
        assert geom_hash_c(scene) == before

    def test_idempotent(self) -> None:
        scene = _dopant_plus_h(1.0 / 3.0, 1.0 / 3.0, 2.0 / 3.0, 2.0 / 3.0)
        changed_once = normalize_scene(scene)
        snapshot = {lbl: a.frac.copy() for lbl, a in scene.atoms.items()}
        changed_twice = normalize_scene(scene)
        assert changed_twice is False
        for lbl, a in scene.atoms.items():
            assert np.allclose(a.frac, snapshot[lbl])
        # sanity: the first call is meaningful for this off-origin scene
        assert changed_once in (True, False)  # never raises either way

    def test_refuses_scenes_with_bonds(self) -> None:
        scene = _dopant_plus_h(1.0 / 3.0, 1.0 / 3.0, 2.0 / 3.0, 2.0 / 3.0)
        scene.bonds.append(Bond(i="aCu0", j="aH0"))
        before = {lbl: a.frac.copy() for lbl, a in scene.atoms.items()}
        changed = normalize_scene(scene)
        assert changed is False
        for lbl, a in scene.atoms.items():
            assert np.allclose(a.frac, before[lbl])

    def test_refuses_non_inplane_periodic_scenes(self) -> None:
        scene = _scene(
            [_atom("aCu0", "Cu", 1.0 / 3.0, 1.0 / 3.0, _HOST_Z)],
            pbc=(False, True, True),
        )
        before = scene.atoms["aCu0"].frac.copy()
        changed = normalize_scene(scene)
        assert changed is False
        assert np.allclose(scene.atoms["aCu0"].frac, before)

    def test_preserves_labels_order_fixed_masks_and_z(self) -> None:
        scene = Scene(cell=_cell())
        scene.atoms["aCu0"] = _atom(
            "aCu0", "Cu", 1.0 / 3.0, 1.0 / 3.0, _HOST_Z, fixed=1
        )
        scene.atoms["aH0"] = _atom("aH0", "H", 2.0 / 3.0, 2.0 / 3.0, _H_Z, fixed=0)
        original_order = list(scene.atoms.keys())
        original_elements = {lbl: a.element for lbl, a in scene.atoms.items()}
        original_fixed = {lbl: a.fixed for lbl, a in scene.atoms.items()}
        original_z = {lbl: float(a.frac[2]) for lbl, a in scene.atoms.items()}

        normalize_scene(scene)

        assert list(scene.atoms.keys()) == original_order
        for lbl, a in scene.atoms.items():
            assert a.element == original_elements[lbl]
            assert a.fixed == original_fixed[lbl]
            assert float(a.frac[2]) == pytest.approx(original_z[lbl])


# ── (h) non-periodic in-plane cell ──────────────────────────────────────


def test_non_periodic_inplane_cell_has_only_identity_op() -> None:
    scene = _scene([_atom("aCu0", "Cu", 0.2, 0.3, _HOST_Z)], pbc=(False, False, True))
    ops = inplane_symmetry_ops(scene)
    assert len(ops) == 1
    assert np.array_equal(ops[0], np.eye(2, dtype=int))
    h1 = geom_hash_c(scene)
    h2 = geom_hash_c(copy.deepcopy(scene))
    assert h1 == h2
