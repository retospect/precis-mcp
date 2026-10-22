"""The regime card — where dimensions re-enter.

The kernel solves in reduced units and its results hold for *any* size with
the same shape; the card is the check on whether a given physical size is
inside the model. From the real ``(L, h0, mu, rho, dp or u0, particle d and
rho_p, molecular D, mean free path)`` — SI units throughout (m, Pa.s, kg/m3,
m/s, Pa) — it computes the six dimensionless numbers of the se-flow-kernel
spec, each with a verdict:

============  ==================================  =====================  ============================
line          number                              model holds when       else
============  ==================================  =====================  ============================
thinness      ``h0/L``                            <= 0.05                3-D Stokes needed (not here)
reynolds      ``Re_h * h0/L``, ``Re_h=rho u0 h0/mu`` <= 0.1             inertia: 3-D Navier-Stokes
stokes        ``St = tau_p u0/h0``, ``tau_p=rho_p d^2/(18 mu)`` <= 0.1  particles are not tracers
gravity       ``G = |v_s| L/(u0 h0)``, ``v_s=(rho_p-rho) g d^2/(18 mu)`` <= 0.1  settling dominates
peclet        ``Pe = u0 h0 / D``                  >= 10                  diffusion dominates the tracer
knudsen       ``Kn = lambda/h0``                  <= 0.01                free-molecular: nano propagator
============  ==================================  =====================  ============================

A line whose inputs were not supplied is marked not-applicable and does not
block ``card.ok`` — but ``line.applicable`` is False, so a stored card never
silently claims a check it did not run.
"""

from __future__ import annotations

from dataclasses import dataclass

G0 = 9.80665  # standard gravity, m/s^2

#: Knudsen-number bounds (the spec's card): the continuum kernel holds at
#: ``Kn <= KN_CONTINUUM_MAX``, the free-molecular propagator takes over at
#: ``Kn >= KN_FREE_MIN``; between them is the transition regime — DSMC,
#: out of scope here (se-multiphysics.md slice 5 later moves the lower
#: bound to 0.1).
KN_CONTINUUM_MAX = 0.01
KN_FREE_MIN = 10.0


@dataclass(frozen=True)
class RegimeLine:
    """One dimensionless number with its verdict."""

    name: str
    value: float  # the number; NaN when its inputs were not supplied
    threshold: float
    ok: bool
    holds_when: str  # e.g. "<= 0.1"
    otherwise: str  # what a failure means
    applicable: bool = True


def _line(
    name: str,
    value: float,
    threshold: float,
    ok: bool,
    otherwise: str,
    cmp: str = "<=",
) -> RegimeLine:
    return RegimeLine(
        name=name,
        value=value,
        threshold=threshold,
        ok=ok,
        holds_when=f"{cmp} {threshold:g}",
        otherwise=otherwise,
    )


def _skipped(name: str, threshold: float, otherwise: str) -> RegimeLine:
    return RegimeLine(
        name=name,
        value=float("nan"),
        threshold=threshold,
        ok=True,
        holds_when="n/a",
        otherwise=otherwise,
        applicable=False,
    )


