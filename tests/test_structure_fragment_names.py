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
