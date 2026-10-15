"""Sampling acceptance for an extraction batch — the Wilson rule from
docs/backlog/measures-substrate.md ("a frontier model reviews a
stratified sample per paper × measurand; the batch is accepted when the
sampled error rate is under the bar — start at 5 %, measured with a
Wilson interval", Reto 2026-10-14).

The math is pure and deterministic so the same batch, sample and
verdicts always land the same verdict:

* :func:`wilson_interval` — the Wilson score interval for a binomial
  proportion; :func:`acceptance` applies the bar to the interval's
  UPPER edge (the one-sided question is "is the error rate below the
  bar?", so the default z is the one-sided 95% quantile 1.645).
* :func:`stratified_sample` — at least one row per non-empty stratum,
  the rest proportional (largest-deficit first); the within-stratum
  draw is a seeded shuffle so re-running the acceptance replays the
  same sample.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

#: One-sided 95% normal quantile — the acceptance question is a
#: lower-than test, not a two-sided estimate.
DEFAULT_Z = 1.645
#: The starting error bar: accept when the Wilson upper edge is under it.
DEFAULT_BAR = 0.05
#: Sample size that makes a zero-error batch pass the bar at DEFAULT_Z
#: (z²/(n+z²) ≤ 0.05 first holds at n=52).
DEFAULT_SAMPLE = 60


def wilson_interval(errors: int, n: int, z: float = DEFAULT_Z) -> tuple[float, float]:
    """The Wilson score interval for a proportion of ``errors`` in ``n``
    trials at confidence ``z`` — ``(lo, hi)``. ``n <= 0`` → ``(0, 1)``:
    no sample says nothing."""
    if n <= 0:
        return (0.0, 1.0)
    p = errors / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True, slots=True)
class Acceptance:
    """The batch verdict and the numbers behind it."""

    accepted: bool
    errors: int
    sampled: int
    error_rate: float
    wilson_lo: float
    wilson_hi: float
    z: float
    bar: float


def acceptance(
    errors: int,
    sampled: int,
    *,
    z: float = DEFAULT_Z,
    bar: float = DEFAULT_BAR,
) -> Acceptance:
    """The batch verdict: accepted iff the Wilson upper edge on the error
    rate is at or under ``bar``. ``errors`` counts rejected + undecidable
    rows (an unverified row is not a verified one)."""
    lo, hi = wilson_interval(errors, sampled, z)
    return Acceptance(
        accepted=hi <= bar,
        errors=errors,
        sampled=sampled,
        error_rate=(errors / sampled) if sampled else 0.0,
        wilson_lo=lo,
        wilson_hi=hi,
        z=z,
        bar=bar,
    )


def stratified_sample(
    strata: dict[str, list[int]], total: int, *, seed: str
) -> dict[str, list[int]]:
    """A deterministic sample across strata: ``{stratum: [ids]}``.

    Each non-empty stratum keeps one draw first (largest first, so a
    sample smaller than the stratum count covers the biggest strata);
    the remainder goes where each stratum's quota most under-fills its
    proportional share.
    """
    sizes = {k: len(v) for k, v in strata.items() if v}
    avail = sum(sizes.values())
    total = max(0, min(total, avail))
    if not sizes or total == 0:
        return {k: [] for k in strata}
    # largest-first so a tiny sample covers the biggest strata
    order = sorted(sizes, key=lambda k: (-sizes[k], k))
    quota = {k: 0 for k in sizes}
    for k in order[:total]:
        quota[k] = 1
    left = total - len(order[:total])
    while left > 0:
        best = max(
            (k for k in order if quota[k] < sizes[k]),
            key=lambda k: (total * sizes[k] / avail) - quota[k],
            default=None,
        )
        if best is None:
            break
        quota[best] += 1
        left -= 1
    rng = random.Random(seed)
    out: dict[str, list[int]] = {}
    for key, ids in strata.items():
        pool = sorted(ids)
        rng.shuffle(pool)
        out[key] = pool[: quota.get(key, 0)]
    return out
