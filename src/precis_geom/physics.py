"""``precis_geom.physics`` — ``evaluate(shape, physics, spec, *, seed,
cells)``, the one physics interface of the workspace toolkit
(se-flow-kernel slice 4a; se-native-fields §The toolkit adds its names).

``physics=['flow']`` is the first physics: the thin-gap Hele-Shaw solve
plus the Lagrangian tracer (``precis_flow``) on the
:class:`~precis_geom.flow.GapDomain` built from the script's own cad
solid. It runs with no DB and no se; ``precis_se.flow``'s
``FLOW_COMPUTERS`` call it too, so the script's number and the server's
re-check are one implementation.

**Physics only** (Reto 2026-10-22: "hole count will not be the answer to
evaluate shape. That's what the tool makes the space to be."):
:attr:`Outcome.measures` carries registered measurands and nothing else
— no feature counts, mass, material or process terms. Those are the
script's own objective; it has the geometry.

**Common random numbers.** ``seed=`` reseeds the tracer identically on
every call, so two shapes compared under one seed differ by geometry,
not by noise, and the same inputs give a bitwise-equal outcome.

**Budget.** A solve above :data:`INLINE_CELLS` refuses before any ray is
cast, naming ``budget='job'`` (the job lane the importer will dispatch;
not live until the workspace run loop lands).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis_flow import Domain, Paths, RegimeCard, Seed, nonuniformity, solve, trace
from precis_flow.regime import KN_CONTINUUM_MAX
from precis_geom import __version__ as _GEOM_VERSION
from precis_geom.flow import (
    DEFAULT_NX,
    EVENNESS_BINS,
    GapDomain,
    PortPatch,
    domain_from_solid,
    gap_cells,
    region_ranges,
)

#: The inline lane's cell cap (se-native-fields §Budgets) — above it
#: ``evaluate`` refuses naming ``budget='job'``.
INLINE_CELLS = 300_000

#: The inline lane's tracer budget: ``n * (frames + 1)`` particle-steps
#: (~24 B per recorded frame-particle at float64). Above it ``evaluate``
#: refuses naming ``budget='job'``.
INLINE_TRACE_WORK = 400_000

#: The flow measurands ``evaluate(['flow'])`` returns — the same four
#: ``precis_se.flow.FLOW_COMPUTERS`` registers.
FLOW_MEASURANDS = (
    "breakthrough-fraction",
    "residence-time-t0",
    "resistance-ratio",
    "evenness",
)

#: physics name → its measurands. se-native-fields slice 1 adds its names.
MEASURANDS: dict[str, tuple[str, ...]] = {"flow": FLOW_MEASURANDS}

#: A spec measure's strength: ``hard`` gates (``hard_ok``), ``soft`` is a
#: script objective term, ``gauge`` reports.
STRENGTHS = ("hard", "soft", "gauge")

#: Tracer particles when the spec does not say.
DEFAULT_N = 400

_FLOW_KEYS = (
    "inlet",
    "outlet",
    "dv",
    "profile",
    "zplane",
    "n",
    "frames",
    "seed_z",
    "evenness_region",
    "fluid",
    "measures",
)
_FLUID_KEYS = (
    "mu",
    "rho",
    "dp",
    "u0",
    "particle_d",
    "rho_p",
    "molecular_d",
    "mean_free_path",
)
_MEASURE_KEYS = (
    "name",
    "measurand",
    "strength",
    "value",
    "min_value",
    "max_value",
    "datum",
    "origin",
)


def _finite(x: Any) -> float | None:
    """A JSON-safe number: NaN/inf (a line the inputs cannot evaluate)
    becomes ``None`` — strict JSON parsers refuse NaN."""
    if x is None:
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def card_dict(card: RegimeCard | None) -> dict[str, Any] | None:
    """The regime card as JSON-safe data (player payload, manifest)."""
    if card is None:
        return None
    return {
        "ok": card.ok,
        "failures": list(card.failures),
        "lines": [
            {
                "name": ln.name,
                "value": _finite(ln.value),
                "threshold": _finite(ln.threshold),
                "ok": ln.ok,
                "holds_when": ln.holds_when,
                "otherwise": ln.otherwise,
                "applicable": ln.applicable,
            }
            for ln in card.lines
        ],
    }


@dataclass(frozen=True)
class Failure:
    """A hard measure the outcome violates — or cannot compute
    (``value`` ``None``, ``reason`` says why)."""

    name: str
    measurand: str
    value: float | None
    min_value: float | None
    max_value: float | None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "measurand": self.measurand,
            "value": _finite(self.value),
            "min_value": self.min_value,
            "max_value": self.max_value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Cost:
    """What the solve cost: cell counts, the kernel stamp, wall seconds
    (``seconds`` is excluded from equality — two equal outcomes may take
    different times)."""

    cells: int
    nx: int
    ny: int
    kernel: str
    seconds: float = field(default=0.0, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cells": self.cells,
            "nx": self.nx,
            "ny": self.ny,
            "kernel": self.kernel,
        }


@dataclass(frozen=True, eq=False)
class Outcome:
    """The result of :func:`evaluate`.

    ``measures`` — measurand slug → value (physics outcomes only);
    ``reasons`` — measurand → why it has no value (never a silent 0);
    ``cards`` — physics → its regime card (``None`` when the spec gave no
    physical inputs); ``hard_ok``/``failures`` — the spec's hard
    measures; ``cost``. ``paths`` (the traced swarm) and ``gap`` (the
    built domain) ride along for a player or a deeper look; they never
    enter :meth:`to_dict` or equality. Equality is :meth:`to_dict`
    equality — bitwise on every number."""

    physics: tuple[str, ...]
    measures: dict[str, float]
    reasons: dict[str, str]
    cards: dict[str, RegimeCard | None]
    hard_ok: bool
    failures: tuple[Failure, ...]
    cost: Cost
    seed: int
    paths: Paths | None = field(default=None, repr=False)
    gap: GapDomain | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form — what a manifest's stated values carry."""
        return {
            "physics": list(self.physics),
            "measures": dict(self.measures),
            "reasons": dict(self.reasons),
            "cards": {p: card_dict(c) for p, c in self.cards.items()},
            "hard_ok": self.hard_ok,
            "failures": [f.to_dict() for f in self.failures],
            "cost": self.cost.to_dict(),
            "seed": self.seed,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Outcome):
            return NotImplemented
        return self.to_dict() == other.to_dict()


