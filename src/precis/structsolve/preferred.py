"""Preferred-number term — differentiable wells pulling declared
measurements toward round values (docs/backlog/
multiscale-optimisation-method.md §4; slice 1 spec
preferred-number-term.md).

The method: for a measurement ``m`` and a pitch ``p``, the distance to
the nearest multiple ``d_p(m) = |m − p·round(m/p)|`` feeds a
bilateral-Huber well ``W_p = A·h(d_p) − B_p`` — quadratic within
``eps`` of the well bottom AND within ``eps`` of the sawtooth peak at
``d = p/2``, linear between, so the gradient is continuous everywhere
and the peak is a zero-gradient ridge (an unstable equilibrium any
physics gradient tips), not a cusp that chatters a descent. Tiers at
several pitches combine by **softmin** (log-sum-exp, sharpness
``beta``): a dimension settles on the coarsest round value that is
good enough, one active well per basin.

Why this shape — the decisions are settled in the method doc
(2026-09-15); this module implements, it does not re-derive:

- **min, not max.** Combining tiers by ``max`` inverts the intent: at
  ``m = 10`` mm on tiers {2.5, 5, 10, 100} the 100-tier's sawtooth is
  at ``d = 10`` with amplitude growing ~linearly in ``p`` while
  ``A_p`` grows only logarithmically, so max drags a perfectly round
  10 mm toward 0 or 100. Under min every tier's grid point is a rest
  point.
- **Bilateral Huber.** Tip-only smoothing leaves the cusp at
  ``d = p/2`` — the chatter mechanism. The inverted-quadratic cap
  removes it; descent alone is well-defined at the midpoint.
- **Softmin across tiers** smooths the tie surfaces between wells the
  same way the cap smooths the peak within a well.
- **Tiers from part size.** Pitches are ``L/10, L/50, L/100`` rounded
  to 1-2-5 values — ``p_min`` and ``lambda`` stop being free
  constants; the remaining coefficients are fixed dimensionless
  numbers or rules evaluated once on the starting design
  (:func:`default_coefficients`, :func:`scale_A`).

Pure numpy, store-free, unit-agnostic (the package docstring's house
rules). No field solve, no torch — that port is a later slice.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

#: Mantissae of the 1-2-5 preferred series.
_MANTISSAE = (1, 2, 5)


@dataclass(frozen=True)
class Coefficients:
    """The coefficient set for a tier list, produced by
    :func:`default_coefficients`. ``B[i]`` aligns with ``tiers[i]``;
    ``tiers`` is carried so the alignment is explicit."""

    tiers: tuple[float, ...]
    #: Well depth — scalar; per-tier ``A_p`` is a later refinement.
    A: float
    #: Per-tier depth offsets, aligned with ``tiers``.
    B: np.ndarray = field(compare=False)
    #: Huber smoothing radius (both ends of the sawtooth).
    eps: float
    #: Softmin sharpness.
    beta: float


@dataclass(frozen=True)
class PreferredResult:
    """A penalty evaluation plus its provenance, in the SimpResult
    posture: callers propagate ``notes`` rather than quoting the number
    alone."""

    value: float | np.ndarray
    grad: float | np.ndarray
    notes: tuple[str, ...]


def _round_125(x: float) -> float:
    """Nearest 1-2-5 value to ``x`` in log10 space."""
    k = math.floor(math.log10(x))
    best, best_d = x, math.inf
    for j in (k - 1, k, k + 1):
        for mantissa in _MANTISSAE:
            v = float(f"{mantissa}e{j}")
            d = abs(math.log10(x / v))
            if d < best_d:
                best, best_d = v, d
    return best


def tiers_for(L: float, *, n: int = 3) -> tuple[float, ...]:
    """Pitches ``L/10, L/50, L/100`` (first ``n``), each rounded to the
    nearest 1-2-5 value in log space, duplicates removed (a small part
    collapses to fewer tiers)."""
    out: list[float] = []
    for raw in (L / 10, L / 50, L / 100)[:n]:
        v = _round_125(raw)
        if not any(math.isclose(v, u, rel_tol=1e-12) for u in out):
            out.append(v)
    return tuple(out)


def _huber_bilateral(
    d: np.ndarray, p: float, eps: float
) -> tuple[np.ndarray, np.ndarray]:
    """``h(d)`` and ``dh/dd`` on ``d in [0, p/2]``: quadratic below
    ``eps``, linear to ``p/2 − eps``, then an inverted-quadratic cap
    with slope 1 at the join and 0 at ``d = p/2``."""
    d0 = p / 2 - eps
    u = d - d0  # >0 in the cap region
    h = np.where(
        d < eps,
        d * d / (2 * eps),
        np.where(
            d <= d0,
            d - eps / 2,
            (d0 - eps / 2) + u - u * u / (2 * eps),
        ),
    )
    dh = np.where(d < eps, d / eps, np.where(d <= d0, 1.0, 1.0 - u / eps))
    return h, dh


def well(
    m: npt.ArrayLike, p: float, *, A: float, B: float, eps: float
) -> tuple[float, float] | tuple[np.ndarray, np.ndarray]:
    """One tier: ``W_p(m) = A·h(d_p(m)) − B`` and ``dW/dm``. Scalar in
    → floats out; array in → arrays out."""
    arr = np.asarray(m, dtype=float)
    off = arr - p * np.rint(arr / p)
    h, dh = _huber_bilateral(np.abs(off), p, eps)
    value = A * h - B
    dvalue = A * dh * np.sign(off)  # sign(0)=0 matches dh(0)=0
    if arr.ndim == 0:
        return float(value), float(dvalue)
    return value, dvalue


def penalty(
    m: npt.ArrayLike,
    tiers: Sequence[float],
    *,
    A: float,
    B: npt.ArrayLike,
    eps: float,
    beta: float,
) -> tuple[float, float] | tuple[np.ndarray, np.ndarray]:
    """Softmin over tiers: ``P = −(1/β)·log Σ_p exp(−β W_p)`` with a
    numerically stable log-sum-exp; gradient ``Σ w_p dW_p/dm`` with
    ``w = softmax(−βW)``. Vectorised over ``m``."""
    arr = np.asarray(m, dtype=float)
    B = np.asarray(B, dtype=float)
    pairs = [
        well(arr, float(p), A=A, B=float(B[i]), eps=eps) for i, p in enumerate(tiers)
    ]
    W = np.stack([v for v, _ in pairs])
    G = np.stack([g for _, g in pairs])
    w_min = np.min(W, axis=0)
    e = np.exp(-beta * (W - w_min))
    z = np.sum(e, axis=0)
    value = w_min - np.log(z) / beta
    grad = np.sum((e / z) * G, axis=0)
    if arr.ndim == 0:
        return float(value), float(grad)
    return value, grad


def evaluate(m: npt.ArrayLike, coeffs: Coefficients) -> PreferredResult:
    """Penalty + provenance under a :class:`Coefficients` set."""
    value, grad = penalty(
        m, coeffs.tiers, A=coeffs.A, B=coeffs.B, eps=coeffs.eps, beta=coeffs.beta
    )
    return PreferredResult(
        value,
        grad,
        (
            f"tiers={coeffs.tiers} A={coeffs.A} B={coeffs.B.tolist()} "
            f"eps={coeffs.eps} beta={coeffs.beta}",
        ),
    )


def default_coefficients(tiers: Sequence[float], *, kappa: float = 0.5) -> Coefficients:
    """Coefficients-as-rules (method doc §4 table): ``eps =
    1e-4·min(tiers)``; ``beta = 1/(A·eps)`` so the softmin transition
    over an energy difference of ``A·eps`` is ~1 unit of exponent
    (blend width = eps, matching the well smoothing); finest tier
    ``B = 0``, each coarser tier deeper by ``kappa·A·p_fine/2`` —
    the survival criterion applied as the definition. ``A = 1``
    unscaled; :func:`scale_A` applies the ρ* rule at use time."""
    ts = tuple(float(p) for p in tiers)
    order = np.argsort(ts)[::-1]  # coarse first
    A = 1.0
    eps = 1e-4 * min(ts)
    beta = 1.0 / (A * eps)
    b_sorted = np.zeros(len(ts))
    for i in range(len(ts) - 2, -1, -1):  # finest B=0, walk coarser
        b_sorted[i] = b_sorted[i + 1] + kappa * A * ts[order[i + 1]] / 2
    B = np.empty(len(ts))
    for rank, idx in enumerate(order):
        B[idx] = b_sorted[rank]
    return Coefficients(ts, A, B, eps, beta)


def pull_ratio(grad_penalty: npt.ArrayLike, grad_physics: npt.ArrayLike) -> float:
    """``‖∇P‖ / ‖∇J_physics‖`` (L2 norms) — the magnitude-discipline
    diagnostic."""
    gp = np.linalg.norm(np.asarray(grad_penalty, dtype=float))
    gj = np.linalg.norm(np.asarray(grad_physics, dtype=float))
    if gj == 0.0:
        raise ValueError("physics gradient is zero — pull ratio undefined")
    return float(gp / gj)


def scale_A(
    grad_penalty: npt.ArrayLike, grad_physics: npt.ArrayLike, *, rho_star: float = 0.1
) -> float:
    """The ``A0`` rule: factor ``s`` such that
    ``‖s·∇P‖ / ‖∇J_physics‖ = ρ*`` on the starting design."""
    gj = np.linalg.norm(np.asarray(grad_physics, dtype=float))
    if gj == 0.0:
        raise ValueError("physics gradient is zero — no scale to match")
    gp = np.linalg.norm(np.asarray(grad_penalty, dtype=float))
    if gp == 0.0:
        raise ValueError("penalty gradient is zero — nothing to scale")
    return float(rho_star * gj / gp)
