"""Sampled batch acceptance — the sign-off shape Reto ruled 2026-10-14.

A local upkeep lane produces a *batch* of proposals (categorise verdicts,
link proposals, merges, summaries). A frontier model judges a
**stratified sample** of the batch — plus, per the design, every
merge/split regardless (the caller forces those into the sample by
giving them their own stratum and a big enough ``sample_size``, or by
judging them unconditionally alongside). The batch is accepted when the
sampled error rate's **one-sided Wilson upper bound** sits under the
bar: "confident the true error rate is under the bar", not "the point
estimate happened to".

Verdicts:

- ``accept`` — Wilson upper bound on the error rate ≤ ``error_bar``.
- ``reject`` — the lower bound is *above* the bar: the batch is bad with
  confidence; sending it to more judging is spend without purpose.
- ``escalate`` — the interval straddles the bar (evidence is ambiguous),
  the sample was empty, or any judge call failed. Escalate means a human
  or a bigger sample decides; it is the honest answer when the sample
  cannot certify either direction.

The judge is injected: ``judge(proposal) -> bool`` (True = the proposal
is correct). A raising judge counts the item as a judge failure and
escalates the batch — a batch whose review itself broke is never
accepted.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Any

from precis.mesh_eval.stats import wilson_interval

VERDICTS: tuple[str, ...] = ("accept", "reject", "escalate")


@dataclass(frozen=True, slots=True)
class BatchDecision:
    """The acceptance decision for one batch of local proposals."""

    verdict: str  # VERDICTS
    batch_size: int
    n_sampled: int
    n_errors: int
    #: Judge calls that failed — any failure forces ``escalate``.
    n_judge_failures: int
    error_rate: float
    ci_lo: float
    ci_hi: float
    error_bar: float
    #: Stratum -> (batch count, sampled count, sampled errors).
    strata: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    #: Indices of sampled proposals, for audit.
    sampled_indices: tuple[int, ...] = ()


def stratified_sample(
    proposals: list[Any],
    n: int,
    key_fn: Callable[[Any], Hashable] | None = None,
    *,
    seed: int = 0,
) -> list[int]:
    """Pick ``n`` proposal indices, proportional across strata.

    ``key_fn`` maps a proposal to its stratum (e.g. lane or proposal
    kind); the default is one stratum. Allocation is largest-remainder so
    every non-empty stratum gets ≥1 draw when ``n`` allows, and the
    within-stratum choice is seeded-random — reproducible for a given
    ``seed`` over the same batch.
    """
    strata: dict[Hashable, list[int]] = {}
    for i, p in enumerate(proposals):
        strata.setdefault(key_fn(p) if key_fn else 0, []).append(i)
    if n >= len(proposals):
        return list(range(len(proposals)))
    if n <= 0:
        return []

    total = len(proposals)
    # Largest-remainder proportional allocation.
    exact = {s: n * len(idx) / total for s, idx in strata.items()}
    alloc = {s: min(len(idx), int(exact[s])) for s, idx in strata.items()}
    for s, idx in strata.items():
        if len(idx) and alloc[s] == 0 and sum(alloc.values()) < n:
            alloc[s] = 1
    remaining = n - sum(alloc.values())
    by_remainder = sorted(strata, key=lambda s: exact[s] - alloc[s], reverse=True)
    i = 0
    while remaining > 0 and by_remainder:
        s = by_remainder[i % len(by_remainder)]
        if alloc[s] < len(strata[s]):
            alloc[s] += 1
            remaining -= 1
        i += 1
        if i > len(by_remainder) * 4 and all(
            alloc[t] >= len(strata[t]) for t in by_remainder
        ):
            break

    rng = random.Random(seed)
    out: list[int] = []
    for s, want in alloc.items():
        pool = list(strata[s])
        rng.shuffle(pool)
        out.extend(pool[:want])
    out.sort()
    return out


def accept_batch(
    proposals: list[Any],
    judge: Callable[[Any], bool],
    *,
    sample_size: int = 40,
    strata_key: Callable[[Any], Hashable] | None = None,
    error_bar: float = 0.10,
    z: float = 1.645,
    seed: int = 0,
) -> BatchDecision:
    """Judge a stratified sample of ``proposals`` and decide the batch.

    ``error_bar`` is the tolerated batch error rate at the ``z``-confidence
    one-sided bound (``z=1.645`` ≈ one-sided 95%). When ``sample_size`` ≥
    the batch, every proposal is judged — a full-review batch is a
    stratified sample at 100%.
    """
    idx = stratified_sample(proposals, sample_size, strata_key, seed=seed)
    strata_counts: dict[str, list[int]] = {}
    errors = failures = 0
    for i in idx:
        p = proposals[i]
        s = str(strata_key(p) if strata_key else 0)
        rec = strata_counts.setdefault(s, [0, 0, 0])
        rec[0] += 0  # batch count filled below
        rec[1] += 1
        try:
            ok = bool(judge(p))
        except Exception:
            failures += 1
            continue
        if not ok:
            errors += 1
            rec[2] += 1
    # Batch counts per stratum (unconditional on sampling).
    batch_counts: dict[str, int] = {}
    for p in proposals:
        s = str(strata_key(p) if strata_key else 0)
        batch_counts[s] = batch_counts.get(s, 0) + 1
    strata = {s: (batch_counts.get(s, 0), v[1], v[2]) for s, v in strata_counts.items()}

    n = len(idx)
    lo, hi = wilson_interval(errors, n, z=z)
    rate = errors / n if n else 0.0
    if failures or n == 0:
        verdict = "escalate"
    elif hi <= error_bar:
        verdict = "accept"
    elif lo > error_bar:
        verdict = "reject"
    else:
        verdict = "escalate"
    return BatchDecision(
        verdict=verdict,
        batch_size=len(proposals),
        n_sampled=n,
        n_errors=errors,
        n_judge_failures=failures,
        error_rate=rate,
        ci_lo=lo,
        ci_hi=hi,
        error_bar=error_bar,
        strata=strata,
        sampled_indices=tuple(idx),
    )


__all__ = ["VERDICTS", "BatchDecision", "accept_batch", "stratified_sample"]
