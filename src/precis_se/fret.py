"""FRET physics — the pure layer under se's optical-link (comm) domain.

Förster resonance energy transfer: a donor chromophore in an excited state
hands its excitation to a nearby acceptor by **near-field dipole-dipole
coupling**. No photon is emitted and none is absorbed; the coupling is
Coulombic, so the "channel" is geometry, not a waveguide. That single fact
is what makes FRET an se domain at all rather than a spectroscopy detail:
the rate is a function of pose, and pose is what a space plan decides.

**Nothing here touches the tree, the store or the cad kernel.** This module
is arrays and floats so the physics stays testable against published pairs
(see ``tests/test_se_fret.py``, which pins Cy3/Cy5 against its literature
R0). The tree-facing layer — the per-block chromophore card, the L0 link,
the L4 budget view — lives in :mod:`precis_se.ops` and
:mod:`precis_se.handler` and calls in here.

**The governing law.**

.. math::

    E = \\frac{1}{1 + (r/R_0)^6},
    \\qquad
    k_{\\mathrm{FRET}} = \\frac{1}{\\tau_D}\\left(\\frac{R_0}{r}\\right)^6

with the Förster radius (the separation at which transfer and every other
donor de-excitation path are equally likely, so :math:`E = 1/2`)

.. math::

    R_0 = 0.211\\,\\left[\\kappa^2\\, n^{-4}\\, Q_D\\, J(\\lambda)\\right]^{1/6}
    \\;\\mathrm{\\AA}

:math:`J` in the spectroscopic convention ``M^-1 cm^-1 nm^4``. Four inputs,
and a design system can move exactly two of them:

- :math:`J` — spectral overlap of donor emission with acceptor absorption.
  A **chemistry** choice (which dye pair), fixed once the blocks are named.
- :math:`Q_D` — donor quantum yield. Chemistry again, though environment
  degrades it.
- :math:`n` — refractive index of the intervening medium, to the **fourth**
  power. A materials choice; se reaches it through the service environment.
- :math:`\\kappa^2` — the orientation factor. **Geometry.** This is se's.

**κ² is the trap this module exists to prevent.** The literature value 2/3
is the isotropic average — correct for dyes tumbling freely on a flexible
tether, and wrong for anything se builds, because a rigid space plan fixes
both dipoles. The real factor is

.. math::

    \\kappa = \\hat\\mu_D \\cdot \\hat\\mu_A
              - 3(\\hat\\mu_D \\cdot \\hat{r})(\\hat\\mu_A \\cdot \\hat{r})

ranging over :math:`\\kappa^2 \\in [0, 4]`: a factor-of-six spread in
:math:`R_0^6`, and — the part that bites — **exactly zero** for dipoles
that are mutually perpendicular and both perpendicular to the separation
vector. A placement optimiser that sees only distance will happily emit a
geometrically perfect, permanently dead link. :func:`kappa_squared` is
therefore mandatory on every realised link, and
:func:`orientation_headroom` is what the L4 view reports so a near-null
pose is visible *before* it is fabricated.

**The r^-6 law cuts both ways.** It buys spatial channel isolation for free
— double the separation and the rate drops 64-fold, which is why a FRET
network can be dense — and it makes the link brutally sensitive to pose
error: see :func:`distance_sensitivity`, which is the closed form
:math:`6(1-E)` for the relative efficiency error produced by a relative
distance error. At the usual :math:`E = 0.5` operating point a 1% distance
error is a 3% efficiency error, so se's tolerance machinery matters more
here than it does for a structural fit.

**Regimes where this formula is simply wrong** — :func:`regime` classifies
every pair so the view can refuse to quote a number it does not believe:

- Below ~1 nm, Dexter exchange transfer (exponential in :math:`r`, needs
  orbital overlap) competes or dominates, and the point-dipole
  approximation behind Förster breaks down anyway once separation
  approaches chromophore size. A transition-density treatment is needed;
  this module reports :attr:`Regime.DEXTER` and declines.
- Near a metal, energy transfer to the surface goes as :math:`r^{-4}`
  (NSET), not :math:`r^{-6}`, and the metal quenches besides. se knows a
  block's material, so the view can flag it; the exponent change is not
  modelled here.
- Beyond ~2.5 :math:`R_0` the efficiency is under 0.03% and the pair is
  reported :attr:`Regime.NEGLIGIBLE` — not an error, just not a link.

**A donor is a broadcast, not a wire.** Every acceptor in range competes
for the same excitation, so a per-pair efficiency is meaningless in
isolation: the branching ratios share one denominator
(:func:`solve_donor`). This is the whole reason the L4 view is an all-pairs
matrix and not a list of links.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.vec import Vec3, as_vec3

#: What this module's vector parameters actually accept. Every one of them
#: runs its argument through :func:`~precis.cad.vec.as_vec3`, so a plain
#: ``[0, 0, 1]`` works as well as an array — and callers reach for the list
#: constantly (a stored dipole is JSON, a test geometry is three literals).
#: Annotating the narrow :data:`~precis.cad.vec.Vec3` would make mypy
#: reject the ergonomic call the implementation has always supported.
VecLike = Vec3 | Sequence[float]

#: Metres per nanometre. se is float64 metres everywhere (see the package
#: docstring's units decision); the spectroscopic literature is
#: nanometres. The conversion is confined to this module's boundary —
#: every length this module *returns* is metres.
#:
#: There is deliberately **no ångström constant here**, even though
#: Förster's prefactor is conventionally quoted in Å: an Å↔m factor inside
#: ``precis_se`` means the atomic mode's structure-enclave seam, which is
#: policed to two named modules by
#: ``tests/test_se_atomic_angstrom_seam.py``. This module is not that
#: seam — it has no atoms — so the ångström is folded into
#: :data:`_R0_PREFACTOR_M` at its source instead of being carried as a
#: unit conversion it would be wrong to reuse.
NM_M: float = 1e-9

#: Förster's prefactor, **in metres**. The literature form is
#: ``R0[Å] = 0.211 · [κ² n⁻⁴ Q_D J]^(1/6)`` for ``J`` in ``M⁻¹ cm⁻¹ nm⁴``
#: (Lakowicz, *Principles of Fluorescence Spectroscopy* 3rd ed.,
#: eq. 13.5); ``0.211 Å`` is carried here as ``2.11e-11 m`` so the
#: ångström never becomes a *constant* this module could accidentally
#: reuse as a unit conversion (see :data:`NM_M`'s note).
#:
#: The 0.211 itself is ``(9000 ln10 / (128 π⁵ N_A))^(1/6)`` with the unit
#: bookkeeping folded in, which is why it is carried as a number rather
#: than rebuilt from :mod:`scipy.constants` — the folded-in centimetre and
#: nanometre make a from-constants rederivation more error-prone, not
#: less. Pinned against a published pair in ``tests/test_se_fret.py``.
_R0_PREFACTOR_M: float = 2.11e-11

#: Separation below which Dexter exchange transfer competes with Förster
#: and the point-dipole approximation is no longer trustworthy. A physical
#: crossover scale (roughly the size of an aromatic chromophore), not a
#: numerical tolerance — hence absolute, and deliberately not named
#: ``*_EPS``/``*_TOL``.
DEXTER_CROSSOVER_M: float = 1.0 * NM_M

#: Separation, as a multiple of R0, beyond which a pair is reported
#: :attr:`Regime.NEGLIGIBLE`. At 2.5·R0 the efficiency is 1/(1+2.5⁶) ≈
#: 0.0041 — under half a percent, below any link budget worth quoting.
NEGLIGIBLE_R0_MULTIPLE: float = 2.5

#: κ² for freely rotating dipoles that sample every orientation fast
#: compared with the donor lifetime. Provided so callers can *name* the
#: assumption they are making; :func:`kappa_squared` is what a rigid se
#: design must use instead.
ISOTROPIC_KAPPA_SQUARED: float = 2.0 / 3.0


class FretError(ValueError):
    """A physically meaningless FRET input (negative yield, empty spectrum).

    Raised by this module's validators. The ops layer catches it and
    re-raises as its own ``OpError`` so a bad ``set_chromophore`` reads
    like every other rejected op.
    """


class Regime(Enum):
    """Which physics actually governs a pair at its realised separation.

    The L4 view quotes an efficiency only for :attr:`FORSTER`. The other
    three are the honest answers: too close for this model, far enough to
    ignore, or geometrically nulled.
    """

    #: Förster applies: separation is above the Dexter crossover and
    #: within :data:`NEGLIGIBLE_R0_MULTIPLE` of R0.
    FORSTER = "forster"
    #: Below :data:`DEXTER_CROSSOVER_M` — exchange transfer competes and
    #: the point-dipole approximation is unsafe. No number is quoted.
    DEXTER = "dexter"
    #: Beyond :data:`NEGLIGIBLE_R0_MULTIPLE` · R0. Not a link.
    NEGLIGIBLE = "negligible"
    #: κ² is ~0: the dipoles are mutually perpendicular and perpendicular
    #: to the separation vector. Dead at any distance.
    ORIENTATION_NULL = "orientation_null"
    #: The two blocks share a pose, so there is no separation vector and
    #: κ² is undefined. Not a physical state — a design in which nobody has
    #: placed anything yet, which is se's NORMAL early state (a space plan
    #: is suggestive by contract and every field beyond a name is
    #: optional). Reported rather than raised for exactly that reason: a
    #: view that dies on an unplaced pair is useless at the moment it is
    #: most needed.
    COINCIDENT = "coincident"


@dataclass(frozen=True)
class Spectrum:
    """A sampled spectrum over wavelength.

    ``wavelength_nm`` is strictly ascending nanometres; ``value`` carries
    the matching ordinates. For a donor *emission* spectrum the ordinate is
    arbitrary units (it cancels in :func:`overlap_integral`); for an
    acceptor *absorption* spectrum it is molar extinction in
    ``M⁻¹ cm⁻¹``, and the absolute scale very much does not cancel.

    Nanometres rather than metres because this is an ingest boundary:
    every published spectrum, every dye datasheet and every plate-reader
    export is in nm, and a conversion at the door is one place to be wrong
    instead of hundreds.
    """

    wavelength_nm: tuple[float, ...]
    value: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.wavelength_nm) != len(self.value):
            raise FretError(
                f"spectrum length mismatch: {len(self.wavelength_nm)} "
                f"wavelengths vs {len(self.value)} values"
            )
        if len(self.wavelength_nm) < 2:
            raise FretError("a spectrum needs at least two samples to integrate")
        lam = np.asarray(self.wavelength_nm, dtype=np.float64)
        if not np.all(np.diff(lam) > 0.0):
            raise FretError("spectrum wavelengths must be strictly ascending")
        if float(lam[0]) <= 0.0:
            raise FretError("spectrum wavelengths must be positive")
        val = np.asarray(self.value, dtype=np.float64)
        if not np.all(np.isfinite(val)):
            raise FretError("spectrum values must be finite")
        if np.any(val < 0.0):
            raise FretError("spectrum values must be non-negative")

    @classmethod
    def of(
        cls, samples: Sequence[tuple[float, float]] | Sequence[Sequence[float]]
    ) -> Spectrum:
        """Build from ``[(wavelength_nm, value), ...]`` pairs.

        The shape the store round-trips and the shape a caller hands an op,
        so the conversion lives here rather than at three call sites.
        """
        lam: list[float] = []
        val: list[float] = []
        for pair in samples:
            point = tuple(pair)
            if len(point) != 2:
                raise FretError(
                    f"spectrum sample must be a (wavelength, value) pair: {point!r}"
                )
            lam.append(float(point[0]))
            val.append(float(point[1]))
        return cls(tuple(lam), tuple(val))

    def as_arrays(self) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Wavelength and ordinate as float64 arrays."""
        return (
            np.asarray(self.wavelength_nm, dtype=np.float64),
            np.asarray(self.value, dtype=np.float64),
        )

    def at(self, wavelength_nm: float) -> float:
        """Linearly interpolated ordinate; 0.0 outside the sampled range.

        Zero rather than an edge-hold because both spectra are bounded
        physical bands — holding the edge would invent absorption in a
        region the datasheet says is dark, and that invention lands
        straight in :func:`overlap_integral`.
        """
        lam, val = self.as_arrays()
        if wavelength_nm < float(lam[0]) or wavelength_nm > float(lam[-1]):
            return 0.0
        return float(np.interp(wavelength_nm, lam, val))

    def peak_nm(self) -> float:
        """Wavelength of maximum ordinate — the nominal channel label."""
        lam, val = self.as_arrays()
        return float(lam[int(np.argmax(val))])