@dataclass(frozen=True)
class _FlowCase:
    inlet: PortPatch
    outlet: PortPatch
    d_v_star: float
    profile: str
    zplane: float
    n: int
    frames: int
    z_lo: float
    z_hi: float
    evenness_region: PortPatch | None
    fluid: dict[str, float] | None


def _number(spec: dict[str, Any], key: str, what: str) -> float | None:
    v = spec.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise BadInput(f"{what}.{key} must be a finite number, got {v!r}")
    return float(v)


def _flow_case(spec: dict[str, Any]) -> _FlowCase:
    stray = sorted(set(spec) - set(_FLOW_KEYS))
    if stray:
        raise BadInput(
            f"evaluate(['flow']): unknown spec key(s) {', '.join(stray)}",
            options=list(_FLOW_KEYS),
        )
    for req in ("inlet", "outlet", "dv"):
        if spec.get(req) is None:
            raise BadInput(
                f"evaluate(['flow']) needs spec[{req!r}]",
                next="spec = {'inlet': PortPatch, 'outlet': PortPatch, "
                "'dv': <D_v* = D_v t0/h0²>} — dv is never defaulted",
            )
    dv = _number(spec, "dv", "spec")
    assert dv is not None
    if dv < 0.0:
        raise BadInput(f"spec.dv must be ≥ 0, got {dv!r}")
    profile = str(spec.get("profile") or "parabolic").strip().lower()
    if profile not in ("plug", "parabolic"):
        raise BadInput(
            f"spec.profile is 'plug' or 'parabolic', got {profile!r}",
            options=["plug", "parabolic"],
        )
    zplane = _number(spec, "zplane", "spec")
    zplane = 0.99 if zplane is None else zplane
    if not 0.0 < zplane <= 1.0:
        raise BadInput(f"spec.zplane must lie in (0, 1], got {zplane!r}")
    n = _number(spec, "n", "spec")
    frames = _number(spec, "frames", "spec")
    if n is not None and (n < 1 or n != int(n)):
        raise BadInput(f"spec.n must be a whole number ≥ 1, got {n!r}")
    if frames is not None and (frames < 0 or frames != int(frames)):
        raise BadInput(f"spec.frames must be a whole number ≥ 0, got {frames!r}")
    seed_z = spec.get("seed_z") or [0.0, 0.5]
    if (
        not isinstance(seed_z, list | tuple)
        or len(seed_z) != 2
        or not all(
            isinstance(z, int | float) and not isinstance(z, bool) for z in seed_z
        )
    ):
        raise BadInput(
            f"spec.seed_z must be [z_lo, z_hi] gap fractions, got {seed_z!r}"
        )
    z_lo, z_hi = float(seed_z[0]), float(seed_z[1])
    if not (0.0 <= z_lo <= z_hi <= 1.0):
        raise BadInput(
            f"spec.seed_z must be ordered fractions in [0, 1], got {seed_z!r}"
        )
    fluid_raw = spec.get("fluid")
    fluid: dict[str, float] | None = None
    if fluid_raw is not None:
        if not isinstance(fluid_raw, dict):
            raise BadInput(
                "spec.fluid is a dict of SI inputs", options=list(_FLUID_KEYS)
            )
        stray_f = sorted(set(fluid_raw) - set(_FLUID_KEYS))
        if stray_f:
            raise BadInput(
                f"spec.fluid: unknown key(s) {', '.join(stray_f)}",
                options=list(_FLUID_KEYS),
            )
        fluid = {}
        for k in _FLUID_KEYS:
            v = _number(fluid_raw, k, "spec.fluid")
            if v is not None:
                fluid[k] = v
    region = spec.get("evenness_region")
    return _FlowCase(
        inlet=PortPatch.coerce(spec["inlet"], "spec.inlet"),
        outlet=PortPatch.coerce(spec["outlet"], "spec.outlet"),
        d_v_star=dv,
        profile=profile,
        zplane=zplane,
        n=DEFAULT_N if n is None else int(n),
        frames=0 if frames is None else int(frames),
        z_lo=z_lo,
        z_hi=z_hi,
        evenness_region=(
            None if region is None else PortPatch.coerce(region, "spec.evenness_region")
        ),
        fluid=fluid,
    )


