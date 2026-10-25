"""Barrier trust-gate replay over a pathway's stored trust records.

Read-only diagnostic (gr515871): every input the barrier gate uses is
already stored on the ``pathway`` ref — ``results.trust`` records,
``results.route_steps``, ``results.trust_summary.barrier`` — so the gate
can be *replayed* without new compute and rendered as one row per check
(``view='trust'`` on the pathway kind).

Three seams:

* :func:`route_scope` derives the route's node set from ``route_steps``
  (each step is ``"a->b"``) — the engine never persists ``route_nodes``.
* :func:`replay_barrier_gate` re-derives ``blocked_by`` by
  ``autocatpath.pipeline._trust_summary``'s rule (a ``fail`` at
  ``severity: fatal`` whose ``step`` is a route step or whose ``state`` is
  a route node). Comparing it to the stored set catches a stale or
  hand-edited summary.
* :func:`gate_rows` renders one row per gating check: verdict, an input
  digest, the threshold it was judged against, and how many of its
  records block. A check that emitted no records renders ``not-run``
  with a reason — never ``pass`` (a silent check is not a passing one).

The engine itself is imported lazily and only for ``ENDPOINT_TOL_EV``
(the assumed tolerance when a record predates the ``tol_eV`` stamp,
gr508948): the module imports and tests run on a venv without the
``catalyst`` extra. ``struct_neb_runs.trust`` rows
(``neb-structure-builder.md``) share the record shape, so that item's
``view='neb'`` can render through :func:`gate_rows` too.
"""

from __future__ import annotations

import dataclasses
from typing import Any

#: Gating checks in display order — autocatpath.trust.CHECKS minus
#: ``endpoint_identity`` (an input measurement, not a verdict gate).
#: Copied, not imported: the module must work on a venv without the
#: ``catalyst`` extra, and the check vocabulary is schema-pinned (a new
#: check the engine mints still renders — it appends after this list).
GATE_ORDER: tuple[str, ...] = (
    "endpoint_agreement",
    "detachment",
    "neb_convergence",
    "saddle_verified",
    "multistart",
    "relax_convergence",
    "wrong_binder",
    "clash",
    "reconstruction",
    "layer_intact",
    "fragment_merge",
    "hidden_intermediate",
)

#: Fallback when ``autocatpath.trust`` is not importable and no record
#: carries a stamped ``tol_eV`` — the engine constant's long-standing
#: value, rendered labelled ``assumed`` so a reader knows it isn't
#: measured provenance.
_DEFAULT_ENDPOINT_TOL_EV = 0.05

#: Strict-engine ruling (Reto 2026-10-25): only records minted under the
#: pathway's own ``autocatpath_version`` count toward the gate — the
#: endpoint basin comparison across engine versions is apples-to-oranges
#: (a bump can shift absolute energies), and cross-version pooling is out
#: everywhere. A record's engine is read from ``engine`` /
#: ``autocatpath_version`` when the writer stamped one; an unstamped
#: record counts as current (its provenance is unprovable — never
#: fabricate staleness). Stale records never gate; they render
#: ``stale, rerun on current`` instead.


def record_engine(rec: dict[str, Any]) -> str | None:
    """The engine version a trust record was minted under — ``engine`` or
    ``autocatpath_version``, whichever the writer stamped; ``None`` when
    unstamped (every record predating a stamp)."""
    for key in ("engine", "autocatpath_version"):
        v = rec.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def is_stale(rec: dict[str, Any], current_engine: str | None) -> bool:
    """``True`` when the record carries an engine stamp that differs from
    the pathway's current engine — it does not count toward the gate."""
    if not current_engine:
        return False
    engine = record_engine(rec)
    return engine is not None and engine != current_engine


@dataclasses.dataclass(frozen=True)
class Thresholds:
    """The numbers each gate was judged against."""

    endpoint_tol_ev: float
    endpoint_tol_stamped: bool
    absurd_ev: float
    twin_tol_ev: float
    energy_thresh_ev: float | None