@dataclass(frozen=True, eq=False)
class Chromophore:
    """The per-block optical property card: what makes a block a radio.

    ``eq=False`` because ``dipole`` is a numpy array: a generated
    ``__eq__`` would compare it elementwise and then try to reduce the
    resulting array to a bool, and the generated ``__hash__`` would try to
    hash it — both raise. Comparing two cards means comparing the stored
    *payloads* (:func:`validate_chromophore`'s output, plain JSON), which
    is what the persist layer round-trips anyway.

    ``dipole`` is the **transition dipole direction in the block's own
    frame**, not world space. That is the whole point of hanging it off a
    block: the pose that se already stores rotates it into world space
    (:func:`world_dipole`), so moving a block updates its optics for free
    and κ² is always computed from the realised geometry rather than
    assumed.

    ``lifetime_s`` is the donor excited-state lifetime *in the absence of
    the acceptor* — it sets the clock every transfer rate is measured
    against, and it is also the hard ceiling on symbol rate: nanosecond
    lifetimes mean a sub-GHz channel at the very best.
    """

    #: Free-text label — the dye or moiety ("Cy3", "tryptophan").
    label: str
    #: Transition dipole direction in the block frame. Normalised on
    #: construction; magnitude is carried by the spectra, not here.
    dipole: Vec3
    #: Fluorescence quantum yield of this chromophore acting as donor.
    quantum_yield: float
    #: Excited-state lifetime, seconds, acceptor-free.
    lifetime_s: float
    #: Emission band, arbitrary ordinate units.
    emission: Spectrum
    #: Molar extinction band, ``M⁻¹ cm⁻¹``.
    absorption: Spectrum

    def __post_init__(self) -> None:
        if not 0.0 <= self.quantum_yield <= 1.0:
            raise FretError(
                f"quantum yield must be in [0, 1], got {self.quantum_yield}"
            )
        if not self.lifetime_s > 0.0:
            raise FretError(f"lifetime must be positive, got {self.lifetime_s}")
        unit = as_vec3(self.dipole)
        norm = float(np.linalg.norm(unit))
        # Dimensionless direction vector, so a bare magnitude guard has no
        # governing length to be relative to.
        if norm < 1e-12:
            raise FretError("transition dipole must not be the zero vector")
        object.__setattr__(self, "dipole", unit / norm)


