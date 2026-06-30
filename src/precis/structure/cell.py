"""The periodic cell — lattice + per-axis PBC (ADR 0043 §3).

The cell is three lattice vectors ``a, b, c`` (rows of a 3×3 matrix, Å) plus a
per-axis ``pbc`` flag triple. Crystals tile by **pure translation** (PBC), never
mirror (§wording). Positions are stored fractional; this module is the one place
fractional↔Cartesian and the **minimum-image convention** (MIC) live.

The MIC search is *exact for any cell shape* (including triclinic): it reduces
the fractional delta per periodic axis and then checks the 3×3×3 block of
surrounding images, returning both the nearest distance and the integer image
offset on ``j`` (the ``to_jimage`` of ADR 0043 §4.1).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

ImageOffset = tuple[int, int, int]


@dataclass(frozen=True)
class Cell:
    """A periodic box: lattice (3×3, rows = a,b,c in Å) + per-axis PBC."""

    lattice: np.ndarray
    pbc: tuple[bool, bool, bool] = (True, True, True)

    @classmethod
    def from_lengths_angles(
        cls,
        a: float,
        b: float,
        c: float,
        alpha: float = 90.0,
        beta: float = 90.0,
        gamma: float = 90.0,
        pbc: tuple[bool, bool, bool] = (True, True, True),
    ) -> Cell:
        """Build from conventional lengths (Å) + angles (degrees)."""
        al, be, ga = np.radians([alpha, beta, gamma])
        va = np.array([a, 0.0, 0.0])
        vb = np.array([b * np.cos(ga), b * np.sin(ga), 0.0])
        cx = c * np.cos(be)
        cy = c * (np.cos(al) - np.cos(be) * np.cos(ga)) / np.sin(ga)
        cz = np.sqrt(max(c * c - cx * cx - cy * cy, 0.0))
        return cls(np.array([va, vb, [cx, cy, cz]]), pbc)

    def frac_to_cart(self, frac: np.ndarray) -> np.ndarray:
        """Fractional → Cartesian (Å)."""
        return np.asarray(frac, dtype=float) @ self.lattice

    def cart_to_frac(self, cart: np.ndarray) -> np.ndarray:
        """Cartesian (Å) → fractional."""
        return np.asarray(cart, dtype=float) @ np.linalg.inv(self.lattice)

    @property
    def volume(self) -> float:
        """Cell volume in Å³."""
        return float(abs(np.linalg.det(self.lattice)))

    def wrap(self, frac: np.ndarray) -> np.ndarray:
        """Wrap a fractional position into ``[0,1)`` on periodic axes only.

        Implements ADR 0043 §3's "place-outside-wraps-inside": a position given
        outside the cell lands inside, in the right place.
        """
        f = np.asarray(frac, dtype=float).copy()
        for ax in range(3):
            if self.pbc[ax]:
                f[ax] = f[ax] % 1.0
        return f

    def mic(self, frac_i: np.ndarray, frac_j: np.ndarray) -> tuple[float, ImageOffset]:
        """Minimum-image distance (Å) from ``i`` to ``j`` + the image offset on ``j``.

        Exact for any cell: reduce per periodic axis, then check the 3×3×3
        surrounding images and keep the nearest. The returned offset ``img`` is
        the lattice translation such that the nearest copy of ``j`` sits at
        ``frac_j + img`` (the ``to_jimage`` of §4.1).
        """
        d0 = np.asarray(frac_j, dtype=float) - np.asarray(frac_i, dtype=float)
        img_base = np.zeros(3, dtype=int)
        for ax in range(3):
            if self.pbc[ax]:
                img_base[ax] = -int(np.round(d0[ax]))
        ranges = [(-1, 0, 1) if self.pbc[ax] else (0,) for ax in range(3)]
        best_d2 = np.inf
        best_img: ImageOffset = (0, 0, 0)
        for na, nb, nc in itertools.product(*ranges):
            img = img_base + np.array([na, nb, nc], dtype=int)
            cart = (d0 + img) @ self.lattice
            d2 = float(cart @ cart)
            if d2 < best_d2:
                best_d2 = d2
                best_img = (int(img[0]), int(img[1]), int(img[2]))
        return float(np.sqrt(best_d2)), best_img