def endpoint_tol_ev() -> float:
    """The engine's endpoint-agreement tolerance, lazily; the baked-in
    default when autocatpath is absent."""
    try:
        from autocatpath.trust import ENDPOINT_TOL_EV
    except Exception:  # no catalyst extra, or an engine too old to carry it
        return _DEFAULT_ENDPOINT_TOL_EV
    return float(ENDPOINT_TOL_EV)


def thresholds_from(
    records: list[dict[str, Any]],
    config: dict[str, Any] | None,
) -> Thresholds:
    """Resolve display thresholds: the ``tol_eV`` stamped on an
    ``endpoint_agreement`` record wins (it is what the run was actually
    judged against); otherwise the engine constant, flagged assumed."""
    stamped: float | None = None
    for r in records:
        if r.get("check") != "endpoint_agreement":
            continue
        ev = r.get("evidence")
        tol = ev.get("tol_eV") if isinstance(ev, dict) else None
        if isinstance(tol, int | float) and not isinstance(tol, bool):
            stamped = float(tol)
            break
    from precis.quest.compute import _TWIN_BARRIER_TOL_EV
    from precis.quest.frontier import _BARRIER_ABSURD_EV

    search = (config or {}).get("search") or {}
    thresh = search.get("energy_thresh")
    return Thresholds(
        endpoint_tol_ev=stamped if stamped is not None else endpoint_tol_ev(),
        endpoint_tol_stamped=stamped is not None,
        absurd_ev=float(_BARRIER_ABSURD_EV),
        twin_tol_ev=float(_TWIN_BARRIER_TOL_EV),
        energy_thresh_ev=(
            float(thresh)
            if isinstance(thresh, int | float) and not isinstance(thresh, bool)
            else None
        ),
    )


def route_scope(
    results: dict[str, Any],
) -> tuple[set[str] | None, set[str]]:
    """``(route_steps, route_nodes)`` — the node set derived from each
    ``"a->b"`` route step (the engine derives it the same way and never
    stores it).

    ``route_steps`` is ``None`` when the results payload doesn't carry the
    key at all (older engine): callers then treat every fatal fail as
    on-route — the same convention ``_trust_context`` documents for the
    warnings view.
    """
    raw = results.get("route_steps")
    if not isinstance(raw, list):
        return None, set()
    steps = {str(s) for s in raw if isinstance(s, str)}
    nodes: set[str] = set()
    for s in steps:
        a, sep, b = s.partition("->")
        if sep:
            nodes.update((a, b))
    return steps, nodes


def _on_route(
    rec: dict[str, Any],
    scope: tuple[set[str] | None, set[str]],
) -> bool:
    """``_trust_summary``'s membership rule: the record's step is a route
    step OR its state is a route node. ``state``-only records (the
    ``endpoint_agreement`` records carry no step) are on-route through
    their state."""
    steps, nodes = scope
    if steps is None:
        return True
    return rec.get("step") in steps or rec.get("state") in nodes


def replay_barrier_gate(
    records: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    *,
    engine: str | None = None,
) -> list[str]:
    """The barrier blocked-set, recomputed from stored records by
    ``autocatpath.pipeline._trust_summary``'s rule: ids of ``fail`` +
    ``severity: fatal`` records on the route, sorted. ``["no_route"]``
    when the route exists but is empty — the engine's own sentinel.
    Records stamped to a different engine version (:func:`is_stale`)
    never count."""
    steps, _nodes = scope
    if steps is not None and not steps:
        return ["no_route"]
    return sorted(
        {
            str(r["id"])
            for r in records
            if r.get("verdict") == "fail"
            and r.get("severity") == "fatal"
            and r.get("id") is not None
            and _on_route(r, scope)
            and not is_stale(r, engine)
        }
    )


def _subject(rec: dict[str, Any]) -> str:
    state, step = rec.get("state"), rec.get("step")
    if state and step:
        return f"{state}@{step}"
    return str(state or step or "?")