def world_dipole(dipole: VecLike, rotation: NDArray[np.float64]) -> Vec3:
    """Rotate a block-frame dipole into world space.

    Rotation only — a transition dipole is a direction, so the translation
    half of the block's pose is irrelevant to it and applying the full
    transform would be a bug.
    """
    world = np.asarray(rotation, dtype=np.float64) @ as_vec3(dipole)
    norm = float(np.linalg.norm(world))
    if norm < 1e-12:  # dimensionless: a rotation cannot shrink a unit vector
        raise FretError("rotation collapsed the transition dipole")
    return world / norm


def kappa_squared(
    donor_dipole: VecLike,
    acceptor_dipole: VecLike,
    separation: VecLike,
) -> float:
    """Orientation factor κ² for one realised pair geometry.

    All three vectors in the **same** frame (world, in practice);
    ``separation`` runs donor → acceptor and need not be normalised.

    κ = μ̂_D·μ̂_A − 3(μ̂_D·r̂)(μ̂_A·r̂), so κ² ∈ [0, 4]: 4 for collinear
    head-to-tail dipoles along r̂, 1 for parallel dipoles perpendicular to
    r̂, and **0** for the mutually-perpendicular-and-perpendicular-to-r̂
    arrangement that kills a link outright.
    """
    d = as_vec3(donor_dipole)
    a = as_vec3(acceptor_dipole)
    r = as_vec3(separation)
    r_norm = float(np.linalg.norm(r))
    if r_norm <= 0.0:
        raise FretError("donor and acceptor are coincident: κ² is undefined")
    r_hat = r / r_norm
    d_hat = d / float(np.linalg.norm(d))
    a_hat = a / float(np.linalg.norm(a))
    kappa = float(
        np.dot(d_hat, a_hat) - 3.0 * np.dot(d_hat, r_hat) * np.dot(a_hat, r_hat)
    )
    return kappa * kappa