@dataclass(frozen=True)
class RegimeCard:
    """The six regime numbers plus the scale triple they were computed from.

    ``ok`` is True iff every applicable line holds; ``failures`` names the
    failing lines (``regime.thinness``, ``regime.reynolds``, …) for a
    refusal-style report.
    """

    L: float  # m — lateral extent; x* = x/L
    h0: float  # m — reference gap; h* = h/h0
    u0: float  # m/s — velocity scale; U* = U/u0, t0 = L/u0
    thinness: RegimeLine
    reynolds: RegimeLine
    stokes: RegimeLine
    gravity: RegimeLine
    peclet: RegimeLine
    knudsen: RegimeLine

    @property
    def lines(self) -> tuple[RegimeLine, ...]:
        return (
            self.thinness,
            self.reynolds,
            self.stokes,
            self.gravity,
            self.peclet,
            self.knudsen,
        )

    @property
    def ok(self) -> bool:
        return all(line.ok for line in self.lines)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            line.name for line in self.lines if line.applicable and not line.ok
        )

    @property
    def route(self) -> str:
        """Which solver this card sends the case to: ``'continuum'`` or
        ``'knudsen'`` (the slice-3 nano propagator).

        ``'knudsen'`` when the knudsen line was computed and reads
        ``Kn >= KN_FREE_MIN`` — the five other lines are continuum-model
        checks and do not gate a free-molecular case (a lumen is never
        "thin"). ``'continuum'`` only when every applicable line holds.
        Otherwise raises :class:`ValueError` naming the out-of-scope
        regime: transition (DSMC) for ``0.01 < Kn < 10``, else the
        card's failing lines."""
        kn = self.knudsen
        if kn.applicable and kn.value >= KN_FREE_MIN:
            return "knudsen"
        if kn.applicable and kn.value > KN_CONTINUUM_MAX:
            raise ValueError(
                f"Kn = {kn.value:.3g} is the transition regime "
                f"({KN_CONTINUUM_MAX:g} < Kn < {KN_FREE_MIN:g}) — neither "
                "the continuum kernel nor the nano propagator holds; "
                "DSMC is out of scope"
            )
        if self.ok:
            return "continuum"
        raise ValueError(
            "regime card fails: "
            + ", ".join(self.failures)
            + " — the continuum kernel does not hold"
        )

    @classmethod
    def from_physical(
        cls,
        L: float,
        h0: float,
        mu: float,
        rho: float,
        dp: float | None = None,
        u0: float | None = None,
        particle_d: float | None = None,
        rho_p: float | None = None,
        molecular_d: float | None = None,
        mean_free_path: float | None = None,
    ) -> RegimeCard:
        """Compute the card for a physical case (SI units throughout).

        Exactly one of ``dp`` (Pa) or ``u0`` (m/s) is required; given ``dp``,
        the scale velocity is ``u0 = h0^2 dp / (12 mu L)``. The particle lines
        (``stokes``, ``gravity``) need ``particle_d`` and ``rho_p``; ``peclet``
        needs ``molecular_d``; ``knudsen`` needs ``mean_free_path``.
        """
        if dp is not None and u0 is not None:
            raise ValueError("state exactly one of dp or u0")
        if L <= 0 or h0 <= 0 or mu <= 0 or rho <= 0:
            raise ValueError("L, h0, mu, rho must be positive")
        if dp is not None:
            u0 = h0 * h0 * dp / (12.0 * mu * L)
        if u0 is None:
            raise ValueError("state exactly one of dp or u0")
        if u0 <= 0:
            raise ValueError("u0 must be positive")

        thinness = _line(
            "thinness",
            h0 / L,
            0.05,
            h0 / L <= 0.05,
            "3-D Stokes needed (not this kernel)",
        )

        re_h = rho * u0 * h0 / mu
        red_re = re_h * h0 / L
        reynolds = _line(
            "reynolds",
            red_re,
            0.1,
            red_re <= 0.1,
            "inertia: 3-D Navier-Stokes (not this kernel)",
        )

        if particle_d is not None and rho_p is not None and particle_d > 0:
            tau_p = rho_p * particle_d**2 / (18.0 * mu)
            st = tau_p * u0 / h0
            stokes = _line("stokes", st, 0.1, st <= 0.1, "particles are not tracers")
            v_s = (rho_p - rho) * G0 * particle_d**2 / (18.0 * mu)
            g = abs(v_s) * L / (u0 * h0)
            gravity = _line(
                "gravity",
                g,
                0.1,
                g <= 0.1,
                "settling dominates; add -v_s* to the vertical model",
            )
        else:
            stokes = _skipped("stokes", 0.1, "particles are not tracers")
            gravity = _skipped(
                "gravity", 0.1, "settling dominates; add -v_s* to the vertical model"
            )

        if molecular_d is not None and molecular_d > 0:
            pe = u0 * h0 / molecular_d
            peclet = _line(
                "peclet",
                pe,
                10.0,
                pe >= 10.0,
                "diffusion dominates the tracer",
                cmp=">=",
            )
        else:
            peclet = _skipped("peclet", 10.0, "diffusion dominates the tracer")

        if mean_free_path is not None:
            kn = mean_free_path / h0
            knudsen = _line(
                "knudsen",
                kn,
                0.01,
                kn <= 0.01,
                "free-molecular: the nano propagator, not this kernel",
            )
        else:
            knudsen = _skipped(
                "knudsen", 0.01, "free-molecular: the nano propagator, not this kernel"
            )

        return cls(
            L=L,
            h0=h0,
            u0=u0,
            thinness=thinness,
            reynolds=reynolds,
            stokes=stokes,
            gravity=gravity,
            peclet=peclet,
            knudsen=knudsen,
        )
