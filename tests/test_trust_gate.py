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
    """gr517804 ruling: a 1-seed neb run is no longer a vacuous pass — the
    seed-independence gate blocks it, and the footer advises a rerun with
    ≥2 fresh seeds. The endpoint_agreement row still reads not-run, never
    pass."""
    records = _records([0], with_detachment=False)
    meta = _meta(_results(records, [0]))
    body = trust_toon(meta, pw_handle="pw1")
    assert "verdict: BLOCKED" in body
    assert "seeds:1-independent" in body
    ea_line = next(
        ln for ln in body.splitlines() if ln.startswith("endpoint_agreement")
    )
    assert "not-run" in ea_line
    assert "1 seed" in ea_line
    seed_line = next(
        ln for ln in body.splitlines() if ln.startswith("seed independence")
    )
    assert "fail" in seed_line
    nxt = next(ln for ln in body.splitlines() if ln.startswith("next:"))
    assert "'seeds': [0, 1, 2]" in nxt


def test_run_seeds_prefers_records_and_drops_stale() -> None:
    recs: list[dict[str, Any]] = [
        {"seed": 0},  # unstamped → current
        {"seed": 1, "engine": "0.37.0"},  # stale under 0.39.0
        {"seed": "x"},  # not an int — never a seed
        {"seed": True},  # bool is not a seed
    ]
    assert trust_gate.run_seeds(recs, engine="0.39.0", declared=[9]) == {0}
    # under the records' own engine the stale stamp is current again
    assert trust_gate.run_seeds(recs, engine="0.37.0", declared=[9]) == {0, 1}


def test_run_seeds_declared_fallback_and_unknowable() -> None:
    # record-poor run: the declared list is the only seed evidence
    assert trust_gate.run_seeds([], declared=[0, 2]) == {0, 2}
    assert trust_gate.run_seeds([], declared=None) is None
    assert trust_gate.run_seeds([{"check": "clash"}], declared=None) is None


def test_run_seeds_all_stale_never_falls_back_to_declared() -> None:
    """Seeded records exist but every one is stale → the run sampled
    nothing at this engine: empty set, NOT the declared list (gr517804
    item 1 — a stale run must not inherit its declared seeds)."""
    recs = [
        {"seed": 0, "engine": "0.37.0"},
        {"seed": 1, "autocatpath_version": "0.37.0"},
    ]
    assert trust_gate.run_seeds(recs, engine="0.39.0", declared=[0, 1]) == set()
    # …and the gate fires on it
    assert (
        trust_gate.seed_blocker(recs, engine="0.39.0", declared=[0, 1], tier="neb")
        == "seeds:0-independent"
    )


def test_seed_blocker_neb_only_and_independence() -> None:
    recs = [{"seed": 0}, {"seed": 1}]
    assert trust_gate.seed_blocker(recs, tier="verify") is None  # neb-tier only
    assert trust_gate.seed_blocker(recs, tier="neb") is None  # 2 distinct
    assert trust_gate.seed_blocker([{"seed": 0}], tier="neb") == "seeds:1-independent"
    # union semantics: a sibling's distinct seed pools in; a seed it
    # already ran counts once and never adds independence
    assert trust_gate.seed_blocker([{"seed": 0}], tier="neb", pooled={1}) is None
    assert (
        trust_gate.seed_blocker([{"seed": 0}], tier="neb", pooled={0})
        == "seeds:1-independent"
    )
    # R7: an unknowable seed set is untrusted — a named blocker, not silent
    assert trust_gate.seed_blocker([], declared=None, tier="neb") == "seeds:unknown"
    assert (
        trust_gate.seed_blocker([{"check": "clash"}], declared=None, tier="neb")
        == "seeds:unknown"
    )


def test_seed_row_verdicts() -> None:
    row = trust_gate.seed_row([], declared=None, tier="neb")
    assert (row["verdict"], row["blocking"]) == ("fail", 1)  # unknowable blocks
    assert "unknowable" in row["input"]
    row = trust_gate.seed_row([], tier="screening")
    assert row["verdict"] == "not-run" and "neb-tier only" in row["input"]
    row = trust_gate.seed_row([{"seed": 0}], tier="neb")
    assert row["gate"] == "seed independence (precis)"
    assert (row["verdict"], row["blocking"]) == ("fail", 1)
    # a sibling-pooled distinct seed lifts the union to a pass
    row = trust_gate.seed_row([{"seed": 0}], tier="neb", pooled={1})
    assert row["verdict"] == "pass" and "pooled" in row["input"]
    row = trust_gate.seed_row([{"seed": 0}, {"seed": 1}], tier="neb")
    assert row["verdict"] == "pass" and "2 independent" in row["input"]
    # a sibling-lookup failure renders not-run and blocks, never a pass
    row = trust_gate.seed_row(
        [{"seed": 0}, {"seed": 1}], tier="neb", lookup_failed=True
    )
    assert (row["verdict"], row["blocking"]) == ("not-run", 1)
    assert "lookup failed" in row["input"]