def orientation_headroom(kappa_sq: float) -> float:
    """κ² as a fraction of the isotropic 2/3 — the number the view reports.

    A rigid design's κ² is a *fact*, not an average, so the useful question
    is not "what is κ²" but "how far is this pose from the null". Values
    below 1 mean the geometry is working against the link; below ~0.1 the
    pose is one fabrication tolerance away from dead, whatever the
    separation says.
    """
    return kappa_sq / ISOTROPIC_KAPPA_SQUARED


def _trapz(y: NDArray[np.float64], x: NDArray[np.float64]) -> float:
    """Trapezoidal integral of ``y`` over ``x``.

    Spelled out rather than called from numpy because the spelling moved:
    ``np.trapz`` in 1.x, ``np.trapezoid`` in 2.x, and this repo's floor is
    ``numpy>=1.24`` — so either name breaks on half the supported range.
    Four lines is cheaper than a version shim.
    """
    return float(np.sum(0.5 * (y[1:] + y[:-1]) * np.diff(x)))


def overlap_integral(donor_emission: Spectrum, acceptor_absorption: Spectrum) -> float:
    """Spectral overlap ``J``, in ``M⁻¹ cm⁻¹ nm⁴``.

    .. math::

        J = \\frac{\\int F_D(\\lambda)\\,\\varepsilon_A(\\lambda)\\,
                   \\lambda^4\\,d\\lambda}
                  {\\int F_D(\\lambda)\\,d\\lambda}

    The λ⁴ weighting is why red-shifted pairs punch above their apparent
    overlap. The denominator normalises the donor's arbitrary emission
    units away — and it integrates the **whole** donor band, not just the
    part that overlaps, which is the single most common way to get this
    integral wrong by a factor of several.

    Integration is trapezoidal on the union of both wavelength grids
    (restricted to the overlap window for the numerator), so neither
    spectrum's sampling silently decimates the other's.
    """
    d_lam, d_val = donor_emission.as_arrays()
    a_lam, _ = acceptor_absorption.as_arrays()

    donor_area = _trapz(d_val, d_lam)
    if not donor_area > 0.0:
        raise FretError("donor emission spectrum integrates to zero")

    lo = max(float(d_lam[0]), float(a_lam[0]))
    hi = min(float(d_lam[-1]), float(a_lam[-1]))
    if hi <= lo:
        return 0.0

    grid = np.unique(np.concatenate([d_lam, a_lam, np.array([lo, hi])]))
    grid = grid[(grid >= lo) & (grid <= hi)]
    emission = np.interp(grid, d_lam, d_val)
    extinction = np.interp(grid, *acceptor_absorption.as_arrays())
    numerator = _trapz(emission * extinction * grid**4, grid)
    return numerator / donor_area


def forster_radius(
    *,
    overlap: float,
    quantum_yield: float,
    kappa_sq: float,
    refractive_index: float,
) -> float:
    """R0 in **metres** from the four inputs.

    ``overlap`` in ``M⁻¹ cm⁻¹ nm⁴`` (:func:`overlap_integral`). Note the
    ``n⁻⁴``: swapping the medium between vacuum (1.0) and a dense polymer
    (1.6) moves R0 by ~25%, which is a larger effect than most pose
    tweaks — a FRET design is only as trustworthy as its declared medium.

    Returns 0.0 for a pair with no overlap or a nulled orientation, which
    :func:`pair_efficiency` then reports as zero transfer rather than a
    division blowing up.
    """
    if overlap < 0.0:
        raise FretError(f"overlap integral must be non-negative, got {overlap}")
    if not 0.0 <= quantum_yield <= 1.0:
        raise FretError(f"quantum yield must be in [0, 1], got {quantum_yield}")
    if kappa_sq < 0.0:
        raise FretError(f"κ² must be non-negative, got {kappa_sq}")
    if not refractive_index > 0.0:
        raise FretError(f"refractive index must be positive, got {refractive_index}")
    product = kappa_sq * quantum_yield * overlap / refractive_index**4
    if product <= 0.0:
        return 0.0
    return _R0_PREFACTOR_M * product ** (1.0 / 6.0)


