"""Pure supply/client role matcher (Slice 6 of pcb-lazy-netlist-and-checks).

Assigns client pins to supplier terminals per role by minimum-distance
linear assignment, with voltage admissibility, fanout and a warm-start
``prior``. Members are tuples (§1c): a supply ``blocks`` entry of k pins is
carried as a unit and only a k-pin client tuple can take it (a
differential pair; ``within_block_swappable`` permits permuting the
pairing, otherwise declared order binds member-for-member). No DB, no IR
mutation. Design: docs/backlog/pcb-lazy-netlist-and-checks.md.
"""

from __future__ import annotations

import itertools
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
    #: a tuple client (§1c): all member pins on ``instance`` in declared
    #: order; ``pin`` is its head (``members[0]``). ``None`` = a single pin.
    members: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    assignment: dict[Pin, Pin]
    unsupplied: tuple[Pin, ...]
    total_cost: float
    findings: tuple[str, ...]


#: ``(clients, supplies, roles)`` -- the matcher's inputs off a stored graph.
MatchModel = tuple[list[Client], list[Supply], dict[str, Role]]


def _client_pins(c: Client) -> tuple[str, ...]:
    return c.members if c.members is not None else (c.pin,)


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
    for c in clients:
        if c.members is None:
            continue
        if (
            not c.members
            or c.members[0] != c.pin
            or len(set(c.members)) != len(c.members)
        ):
            raise ValueError(
                f"client {c.instance}/{c.pin}: members must be headed by the "
                "client's own pin, distinct"
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
        # supply units: (instance, block, voltage, swappable) — a block is
        # admissible only whole, so every member pin must be allowed.
        units: list[tuple[str, tuple[str, ...], float | None, bool]] = []
        for s in sorted(
            (s for s in supplies if s.role == rname), key=lambda s: s.instance
        ):
            v = s.voltage_v if s.voltage_v is not None else role.voltage_v
            for b in s.blocks:
                if allowed is None or all((s.instance, p) in allowed for p in b):
                    units.append((s.instance, b, v, s.within_block_swappable))

        known = {v for _i, _b, v, _w in units if v is not None}
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
        cols = [u for u in units for _ in range(max(fan, 0))]
        # chosen member order per filled cell: (i, j) -> block index order
        orders: dict[tuple[int, int], tuple[int, ...]] = {}
        kept: list[tuple[int, int, float]] = []
        if rclients and cols:
            real = np.full((len(rclients), len(cols)), _SENTINEL)
            adj = real.copy()
            for i, c in enumerate(rclients):
                cpins = _client_pins(c)
                demand = (
                    c.min_voltage_v if c.min_voltage_v is not None else role.voltage_v
                )
                if mixed and c.min_voltage_v is None:
                    continue
                cpos = [position(c.instance, p) for p in cpins]
                for j, (inst, block, sv, swappable) in enumerate(cols):
                    if len(block) != len(cpins):
                        continue
                    if sv is not None and demand is not None and sv < demand:
                        continue
                    spos = [position(inst, p) for p in block]
                    choices = (
                        itertools.permutations(range(len(block)))
                        if swappable
                        else (tuple(range(len(block))),)
                    )
                    best: tuple[float, float, tuple[int, ...]] | None = None
                    for order in choices:
                        d = 0.0
                        bonus = 0.0
                        for k, oi in enumerate(order):
                            pa, pb = cpos[k], spos[oi]
                            dd = (
                                _UNPLACED_COST
                                if pa is None or pb is None
                                else math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                            )
                            if penalty is not None:
                                dd += penalty((c.instance, cpins[k]), (inst, block[oi]))
                            d += dd
                            if prior.get((c.instance, cpins[k])) == (
                                inst,
                                block[oi],
                            ):
                                bonus += _PRIOR_BONUS
                        if best is None or d - bonus < best[0]:
                            best = (d - bonus, d, order)
                    if best is not None:
                        real[i, j] = best[1]
                        adj[i, j] = best[0]
                        orders[i, j] = best[2]
            ri, ci = linear_sum_assignment(adj)
            for i, j in zip(ri, ci, strict=True):
                if real[i, j] >= _SENTINEL:
                    continue
                kept.append((int(i), int(j), float(real[i, j])))
        matched: set[int] = set()
        for i, j, d in kept:
            c = rclients[i]
            inst, block, _v, _w = cols[j]
            for k, oi in enumerate(orders[i, j]):
                assignment[(c.instance, _client_pins(c)[k])] = (inst, block[oi])
            matched.add(i)
            total += d
        miss = [c for i, c in enumerate(rclients) if i not in matched]
        if miss:
            unsupplied.extend((c.instance, p) for c in miss for p in _client_pins(c))
            findings.append(f"{len(miss)} {rname} clients unsupplied")

    return MatchResult(assignment, tuple(sorted(unsupplied)), total, tuple(findings))


@dataclass(frozen=True, slots=True)
class ChannelRow:
    """One client pin of the channel map: its role, effective net and the
    supply terminal on that net (``None`` = unbound)."""

    role: str
    client: Pin
    net: str | None
    supplier: Pin | None


def channel_map(
    clients: Sequence[Client],
    supplies: Sequence[Supply],
    pin_nets: Mapping[Pin, str | None],
) -> list[ChannelRow]:
    """Which supply terminal each client pin is wired to, read off the
    effective nets (post pin-swap) rather than solved: a client's supplier
    is a same-role supply terminal — any member pin of any block — whose
    net equals the client's net. Several same-role terminals on one net
    (paralleled channels) bind to the first sorted; a tuple client emits
    one row per member pin. Derived, never stored."""
    by_net: dict[tuple[str, str], list[Pin]] = {}
    for s in supplies:
        for b in s.blocks:
            for p in b:
                t = (s.instance, p)
                net = pin_nets.get(t)
                if net is not None:
                    by_net.setdefault((s.role, net), []).append(t)
    rows: list[ChannelRow] = []
    for c in sorted(clients, key=lambda c: (c.role, c.instance, c.pin)):
        for p in _client_pins(c):
            cp = (c.instance, p)
            net = pin_nets.get(cp)
            hits = sorted(by_net.get((c.role, net), [])) if net is not None else []
            rows.append(ChannelRow(c.role, cp, net, hits[0] if hits else None))
    return rows