def test_sibling_key_needs_cand_engine_geometry_route() -> None:
    base: dict[str, Any] = {
        "candidate_ref": 5,
        "autocatpath_version": "0.39.0",
        "tier": "neb",
        "reaction_key": "rk",
        "validation_manifest": {"inputs": {"slab_extxyz_sha256": "s"}},
    }
    assert trust_gate.sibling_key(base) == ("5", "0.39.0", "neb", "slab:s", "rk:rk")
    assert trust_gate.sibling_key({**base, "candidate_ref": None}) is None
    assert trust_gate.sibling_key({**base, "autocatpath_version": None}) is None
    # no route identity → no pooling
    assert (
        trust_gate.sibling_key({**base, "reaction_key": None, "config": None}) is None
    )
    # a pre-stamp pathway falls back to its normalized config hash
    keyed = trust_gate.sibling_key({**base, "reaction_key": None, "config": {"a": 1}})
    assert keyed is not None and keyed[4].startswith("cfg:")
    # no manifest sha → the structure_refs map is the geometry identity
    keyed = trust_gate.sibling_key(
        {**base, "validation_manifest": None, "structure_refs": {"P": 9, "R": 7}}
    )
    assert keyed is not None and keyed[3] == "refs:7,9"
    assert (
        trust_gate.sibling_key(
            {**base, "validation_manifest": None, "structure_refs": None}
        )
        is None
    )


class _FakeConn:
    """``pool.connection()`` stand-in: a context manager whose execute
    returns a fixed row list — ``sibling_seeds``' only SQL reads pathway
    ref_ids, which the test controls directly."""

    def __init__(self, rows: list[tuple[int, ...]]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, *_a: Any, **_kw: Any) -> _FakeConn:
        return self

    def fetchall(self) -> list[tuple[int, ...]]:
        return self._rows


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connection(self) -> _FakeConn:
        return self._conn


class _FakeRef:
    def __init__(self, meta: dict[str, Any]) -> None:
        self.meta = meta


class _FakeStore:
    def __init__(
        self, sibling_ids: list[int], metas: dict[int, dict[str, Any]]
    ) -> None:
        self.pool = _FakePool(_FakeConn([(i,) for i in sibling_ids]))
        self._metas = metas

    def fetch_refs_by_ids(self, ids: Any) -> dict[int, _FakeRef]:
        return {i: _FakeRef(self._metas[i]) for i in ids if i in self._metas}


_SIB: dict[str, Any] = {
    "status": "ready",
    "autocatpath_version": "0.39.0",
    "tier": "neb",
    "candidate_ref": 336035,
    "reaction_key": "rk1",
    "validation_manifest": {"inputs": {"slab_extxyz_sha256": "sha1"}},
}


def test_sibling_seeds_pools_ready_same_key_only() -> None:
    """R6: pooling needs the full sibling key — same candidate_ref +
    engine + tier + geometry + route — AND a completed run; a computing
    or failed sibling contributes nothing, and per-sibling staleness
    drops records stamped to another engine."""
    metas: dict[int, dict[str, Any]] = {
        10: {**_SIB, "results": {"seeds": [0, 1]}},  # declared only
        11: {
            **_SIB,
            # record seeds win; s3 stamped to an older engine is stale
            "results": {"trust": [{"seed": 2}, {"seed": 3, "engine": "0.37.0"}]},
        },
        12: {**_SIB, "status": "computing", "results": {"seeds": [9]}},
        13: {**_SIB, "status": "failed", "results": {"seeds": [8]}},
        14: {**_SIB, "reaction_key": "rk2", "results": {"seeds": [6]}},
        15: {
            **_SIB,
            "validation_manifest": {"inputs": {"slab_extxyz_sha256": "shaX"}},
            "results": {"seeds": [5]},
        },
        16: {**_SIB, "tier": "verify", "results": {"seeds": [4]}},
    }
    store = _FakeStore(list(metas), metas)
    out = trust_gate.sibling_seeds(
        store, meta={**_SIB, "results": {}}, exclude_ref_id=99
    )
    assert out == {0, 1, 2}