def pair_efficiency(separation_m: float, forster_radius_m: float) -> float:
    """``E = 1/(1 + (r/R0)^6)`` for an isolated donor-acceptor pair.

    *Isolated* is load-bearing: with more than one acceptor in range the
    branching ratios share a denominator and this overstates every link.
    Use :func:`solve_donor` for anything with a second acceptor in it.

    This is also the **bare formula, ungated by** :func:`regime`. It will
    quote 0.9999 for a pair 0.2 nm apart, where :func:`solve_donor`
    contributes no rate at all because Förster does not apply there. The
    two disagreeing is by design — one is the equation, the other is the
    model with its domain of validity attached — but a caller that mixes
    them will read a regime boundary as a competition bug.
    """
    if separation_m <= 0.0:
        raise FretError("separation must be positive")
    if forster_radius_m <= 0.0:
        return 0.0
    ratio = separation_m / forster_radius_m
    return 1.0 / (1.0 + ratio**6)


def transfer_rate(
    separation_m: float, forster_radius_m: float, lifetime_s: float
) -> float:
    """``k = (1/τ_D)(R0/r)^6``, per second.

    The rate, not the efficiency, is what composes: competing acceptors add
    their rates (:func:`solve_donor`), and a relay stage's throughput is set
    by its rate against the donor's own decay clock.
    """
    if separation_m <= 0.0:
        raise FretError("separation must be positive")
    if not lifetime_s > 0.0:
        raise FretError("lifetime must be positive")
    if forster_radius_m <= 0.0:
        return 0.0
    return (1.0 / lifetime_s) * (forster_radius_m / separation_m) ** 6


def regime(separation_m: float, forster_radius_m: float, kappa_sq: float) -> Regime:
    """Classify a pair so the view never quotes a number it cannot support.

    Order matters: an orientation null is reported ahead of the distance
    checks because it is the *actionable* diagnosis — "rotate this block"
    is a fix, "it is 4 nm away" is not.
    """
    # Relative test against the isotropic reference: κ² is dimensionless,
    # so this threshold needs no governing length.
    if orientation_headroom(kappa_sq) < 1e-3:
        return Regime.ORIENTATION_NULL
    if separation_m < DEXTER_CROSSOVER_M:
        return Regime.DEXTER
    if forster_radius_m <= 0.0:
        return Regime.NEGLIGIBLE
    if separation_m > NEGLIGIBLE_R0_MULTIPLE * forster_radius_m:
        return Regime.NEGLIGIBLE
    return Regime.FORSTER


def distance_sensitivity(efficiency: float) -> float:
    """Relative efficiency error per unit relative distance error: ``6(1-E)``.

    Differentiating ``E = 1/(1+x⁶)`` at ``x = r/R0`` gives
    ``dlnE/dlnr = -6x⁶/(1+x⁶) = -6(1-E)`` exactly — no approximation. So a
    link parked at E = 0.5 multiplies pose error by 3, and one at E = 0.1
    multiplies it by 5.4. This is the number to hand se's tolerance
    machinery; a FRET budget computed from nominal poses alone is fiction.
    """
    if not 0.0 <= efficiency <= 1.0:
        raise FretError(f"efficiency must be in [0, 1], got {efficiency}")
    return 6.0 * (1.0 - efficiency)


def separation_for_efficiency(efficiency: float, forster_radius_m: float) -> float:
    """Invert ``E(r)`` — the separation that hits a target efficiency.

    What a placement pass actually wants: a declared "this link needs 60%"
    becomes a distance constraint the geometry layer can solve against.
    """
    if not 0.0 < efficiency < 1.0:
        raise FretError("target efficiency must be strictly between 0 and 1")
    if forster_radius_m <= 0.0:
        raise FretError("R0 must be positive to invert E(r)")
    return forster_radius_m * ((1.0 - efficiency) / efficiency) ** (1.0 / 6.0)


@dataclass(frozen=True)
class AcceptorChannel:
    """One acceptor's share of a donor's excitation.

    ``efficiency`` is the branching ratio *after* competition, so the
    channels of one donor plus its residual decay sum to exactly 1.
    """

    #: Acceptor block uid, carried through so the view can name the block.
    block_uid: int
    #: Donor-acceptor separation, metres.
    separation_m: float
    #: Realised orientation factor for this pair.
    kappa_sq: float
    #: Förster radius for this pair, metres.
    forster_radius_m: float
    #: Transfer rate, s⁻¹.
    rate_hz: float
    #: Branching ratio: this acceptor's share of the donor's excitation.
    efficiency: float
    #: Which physics governs — see :class:`Regime`.
    regime: Regime


