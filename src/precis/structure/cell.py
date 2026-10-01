"""The periodic cell — lattice + per-axis PBC.

The cell is three lattice vectors ``a, b, c`` (rows of a 3×3 matrix, Å) plus a
per-axis ``pbc`` flag triple. Crystals tile by **pure translation** (PBC), never
mirror (§wording). Positions are stored fractional; this module is the one place
fractional↔Cartesian and the **minimum-image convention** (MIC) live.

The MIC search is *exact for any cell shape* (including triclinic): it reduces
the fractional delta per periodic axis and then checks the 3×3×3 block of
surrounding images, returning both the nearest distance and the integer image
offset on ``j`` (the ``to_jimage`` of the structure atomistic IR).

Unit enclave (package docstring): Å-native; ``Cell.lattice`` is never
converted to SI inside this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

ImageOffset = tuple[int, int, int]


def as_pbc3(
    value: Iterable[object] | None,
    default: tuple[bool, bool, bool] = (True, True, True),
) -> tuple[bool, bool, bool]:
    """Coerce a dynamic (op-dict / JSON / DB-meta) value to a checked 3-axis PBC.

    Callers pull ``pbc`` out of untyped dicts, so the raw value is only known
    to be *some* iterable — ``tuple(value)`` would give mypy a ``tuple[Any,
    ...]`` that never satisfies :attr:`Cell.pbc`'s fixed-length type. Explicit
    unpacking both gives a real ``tuple[bool, bool, bool]`` and raises a clear
    error if the caller sent the wrong number of axes instead of silently
    truncating/padding.
    """
    if not value:  # None or empty → default (matches ``as_float3``)
        return default
    x, y, z = (bool(v) for v in value)
    return (x, y, z)


def as_image3(
    value: Iterable[Any] | None, default: ImageOffset = (0, 0, 0)
) -> ImageOffset:
    """Coerce a dynamic value to a checked 3-axis integer image offset.

    Same rationale as :func:`as_pbc3`, for :attr:`Bond.image` /
    ``to_jimage``-shaped triples.
    """
    if not value:  # None or empty → default (matches ``as_float3``)
        return default
    x, y, z = (int(v) for v in value)
    return (x, y, z)


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

        Implements the structure atomistic IR's "place-outside-wraps-inside": a position given
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
        from . import _pair_kernel  # lazy: numba import stays off cli.main

        lat, pbc = _pair_kernel.cell_arrays(self)
        d, ia, ib, ic = _pair_kernel.mic_scalar(
            np.ascontiguousarray(frac_i, dtype=np.float64),
            np.ascontiguousarray(frac_j, dtype=np.float64),
            lat,
            pbc,
        )
        return float(d), (int(ia), int(ib), int(ic))