def _ev_short(rec: dict[str, Any], keys: tuple[str, ...]) -> str:
    ev = rec.get("evidence")
    if not isinstance(ev, dict):
        return ""
    return ", ".join(f"{k}={ev[k]}" for k in keys if k in ev and ev[k] is not None)


def _n_seeds(records: list[dict[str, Any]], results: dict[str, Any]) -> int:
    seeds = results.get("seeds")
    if isinstance(seeds, list) and seeds:
        return len(seeds)
    return len({r.get("seed") for r in records if r.get("seed") is not None})


def _check_verdict(
    recs: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    blocked: set[str],
) -> str:
    if not recs:
        return "not-run"
    if any(str(r.get("id")) in blocked for r in recs):
        return "fail"
    on_route = [r for r in recs if _on_route(r, scope)]
    if any(r.get("verdict") in ("fail", "marginal") for r in on_route):
        return "warn"
    return "pass"


def _not_run_reason(
    check: str,
    records: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    results: dict[str, Any],
) -> str:
    n_seeds = _n_seeds(records, results)
    if check == "endpoint_agreement" and n_seeds < 2:
        return "1 seed: agreement needs ≥ 2"
    if check == "endpoint_agreement":
        return "no endpoint_identity records"
    if check == "saddle_verified":
        return "no band"
    return "no records emitted"


def _endpoint_input(
    recs: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    results: dict[str, Any],
    blocked: set[str],
) -> str:
    steps, nodes = scope
    n_states = (
        len(nodes)
        if steps is not None
        else len({r.get("state") for r in recs if r.get("state")})
    )
    n_seeds = _n_seeds(recs, results)
    fails = [
        r for r in recs if r.get("verdict") == "fail" and str(r.get("id")) in blocked
    ]
    states_split = {r.get("state") for r in fails}
    out = f"{n_states} route states × {n_seeds} seeds"
    if fails:
        worst = max(
            fails,
            key=lambda r: (r.get("evidence") or {}).get("delta_meV") or 0.0,
        )
        delta = (worst.get("evidence") or {}).get("delta_meV")
        out += (
            f"; {len(states_split)} state{'s' if len(states_split) != 1 else ''} split; worst "
            f"{worst.get('state')} s{worst.get('seed')} +{delta} meV"
        )
    return out


def _generic_input(
    check: str,
    recs: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    blocked: set[str],
) -> str:
    steps, _ = scope
    on_route = [r for r in recs if _on_route(r, scope)]
    n_bad = sum(1 for r in recs if str(r.get("id")) in blocked)
    if check == "neb_convergence":
        n_steps = (
            len(steps)
            if steps is not None
            else len({r.get("step") for r in recs if r.get("step")})
        )
        n_seeds = len({r.get("seed") for r in recs if r.get("seed") is not None})
        out = f"{n_steps} route steps × {n_seeds} seeds"
    elif check == "saddle_verified":
        counts: dict[str, int] = {}
        for r in on_route:
            v = str(r.get("verdict") or "?")
            counts[v] = counts.get(v, 0) + 1
        out = " · ".join(f"{n} {v}" for v, n in sorted(counts.items())) or "0"
    elif check == "multistart":
        n_zero = sum(1 for r in recs if not ((r.get("evidence") or {}).get("n_valid")))
        out = (
            f"{len(recs)} records, all n_valid ≥ 1"
            if not n_zero
            else (f"{len(recs)} records, {n_zero} with n_valid = 0")
        )
    else:
        out = (
            f"{len(on_route)} on-route records"
            if on_route
            else (f"{len(recs)} records (none on route)")
        )
    if n_bad:
        first = next(r for r in recs if str(r.get("id")) in blocked)
        detail = _ev_short(
            first,
            ("delta_meV", "tol_eV", "image", "closest_A", "reason", "fmax"),
        )
        out += f"; first blocker {_subject(first)} s{first.get('seed')}" + (
            f": {detail}" if detail else ""
        )
    return out


