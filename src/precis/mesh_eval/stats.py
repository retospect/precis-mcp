"""Small binomial statistics for the mesh evals — Wilson score intervals.

Why Wilson and not a normal approximation: eval samples are small (tens to
a few hundred) and the rates of interest sit near 0 or 1, where the normal
interval is badly anti-conservative. Wilson stays bounded in ``[0, 1]``
and is what ``local-mesh-upkeep.md``'s batch acceptance reads ("sampled
error rate under the bar" is a *one-sided* statement — the decision uses
the interval's upper edge at the batch's confidence level).
"""

from __future__ import annotations

import math


def wilson_interval(k: int, n: int, *, z: float = 1.96) -> tuple[float, float]:
    """Two-sided Wilson score interval for ``k`` successes in ``n`` trials.

    ``z=1.96`` gives the 95% interval. ``(0.0, 0.0)``-safe: empty ``n``
    returns ``(0.0, 1.0)`` — no information, widest possible bound.
    """
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, centre - half), min(1.0, centre + half)


def wilson_upper(k: int, n: int, *, z: float = 1.645) -> float:
    """One-sided upper confidence bound (``z=1.645`` ≈ 95% one-sided).

    Batch acceptance asks only "is the error rate *under* the bar", so the
    one-sided bound is the right statistic for it — cheaper evidence than a
    two-sided interval for the same confidence.
    """
    return wilson_interval(k, n, z=z)[1]


__all__ = ["wilson_interval", "wilson_upper"]
