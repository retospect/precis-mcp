"""Open-valence geometry for ``precis_geom.mol`` — where a new bond leaves
an atom, and how many valences it has left.

Pure reads over a ``precis.structure.scene.Scene``; the direction rules
reuse :func:`precis.structure.probe.local_normal` (POAV normal of a
3-coordinate centre) and :mod:`precis.structure.vsepr` (hybridization,
ideal angle) rather than carrying a second set of angles.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis.structure import elements, probe, vsepr

#: Half the angle between the two free sp3 lone directions of a
#: two-coordinate centre: 109.47° / 2 off the bisector plane.
_SP3_HALF = math.radians(109.4712 / 2.0)
#: Valence-budget slack: aromatic 1.5 orders sum with float round-off.
_EPS = 1e-6


def cart(scene: Any, label: str) -> np.ndarray:
    return np.asarray(scene.cell.frac_to_cart(scene.atoms[label].frac), dtype=float)


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


def bond_vectors(scene: Any, label: str) -> list[tuple[str, float, np.ndarray]]:
    """``(other, order, unit vector label→other)`` for every declared bond
    on ``label`` (periodic images honoured, as ``probe.local_normal``)."""
    out: list[tuple[str, float, np.ndarray]] = []
    fi = scene.atoms[label].frac
    for b in scene.bonds:
        if label not in (b.i, b.j) or b.i not in scene.atoms or b.j not in scene.atoms:
            continue
        other = b.j if b.i == label else b.i
        sign = 1.0 if b.i == label else -1.0
        fj = scene.atoms[other].frac + sign * np.asarray(b.image, dtype=float)
        v = np.asarray(scene.cell.frac_to_cart(fj - fi), dtype=float)
        if float(np.linalg.norm(v)) > 1e-9:
            out.append((other, float(b.order), _unit(v)))
    return out


def bonded_hydrogens(scene: Any, label: str) -> list[str]:
    """Labels of the H atoms bonded to ``label``, in scene order."""
    hs = {
        o
        for o, _order, _v in bond_vectors(scene, label)
        if scene.atoms[o].element == "H"
    }
    return [la for la in scene.atoms if la in hs]


def free_valence(scene: Any, label: str) -> float | None:
    """Unused valence budget of ``label`` (bond orders summed against
    ``elements.effective_valence``); ``None`` for a metal (unbounded)."""
    atom = scene.atoms[label]
    mv, _known = elements.effective_valence(atom.element, atom.charge)
    if mv is None:
        return None
    used = sum(o for _la, o, _v in bond_vectors(scene, label))
    return float(mv) - used


def _any_perpendicular(v: np.ndarray) -> np.ndarray:
    ref = np.array([1.0, 0.0, 0.0]) if abs(v[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    return _unit(np.cross(v, ref))


def free_direction(
    scene: Any, label: str, *, prefer_h: bool = True, hyb: str | None = None
) -> np.ndarray:
    """Unit vector from ``label`` toward where a new bond would leave it.

    With ``prefer_h`` and a bonded H, that H's own direction (the bond a
    substitution replaces). Otherwise built from the existing neighbours:
    one neighbour → the hybridization's ideal angle; two → the bisector
    (planar sp/sp2) or the out-of-plane sp3 lone direction; three or
    more → opposite the neighbour sum, or the POAV normal for a planar
    centre. ``hyb`` overrides the inferred hybridization (a hexfold Net's
    per-atom ``sp2``/``sp3`` tag, which its all-single-bond scene hides)."""
    pos = cart(scene, label)
    if prefer_h:
        hs = bonded_hydrogens(scene, label)
        if hs:
            return _unit(cart(scene, hs[0]) - pos)
    vecs = [v for _o, _od, v in bond_vectors(scene, label)]
    atom = scene.atoms[label]
    hyb = hyb or vsepr.infer_hybridization(scene, label)
    if not vecs:
        return np.array([0.0, 0.0, 1.0])
    if len(vecs) == 1:
        theta = vsepr.ideal_angle(atom.element, hyb) if hyb else None
        theta = 109.4712 if theta is None else theta
        v0 = vecs[0]
        if theta >= 179.0:
            return -v0
        t = math.radians(theta)
        return _unit(v0 * math.cos(t) + _any_perpendicular(v0) * math.sin(t))
    if len(vecs) == 2:
        s = -(vecs[0] + vecs[1])
        if float(np.linalg.norm(s)) < 1e-6:  # linear: any perpendicular
            return _any_perpendicular(vecs[0])
        b = _unit(s)
        if hyb in ("sp", "sp2"):
            return b
        nrm = _unit(np.cross(vecs[0], vecs[1]))
        return _unit(b * math.cos(_SP3_HALF) + nrm * math.sin(_SP3_HALF))
    s = -np.sum(vecs, axis=0)
    if float(np.linalg.norm(s)) > 0.3:
        return _unit(s)
    poav = probe.local_normal(scene, label)
    nrm = poav if poav is not None else _any_perpendicular(vecs[0])
    centre = np.mean([cart(scene, la) for la in scene.atoms], axis=0)
    return nrm if float((pos - centre) @ nrm) >= 0.0 else -nrm


def require_open_valence(scene: Any, label: str, order: float, n_h: int) -> None:
    """Refuse when ``label`` cannot take a bond of ``order`` even after
    dropping ``n_h`` bonded hydrogens."""
    free = free_valence(scene, label)
    if free is None:
        return
    if free + n_h + _EPS < order:
        el = scene.atoms[label].element
        raise BadInput(
            f"atom {label!r} ({el}) has no open valence for a bond of order "
            f"{order:g}: {free:g} free" + (f" (+{n_h} removable H)" if n_h else ""),
            next="pick an atom with an open valence, or bind a Port on the "
            "host atom (mol.frag(scene=..., ports=...))",
        )


#: Sigma-neighbour budget of a hybridised carbon: sp2 takes three, sp two.
#: Declared bond orders cannot say so on a scene whose pi bonds are
#: implicit (a hexfold Net exports every bond as order 1).
_SIGMA_BUDGET = {"sp": 2, "sp2": 3}


def bond_tables(scene: Any) -> tuple[dict[str, float], dict[str, int]]:
    """``(order sum, bond count)`` per atom label in one pass over the
    bonds — :func:`open_count`'s input when it runs over every atom."""
    used: dict[str, float] = {}
    count: dict[str, int] = {}
    for b in scene.bonds:
        if b.i in scene.atoms and b.j in scene.atoms:
            for la in (b.i, b.j):
                used[la] = used.get(la, 0.0) + float(b.order)
                count[la] = count.get(la, 0) + 1
    return used, count


def open_count(
    scene: Any,
    label: str,
    *,
    hyb: str | None = None,
    tables: tuple[dict[str, float], dict[str, int]] | None = None,
) -> int:
    """How many more single bonds ``label`` can take: the unused valence
    budget (bond orders against :func:`elements.effective_valence`,
    rounded down; none for a metal), capped for an ``sp``/``sp2`` carbon
    by its sigma-neighbour budget. ``hyb`` is the caller's
    hybridization for the atom (else the atom's own tag); ``tables``
    (:func:`bond_tables`) spares the per-atom walk over the bonds."""
    atom = scene.atoms[label]
    mv, _known = elements.effective_valence(atom.element, atom.charge)
    if mv is None:
        return 0
    if tables is None:
        vecs = bond_vectors(scene, label)
        used, count = sum(o for _la, o, _v in vecs), len(vecs)
    else:
        used, count = tables[0].get(label, 0.0), tables[1].get(label, 0)
    free = float(mv) - used
    tag = hyb or atom.hybridization
    if atom.element == "C" and tag in _SIGMA_BUDGET:
        free = min(free, _SIGMA_BUDGET[tag] - count)
    return max(0, math.floor(free + _EPS))