@dataclass(frozen=True)
class DonorBudget:
    """Every acceptor competing for one donor, solved together.

    The object the L4 view renders per donor. ``total_efficiency`` is the
    fraction of excitations that leave by *some* transfer;
    ``residual_efficiency`` is what the donor keeps (and emits or wastes).
    """

    #: Donor block uid.
    block_uid: int
    #: Channels, strongest first.
    channels: tuple[AcceptorChannel, ...]
    #: Σ branching ratios over all acceptors.
    total_efficiency: float
    #: 1 − ``total_efficiency``: the donor's own decay share.
    residual_efficiency: float

    def channel_for(self, block_uid: int) -> AcceptorChannel | None:
        """The channel to one acceptor, or ``None`` if it is not coupled."""
        for channel in self.channels:
            if channel.block_uid == block_uid:
                return channel
        return None

    def isolation_db(self, intended_uid: int) -> float:
        """Intended channel over the strongest unintended one, in dB.

        The comm-system figure of merit. ``inf`` when nothing else is in
        range (a clean point-to-point link), ``-inf`` when the intended
        channel is dead but others are live — which is the signature of a
        link that will work, just not to the block you meant.
        """
        intended = self.channel_for(intended_uid)
        if intended is None:
            return -math.inf
        crosstalk = [c.efficiency for c in self.channels if c.block_uid != intended_uid]
        worst = max(crosstalk, default=0.0)
        if worst <= 0.0:
            return math.inf
        if intended.efficiency <= 0.0:
            return -math.inf
        return 10.0 * math.log10(intended.efficiency / worst)


@dataclass(frozen=True, eq=False)
class PairGeometry:
    """A candidate acceptor as the tree layer sees it, before any physics.

    Separating this from :class:`AcceptorChannel` keeps :func:`solve_donor`
    free of tree types: the ops layer walks blocks and poses, fills these
    in, and gets physics back.

    ``eq=False`` for :class:`Chromophore`'s reason — two of these fields
    are numpy arrays, and a generated ``__eq__``/``__hash__`` over an
    array raises rather than answering.
    """

    #: Acceptor block uid.
    block_uid: int
    #: Acceptor's chromophore card.
    chromophore: Chromophore
    #: World-space donor → acceptor vector, metres.
    separation: VecLike
    #: World-space acceptor transition dipole (already rotated by pose).
    world_dipole: VecLike


def solve_donor(
    *,
    donor_uid: int,
    donor: Chromophore,
    donor_world_dipole: VecLike,
    acceptors: Sequence[PairGeometry],
    refractive_index: float,
) -> DonorBudget:
    """Solve one donor against every acceptor competing for it.

    The competition is the point. Each acceptor gets a rate
    :math:`k_i`; the donor's excitation splits as

    .. math::

        E_i = \\frac{k_i}{1/\\tau_D + \\sum_j k_j}

    so adding a second acceptor **reduces** the first one's efficiency even
    though neither moved. Quoting :func:`pair_efficiency` per link in a
    dense network therefore overstates every link, and the sum can exceed
    1 — a tell that the pair formula was used where this one belongs.

    Pairs outside the Förster regime contribute **no rate**: a Dexter-range
    pair because this model cannot quantify it (and silently adding a
    wrong, enormous rate would corrupt every other channel's share), a
    negligible or orientation-nulled pair because it genuinely transfers
    nothing. The regime survives on the channel so the view can distinguish
    "not modelled" from "modelled as zero" — they are not the same claim,
    and only the first needs a human.

    A **coincident** pair — two blocks sharing a pose, which is what an se
    design looks like before anyone has placed anything — is reported as
    :attr:`Regime.COINCIDENT` rather than raised. :func:`kappa_squared`
    still refuses that geometry, because as a pure function it has no
    defined answer; this one has a caller to protect, and a whole view that
    dies on one unplaced block would be useless exactly when a design is
    most in flux.
    """
    if not refractive_index > 0.0:
        raise FretError(f"refractive index must be positive, got {refractive_index}")

    donor_hat = as_vec3(donor_world_dipole)
    resolved: list[AcceptorChannel] = []
    for acceptor in acceptors:
        separation_m = float(np.linalg.norm(as_vec3(acceptor.separation)))
        if separation_m <= 0.0:
            resolved.append(
                AcceptorChannel(
                    block_uid=acceptor.block_uid,
                    separation_m=0.0,
                    kappa_sq=0.0,
                    forster_radius_m=0.0,
                    rate_hz=0.0,
                    efficiency=0.0,
                    regime=Regime.COINCIDENT,
                )
            )
            continue
        kappa_sq = kappa_squared(donor_hat, acceptor.world_dipole, acceptor.separation)
        overlap = overlap_integral(donor.emission, acceptor.chromophore.absorption)
        r0_m = forster_radius(
            overlap=overlap,
            quantum_yield=donor.quantum_yield,
            kappa_sq=kappa_sq,
            refractive_index=refractive_index,
        )
        pair_regime = regime(separation_m, r0_m, kappa_sq)
        rate = (
            transfer_rate(separation_m, r0_m, donor.lifetime_s)
            if pair_regime is Regime.FORSTER
            else 0.0
        )
        resolved.append(
            AcceptorChannel(
                block_uid=acceptor.block_uid,
                separation_m=separation_m,
                kappa_sq=kappa_sq,
                forster_radius_m=r0_m,
                rate_hz=rate,
                efficiency=0.0,  # filled below, once the denominator is known
                regime=pair_regime,
            )
        )

    denominator = 1.0 / donor.lifetime_s + sum(c.rate_hz for c in resolved)
    channels = tuple(
        sorted(
            (
                AcceptorChannel(
                    block_uid=c.block_uid,
                    separation_m=c.separation_m,
                    kappa_sq=c.kappa_sq,
                    forster_radius_m=c.forster_radius_m,
                    rate_hz=c.rate_hz,
                    efficiency=c.rate_hz / denominator,
                    regime=c.regime,
                )
                for c in resolved
            ),
            key=lambda c: (-c.efficiency, c.block_uid),
        )
    )
    total = sum(c.efficiency for c in channels)
    return DonorBudget(
        block_uid=donor_uid,
        channels=channels,
        total_efficiency=total,
        residual_efficiency=1.0 - total,
    )