def _measure_list(
    spec: dict[str, Any], allowed: tuple[str, ...]
) -> list[dict[str, Any]]:
    raw = spec.get("measures") or []
    if not isinstance(raw, list):
        raise BadInput("spec.measures is a list of measure dicts")
    out: list[dict[str, Any]] = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise BadInput(f"spec.measures[{i}] is a dict, got {type(m).__name__}")
        stray = sorted(set(m) - set(_MEASURE_KEYS))
        if stray:
            raise BadInput(
                f"spec.measures[{i}]: unknown key(s) {', '.join(stray)}",
                options=list(_MEASURE_KEYS),
            )
        slug = m.get("measurand")
        if slug not in allowed:
            raise BadInput(
                f"spec.measures[{i}]: measurand {slug!r} is not a physics "
                "outcome of this evaluate call",
                options=list(allowed),
                next="design-space terms (feature count, mass, material, "
                "process) are the script's own objective, not a measure",
            )
        strength = m.get("strength") or "gauge"
        if strength not in STRENGTHS:
            raise BadInput(
                f"spec.measures[{i}]: strength {strength!r}", options=list(STRENGTHS)
            )
        lo = _number(m, "min_value", f"spec.measures[{i}]")
        hi = _number(m, "max_value", f"spec.measures[{i}]")
        if strength == "hard" and lo is None and hi is None:
            raise BadInput(
                f"spec.measures[{i}]: a hard measure needs min_value or max_value",
                next="a measure without a band is 'soft' or 'gauge'",
            )
        if lo is not None and hi is not None and lo > hi:
            raise BadInput(
                f"spec.measures[{i}]: min_value {lo:g} exceeds max_value {hi:g}"
            )
        out.append(
            {
                "name": str(m.get("name") or slug),
                "measurand": slug,
                "strength": strength,
                "min_value": lo,
                "max_value": hi,
            }
        )
    return out


