"""Brinkman / Darcy face transmissivity for porous thin-gap regions
(docs/backlog/se-multiphysics.md slice 4).

A porous insert that fills the local gap replaces the open cubic-gap
transmissivity ``T* = h*^3`` with the Brinkman closed form, in the same
reduced units as :mod:`precis_flow.flow`:

    T* = 12 κ* h* [1 − (2√κ*/h*) tanh(h*/(2√κ*))]

``κ* = κ/h0^2`` is the reduced permeability. Writing the bracket as
``g = 1 − tanh(z)/z`` with ``z = h*/(2√κ*)``, the two limits are

    Darcy      12 κ* h*     as κ* → 0   (z → ∞, g → 1)
    Hele-Shaw  h*^3         as κ* → ∞   (z → 0, 12 κ* g / h*² → 1)

so no ``κ/h²`` threshold is needed: the closed form carries both limits and
every point between. :func:`porous_t_factor` returns the per-cell factor
``t_factor = T*/h*^3 = 12 κ* g / h*²`` that :func:`precis_flow.solve` multiplies
onto ``h*^3`` — an open (non-porous) cell keeps ``t_factor = 1``.

At large ``κ*`` the bracket ``1 − tanh(z)/z`` loses its significant digits to
cancellation (slice-4 acceptance D2: the naive form reads ``−5e-8`` where the
true ``T*/h*^3 − 1`` is ``−1e-9``). Below ``_SERIES_Z`` the Taylor series
``g = z²/3 − 2z⁴/15 + 17z⁶/315 − 62z⁸/2835 + 1382z¹⁰/155925 − …`` is used
instead, which is accurate to ~1e-12 there and carries the exact
``T*/h*^3 → 1 − 1/(10κ*)`` Hele-Shaw expansion.

Species inside the region (D6): :class:`PorousRegion` states ``φ``, ``τ`` and
``α_L/L``; :func:`porous_dispersion` gives the tensor
``ε(φ/τ)/Pe · I + (α_L/L)|U*| ûûᵀ`` that replaces the Taylor tensor there.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

#: Switch to the series for ``g`` below this ``z = h*/(2√κ*)``: the direct
#: ``1 − tanh(z)/z`` is accurate above it, the series below (both to ~1e-12
#: at the crossover). See the module docstring for why the direct form fails.
_SERIES_Z = 0.1


def _bracket(z: np.ndarray) -> np.ndarray:
    """``g = 1 − tanh(z)/z``, evaluated without cancellation near ``z = 0``."""
    w = z * z
    # Horner on w = z² of z²/3 − 2z⁴/15 + 17z⁶/315 − 62z⁸/2835 + 1382z¹⁰/155925.
    series = w * (
        1.0 / 3.0
        + w
        * (
            -2.0 / 15.0
            + w * (17.0 / 315.0 + w * (-62.0 / 2835.0 + w * (1382.0 / 155925.0)))
        )
    )
    # Guard the unused direct branch against z = 0 before np.where selects it.
    zc = np.where(z < _SERIES_Z, 1.0, z)
    direct = 1.0 - np.tanh(zc) / zc
    return np.where(z < _SERIES_Z, series, direct)


def brinkman_transmissivity(
    kappa_star: np.ndarray | float, h_star: np.ndarray | float
) -> np.ndarray:
    """Reduced Brinkman transmissivity ``T*`` of a gap-filling porous medium.

    ``kappa_star`` (``κ/h0²``) and ``h_star`` (``h/h0``) broadcast together and
    must be ``> 0``. Returns ``12 κ* h* (1 − tanh(z)/z)`` with ``z = h*/(2√κ*)``,
    reducing to Darcy ``12 κ* h*`` as ``κ* → 0`` and to the open cubic gap
    ``h*^3`` as ``κ* → ∞``.
    """
    kappa = np.asarray(kappa_star, dtype=float)
    h = np.asarray(h_star, dtype=float)
    if np.any(kappa <= 0.0) or not np.all(np.isfinite(kappa)):
        raise ValueError("brinkman_transmissivity: κ* must be finite and > 0")
    if np.any(h <= 0.0) or not np.all(np.isfinite(h)):
        raise ValueError("brinkman_transmissivity: h* must be finite and > 0")
    z = h / (2.0 * np.sqrt(kappa))
    return 12.0 * kappa * h * _bracket(z)


def porous_t_factor(
    h_star: np.ndarray,
    kappa_star: np.ndarray | float,
    region: np.ndarray,
) -> np.ndarray:
    """Per-cell ``solve`` factor for a porous ``region`` over the gap field.

    Returns ``t_factor = T*/h*^3`` on ``region`` cells (the Brinkman factor that
    scales the cubic-gap law to the gap-filling porous form) and ``1.0``
    elsewhere. ``kappa_star`` is a scalar or an ``(ny, nx)`` field; only its
    values on ``region`` are read, and they must be finite and ``> 0``. The
    region uses the **local** gap ``h_star`` (D5), so a pocket deepens ``T*``.
    """
    h = np.asarray(h_star, dtype=float)
    region = np.asarray(region, dtype=bool)
    if region.shape != h.shape:
        raise ValueError("porous_t_factor: region must have the gap field's shape")
    kappa = np.asarray(kappa_star, dtype=float)
    k_in = kappa[region] if kappa.ndim else kappa
    if np.any(k_in <= 0.0) or not np.all(np.isfinite(k_in)):
        raise ValueError("porous_t_factor: permeability κ* must be finite and > 0")
    h_safe = np.where(h > 0.0, h, 1.0)
    factor = brinkman_transmissivity(np.where(region, kappa, 1.0), h_safe) / h_safe**3
    return np.where(region, factor, 1.0)


@dataclass(frozen=True)
class PorousRegion:
    """A gap-filling porous region as the species solve sees it (slice 4, D6).

    ``cells`` is the ``(ny, nx)`` region mask; ``porosity`` ``φ ∈ (0, 1]`` and
    ``tortuosity`` ``τ > 0`` set the effective diffusivity ``D φ/τ``;
    ``alpha_l_star`` is the longitudinal dispersivity over the length scale,
    ``α_L/L`` (``0`` = no mechanical dispersion). The Taylor term is off inside
    the region: the across-gap profile there is near plug. The region is
    steady-only (no ``φ`` storage term) and carries no reactive wall (the
    series ``k_eff`` assumes open-gap ``Sh``); :func:`precis_flow.species.transport`
    refuses both (M15).
    """

    cells: np.ndarray
    porosity: float
    tortuosity: float
    alpha_l_star: float = 0.0

    def __post_init__(self) -> None:
        if not (0.0 < self.porosity <= 1.0):
            raise ValueError("PorousRegion: porosity must be in (0, 1]")
        if not (math.isfinite(self.tortuosity) and self.tortuosity > 0.0):
            raise ValueError("PorousRegion: tortuosity must be finite and > 0")
        if not (math.isfinite(self.alpha_l_star) and self.alpha_l_star >= 0.0):
            raise ValueError("PorousRegion: alpha_l_star must be finite and >= 0")


def porous_dispersion(
    u_star: np.ndarray, epsilon: float, pe: float, region: PorousRegion
) -> np.ndarray:
    """``(ny, nx, 2, 2)`` reduced dispersion inside a porous region.

    ``𝔻* = ε(φ/τ)/Pe · I + (α_L/L)|U*| ûûᵀ`` with ``û = U*/|U*|`` the
    superficial-velocity direction (zero where ``U* = 0``). Cells outside
    ``region.cells`` are zero; the caller overlays this on the open-gap tensor.
    """
    cells = np.asarray(region.cells, dtype=bool)
    ux, uy = np.asarray(u_star[0], dtype=float), np.asarray(u_star[1], dtype=float)
    speed = np.hypot(ux, uy)
    mol = epsilon * (region.porosity / region.tortuosity) / pe
    with np.errstate(divide="ignore", invalid="ignore"):
        mech = np.where(speed > 0.0, region.alpha_l_star / speed, 0.0)
    out = np.zeros(cells.shape + (2, 2), dtype=float)
    out[:, :, 0, 0] = mol + mech * ux * ux
    out[:, :, 1, 1] = mol + mech * uy * uy
    out[:, :, 0, 1] = mech * ux * uy
    out[:, :, 1, 0] = out[:, :, 0, 1]
    out[~cells] = 0.0
    return out