def gate_rows(
    records: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    thresholds: Thresholds,
    results: dict[str, Any] | None = None,
    *,
    engine: str | None = None,
) -> list[dict[str, Any]]:
    """One row per gating check: ``gate``, ``verdict``, ``input`` digest,
    ``threshold``, ``blocking`` count of this check's records in the
    replayed blocked set. A silent check is ``not-run`` with a reason —
    never ``pass``. Checks the engine emitted but this list doesn't know
    append at the end rather than disappearing. Records stamped to an
    engine version other than ``engine`` are stale — they don't count,
    and the input digest says how many were set aside."""
    results = results if isinstance(results, dict) else {}
    live = [r for r in records if not is_stale(r, engine)]
    stale_by_check: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if is_stale(r, engine):
            stale_by_check.setdefault(str(r.get("check")), []).append(r)
    blocked = set(replay_barrier_gate(records, scope, engine=engine))
    checks = list(GATE_ORDER)
    seen = {r.get("check") for r in records}
    checks += sorted(
        c for c in seen if c and c not in checks and c != "endpoint_identity"
    )

    rows: list[dict[str, Any]] = []
    for check in checks:
        recs = [r for r in live if r.get("check") == check]
        stale = stale_by_check.get(check, [])
        verdict = _check_verdict(recs, scope, blocked)
        n_block = sum(1 for r in recs if str(r.get("id")) in blocked)
        if not recs:
            input_ = (
                _not_run_reason(check, records, scope, results) if not stale else ""
            )
        elif check == "endpoint_agreement":
            input_ = _endpoint_input(recs, scope, results, blocked)
        else:
            input_ = _generic_input(check, recs, scope, blocked)
        if stale:
            engines = "+".join(sorted({record_engine(r) or "?" for r in stale}))
            tail = f"{len(stale)} stale, engine {engines}"
            input_ = f"{input_}; {tail}" if input_ else tail
        if check == "endpoint_agreement":
            src = "stamped" if thresholds.endpoint_tol_stamped else "assumed"
            threshold = f"tol {thresholds.endpoint_tol_ev:g} eV ({src})"
        elif check == "neb_convergence":
            threshold = "converged"
        elif check == "saddle_verified":
            threshold = "ts not at endpoint, curvature < 0"
        elif check == "multistart":
            threshold = "n_valid ≥ 1"
        else:
            threshold = "any fatal fail"
        rows.append(
            {
                "gate": check,
                "verdict": verdict,
                "input": input_,
                "threshold": threshold,
                "blocking": n_block,
            }
        )
    return rows


