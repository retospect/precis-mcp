"""Stored-point bottleneck objectives, with no search, solve or writes.

All objective values are eV. The kinetic record's own temperature supplies
kT; a requested T/p point must match that record or a sweep row. CHE only
re-tilts U today: thermodynamics retain their stored reference footing and
kinetics stay at the run U. Missing evidence is never a successful mask.
Plugin helpers are loaded lazily, following ``window_objectives``' boundary.
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass, field
from typing import Any

from precis.utils.pathway_conditions import kinetics_environment

KB_EV_K = 8.617333262145e-5
H_EV_S = 4.135667696e-15
STATE_SETTLED_TOL = 1e-4  # toon_views::_mf_reading's ln-coverage tolerance


@dataclass(frozen=True)
class EnvironmentPoint:
    """Explicit coordinates; unknown coordinates stay None (pressures in bar)."""

    U: float | None
    pH: float | None
    T: float | None
    pressures: dict[str, float] | None
    ion_conc_M: float | None


@dataclass(frozen=True)
class Term:
    """A value and its provenance, or None and the reason it is unavailable."""

    value: float | None
    reason: str
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PointEvaluation:
    point: EnvironmentPoint
    terms: dict[str, Term]
    mask: Term
    objective: Term
    limiting: str | None
    checks: dict[str, Term] = field(default_factory=dict)


def number(value: Any) -> float | None:
    """Finite stored number; booleans and null are not measurements."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _same(a: Any, b: Any) -> bool:
    x, y = number(a), number(b)
    return (
        x is not None
        and y is not None
        and math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12)
    )


def _matches(point: EnvironmentPoint, env: dict[str, Any]) -> bool:
    pressures = env.get("pressures")
    return (
        _same(point.T, env.get("T"))
        and isinstance(pressures, dict)
        and point.pressures is not None
        and pressures.keys() == point.pressures.keys()
        and all(_same(p, pressures[g]) for g, p in point.pressures.items())
    )


def kinetic_record(
    results: dict[str, Any], point: EnvironmentPoint
) -> tuple[dict[str, Any] | None, float | None, str]:
    """Exact T/partial-pressure match; never interpolate or borrow row trust."""
    v2 = results.get("kinetics_v2")
    if isinstance(v2, dict):
        env = kinetics_environment({"kinetics_v2": v2})
        if env is not None and _matches(point, env):
            return v2, number(env.get("T")), "kinetics_v2.environment.T"
    sweep = results.get("kinetics_sweep") or {}
    for row in sweep.get("points") or []:
        if isinstance(row, dict) and _matches(point, row):
            return row, number(row.get("T")), "kinetics_sweep.points.T"
    return None, None, "no stored kinetics at this T/partial-pressure point"


def _tof(record: dict[str, Any]) -> Term:
    kmc = record.get("tof_kmc") or {}
    median = number(kmc.get("median")) if isinstance(kmc, dict) else None
    if median is not None and median >= 0:
        return Term(median, "tof_kmc.median")
    sign_module = importlib.import_module("precis_pathway.tof_sign")
    sign = sign_module.tof_sign(record)
    if sign is not None:
        return Term(None, f"tof_mf {sign['class']}: {sign_module.tof_sign_label(sign)}")
    mf = number(record.get("tof_mf"))
    if mf is None or mf < 0:
        return Term(
            None, "tof_mf absent, non-finite or negative; no positive KMC measurement"
        )
    return Term(mf, "tof_mf")


def kinetic_terms(
    record: dict[str, Any] | None,
    temperature: float | None,
    source: str,
    *,
    target: str | None,
    tof_floor: float | None,
) -> tuple[Term, Term, Term]:
    """Selectivity log odds, log rate above floor, and the selected raw TOF."""
    if record is None:
        absent = Term(None, source)
        return absent, absent, absent
    tof = _tof(record)
    if temperature is None or temperature <= 0:
        absent = Term(None, "positive recorded kinetic temperature absent")
        return absent, absent, tof
    kt = KB_EV_K * temperature
    provenance = f"kT from {source} = {temperature:g} K; kinetics at the run U"
    selectivity = number((record.get("selectivity") or {}).get(target))
    if selectivity is None or not 0 <= selectivity <= 1:
        s = Term(None, f"selectivity for {target!r} absent or outside [0, 1]")
    else:
        odds = (
            -math.inf
            if selectivity == 0
            else math.inf
            if selectivity == 1
            else math.log(selectivity) - math.log1p(-selectivity)
        )
        s = Term(kt * odds, provenance)
    floor = number(tof_floor)
    if tof.value is None:
        r = tof
    elif tof.value == 0:
        r = Term(None, "TOF is zero, so ln(TOF/floor) is undefined")
    elif floor is None or floor <= 0:
        r = Term(None, "positive TOF_floor absent from subject_to")
    else:
        log_rate = math.log(tof.value) - math.log(floor)
        r = Term(kt * log_rate, f"{tof.reason}; floor {floor:g} /s; {provenance}")
    return s, r, tof


