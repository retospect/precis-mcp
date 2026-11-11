"""Navier-slip gap profile and Taylor coefficient (se-multiphysics.md slice 5).

The slip *kernel* — the Navier flux factor on ``h*^3/12μ`` — is already
carried by :attr:`precis_flow.domain.Domain.slip_factor` and scales the
pressure solve in :func:`precis_flow.flow.solve`. This module adds the
*species-side* of slip: the across-gap velocity profile that the slip walls
impose, and the Taylor dispersion coefficient that profile produces.

Everything here is a function of the two *local* reduced slip ratios

    β₁ = b_floor / h,   β₂ = b_lid / h

(the per-cell Navier slip length over the local gap, ``b*/h*`` with
``b* = b/h0``, ``h* = h/h0``). The depth-averaged Poiseuille flux factor is

    F = (1 + 4(β₁+β₂) + 12 β₁β₂) / (1 + β₁ + β₂)

— :func:`flux_factor`, the reduced form of ``Domain.slip_factor``. The gap
profile whose depth average is one and whose flux is ``F`` times the no-slip
value is

    w(ψ) = 6(−ψ² + a ψ + a β₁) / F,   a = (1 + 2β₂) / (1 + β₁ + β₂)

(:func:`gap_profile`; ``ψ`` the gap fraction, 0 = floor, 1 = lid). It reduces
to the Hele-Shaw ``6 ψ(1−ψ)`` at no slip. The Taylor dispersion coefficient is
the Aris integral of its shear

    κ = ∫₀¹ ( ∫₀^ψ (w − 1) dψ′ )² dψ

(:func:`taylor_coefficient`), evaluated in closed form from the cubic
``g(ψ) = ∫₀^ψ (w − 1) dψ′``. It is ``1/210`` at no slip, ``1/(210(1+6β)²)``
for equal slip ``β`` on both walls, and tends to ``2/105`` as one slip length
grows without bound.

Wiring into the species solvers needs **no signature change**: the Eulerian
:func:`precis_flow.species.transport` already takes the shear-carrying velocity
through ``shear_velocity=``, and the slip profile rides it as
``U* · sqrt(210 κ)`` — the ``210 κ`` factor turns the hard-coded ``Pe/210`` of
:func:`precis_flow.species.taylor_tensor` into the slip ``Pe κ``. The Lagrangian
:func:`precis_flow.tracer.trace` takes the profile through its ``cell_profile``
hook: :class:`SlipProfile` samples the per-cell ``β`` on the solved
:class:`~precis_flow.domain.Domain` and returns ``w(ψ)`` per particle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .domain import Domain
from .tracer import _bilinear


def _reduced(b1: np.ndarray, b2: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(F, a)`` for reduced slip ratios ``β₁``/``β₂`` (floor/lid)."""
    denom = 1.0 + b1 + b2
    f = (1.0 + 4.0 * (b1 + b2) + 12.0 * b1 * b2) / denom
    a = (1.0 + 2.0 * b2) / denom
    return f, a


def flux_factor(beta1: np.ndarray | float, beta2: np.ndarray | float) -> np.ndarray:
    """The depth-averaged Poiseuille flux factor ``F`` on ``h*^3/12μ``.

    ``beta1``/``beta2`` are the reduced *local* slip ratios ``b/h`` on the
    floor and lid. Returns ``1`` at no slip and the spec's ``F`` otherwise;
    this is the reduced form of :attr:`Domain.slip_factor` (which carries the
    same expression with absolute ``h*`` and ``b*``).
    """
    b1 = np.asarray(beta1, dtype=float)
    b2 = np.asarray(beta2, dtype=float)
    f, _ = _reduced(b1, b2)
    return f


def gap_profile(
    psi: np.ndarray | float,
    beta1: np.ndarray | float,
    beta2: np.ndarray | float,
) -> np.ndarray:
    """The across-gap horizontal velocity profile ``w(ψ)``, depth average one.

    ``w(ψ) = 6(−ψ² + a ψ + a β₁)/F`` with ``a = (1+2β₂)/(1+β₁+β₂)``. At
    ``β₁ = β₂ = 0`` it is the Hele-Shaw parabola ``6 ψ(1−ψ)``. ``psi`` and the
    slip ratios broadcast together.
    """
    p = np.asarray(psi, dtype=float)
    b1 = np.asarray(beta1, dtype=float)
    b2 = np.asarray(beta2, dtype=float)
    f, a = _reduced(b1, b2)
    return 6.0 * (-p * p + a * p + a * b1) / f


def taylor_coefficient(
    beta1: np.ndarray | float, beta2: np.ndarray | float
) -> np.ndarray:
    """The Taylor dispersion coefficient ``κ`` of the slip profile.

    ``κ = ∫₀¹ (∫₀^ψ (w−1) dψ′)² dψ``. With ``g(ψ) = c₁ψ + c₂ψ² + c₃ψ³``
    (``c₃ = −2/F``, ``c₂ = 3a/F``, ``c₁ = 6aβ₁/F − 1``) the integral is the
    closed form below — exact, not quadrature. Equals ``1/210`` at no slip,
    ``1/(210(1+6β)²)`` for equal slip ``β``, and ``→ 2/105`` as a slip length
    grows without bound.
    """
    b1 = np.asarray(beta1, dtype=float)
    b2 = np.asarray(beta2, dtype=float)
    f, a = _reduced(b1, b2)
    c3 = -2.0 / f
    c2 = 3.0 * a / f
    c1 = 6.0 * a * b1 / f - 1.0
    return (
        c1 * c1 / 3.0
        + c1 * c2 / 2.0
        + (c2 * c2 + 2.0 * c1 * c3) / 5.0
        + c2 * c3 / 3.0
        + c3 * c3 / 7.0
    )


@dataclass(frozen=True)
class SlipProfile:
    """A :class:`~precis_flow.tracer.CellProfile` for the slip gap profile.

    Built from a solved slip :class:`~precis_flow.domain.Domain`, it precomputes
    the per-cell reduced slip ratios ``β = b*/h*`` on each wall and, when called
    by :func:`precis_flow.tracer.trace`, samples them at the particle positions
    and returns :func:`gap_profile` at each particle's gap fraction. Refuses a
    domain with no slip fields — then the plain ``profile='parabolic'`` is the
    no-slip shear and there is nothing to add.
    """

    domain: Domain
    _beta1: np.ndarray = field(init=False, repr=False)
    _beta2: np.ndarray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.domain.slip_floor_star is None and self.domain.slip_lid_star is None:
            raise ValueError(
                "SlipProfile needs a Domain with slip walls; a no-slip Domain "
                "has no slip profile to carry (use profile='parabolic')"
            )
        h = self.domain.h_star
        h_safe = np.where(h > 0, h, 1.0)
        shape = self.domain.shape
        zeros = np.zeros(shape)
        b1 = (
            self.domain.slip_floor_star
            if self.domain.slip_floor_star is not None
            else zeros
        )
        b2 = (
            self.domain.slip_lid_star
            if self.domain.slip_lid_star is not None
            else zeros
        )
        object.__setattr__(self, "_beta1", np.where(self.domain.mask, b1 / h_safe, 0.0))
        object.__setattr__(self, "_beta2", np.where(self.domain.mask, b2 / h_safe, 0.0))

    def __call__(
        self,
        domain: Domain,
        x_star: np.ndarray,
        y_star: np.ndarray,
        psi: np.ndarray,
    ) -> np.ndarray:
        stack = _bilinear(np.stack([self._beta1, self._beta2]), domain, x_star, y_star)
        return gap_profile(psi, stack[0], stack[1])
