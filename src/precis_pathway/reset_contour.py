"""Surface-Pourbaix slice 7 — the guaranteed-reset hysteresis contour.

Design of record: ``docs/backlog/surface-pourbaix-staircase-optimizer.md``
(prompt §6.2 estimation order, §6.3 series composition, and the slice-7
contract). Pure numpy + dataclasses — the same layer as
:mod:`precis_pathway.staircase_masks`, whose ``Route``/``Rung``/
``RestingMap`` it consumes.

**The object.** The operating window answers the *equilibrium* question —
``ΔG(U) <= eps``, may this step run. A step that is only just downhill
can still sit behind a barrier, so hysteresis can preserve the unwanted
state whatever the thermodynamics say. The **reset contour** answers the
stronger question: where is every step of the cycle downhill by enough
that no barrier is left to trap it. Every rung of a route is a reset
edge — a cycle must close for the surface to reset. Between the
equilibrium line and the reset line lies the **hysteretic band**:
thermodynamically allowed, not guaranteed. The map marks it "don't rely
on it" (§6.2).

**The estimator ladder** (§6.2's order) prices each edge's *required
overdrive margin* ``m_e``, labelled per edge:

* ``overdrive`` — ``m_e = eta`` (:data:`DEFAULT_ETA_EV`, 0.2 eV;
  "several hundred meV" — a parameter, not a measurement). Free and
  safe-sided.
* ``bep`` — the edge carries a Brønsted–Evans–Polanyi fit
  ``E_a = alpha·ΔG + beta`` (``rung.bep``, fitted on the run's sparse
  NEBs); guaranteed where the predicted barrier hits ``floor``:
  ``m_e = (beta − floor)/alpha``, clipped ≥ 0 (a negative intercept is a
  barrierless step — guaranteed wherever it is viable). ``alpha <= 0``
  can't extrapolate → falls back to ``overdrive`` with a note.
* ``neb`` — the edge carries a measured forward barrier
  (``rung.barrier_ea``); ``m_e`` is that barrier: guaranteed where the
  driving force exceeds what was measured. A lone barrier can't be
  re-tilted in U without α, so this is the honest read — real data
  tightening or loosening flat η.

A ``bep`` block wins over ``barrier``, which wins over the flat η.
Nothing is invented: an edge with no kinetics record stays ``overdrive``.

**Pessimistic series composition** (§6.3). The DFT band and the reset
band answer different questions and compose in series, never averaged:
the reset margin launches from the *far edge* of the propagated band.
Here that is one line of arithmetic — edge slack
``slack_e(U) = −ΔG_e(U) − m_e − σ_e`` with σ_e the rung's propagated bar
(``rung.sigma``, hypot of the endpoint stds; 0 when unrecorded — never an
invented sigma). ``reset_ok`` where every edge's slack ≥ 0; ``margin``
is the min slack — the composite contour — and ``limiting`` names the
edge setting it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .staircase_masks import RestingMap, Route, Rung

#: Default overdrive margin η (eV): the "several hundred meV" safety
#: margin past the equilibrium line (prompt §6.2, proxy 1). A parameter,
#: not a measurement.
DEFAULT_ETA_EV = 0.2

#: Default forward-barrier floor (eV) for the BEP/NEB rungs: the contour
#: is where no barrier is left — ``E_a <= floor``. A positive floor makes
#: the requirement stricter than barrier-free.
DEFAULT_EA_FLOOR_EV = 0.0

#: The estimator labels (§6.2's ladder). Each edge is priced by exactly
#: one — the best evidence it carries.
ESTIMATORS = ("overdrive", "bep", "neb")


@dataclass(frozen=True)
class ResetEdge:
    """One rung's reset requirement: which estimator priced it and the
    required overdrive ``margin_eV``. ``required_eV`` adds the rung's
    propagated σ — the pessimistic launch point (§6.3)."""

    rung: Rung
    estimator: str
    margin_eV: float
    note: str | None = None

    def __post_init__(self) -> None:
        if self.estimator not in ESTIMATORS:
            raise ValueError(
                f"rung {self.rung.name!r}: estimator must be one of "
                f"{ESTIMATORS}, got {self.estimator!r}"
            )

    @property
    def sigma_eV(self) -> float:
        """The rung's propagated ΔG bar (0 when unrecorded — never invented)."""
        return 0.0 if self.rung.sigma is None else float(self.rung.sigma)

    @property
    def required_eV(self) -> float:
        """``m_e + σ_e`` — the margin launched from the far edge of the
        DFT band (§6.3's series composition)."""
        return self.margin_eV + self.sigma_eV

    def slack(self, u_rhe: Any) -> np.ndarray:
        """``−ΔG(U) − required_eV``: ≥ 0 where the edge is guaranteed."""
        return -self.rung.delta_g(u_rhe) - self.required_eV


def _edge_margin(
    rung: Rung, *, eta_eV: float, ea_floor_eV: float
) -> tuple[str, float, str | None]:
    """The estimator ladder for one edge → ``(label, m_e, note)``.

    ``bep`` wins over ``barrier_ea``, which wins over the flat η; a
    degenerate BEP (``alpha <= 0``, barrier can't be extrapolated) falls
    back to ``overdrive`` and says so."""
    if rung.bep is not None:
        alpha, beta = float(rung.bep[0]), float(rung.bep[1])
        if math.isfinite(alpha) and math.isfinite(beta) and alpha > 0:
            return "bep", max((beta - ea_floor_eV) / alpha, 0.0), None
        return (
            "overdrive",
            float(eta_eV),
            f"bep params degenerate (alpha={alpha:g}, beta={beta:g}) — "
            "fell back to overdrive",
        )
    if rung.barrier_ea is not None:
        return "neb", max(float(rung.barrier_ea), 0.0), None
    return "overdrive", float(eta_eV), None


