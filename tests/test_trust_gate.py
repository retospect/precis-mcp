"""``precis.utils.trust_gate`` — the barrier trust-gate replay
(gr515871): pure functions over a pathway's stored ``results.trust``
records, no DB and no autocatpath import (the engine is an optional
extra; the module must run where it is absent).

Fixtures are shaped like pw515950's stored payload: a 3-seed run whose
seed 1 lands in a higher basin on route state ``A`` (one
``endpoint_agreement`` fatal fail per agreed-against seed above tol)
plus a ``detachment`` fatal on route step ``R->A``, and — to prove the
replay scopes by route, not by record — an off-route fatal fail on
``Q->Z`` that must never enter ``blocked_by``.
"""

from __future__ import annotations

from typing import Any

from precis.utils import trust_gate
from precis_pathway.toon_views import trust_toon


def _rec(
    check: str,
    verdict: str,
    severity: str,
    *,
    seed: int = 0,
    step: str | None = None,
    state: str | None = None,
    evidence: dict[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    subj = state or step
    if state and step:
        subj = f"{state}@{step}"
    rec: dict[str, Any] = {
        "check": check,
        "verdict": verdict,
        "severity": severity,
        "seed": seed,
        "attempt": 1,
        "evidence": dict(evidence or {}),
        "id": f"{subj}#s{seed}#{check}",
    }
    if step is not None:
        rec["step"] = step
    if state is not None:
        rec["state"] = state
    if model is not None:
        rec["model"] = model
    return rec


_ROUTE_STEPS = ["R->A", "A->P"]
_STATES = ["R", "A", "P"]
_FP_LO = "aaaabbbbcccc"
_FP_HI = "dddd00001111"


def _endpoint_records(
    seeds: list[int],
    *,
    hi_states: tuple[str, ...] = ("A",),
    hi_delta_eV: float = 0.8669,
    tol_eV: float | None = 0.05,
) -> list[dict[str, Any]]:
    """endpoint_identity rows plus the agreement rows the engine mints:
    pass at the (state, model) minimum, fatal fail above ``tol``."""
    recs: list[dict[str, Any]] = []
    for st in _STATES:
        hi = st in hi_states
        for s in seeds:
            recs.append(
                _rec(
                    "endpoint_identity",
                    "pass",
                    "warn",
                    seed=s,
                    state=st,
                    evidence={
                        "e_eV": round(
                            -10.0 + (hi_delta_eV if hi and s == 1 else 0.0), 6
                        ),
                        "fingerprint": _FP_HI if hi and s == 1 else _FP_LO,
                    },
                    model="mace:medium",
                )
            )
        if len(seeds) < 2:
            continue
        for s in seeds:
            bad = hi and s == 1
            ev: dict[str, Any] = {
                "e_eV": round(-10.0 + (hi_delta_eV if bad else 0.0), 6),
                "e_min_eV": -10.0,
                "delta_meV": round(hi_delta_eV * 1000, 1) if bad else 0.0,
                "n_runs": len(seeds),
                "n_fingerprints": 2 if hi else 1,
            }
            if bad:
                ev["reason"] = "endpoint-mismatch"
            if tol_eV is not None:
                ev["tol_eV"] = tol_eV
            recs.append(
                _rec(
                    "endpoint_agreement",
                    "fail" if bad else "pass",
                    "fatal",
                    seed=s,
                    state=st,
                    evidence=ev,
                    model="mace:medium",
                )
            )
    return recs


def _records(
    seeds: list[int],
    *,
    hi_states: tuple[str, ...] = ("A",),
    tol_eV: float | None = 0.05,
    with_detachment: bool = True,
) -> list[dict[str, Any]]:
    recs = _endpoint_records(seeds, hi_states=hi_states, tol_eV=tol_eV)
    for step in _ROUTE_STEPS:
        for s in seeds:
            recs.append(
                _rec(
                    "neb_convergence",
                    "pass",
                    "fatal",
                    seed=s,
                    step=step,
                    evidence={"method": "cisneb", "retried": False},
                )
            )
            recs.append(
                _rec(
                    "saddle_verified",
                    "pass",
                    "fatal",
                    seed=s,
                    step=step,
                    evidence={
                        "images": 7,
                        "ts_image": 4,
                        "barrier_eV": 0.5 + 0.1 * s + (0.35 if step == "A->P" else 0.0),
                        "curvature_eV": -0.2,
                    },
                )
            )
            recs.append(
                _rec(
                    "multistart",
                    "pass",
                    "fatal",
                    seed=s,
                    step=step,
                    evidence={"n_valid": 2, "n_starts": 4},
                )
            )
    if with_detachment:
        recs.append(
            _rec(
                "detachment",
                "fail",
                "fatal",
                seed=1,
                step="R->A",
                evidence={"image": 6, "closest_A": 4.69},
            )
        )
        # Off-route: fatal but must never enter the barrier blocked set.
        recs.append(
            _rec(
                "detachment",
                "fail",
                "fatal",
                seed=1,
                step="Q->Z",
                state="Z",
                evidence={"image": 2, "closest_A": 5.1},
            )
        )
    return recs


def _results(
    records: list[dict[str, Any]],
    seeds: list[int],
    *,
    tamper: bool = False,
) -> dict[str, Any]:
    scope = trust_gate.route_scope({"route_steps": _ROUTE_STEPS})
    blocked = trust_gate.replay_barrier_gate(records, scope)
    if tamper:
        blocked = blocked[1:]
    return {
        "pathway": ["R", "A", "P"],
        "target": "P",
        "trust_schema": 2,
        "trust": records,
        "route_steps": _ROUTE_STEPS,
        "seeds": seeds,
        "models": ["mace:medium"],
        "trust_summary": {"barrier": {"available": not blocked, "blocked_by": blocked}},
    }


def _meta(results: dict[str, Any], **extra: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "status": "ready",
        "autocatpath_version": "0.39.0",
        "config": {"search": {"energy_thresh": 0.05}},
        "graph": {
            "directed": True,
            "nodes": [{"id": n} for n in _STATES],
            "links": [
                {
                    "source": "R",
                    "target": "A",
                    "barrier": 0.55,
                    "barrier_std": 0.1,
                    "delta_e": -0.1,
                    "kind": "reaction",
                },
                {
                    "source": "A",
                    "target": "P",
                    "barrier": 0.85,
                    "barrier_std": 0.94,
                    "delta_e": -0.2,
                    "kind": "reaction",
                },
            ],
        },
        "results": results,
        "rate_Ea": 1.004,
        "low_confidence": True,
    }
    meta.update(extra)
    return meta


def test_route_scope_derives_nodes_from_steps() -> None:
    steps, nodes = trust_gate.route_scope({"route_steps": ["R->A", "A->P"]})
    assert steps == {"R->A", "A->P"}
    assert nodes == {"R", "A", "P"}
    assert trust_gate.route_scope({}) == (None, set())


def test_replay_reproduces_stored_blocked_by() -> None:
    seeds = [0, 1, 2]
    records = _records(seeds)
    results = _results(records, seeds)
    scope = trust_gate.route_scope(results)
    replay = trust_gate.replay_barrier_gate(records, scope)
    assert replay == results["trust_summary"]["barrier"]["blocked_by"]
    # the fatal off-route fail is never a barrier blocker
    assert not any("Q->Z" in rid for rid in replay)
    assert "R->A#s1#detachment" in replay
    assert "A#s1#endpoint_agreement" in replay


def test_gate_rows_verdicts_and_blocking() -> None:
    seeds = [0, 1, 2]
    records = _records(seeds)
    results = _results(records, seeds)
    scope = trust_gate.route_scope(results)
    rows = trust_gate.gate_rows(
        records, scope, trust_gate.thresholds_from(records, {}), results
    )
    by_gate = {r["gate"]: r for r in rows}
    assert by_gate["endpoint_agreement"]["verdict"] == "fail"
    assert by_gate["endpoint_agreement"]["blocking"] == 1
    assert by_gate["endpoint_agreement"]["threshold"] == "tol 0.05 eV (stamped)"
    assert by_gate["detachment"]["verdict"] == "fail"
    assert by_gate["detachment"]["blocking"] == 1
    assert by_gate["neb_convergence"]["verdict"] == "pass"
    assert by_gate["saddle_verified"]["verdict"] == "pass"
    assert by_gate["multistart"]["verdict"] == "pass"
    # a silent check is not-run with a reason, never pass
    assert by_gate["clash"]["verdict"] == "not-run"
    assert by_gate["clash"]["blocking"] == 0


def test_assumed_tol_when_records_lack_stamp() -> None:
    seeds = [0, 1, 2]
    records = _records(seeds, tol_eV=None)
    results = _results(records, seeds)
    scope = trust_gate.route_scope(results)
    rows = trust_gate.gate_rows(
        records,
        scope,
        trust_gate.thresholds_from(records, {}),
        results,
    )
    ea = next(r for r in rows if r["gate"] == "endpoint_agreement")
    assert ea["threshold"] == "tol 0.05 eV (assumed)"


def test_single_seed_vacuous_pass_is_labelled() -> None:
    records = _records([0], with_detachment=False)
    meta = _meta(_results(records, [0]))
    body = trust_toon(meta, pw_handle="pw1")
    assert "PASS (vacuous" in body
    ea_line = next(
        ln for ln in body.splitlines() if ln.startswith("endpoint_agreement")
    )
    assert "not-run" in ea_line
    assert "1 seed" in ea_line


def test_replay_disagree_names_missing_id() -> None:
    seeds = [0, 1, 2]
    records = _records(seeds)
    meta = _meta(_results(records, seeds, tamper=True))
    body = trust_toon(meta, pw_handle="pw515950", candidate_handle="st336035")
    assert "replay DISAGREES" in body
    assert "A#s1#endpoint_agreement" in body
    assert "BLOCKED" in body


def test_endpoint_table_failing_states_first() -> None:
    seeds = [0, 1, 2]
    records = _records(seeds)
    results = _results(records, seeds)
    scope = trust_gate.route_scope(results)
    rows, folded = trust_gate.endpoint_rows(records, scope)
    assert folded == 2  # R and P agree on one basin each
    assert rows[0]["state"] == "A"
    fail = [r for r in rows if r["verdict"] == "fail"]
    assert len(fail) == 1 and fail[0]["seed"] == 1
    assert fail[0]["Δ meV"] == 866.9
    assert fail[0]["fingerprint"] == _FP_HI


def test_stale_engine_records_never_gate_and_render_stale() -> None:
    seeds = [0, 1, 2]
    records = _records(seeds)
    for r in records:
        if (
            r["check"] in ("endpoint_identity", "endpoint_agreement")
            or r["id"] == "R->A#s1#detachment"
        ):
            r["engine"] = "0.37.0"
    results = _results(records, seeds)
    scope = trust_gate.route_scope(results)
    # every fatal fail is stale → nothing blocks under engine 0.39.0
    assert trust_gate.replay_barrier_gate(records, scope, engine="0.39.0") == []
    # under the records' own engine they gate again — unstamped records
    # count as current everywhere
    blocked_37 = trust_gate.replay_barrier_gate(records, scope, engine="0.37.0")
    assert blocked_37 == ["A#s1#endpoint_agreement", "R->A#s1#detachment"]
    rows = trust_gate.gate_rows(
        records,
        scope,
        trust_gate.thresholds_from(records, {}),
        results,
        engine="0.39.0",
    )
    by_gate = {r["gate"]: r for r in rows}
    ea = by_gate["endpoint_agreement"]
    assert ea["verdict"] == "not-run"
    assert "9 stale, engine 0.37.0" in ea["input"]
    det = by_gate["detachment"]
    assert det["blocking"] == 0
    assert "1 stale, engine 0.37.0" in det["input"]
    erows, folded = trust_gate.endpoint_rows(records, scope, engine="0.39.0")
    assert folded == 0
    assert erows and all(r["verdict"] == "stale, rerun on current" for r in erows)


def test_stale_fail_replay_disagrees_with_stored_summary() -> None:
    seeds = [0, 1, 2]
    records = _records(seeds)
    for r in records:
        if r["id"] == "A#s1#endpoint_agreement":
            r["autocatpath_version"] = "0.37.0"
    meta = _meta(_results(records, seeds))
    body = trust_toon(meta, pw_handle="pw1")
    assert "replay DISAGREES" in body
    assert "A#s1#endpoint_agreement" in body


def test_render_blocked_header_and_sections() -> None:
    seeds = [0, 1, 2]
    records = _records(seeds)
    meta = _meta(_results(records, seeds))
    body = trust_toon(
        meta,
        pw_handle="pw515950",
        candidate={"barrier": 1.004, "barrier_fidelity": "neb"},
        candidate_handle="st336035",
    )
    assert "verdict: BLOCKED · 2 fatal on-route fails" in body
    assert "replay agrees with stored trust_summary" in body
    assert "## endpoint agreement" in body
    assert "gate · pw515950 · candidate st336035" in body
    for section in body.split("\n## "):
        assert len(section) <= 4000
    assert len(body) <= 12000
