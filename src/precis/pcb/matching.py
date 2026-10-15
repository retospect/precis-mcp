"""Pure supply/client role matcher (Slice 6 of pcb-lazy-netlist-and-checks).

Assigns client pins to supplier terminals per role by minimum-distance
linear assignment, with voltage admissibility, fanout and a warm-start
``prior``. No DB, no IR mutation. Design: docs/backlog/pcb-lazy-netlist-and-checks.md.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment

_UNPLACED_COST = 1e6
_SENTINEL = 1e12
_PRIOR_BONUS = 1e-6

Pin = tuple[str, str]


@dataclass(frozen=True, slots=True)
class Role:
    name: str
    voltage_v: float | None = None
    fanout: int | None = 1  # None = unbounded


@dataclass(frozen=True, slots=True)
class Supply:
    instance: str
    role: str
    blocks: tuple[tuple[str, ...], ...]
    voltage_v: float | None = None
    within_block_swappable: bool = False


@dataclass(frozen=True, slots=True)
class Client:
    instance: str
    pin: str
    role: str
    min_voltage_v: float | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    assignment: dict[Pin, Pin]
    unsupplied: tuple[Pin, ...]
    total_cost: float
    findings: tuple[str, ...]


def solve(
    clients: Sequence[Client],
    supplies: Sequence[Supply],
    roles: Mapping[str, Role],
    position: Callable[[str, str], tuple[float, float] | None],
    *,
    prior: Mapping[Pin, Pin] | None = None,
    allowed_suppliers: Collection[Pin] | None = None,
    penalty: Callable[[Pin, Pin], float] | None = None,
) -> MatchResult:
    for s in supplies:
        if any(len(b) != 1 for b in s.blocks):
            raise ValueError(
                f"supply {s.instance}: multi-pin blocks need client tuples (not built yet)"
            )
    prior = prior or {}
    allowed = set(allowed_suppliers) if allowed_suppliers is not None else None

    assignment: dict[Pin, Pin] = {}
    unsupplied: list[Pin] = []
    findings: list[str] = []
    total = 0.0

    for rname in sorted({c.role for c in clients}):
        role = roles.get(rname) or Role(name=rname)
        rclients = sorted(
            (c for c in clients if c.role == rname), key=lambda c: (c.instance, c.pin)
        )
        # terminals: (instance, pin, voltage)
        terms: dict[Pin, float | None] = {}
        for s in sorted(
            (s for s in supplies if s.role == rname), key=lambda s: s.instance
        ):
            v = s.voltage_v if s.voltage_v is not None else role.voltage_v
            for b in s.blocks:
                t = (s.instance, b[0])
                if allowed is None or t in allowed:
                    terms[t] = v
        tlist = sorted(terms)

        known = {v for v in terms.values() if v is not None}
        mixed = role.voltage_v is None and len(known) >= 2
        if mixed:
            n = sum(1 for c in rclients if c.min_voltage_v is None)
            if n:
                findings.append(
                    f"{n} {rname} clients have no voltage demand but {rname} suppliers "
                    f"differ in voltage ({sorted(known)}); declare voltage_v on the role "
                    "or min_voltage_v on the client"
                )

        fan = len(rclients) if role.fanout is None else role.fanout
        cols = [t for t in tlist for _ in range(max(fan, 0))]
        kept: list[tuple[Pin, Pin, float]] = []
        if rclients and cols:
            real = np.full((len(rclients), len(cols)), _SENTINEL)
            adj = real.copy()
            for i, c in enumerate(rclients):
                cp = (c.instance, c.pin)
                demand = (
                    c.min_voltage_v if c.min_voltage_v is not None else role.voltage_v
                )
                if mixed and c.min_voltage_v is None:
                    continue
                cpos = position(*cp)
                for j, t in enumerate(cols):
                    sv = terms[t]
                    if sv is not None and demand is not None and sv < demand:
                        continue
                    spos = position(*t)
                    if cpos is None or spos is None:
                        d = _UNPLACED_COST
                    else:
                        d = math.hypot(cpos[0] - spos[0], cpos[1] - spos[1])
                    if penalty is not None:
                        d += penalty(cp, t)
                    real[i, j] = d
                    adj[i, j] = d - (_PRIOR_BONUS if prior.get(cp) == t else 0.0)
            ri, ci = linear_sum_assignment(adj)
            for i, j in zip(ri, ci, strict=True):
                if real[i, j] >= _SENTINEL:
                    continue
                c = rclients[i]
                kept.append(((c.instance, c.pin), cols[j], float(real[i, j])))
        matched = set()
        for cp, t, d in kept:
            assignment[cp] = t
            matched.add(cp)
            total += d
        miss = [
            (c.instance, c.pin) for c in rclients if (c.instance, c.pin) not in matched
        ]
        if miss:
            unsupplied.extend(miss)
            findings.append(f"{len(miss)} {rname} clients unsupplied")

    return MatchResult(assignment, tuple(sorted(unsupplied)), total, tuple(findings))