def reset_edges(
    route: Route,
    *,
    eta_eV: float = DEFAULT_ETA_EV,
    ea_floor_eV: float = DEFAULT_EA_FLOOR_EV,
) -> tuple[ResetEdge, ...]:
    """The route's rungs annotated with their reset estimator and margin —
    every rung is a reset edge (the cycle must close)."""
    out: list[ResetEdge] = []
    for r in route.rungs:
        est, m, note = _edge_margin(r, eta_eV=eta_eV, ea_floor_eV=ea_floor_eV)
        out.append(ResetEdge(rung=r, estimator=est, margin_eV=m, note=note))
    return tuple(out)


@dataclass(frozen=True, eq=False)
class ResetContour:
    """The guaranteed-reset mask over one (U_RHE, pH) grid, with
    provenance.

    ``reset_ok`` — every edge guaranteed (the cycle closes under its own
    driving force, launched past the far edge of the DFT band).
    ``margin`` — the min edge slack per point; its sign is ``reset_ok``
    and its magnitude says how far inside the guarantee the point sits.
    ``limiting`` — the name of the edge setting the slack. ``viable`` —
    every rung's ``ΔG <= eps`` (the window's rung part, before the
    resting and HER masks). ``hysteretic`` — ``viable & ¬reset_ok``:
    the band §6.2 says not to rely on. Areas are the grid's cell
    measure in V·pH; an empty route is vacuously guaranteed
    (``margin`` = +inf, ``limiting`` = '')."""

    route: str
    u_rhe: np.ndarray
    ph: np.ndarray
    edges: tuple[ResetEdge, ...]
    reset_ok: np.ndarray
    margin: np.ndarray
    limiting: np.ndarray
    viable: np.ndarray
    hysteretic: np.ndarray
    eta_eV: float
    ea_floor_eV: float
    estimators: tuple[str, ...]
    notes: tuple[str, ...]
    reset_area: float
    reset_fraction: float
    hysteretic_area: float

    def at(self, u_rhe: float, ph: float) -> dict[str, Any]:
        """The verdict at one condition — the nearest grid cell's
        ``{reset_ok, margin, limiting, viable, hysteretic}``."""
        j = int(np.argmin(np.abs(self.u_rhe - float(u_rhe))))
        i = int(np.argmin(np.abs(self.ph - float(ph))))
        return {
            "reset_ok": bool(self.reset_ok[i, j]),
            "margin": float(self.margin[i, j]),
            "limiting": str(self.limiting[i, j]),
            "viable": bool(self.viable[i, j]),
            "hysteretic": bool(self.hysteretic[i, j]),
        }


def reset_contour(
    resting: RestingMap,
    route: Route,
    *,
    eta_eV: float = DEFAULT_ETA_EV,
    ea_floor_eV: float = DEFAULT_EA_FLOOR_EV,
) -> ResetContour:
    """The guaranteed-reset contour of one route on ``resting``'s grid.

    Every rung is a reset edge; each is priced by its own estimator
    (:func:`reset_edges`). ``resting.wanted`` is NOT consulted — the
    contour is a property of the staircase alone; the caller intersects
    ``reset_ok`` into the window (``window ∩ reset_ok``, §6.3)."""
    edges = reset_edges(route, eta_eV=eta_eV, ea_floor_eV=ea_floor_eV)
    u = resting.u_grid()
    if edges:
        slacks = np.stack([e.slack(u) for e in edges])  # (N, n_ph, n_u)
        margin = slacks.min(axis=0)
        names = np.array([e.rung.name for e in edges], dtype=object)
        limiting = names[slacks.argmin(axis=0)]
        viable = np.stack([e.rung.viable(u) for e in edges]).all(axis=0)
        reset_ok = margin >= 0.0
    else:
        margin = np.full(resting.shape, math.inf)
        limiting = np.full(resting.shape, "", dtype=object)
        viable = np.ones(resting.shape, dtype=bool)
        reset_ok = np.ones(resting.shape, dtype=bool)
    hysteretic = viable & ~reset_ok
    return ResetContour(
        route=route.name,
        u_rhe=resting.u_rhe,
        ph=resting.ph,
        edges=edges,
        reset_ok=reset_ok,
        margin=margin,
        limiting=limiting,
        viable=viable,
        hysteretic=hysteretic,
        eta_eV=float(eta_eV),
        ea_floor_eV=float(ea_floor_eV),
        estimators=tuple(e.estimator for e in edges),
        notes=tuple(n for e in edges if (n := e.note) is not None),
        reset_area=resting.area(reset_ok),
        reset_fraction=resting.fraction(reset_ok),
        hysteretic_area=resting.area(hysteretic),
    )


__all__ = [
    "DEFAULT_EA_FLOOR_EV",
    "DEFAULT_ETA_EV",
    "ESTIMATORS",
    "ResetContour",
    "ResetEdge",
    "reset_contour",
    "reset_edges",
]
