"""Canonical molecular body frame: centre of mass + principal axes.

A molecule's pose in a design is a block ``rot``; two blocks holding the
same molecule differ in ``rot`` for reasons that have nothing to do with
the molecule. :func:`canonical_body_frame` gives the molecule a zero frame
of its own — origin at the centre of mass, x along the smallest moment of
inertia (long axis), z along the largest (plane normal when planar),
``y = z × x`` — with the axis signs fixed by RDKit's canonical atom
ranking so the frame is independent of input orientation and atom order.

Degenerate cases (``kind``):

- ``linear``: spin about x is undefined; y/z are an arbitrary but
  deterministic perpendicular pair. Callers report that angle as 0.
- ``symmetric_top``: the unique axis is fixed by the inertia tensor, the
  perpendicular pair by the highest-ranked off-axis atom.
- ``spherical``: all three axes from the ranking alone.

Atoms that are symmetry-equivalent share a canonical rank, so for
symmetric molecules the frame is unique only up to a symmetry operation of
the molecule (body coordinates agree as atom *sets*, not index by index).

Not yet wired into any pocket name (the reference molecule is undecided).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from precis.cad.vec import euler_rad_from_matrix

Kind = Literal["asymmetric", "linear", "symmetric_top", "spherical"]

_MIN_PROJECTION_A = 1e-6
_DEGENERATE_REL = 1e-3
_LINEAR_REL = 1e-4


@dataclass(frozen=True)
class BodyFrame:
    """``origin`` (Å, centre of mass); ``axes`` rows are the body x, y, z
    unit vectors in the input frame (right-handed)."""

    origin: NDArray[np.float64]
    axes: NDArray[np.float64]
    kind: Kind

    def to_body(self, coords_A: Any) -> NDArray[np.float64]:
        """Input-frame coordinates → body-frame coordinates."""
        return (np.asarray(coords_A, dtype=float) - self.origin) @ self.axes.T


def _masses(symbols: list[str]) -> NDArray[np.float64]:
    from rdkit import Chem

    pt = Chem.GetPeriodicTable()
    return np.array([pt.GetAtomicWeight(s) for s in symbols], dtype=float)


def _ranks(
    symbols: list[str], coords: NDArray[np.float64], bonds: Any
) -> NDArray[np.int64]:
    """RDKit canonical ranks (ties kept: symmetric atoms share a rank)."""
    from rdkit import Chem

    n = len(symbols)
    mol = Chem.RWMol()
    for s in symbols:
        a = Chem.Atom(s)
        a.SetNoImplicit(True)
        mol.AddAtom(a)
    if bonds is None:
        pt = Chem.GetPeriodicTable()
        rad = [pt.GetRcovalent(s) for s in symbols]
        pairs = [
            (i, j)
            for i in range(n)
            for j in range(i + 1, n)
            if np.linalg.norm(coords[i] - coords[j]) < 1.2 * (rad[i] + rad[j])
        ]
    else:
        pairs = [(int(i), int(j)) for i, j, *_ in bonds]
    for i, j in pairs:
        if i != j and mol.GetBondBetweenAtoms(i, j) is None:
            mol.AddBond(i, j, Chem.BondType.SINGLE)
    mol.UpdatePropertyCache(strict=False)
    Chem.FastFindRings(mol)
    return np.asarray(
        list(Chem.CanonicalRankAtoms(mol, breakTies=False)), dtype=np.int64
    )


def _sign_toward_rank(
    axis: NDArray[np.float64], rel: NDArray[np.float64], ranks: NDArray[np.int64]
) -> float:
    """+1/-1: the side of ``axis`` holding the highest-ranked atom with a
    nonzero projection; equal-rank atoms vote by summed projection, so a
    symmetric pair that cancels defers to the next rank."""
    s = _sign_or_none(axis, rel, ranks)
    return 1.0 if s is None else s


def _sign_or_none(
    axis: NDArray[np.float64], rel: NDArray[np.float64], ranks: NDArray[np.int64]
) -> float | None:
    proj = rel @ axis
    for r in sorted(set(ranks.tolist()), reverse=True):
        sel = (ranks == r) & (np.abs(proj) > _MIN_PROJECTION_A)
        if not sel.any():
            continue
        s = float(proj[sel].sum())
        if abs(s) > _MIN_PROJECTION_A:
            return 1.0 if s > 0 else -1.0
    return None


def _unit(v: NDArray[np.float64]) -> NDArray[np.float64]:
    return v / float(np.linalg.norm(v))


def _ranked_perpendicular(
    axis: NDArray[np.float64], rel: NDArray[np.float64], ranks: NDArray[np.int64]
) -> NDArray[np.float64] | None:
    """Unit component, orthogonal to ``axis``, of the highest-ranked atom
    that has one (ties: lowest input index)."""
    perp = rel - np.outer(rel @ axis, axis)
    norms = np.linalg.norm(perp, axis=1)
    cand = [i for i in range(len(rel)) if norms[i] > _MIN_PROJECTION_A]
    if not cand:
        return None
    best = max(cand, key=lambda i: (int(ranks[i]), -i))
    return _unit(perp[best])


def _any_perpendicular(axis: NDArray[np.float64]) -> NDArray[np.float64]:
    helper = np.zeros(3)
    helper[int(np.argmin(np.abs(axis)))] = 1.0
    return _unit(helper - axis * float(helper @ axis))


def canonical_body_frame(
    symbols: list[str], coords_A: Any, bonds: Any = None
) -> BodyFrame:
    """The molecule's canonical frame (see module docstring). ``bonds`` is
    an optional iterable of ``(i, j[, order])``; omitted, bonds are
    inferred from covalent radii."""
    xyz = np.asarray(coords_A, dtype=float).reshape(-1, 3)
    if len(symbols) != len(xyz) or len(xyz) == 0:
        raise ValueError("symbols and coords_A must be the same non-zero length")
    m = _masses(symbols)
    com = (m[:, None] * xyz).sum(axis=0) / m.sum()
    rel = xyz - com
    r2 = (rel**2).sum(axis=1)
    inertia = np.einsum("i,ij,ik->jk", m, rel, rel) * -1.0 + np.eye(3) * float(
        (m * r2).sum()
    )
    w, v = np.linalg.eigh(inertia)  # ascending moments
    imax = float(w[2])
    ranks = _ranks(symbols, xyz, bonds)

    if imax < 1e-12:
        kind: Kind = "spherical"  # single atom
    elif w[0] < _LINEAR_REL * imax:
        kind = "linear"
    elif (w[2] - w[0]) < _DEGENERATE_REL * imax:
        kind = "spherical"
    elif (w[1] - w[0]) < _DEGENERATE_REL * imax or (w[2] - w[1]) < (
        _DEGENERATE_REL * imax
    ):
        kind = "symmetric_top"
    else:
        kind = "asymmetric"

    if kind == "asymmetric":
        x, y, z = v[:, 0], v[:, 1], v[:, 2]
        sx, sy, sz = (_sign_or_none(a, rel, ranks) for a in (x, y, z))
        # Axes no atom can orient (planar z, symmetric pairs) are filled
        # in from the others, keeping the frame right-handed.
        if sx is not None:
            x = x * sx
            if sz is not None:
                z = z * sz
                y = np.cross(z, x)
            else:
                y = y * (sy if sy is not None else 1.0)
                z = np.cross(x, y)
        elif sz is not None:
            z = z * sz
            y = y * (sy if sy is not None else 1.0)
            x = np.cross(y, z)
        else:
            y = y * (sy if sy is not None else 1.0)
            z = z * 1.0
            x = np.cross(y, z)
    elif kind == "linear":
        x = v[:, 0]
        x = x * _sign_toward_rank(x, rel, ranks)
        y = _any_perpendicular(x)
        z = np.cross(x, y)
    elif kind == "symmetric_top":
        if (w[1] - w[0]) < _DEGENERATE_REL * imax:  # oblate: unique = largest
            z = v[:, 2]
            z = z * _sign_toward_rank(z, rel, ranks)
            x_ = _ranked_perpendicular(z, rel, ranks)
            x = x_ if x_ is not None else _any_perpendicular(z)
            y = np.cross(z, x)
        else:  # prolate: unique = smallest
            x = v[:, 0]
            x = x * _sign_toward_rank(x, rel, ranks)
            y_ = _ranked_perpendicular(x, rel, ranks)
            y = y_ if y_ is not None else _any_perpendicular(x)
            z = np.cross(x, y)
    else:  # spherical
        norms = np.linalg.norm(rel, axis=1)
        cand = [i for i in range(len(rel)) if norms[i] > _MIN_PROJECTION_A]
        if not cand:
            x = np.array([1.0, 0.0, 0.0])
        else:
            best = max(cand, key=lambda i: (int(ranks[i]), -i))
            x = _unit(rel[best])
        y_ = _ranked_perpendicular(x, rel, ranks)
        y = y_ if y_ is not None else _any_perpendicular(x)
        z = np.cross(x, y)

    axes = np.vstack([_unit(x), _unit(y), _unit(z)])
    return BodyFrame(origin=com, axes=axes, kind=kind)


def euler_xyz_deg(R: Any) -> tuple[float, float, float]:
    """Euler angles (degrees) of rotation matrix ``R`` in the convention
    of :func:`precis.cad.vec.pose` (``R = Rz @ Ry @ Rx``)."""
    rx, ry, rz = euler_rad_from_matrix(np.asarray(R, dtype=float))
    return (float(np.degrees(rx)), float(np.degrees(ry)), float(np.degrees(rz)))


def relative_rotation(
    body: BodyFrame, wall_axes: Any = None
) -> tuple[float, float, float]:
    """The body frame's rotation relative to the wall frame (rows
    ``wall_axes``, identity when omitted) as x-y-z Euler degrees."""
    wall = np.eye(3) if wall_axes is None else np.asarray(wall_axes, dtype=float)
    R = wall @ body.axes.T
    if body.kind == "linear":
        # spin about the body x axis is undefined: report only the x
        # axis's direction, as (0, ry, rz)
        x = R[:, 0]
        ry = float(np.arcsin(np.clip(-x[2], -1.0, 1.0)))
        rz = float(np.arctan2(x[1], x[0])) if np.cos(ry) > 1e-9 else 0.0
        return (0.0, float(np.degrees(ry)), float(np.degrees(rz)))
    return euler_xyz_deg(R)