def spectral_crosstalk(
    donor: Chromophore, acceptor: Chromophore, excitation_nm: float
) -> float:
    """Direct acceptor excitation as a fraction of donor excitation.

    A FRET readout cannot tell a transferred excitation from an acceptor
    that absorbed the pump light directly, so this ratio
    ``ε_A(λ_ex)/ε_D(λ_ex)`` is the noise floor under every efficiency this
    module computes — an independent, purely spectral channel impairment
    that no amount of good geometry fixes. Returns ``inf`` when the pump
    misses the donor entirely, which is a design error worth surfacing
    loudly rather than a large-but-finite number.
    """
    donor_eps = donor.absorption.at(excitation_nm)
    acceptor_eps = acceptor.absorption.at(excitation_nm)
    if donor_eps <= 0.0:
        return math.inf if acceptor_eps > 0.0 else 0.0
    return acceptor_eps / donor_eps


# ── the stored shapes ───────────────────────────────────────────────────
#
# Three payloads cross the DB boundary, and all three are validated HERE
# rather than in :mod:`precis_se.ops`, for the reason ``joints`` is: write
# time is where a malformed payload gets rejected, and DRC only *reports*
# what an older schema let through. The ops layer wraps the
# :class:`FretError` these raise into its own ``OpError`` so a bad
# ``set_chromophore`` reads like every other rejected op.

#: Refractive index assumed when a design has not declared its optical
#: context. 1.4 is the conventional value for a condensed organic or
#: protein interior (water is 1.33, a dense polymer ~1.5). Because
#: ``R0⁶ ∝ n⁻⁴``, the whole 1.33–1.6 span moves R0 by only about ±6% —
#: small enough that defaulting is honest, large enough that the view says
#: out loud when it has defaulted.
DEFAULT_MEDIUM_INDEX: float = 1.4

_CHROMOPHORE_KEYS = frozenset(
    {"label", "dipole", "quantum_yield", "lifetime_s", "emission", "absorption"}
)
_OPTICAL_LINK_KEYS = frozenset({"min_efficiency", "channel", "reason"})
_OPTICS_KEYS = frozenset({"medium_index", "excitation_nm"})


def _strays(raw: dict[str, Any], allowed: frozenset[str], what: str) -> None:
    """Reject unknown keys rather than dropping them.

    se's standing lesson (the swallowed-facet rule): a typo'd key beside a
    valid one must fail loudly, because a silently ignored
    ``quantum_yeild`` produces a plausible number from the wrong physics.
    """
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise FretError(
            f"{what}: unknown key(s) {', '.join(unknown)} — known keys: "
            f"{', '.join(sorted(allowed))}"
        )


