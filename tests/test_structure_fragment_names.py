"""Card text carries systematic group names (gr334774)."""

from __future__ import annotations

import numpy as np

from precis.handlers.structure import card_text
from precis.structure import Atom, Bond, Scene
from precis.structure.cell import Cell
from precis.structure.fragment_names import fragment_names


def _ring(scene: Scene, prefix: str, kekule: bool = True) -> list[str]:
    labels = [f"{prefix}{k}" for k in range(6)]
    for lb in labels:
        scene.atoms[lb] = Atom(lb, "C", np.zeros(3))
    for k in range(6):
        order = (2.0 if k % 2 == 0 else 1.0) if kekule else 1.5
        scene.bonds.append(Bond(labels[k], labels[(k + 1) % 6], order=order))
    return labels


def _azobenzene() -> Scene:
    s = Scene(cell=Cell(np.diag([20.0, 20.0, 20.0]), pbc=(False, False, False)))
    a = _ring(s, "a")
    b = _ring(s, "b", kekule=False)
    for lb in ("n1", "n2"):
        s.atoms[lb] = Atom(lb, "N", np.zeros(3))
    s.bonds += [
        Bond(a[0], "n1"),
        Bond("n1", "n2", order=2.0),
        Bond("n2", b[0]),
    ]
    return s


def test_azobenzene_gets_diazene_and_phenyl() -> None:
    names = " ".join(fragment_names(_azobenzene()))
    assert "diazene" in names and "phenyl" in names
    text = card_text("azobenzene", _azobenzene())
    assert "diazene" in text and "phenyl" in text


def test_no_bonds_no_groups() -> None:
    s = Scene(cell=Cell(np.diag([5.0, 5.0, 5.0]), pbc=(False, False, False)))
    s.atoms["p1"] = Atom("p1", "P", np.zeros(3))
    assert fragment_names(s) == []
    assert "Groups:" not in card_text("p", s)


def test_carbonyl_and_nitrile() -> None:
    s = Scene(cell=Cell(np.diag([5.0, 5.0, 5.0]), pbc=(False, False, False)))
    for lb, el in (("c1", "C"), ("o1", "O"), ("c2", "C"), ("n1", "N")):
        s.atoms[lb] = Atom(lb, el, np.zeros(3))
    s.bonds += [Bond("c1", "o1", order=2.0), Bond("c2", "n1", order=3.0)]
    assert fragment_names(s) == ["carbonyl (C=O)", "nitrile (cyano, C#N)"]


def _azobenzene_geometry(lattice: float = 40.0, z: float = 0.0) -> Scene:
    """Bondless trans-azobenzene (24 atoms, planar, textbook distances)."""
    s = Scene(cell=Cell(np.diag([lattice] * 3), pbc=(True, True, True)))
    pts: list[tuple[str, np.ndarray]] = []

    def ring(center: np.ndarray, start_deg: float) -> np.ndarray:
        ipso = None
        for k in range(6):
            ang = np.deg2rad(start_deg + 60 * k)
            u = np.array([np.cos(ang), np.sin(ang), 0.0])
            pts.append(("C", center + 1.40 * u))
            if k == 0:
                ipso = center + 1.40 * u
            else:
                pts.append(("H", center + 2.49 * u))
        assert ipso is not None
        return ipso

    ipso_a = ring(np.zeros(3), 0.0)
    n1 = ipso_a + np.array([1.43, 0.0, 0.0])
    n2 = n1 + 1.25 * np.array([np.cos(np.deg2rad(60)), np.sin(np.deg2rad(60)), 0.0])
    pts += [("N", n1), ("N", n2)]
    ring(n2 + np.array([1.43 + 1.40, 0.0, 0.0]), 180.0)
    assert len(pts) == 24
    shift = np.array([lattice / 2, lattice / 2, z])
    for i, (el, xyz) in enumerate(pts):
        frac = s.cell.cart_to_frac(xyz + shift)
        s.atoms[f"x{i}"] = Atom(f"x{i}", el, frac)
    return s


def test_bondless_azobenzene_perceives_groups() -> None:
    s = _azobenzene_geometry()
    assert not s.bonds
    names = " ".join(fragment_names(s))
    assert "azo" in names and "diazene" in names and "phenyl" in names
    assert "diazene" in card_text("azobenzene", s)
    assert not s.bonds  # perception never writes back


def test_azobenzene_on_gold_slab_no_metal_bonds() -> None:
    from precis.structure.fragment_names import _perceive_bonds

    s = _azobenzene_geometry(lattice=40.0, z=10.0)
    k = 0
    for ix in range(8):
        for iy in range(8):
            for iz in range(3):
                xyz = np.array([ix * 2.88, iy * 2.88, iz * 2.35])
                s.atoms[f"au{k}"] = Atom(f"au{k}", "Au", s.cell.cart_to_frac(xyz))
                k += 1
    perceived = _perceive_bonds(s)
    assert perceived and not any(
        b.i.startswith("au") or b.j.startswith("au") for b in perceived
    )
    names = " ".join(fragment_names(s))
    assert "diazene" in names and "phenyl" in names


def test_loosely_relaxed_azobenzene_still_named() -> None:
    """A stored azobenzene has C-C ~1.50 A and N-N ~1.42 A (no bond is under
    the double-bond distance cutoffs); coordination still names the groups."""
    s = _azobenzene_geometry()
    centre = np.array([10.0, 10.0, 10.0])
    for a in s.atoms.values():
        cart = s.cell.frac_to_cart(a.frac)
        a.frac = s.cell.cart_to_frac(centre + 1.10 * (cart - centre))
    names = " ".join(fragment_names(s))
    assert "diazene" in names and "phenyl" in names