def _card(gap: GapDomain, fluid: dict[str, float] | None) -> RegimeCard | None:
    """The six-line regime card from the spec's SI fluid inputs — ``None``
    when none were given; the reduced solve needs none of this."""
    if not fluid:
        return None
    if ("u0" in fluid) == ("dp" in fluid):
        raise BadInput("spec.fluid: state exactly one of u0 (m/s) or dp (Pa)")
    missing = [k for k in ("mu", "rho") if k not in fluid]
    if missing:
        raise BadInput(
            f"spec.fluid: a regime card needs {', '.join(missing)} "
            "(mu in Pa·s, rho in kg/m³, plus u0 or dp)"
        )
    card = RegimeCard.from_physical(
        L=gap.L,
        h0=gap.h0,
        mu=fluid["mu"],
        rho=fluid["rho"],
        dp=fluid.get("dp"),
        u0=fluid.get("u0"),
        particle_d=fluid.get("particle_d"),
        rho_p=fluid.get("rho_p"),
        molecular_d=fluid.get("molecular_d"),
        mean_free_path=fluid.get("mean_free_path"),
    )
    try:
        route = card.route
    except ValueError as exc:
        if card.knudsen.applicable and card.knudsen.value > KN_CONTINUUM_MAX:
            raise BadInput(f"evaluate(['flow']): {exc}") from exc
        route = "continuum"  # failing continuum lines show on the card
    if route != "continuum":
        raise BadInput(
            f"evaluate(['flow']): the regime card routes {route} — Kn "
            f"{card.knudsen.value:.3g} is free-molecular; the thin-gap "
            "kernel does not hold",
            next="a structure lumen runs the nano propagator through "
            "view='flow' on a structure-bound block",
        )
    return card


def _flow_measures(
    gap: GapDomain,
    paths: Paths,
    r_star: float,
    region: PortPatch | None,
) -> tuple[dict[str, float], dict[str, str]]:
    measures: dict[str, float] = {"breakthrough-fraction": float(paths.breakthrough)}
    reasons: dict[str, str] = {}
    res = paths.residence[np.isfinite(paths.residence)]
    if res.size:
        measures["residence-time-t0"] = float(res.mean())
    else:
        reasons["residence-time-t0"] = "no particles resolved — no residence statistic"
    d = gap.domain
    flat = Domain(
        mask=d.mask,
        h_star=np.where(d.mask, 1.0, 0.0),
        ports=d.ports,
        z_plane_star=d.z_plane_star,
    )
    r_ref = solve(flat).r_star
    if not math.isfinite(r_ref) or r_ref <= 0:
        reasons["resistance-ratio"] = "the undrilled chamber's resistance is not finite"
    else:
        measures["resistance-ratio"] = float(r_star / r_ref)
    if region is None:
        reasons["evenness"] = (
            "spec has no evenness_region — evenness scores first crossings "
            "over a floor/lid rectangle"
        )
    else:
        ranges = region_ranges(gap, region)
        if isinstance(ranges, str):
            reasons["evenness"] = ranges
        else:
            nu, kept = nonuniformity(
                paths.crossing_points(), ranges[0], ranges[1], EVENNESS_BINS
            )
            if kept.shape[0] == 0:
                reasons["evenness"] = (
                    "no particle crossed the tracking plane inside the "
                    "evenness region — no evenness"
                )
            else:
                measures["evenness"] = float(1.0 - nu)
    return measures, reasons