def _gate(value: bool | None, reason: str) -> Term:
    return Term(None if value is None else float(value), reason)


def trust_checks(
    record: dict[str, Any] | None, tof: Term, floor: float | None
) -> dict[str, Term]:
    """Read actual mf_status/diagnosis fields; absent diagnostics stay unknown."""
    record = record or {}
    share = number(record.get("bep_flux_share"))
    status = record.get("mf_status") or {}
    err = number(status.get("state_err"))
    settled = None
    if status.get("ok") is False or (err is not None and err > STATE_SETTLED_TOL):
        settled = False
    elif status.get("ok") is True and err is not None:
        settled = 0 <= err <= STATE_SETTLED_TOL
    diagnosis = status.get("diagnosis") or {}
    poisoned = diagnosis.get("kind") == "poisoned_surface"
    not_poisoned = False if poisoned else True if status.get("ok") is True else None
    floor = number(floor)
    return {
        "bep_flux_share": _gate(
            None if share is None else 0 <= share <= 0.5,
            f"bep_flux_share={share}; requires <= 0.5",
        ),
        "settled": _gate(
            settled,
            f"mf_status.ok={status.get('ok')}, state_err={err}; requires <= {STATE_SETTLED_TOL:g}",
        ),
        "not_poisoned": _gate(
            not_poisoned,
            f"mf_status.diagnosis={diagnosis or 'absent'}; ok={status.get('ok')}",
        ),
        "rate_floor": _gate(
            None
            if tof.value is None or floor is None or floor <= 0
            else tof.value >= floor,
            f"{tof.reason}; TOF >= {floor} /s",
        ),
    }


def bulk_check(bulk: dict[str, Any] | None, point: EnvironmentPoint) -> Term:
    """Read pourbaix_map.bulk_layer at exactly U/pH/c_ion, never a nearby cell."""
    if not bulk or not _same(point.ion_conc_M, bulk.get("ion_conc_M")):
        return Term(None, "bulk verdict absent at this ion_conc_M")
    for row in [bulk.get("point") or {}, *(bulk.get("cells") or [])]:
        if not isinstance(row, dict):
            continue
        if not _same(point.U, row.get("U_RHE")) or not _same(point.pH, row.get("pH")):
            continue
        verdict = row.get("verdict")
        if verdict == "dissolved":
            return _gate(False, "bulk dissolved at this U/pH/ion_conc_M")
        if verdict in {"stable", "oxidised", "transformed", "leached"}:
            return _gate(
                True, f"bulk {verdict} at this U/pH/ion_conc_M (uncertain overlay)"
            )
        return Term(None, f"bulk verdict {verdict!r} is not a matched phase verdict")
    return Term(None, "bulk verdict absent at this U/pH")


def _intersect(checks: dict[str, Term]) -> Term:
    failed = [f"{k}: {v.reason}" for k, v in checks.items() if v.value == 0]
    missing = [f"{k}: {v.reason}" for k, v in checks.items() if v.value is None]
    if failed:
        return Term(0.0, "; ".join([*failed, *missing]))
    if missing:
        return Term(None, "; ".join(missing))
    return Term(1.0, "all physical and trust checks pass")