def test_sibling_seeds_early_out() -> None:
    store = _FakeStore([10], {10: {"results": {"seeds": [0]}}})
    # no candidate_ref / no engine → no sibling group at all
    assert trust_gate.sibling_seeds(store, meta={"results": {}}) == set()
    assert (
        trust_gate.sibling_seeds(store, meta={"candidate_ref": 1, "results": {}})
        == set()
    )
    # cand+engine but no geometry/route provenance → pools with itself only
    assert (
        trust_gate.sibling_seeds(
            store,
            meta={
                "candidate_ref": 1,
                "autocatpath_version": "0.39.0",
                "results": {},
            },
        )
        == set()
    )


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


def test_trust_toon_two_seed_pass_renders_seed_row_pass() -> None:
    records = _records([0, 1], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0, 1]))
    body = trust_toon(meta, pw_handle="pw1")
    row = next(ln for ln in body.splitlines() if ln.startswith("seed independence"))
    assert "pass" in row and "2 independent" in row
    assert "verdict: PASS" in body
    # no rerun advice when the seed gate isn't what's blocking
    assert "next:" not in body


def test_trust_toon_pooled_seeds_form_one_union() -> None:
    """R6: a seed a sibling already ran counts once and never untrusts —
    run {0,1} + sibling {1,2} unions to 3 distinct → PASS, and the row
    says which seeds the pool added."""
    records = _records([0, 1], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0, 1]))
    body = trust_toon(meta, pw_handle="pw515950", pooled_seeds={1, 2})
    assert "verdict: PASS" in body
    row = next(ln for ln in body.splitlines() if ln.startswith("seed independence"))
    assert "pass" in row and "s2 pooled from ready sibling runs" in row


def test_trust_toon_union_shortfall_blocks_and_suggests_fresh() -> None:
    """Run {0} + sibling {0} unions to one distinct seed →
    seeds:1-independent, BLOCKED, and the footer proposes two seed
    numbers beyond everything pooled."""
    records = _records([0], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0]))
    body = trust_toon(meta, pw_handle="pw515950", pooled_seeds={0})
    assert "verdict: BLOCKED" in body
    assert "seeds:1-independent" in body
    nxt = next(ln for ln in body.splitlines() if ln.startswith("next:"))
    assert "'seeds': [0, 1, 2]" in nxt


def test_trust_toon_unknowable_seed_set_is_blocked() -> None:
    """R7: no record seed fields and no declared list → seeds:unknown,
    never a vacuous pass."""
    records = _records([0, 1], hi_states=(), with_detachment=False)
    for r in records:
        del r["seed"]
    results = _results(records, [])
    del results["seeds"]
    body = trust_toon(_meta(results), pw_handle="pw1")
    assert "seeds:unknown" in body
    assert "verdict: BLOCKED" in body
    row = next(ln for ln in body.splitlines() if ln.startswith("seed independence"))
    assert "fail" in row and "unknowable" in row


def test_trust_toon_lookup_failed_never_renders_pass() -> None:
    """Item 10: a sibling-lookup failure renders the seed gate
    not-run (lookup failed) and the verdict never PASS."""
    records = _records([0, 1], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0, 1]))
    body = trust_toon(meta, pw_handle="pw1", seed_lookup_failed=True)
    assert "verdict: PASS" not in body
    assert "seeds:lookup-failed" in body
    row = next(ln for ln in body.splitlines() if ln.startswith("seed independence"))
    assert "not-run" in row and "lookup failed" in row
    # a lookup failure needs a view retry, not a rerun — no seeds advice
    assert "next:" not in body


def _ident(
    state: str, seed: int, e_ev: float, *, engine: str | None = None
) -> dict[str, Any]:
    """One ``endpoint_identity`` record — the unit the union replay pools."""
    rec = _rec(
        "endpoint_identity",
        "pass",
        "warn",
        seed=seed,
        state=state,
        evidence={"e_eV": e_ev, "fingerprint": _FP_LO},
    )
    if engine is not None:
        rec["engine"] = engine
    return rec