def precis_guard_rows(
    candidate_meta: dict[str, Any] | None,
    *,
    barrier_ea: float | None,
    rate_limit_std: float | None,
    low_confidence: bool,
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    """The three precis-side rows: the absurd-magnitude guard and the
    twin-disagreement guard (both read from the candidate's stamped meta —
    ``meta.candidate_ref``), and the seed-spread readout that rides along
    for information and never gates."""
    cand = candidate_meta or {}
    rows: list[dict[str, Any]] = []
    if barrier_ea is None:
        rows.append(
            {
                "gate": "absurd magnitude (precis)",
                "verdict": "not-run",
                "input": "no barrier value",
                "threshold": f"≤ {thresholds.absurd_ev} eV",
                "blocking": 0,
            }
        )
    else:
        absurd = bool(cand.get("barrier_absurd"))
        rows.append(
            {
                "gate": "absurd magnitude (precis)",
                "verdict": "fail" if absurd else "pass",
                "input": f"|{barrier_ea:g}| eV",
                "threshold": f"≤ {thresholds.absurd_ev} eV",
                "blocking": 1 if absurd else 0,
            }
        )
    twin = cand.get("barrier_twin_disagreement")
    if twin:
        rows.append(
            {
                "gate": "twin disagreement (precis)",
                "verdict": "fail",
                "input": f"twin {twin} disagrees",
                "threshold": f"≤ {thresholds.twin_tol_ev} eV",
                "blocking": 1,
            }
        )
    else:
        geom = cand.get("geom_hash_c")
        tier = cand.get("barrier_fidelity") or cand.get("tier")
        detail = (
            f"no geom_hash_c twin stamped at tier {tier}"
            if geom and tier
            else "no twin disagreement stamped"
        )
        rows.append(
            {
                "gate": "twin disagreement (precis)",
                "verdict": "not-run",
                "input": detail,
                "threshold": f"≤ {thresholds.twin_tol_ev} eV",
                "blocking": 0,
            }
        )
    spread_v = "warn" if low_confidence else "pass"
    if rate_limit_std is None:
        spread_v, spread_in = "not-run", "no spread recorded"
    else:
        spread_in = f"std {rate_limit_std:g} eV on the rate-limiting step"
    rows.append(
        {
            "gate": "spread (info, never gates)",
            "verdict": spread_v,
            "input": spread_in,
            "threshold": (
                f"energy_thresh {thresholds.energy_thresh_ev:g} eV"
                if thresholds.energy_thresh_ev is not None
                else "energy_thresh (unset)"
            ),
            "blocking": "—",
        }
    )
    return rows


def endpoint_rows(
    records: list[dict[str, Any]],
    scope: tuple[set[str] | None, set[str]],
    *,
    engine: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The per-state, per-seed endpoint table: one row per
    ``endpoint_identity`` record on a route state, annotated with the
    ``endpoint_agreement`` record's ``delta_meV``/verdict when one was
    minted for it. Failing states first; states whose seeds all agree on
    one fingerprint fold into the returned count instead of a row.
    Records stamped to another engine (:func:`is_stale`) render
    ``stale, rerun on current``, and a state touched by staleness never
    folds into the agree-count."""
    identity = [
        r
        for r in records
        if r.get("check") == "endpoint_identity"
        and r.get("state")
        and _on_route(r, scope)
    ]
    agree: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for r in records:
        if r.get("check") == "endpoint_agreement" and r.get("state"):
            agree[(r.get("model"), r.get("state"), r.get("seed"))] = r
            agree.setdefault((None, r.get("state"), r.get("seed")), r)

    failing_states = {
        r.get("state")
        for r in agree.values()
        if r.get("verdict") == "fail" and not is_stale(r, engine)
    }
    stale_states = {
        r.get("state") for r in identity + list(agree.values()) if is_stale(r, engine)
    }
    rows: list[dict[str, Any]] = []
    folded = 0
    by_state: dict[Any, list[dict[str, Any]]] = {}
    for r in identity:
        by_state.setdefault(r["state"], []).append(r)
    for state, recs in by_state.items():
        fps = {(r.get("evidence") or {}).get("fingerprint") for r in recs}
        if state not in failing_states and state not in stale_states and len(fps) <= 1:
            folded += 1
            continue
        for r in sorted(recs, key=lambda x: (x.get("seed") or 0,)):
            a = agree.get((r.get("model"), state, r.get("seed"))) or agree.get(
                (None, state, r.get("seed"))
            )
            ev = r.get("evidence") or {}
            aev = (a or {}).get("evidence") or {}
            stale = is_stale(r, engine) or (a is not None and is_stale(a, engine))
            rows.append(
                {
                    "state": state,
                    "seed": r.get("seed"),
                    "e_eV": ev.get("e_eV"),
                    "Δ meV": aev.get("delta_meV", "—"),
                    "fingerprint": ev.get("fingerprint", "?"),
                    "verdict": (
                        "stale, rerun on current"
                        if stale
                        else (a or {}).get("verdict", "not compared")
                    ),
                }
            )
    rows.sort(
        key=lambda row: (
            row["state"] not in failing_states,
            str(row["state"]),
            row["seed"] if isinstance(row["seed"], int) else 0,
        )
    )
    return rows, folded