def evaluate(
    shape: Any,
    physics: list[str] | tuple[str, ...] = ("flow",),
    spec: dict[str, Any] | None = None,
    *,
    seed: int = 0,
    cells: int = DEFAULT_NX,
    budget: str = "inline",
) -> Outcome:
    """Run ``physics`` on ``shape`` under ``spec`` → :class:`Outcome`.

    ``shape`` is a ``Placed`` envelope or a ``(Design, Expr)`` cad solid
    (coordinates in its own frame, metres). ``cells`` is the kernel's
    lateral cell count ``nx``. Pure over ``(shape, physics, spec, seed,
    cells)``: ``spec`` is never mutated and the same inputs give an
    equal outcome. Refusals are ``BadInput`` with ``next=``/``options=``."""
    names = tuple(physics)
    if not names:
        raise BadInput("evaluate: physics is empty", options=sorted(MEASURANDS))
    unknown = [p for p in names if p not in MEASURANDS]
    if unknown:
        raise BadInput(
            f"evaluate: unknown physics {', '.join(map(repr, unknown))}",
            options=sorted(MEASURANDS),
        )
    if spec is None or not isinstance(spec, dict):
        raise BadInput(
            "evaluate needs spec= (a dict)",
            next="flow: {'inlet': PortPatch, 'outlet': PortPatch, 'dv': <D_v*>}",
        )
    if budget not in ("inline", "job"):
        raise BadInput(f"evaluate: budget {budget!r}", options=["inline", "job"])
    if isinstance(cells, bool) or not isinstance(cells, int):
        raise BadInput(
            f"evaluate: cells must be an int (the kernel's nx), got {cells!r}"
        )
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise BadInput(f"evaluate: seed must be an int, got {seed!r}")
    nx, ny = gap_cells(shape, cells)
    if nx * ny > INLINE_CELLS:
        if budget == "inline":
            raise BadInput(
                f"evaluate: {nx}×{ny} = {nx * ny:,} cells is above the inline "
                f"cap INLINE_CELLS = {INLINE_CELLS:,}",
                next="pass budget='job' to request a job-lane solve, or lower cells=",
            )
        raise BadInput(
            "evaluate: budget='job' needs the workspace run loop's job lane, "
            "which is not live yet (structure-workspaces slice 1)",
            next=f"lower cells= so nx×ny ≤ {INLINE_CELLS:,} and run inline",
        )
    t0 = time.perf_counter()
    case = _flow_case(spec)
    if case.n * (case.frames + 1) > INLINE_TRACE_WORK:
        if budget == "inline":
            raise BadInput(
                f"evaluate: n × (frames + 1) = "
                f"{case.n * (case.frames + 1):,} particle-steps is above the "
                f"inline cap INLINE_TRACE_WORK = {INLINE_TRACE_WORK:,}",
                next="pass budget='job' to request a job-lane solve, or lower "
                "spec.n / spec.frames",
            )
        raise BadInput(
            "evaluate: budget='job' needs the workspace run loop's job lane, "
            "which is not live yet (structure-workspaces slice 1)",
            next=f"lower spec.n / spec.frames so n×(frames+1) ≤ "
            f"{INLINE_TRACE_WORK:,} and run inline",
        )
    wanted = _measure_list(spec, tuple(m for p in names for m in MEASURANDS[p]))
    try:
        gap = domain_from_solid(
            shape, case.inlet, case.outlet, nx=cells, z_plane_star=case.zplane
        )
        card = _card(gap, case.fluid)
        fld = solve(gap.domain)
        paths = trace(
            fld,
            Seed.across_inlet(gap.domain, z_lo=case.z_lo, z_hi=case.z_hi),
            case.n,
            d_v_star=case.d_v_star,
            seed_rng=seed,
            record_frames=case.frames,
            profile=case.profile,
        )
    except BadInput:
        # _card / domain_from_solid refuse with the way out already named
        # (gr515473 — BadInput is a ValueError; a plain except would
        # swallow its next= into the generic wrap below)
        raise
    except ValueError as exc:
        # a kernel or adapter refusal surfaced as a raw error — report it
        # with the way out
        raise BadInput(
            f"evaluate(['flow']): {exc}",
            next="the kernel message above names the failure — check the "
            "solid bounds a thin gap and the spec's inputs are in range",
        ) from exc
    measures, reasons = _flow_measures(gap, paths, fld.r_star, case.evenness_region)
    failures: list[Failure] = []
    for m in wanted:
        if m["strength"] != "hard":
            continue
        value = measures.get(m["measurand"])
        lo, hi = m["min_value"], m["max_value"]
        if value is None:
            failures.append(
                Failure(
                    m["name"], m["measurand"], None, lo, hi, reasons[m["measurand"]]
                )
            )
        elif (lo is not None and value < lo) or (hi is not None and value > hi):
            failures.append(Failure(m["name"], m["measurand"], value, lo, hi))
    d = gap.domain
    return Outcome(
        physics=names,
        measures=measures,
        reasons=reasons,
        cards={"flow": card},
        hard_ok=not failures,
        failures=tuple(failures),
        cost=Cost(
            cells=int(d.nx * d.ny),
            nx=int(d.nx),
            ny=int(d.ny),
            kernel=f"precis_flow (precis-geom {_GEOM_VERSION})",
            seconds=time.perf_counter() - t0,
        ),
        seed=seed,
        paths=paths,
        gap=gap,
    )


__all__ = [
    "DEFAULT_N",
    "FLOW_MEASURANDS",
    "INLINE_CELLS",
    "INLINE_TRACE_WORK",
    "MEASURANDS",
    "STRENGTHS",
    "Cost",
    "Failure",
    "Outcome",
    "card_dict",
    "evaluate",
]