def test_endpoint_union_blocker_agree_disagree_notrun() -> None:
    """Round-2 R1: pooled seeds must AGREE at the endpoints — the merged
    endpoint_identity evidence is replayed state by state."""
    scope = trust_gate.route_scope({"route_steps": ["R->A", "A->P"]})
    own = [_ident(st, 0, -10.0) for st in _STATES]
    sib = [_ident(st, 1, -10.0) for st in _STATES]
    blk, text = trust_gate.endpoint_union_blocker(
        own, sib, scope, run={0}, union={0, 1}, engine="0.39.0", tol_eV=0.05
    )
    assert blk is None and "3 route states agree" in text
    # a 900 meV spread on one state fails the union
    sib_bad = [_ident("R", 1, -10.0), _ident("A", 1, -9.1), _ident("P", 1, -10.0)]
    blk, text = trust_gate.endpoint_union_blocker(
        own, sib_bad, scope, run={0}, union={0, 1}, engine="0.39.0", tol_eV=0.05
    )
    assert blk == "endpoint:union-disagree" and "A endpoints 900 meV" in text
    # no cross-seed pair anywhere → unprovable, not a pass
    blk, text = trust_gate.endpoint_union_blocker(
        own, [], scope, run={0}, union={0, 1}, engine="0.39.0", tol_eV=0.05
    )
    assert blk == "endpoint:union-not-run" and "no cross-seed pair" in text
    # no identity records at all → the same, named differently
    blk, text = trust_gate.endpoint_union_blocker(
        [], [], scope, run={0}, union={0, 1}, engine="0.39.0", tol_eV=0.05
    )
    assert blk == "endpoint:union-not-run" and "no endpoint_identity" in text


def test_endpoint_union_blocker_stale_offroute_and_own_run() -> None:
    """Stale sibling records never enter the replay; an off-route state
    is out of scope; a run standing on ≥2 own seeds needs no replay."""
    scope = trust_gate.route_scope({"route_steps": ["R->A", "A->P"]})
    own = [_ident(st, 0, -10.0) for st in _STATES]
    # stale (old engine) + off-route (Z) sibling records count for nothing
    sib = [
        _ident("A", 1, -9.0, engine="0.37.0"),
        _ident("Z", 1, -5.0),
    ]
    blk, _text = trust_gate.endpoint_union_blocker(
        own, sib, scope, run={0}, union={0, 1}, engine="0.39.0", tol_eV=0.05
    )
    assert blk == "endpoint:union-not-run"
    # run ≥2 own seeds: the engine's own agreement governs — no replay fail
    blk, _text = trust_gate.endpoint_union_blocker(
        own, sib, scope, run={0, 1}, union={0, 1}, engine="0.39.0", tol_eV=0.05
    )
    assert blk is None


def test_sibling_evidence_pools_seeds_and_identity() -> None:
    """The second half of R1: ready same-key siblings contribute their
    non-stale endpoint_identity records, each tagged by a seed the
    sibling actually ran."""
    ident = [_ident(st, 2, -10.0) for st in _STATES]
    metas: dict[int, dict[str, Any]] = {
        10: {
            **_SIB,
            "results": {
                "seeds": [2],
                "trust": [
                    *ident,
                    # stale at this engine → dropped, seed and all
                    _ident("A", 3, -9.0, engine="0.37.0"),
                ],
            },
        },
        11: {**_SIB, "status": "computing", "results": {"seeds": [5]}},
    }
    store = _FakeStore(list(metas), metas)
    seeds, identity = trust_gate.sibling_evidence(
        store, meta={**_SIB, "results": {}}, exclude_ref_id=99
    )
    assert seeds == {2}
    assert identity == ident


def test_trust_toon_union_disagree_blocks() -> None:
    """The endpoint_agreement row reflects the union replay: a sibling
    pool whose seeds disagree forces BLOCKED even when the engine's own
    records were clean."""
    records = _records([0], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0]))
    sib = [
        _ident("R", 1, -10.0),
        _ident("A", 1, -9.1),
        _ident("P", 1, -10.0),
    ]
    body = trust_toon(meta, pw_handle="pw1", pooled_seeds={1}, pooled_identity=sib)
    assert "verdict: BLOCKED" in body
    assert "endpoint:union-disagree" in body
    ea_line = next(
        ln for ln in body.splitlines() if ln.startswith("endpoint_agreement")
    )
    assert "fail" in ea_line and "union replay" in ea_line


def test_trust_toon_union_agree_lifts_vacuous_row() -> None:
    """A vacuous own endpoint check the union answered renders the
    union's pass — never a bare not-run."""
    records = _records([0], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0]))
    sib = [_ident(st, 1, -10.0) for st in _STATES]
    body = trust_toon(meta, pw_handle="pw1", pooled_seeds={1}, pooled_identity=sib)
    assert "verdict: PASS" in body
    assert "vacuous" not in body
    ea_line = next(
        ln for ln in body.splitlines() if ln.startswith("endpoint_agreement")
    )
    assert "pass" in ea_line and "union replay" in ea_line


def test_trust_toon_union_not_run_blocks() -> None:
    """Two vacuous 1-seed passes pooled: no cross-seed endpoint evidence
    → endpoint:union-not-run, BLOCKED."""
    records = _records([0], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0]))
    body = trust_toon(meta, pw_handle="pw1", pooled_seeds={1}, pooled_identity=[])
    assert "verdict: BLOCKED" in body
    assert "endpoint:union-not-run" in body