def evaluate_point(
    results: dict[str, Any],
    staircase: Any,
    point: EnvironmentPoint,
    *,
    target: str | None,
    tof_floor: float | None,
    route_index: int = 0,
    map_entry: dict[str, Any] | None = None,
    coads: dict[str, int] | None = None,
    bulk: dict[str, Any] | None = None,
    include_ea: bool = False,
    tau_s: float = 1.0,
) -> PointEvaluation:
    """Evaluate one target route at e, using the routes_for_pathway adapter.

    J is -inf for a failed M, None for unknown M/required terms. The
    optional barrier screen never turns an untrusted solve into M=1.
    ``coads=None`` means the assumed surface is unknown; ``{}`` means clean.
    """
    routes = staircase.pcet_routes
    route = routes[route_index] if 0 <= route_index < len(routes) else None
    complete = (
        route is not None
        and bool(route.rungs)
        and not any("dropped" in m for m in staircase.missing)
    )
    u = number(point.U)
    thermo = Term(None, "complete che_affine target route or U absent")
    her = Term(None, "her_stats/HER rungs, complete target route or U absent")
    if route is not None and complete and u is not None:
        thermo = Term(
            min(
                (0.3 if r.n_e == 0 else 0.0) - float(r.delta_g(u)) for r in route.rungs
            ),
            "one-rung PCET; chemical epsilon 0.3 eV, electrochemical 0; stored thermodynamic footing",
        )
        if (
            staircase.her_stats is not None
            and staircase.her is not None
            and staircase.her.rungs
        ):
            her = Term(
                float(staircase.her.limiting_g(u)) - float(route.limiting_g(u)),
                "HER limiting DeltaG minus target-route limiting DeltaG",
            )
    # The brief's selectivity_margin_eV is a prose label. The engine's
    # persisted field is score.selectivity.margin_eV (_selectivity_scalars).
    margin = number(
        ((results.get("score") or {}).get("selectivity") or {}).get("margin_eV")
    )
    edges = results.get("edges") or []
    measured = bool(edges) and all(
        isinstance(e, dict) and number((e.get("barrier") or {}).get("mean")) is not None
        for e in edges
    )
    kinetic = Term(
        margin,
        "results.score.selectivity.margin_eV (worst fork); U-flat"
        if margin is not None
        else "results.score.selectivity.margin_eV absent",
        () if measured else ("BEP: NEB coverage incomplete or unknown",),
    )
    record, temperature, source = kinetic_record(results, point)
    s, r, tof = kinetic_terms(
        record, temperature, source, target=target, tof_floor=tof_floor
    )
    terms = {"m_th": thermo, "m_kin": kinetic, "m_HER": her, "s": s, "r": r}
    checks = trust_checks(record, tof, tof_floor)
    trust = _intersect({k: v for k, v in checks.items() if k != "rate_floor"})
    checks["termination"] = Term(None, "surface map, assumed surface or U/pH absent")
    if (
        map_entry is not None
        and coads is not None
        and u is not None
        and number(point.pH) is not None
    ):
        states_module = importlib.import_module("precis_pathway.staircase_states")
        states = states_module.point_states(
            staircase, map_entry, u_rhe=u, ph=point.pH, coads=coads
        )
        checks["termination"] = _gate(
            states.off_map_reason is None if states.mapped else None,
            states.off_map_reason
            or f"termination={states.termination}, assumed={states.assumed}",
        )
    checks["bulk"] = bulk_check(bulk, point)
    checks["reset_ok"] = Term(None, "complete split target route or U absent")
    if complete and u is not None and route_index < len(staircase.routes):
        reset_module = importlib.import_module("precis_pathway.reset_contour")
        reset = reset_module.reset_edges(staircase.routes[route_index])
        if reset:
            checks["reset_ok"] = _gate(
                all(float(e.slack(u)) >= 0 for e in reset),
                "reset_contour.reset_edges: " + ", ".join(e.estimator for e in reset),
            )
    if include_ea and trust.value != 1:
        terms["m_Ea"] = barrier_term(
            staircase.routes[route_index] if complete else None, temperature, tau_s
        )
    mask = _intersect(checks)
    available = {k: t.value for k, t in terms.items() if t.value is not None}
    limiting = min(available, key=lambda k: available[k]) if available else None
    if mask.value == 0:
        objective = Term(-math.inf, "M=0: " + mask.reason)
    elif mask.value is None or len(available) != len(terms):
        missing = [k for k, t in terms.items() if t.value is None]
        objective = Term(
            None,
            "M unknown"
            if mask.value is None
            else "absent terms: " + ", ".join(missing),
        )
    else:
        objective = Term(
            min(available.values()),
            f"minimum of {', '.join(terms)}; limiting={limiting}",
        )
    return PointEvaluation(point, terms, mask, objective, limiting, checks)


def barrier_term(route: Any, temperature: float | None, tau_s: float) -> Term:
    """Optional screening margin, measured barriers first, then stored BEP."""
    if (
        route is None
        or not route.rungs
        or temperature is None
        or temperature <= 0
        or not math.isfinite(tau_s)
        or tau_s <= 0
    ):
        return Term(None, "route barriers, positive kinetic T or tau absent")
    barriers = []
    flags = []
    for rung in route.rungs:
        if rung.barrier_ea is not None:
            barriers.append(rung.barrier_ea)
        elif rung.bep is not None:
            alpha, beta = rung.bep
            barriers.append(max(0.0, alpha * rung.delta_g0 + beta))
            flags.append(f"BEP: {rung.name}")
        else:
            return Term(None, f"barrier absent for {rung.name}")
    kt = KB_EV_K * temperature
    threshold = kt * (math.log(kt / H_EV_S) + math.log(tau_s))
    return Term(
        threshold - max(barriers),
        f"kT from matched kinetics T={temperature:g} K; tau={tau_s:g} s; barriers at run footing",
        tuple(flags),
    )