def _positive(raw: dict[str, Any], key: str, what: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FretError(f"{what}: {key!r} must be a number, got {value!r}")
    number = float(value)
    if not number > 0.0:
        raise FretError(f"{what}: {key!r} must be positive, got {number}")
    return number


def _samples(raw: Any, key: str, what: str) -> list[list[float]]:
    """Vet a ``[[wavelength_nm, value], ...]`` payload into JSON-safe lists."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise FretError(
            f"{what}: {key!r} must be a non-empty list of [nm, value] pairs"
        )
    out: list[list[float]] = []
    for sample in raw:
        if not isinstance(sample, (list, tuple)) or len(sample) != 2:
            raise FretError(
                f"{what}: each {key!r} sample must be [wavelength_nm, value], got {sample!r}"
            )
        try:
            out.append([float(sample[0]), float(sample[1])])
        except (TypeError, ValueError) as exc:
            raise FretError(f"{what}: non-numeric {key!r} sample {sample!r}") from exc
    # Round-trips through Spectrum so the ordering/positivity/length rules
    # are enforced by the one validator rather than a second copy of them.
    Spectrum.of(out)
    return out


def validate_chromophore(raw: Any) -> dict[str, Any]:
    """Vet a stored chromophore card; returns the normalised payload.

    The card is **block-owned** — it lives on ``se_blocks.chromophore``
    beside ``dof`` and ``objectives``, not in a table of its own, because
    it is exactly that kind of fact: one per block, meaningless without
    the block, and gone when the block goes.
    """
    what = "chromophore"
    if not isinstance(raw, dict):
        raise FretError(f"{what} must be a JSON object, got {raw!r}")
    _strays(raw, _CHROMOPHORE_KEYS, what)
    missing = sorted(_CHROMOPHORE_KEYS - set(raw))
    if missing:
        raise FretError(
            f"{what}: missing {', '.join(missing)} — a card without its "
            "spectra, yield and lifetime cannot produce a rate, and a "
            "half-filled card would read as a modelled link"
        )
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise FretError(f"{what}: 'label' must be a non-empty string")
    dipole = raw.get("dipole")
    if not isinstance(dipole, (list, tuple)) or len(dipole) != 3:
        raise FretError(
            f"{what}: 'dipole' must be [x, y, z] in the BLOCK frame, got {dipole!r}"
        )
    try:
        direction = [float(v) for v in dipole]
    except (TypeError, ValueError) as exc:
        raise FretError(f"{what}: non-numeric 'dipole' {dipole!r}") from exc
    if sum(v * v for v in direction) <= 0.0:
        raise FretError(f"{what}: 'dipole' must not be the zero vector")
    quantum_yield = raw.get("quantum_yield")
    if isinstance(quantum_yield, bool) or not isinstance(quantum_yield, (int, float)):
        raise FretError(
            f"{what}: 'quantum_yield' must be a number, got {quantum_yield!r}"
        )
    if not 0.0 <= float(quantum_yield) <= 1.0:
        raise FretError(
            f"{what}: 'quantum_yield' must be in [0, 1], got {quantum_yield}"
        )
    return {
        "label": label.strip(),
        "dipole": direction,
        "quantum_yield": float(quantum_yield),
        "lifetime_s": _positive(raw, "lifetime_s", what),
        "emission": _samples(raw.get("emission"), "emission", what),
        "absorption": _samples(raw.get("absorption"), "absorption", what),
    }


def chromophore_from_spec(raw: Any) -> Chromophore:
    """Build the physics object from a stored card.

    Re-validates on the way in: a card written by an older schema, or
    hand-edited in the database, reaches this function too, and the physics
    layer should never see a shape the validator would have rejected.
    """
    spec = validate_chromophore(raw)
    return Chromophore(
        label=str(spec["label"]),
        dipole=as_vec3(spec["dipole"]),
        quantum_yield=float(spec["quantum_yield"]),
        lifetime_s=float(spec["lifetime_s"]),
        emission=Spectrum.of(spec["emission"]),
        absorption=Spectrum.of(spec["absorption"]),
    )


def validate_optical_link(raw: Any) -> dict[str, Any]:
    """Vet a connect's declared optical invariant (L2).

    ``min_efficiency`` is the whole statement: "this pair must transfer at
    least this fraction of the donor's excitation". It is a *declared
    invariant* in se's sense — stored explicitly, never derived from the
    realised geometry — and the L4 view is what checks the geometry
    against it.

    Deliberately **not** mutually exclusive with ``joint`` or ``kind`` on
    the same connect, unlike those two with each other. A kinematic joint
    and a covalent bond are competing claims about one physics; an optical
    link is a *different* physics on the same pair, and two blocks that are
    bonded and also exchange excitation are an ordinary molecule, not a
    contradiction.
    """
    what = "optical"
    if not isinstance(raw, dict):
        raise FretError(f"{what} must be a JSON object, got {raw!r}")
    _strays(raw, _OPTICAL_LINK_KEYS, what)
    efficiency = raw.get("min_efficiency")
    if isinstance(efficiency, bool) or not isinstance(efficiency, (int, float)):
        raise FretError(
            f"{what}: 'min_efficiency' must be a number, got {efficiency!r}"
        )
    if not 0.0 < float(efficiency) < 1.0:
        raise FretError(
            f"{what}: 'min_efficiency' must be strictly between 0 and 1, got "
            f"{efficiency} — E=1 would need r=0, and E=0 is not a link"
        )
    out: dict[str, Any] = {"min_efficiency": float(efficiency)}
    for key in ("channel", "reason"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise FretError(f"{what}: {key!r} must be a non-empty string")
        out[key] = value.strip()
    return out


def validate_optics(raw: Any) -> dict[str, Any]:
    """Vet a design's optical context — the medium and the pump.

    Design-level rather than per-link because both are facts about the
    *space* a design sits in, not about any one pair: every R0 in the
    design divides by the same ``n⁴`` under the sixth root, and every
    spectral-crosstalk figure is quoted at the same pump wavelength.
    Per-link overrides are a later slice; a design whose blocks genuinely
    sit in different media should say so explicitly then, rather than have
    the view guess now.
    """
    what = "optics"
    if not isinstance(raw, dict):
        raise FretError(f"{what} must be a JSON object, got {raw!r}")
    _strays(raw, _OPTICS_KEYS, what)
    out: dict[str, Any] = {"medium_index": _positive(raw, "medium_index", what)}
    if raw.get("excitation_nm") is not None:
        out["excitation_nm"] = _positive(raw, "excitation_nm", what)
    return out
