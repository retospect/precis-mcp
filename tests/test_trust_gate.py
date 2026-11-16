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

import pytest

from precis.utils import trust_gate
from precis_pathway.toon_views import compare_toon, gate_verdict, row_anchor, trust_toon


@pytest.fixture(autouse=True)
def _pin_engine_version(monkeypatch: Any) -> None:
    """gr518351: view='trust' demotes a pathway minted on an engine older
    than the env-or-pin to ``engine:stale``. Every ``_meta`` here stamps
    ``0.39.0`` — pin the env override to match so the gates under test
    stay reachable (the real dist pin sits above it)."""
    from precis.quest import compute as compute_mod

    monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.39.0")


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
    # gr593139: seed 1's higher-fingerprint landing is a distinct minimum —
    # landscape information, demoted out of the blocked set
    assert "A#s1#endpoint_agreement" not in replay


def test_gate_rows_verdicts_and_blocking() -> None:
    seeds = [0, 1, 2]
    records = _records(seeds)
    results = _results(records, seeds)
    scope = trust_gate.route_scope(results)
    rows = trust_gate.gate_rows(
        records, scope, trust_gate.thresholds_from(records, {}), results
    )
    by_gate = {r["gate"]: r for r in rows}
    # gr593139: seed 1's distinct minimum demotes — the row reads warn,
    # nothing blocks, and the input reports the minima (never "split")
    assert by_gate["endpoint_agreement"]["verdict"] == "warn"
    assert by_gate["endpoint_agreement"]["blocking"] == 0
    assert "2 minima found on A" in by_gate["endpoint_agreement"]["input"]
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
    assert "R->A#s1#detachment" in body


def test_endpoint_table_failing_states_first() -> None:
    """Each minimum is its own state entry (gr593139): the split state
    still leads the table, but seed 1's landing renders as its own
    ``m1`` minimum — ``distinct minimum``, never a gating fail."""
    seeds = [0, 1, 2]
    records = _records(seeds)
    results = _results(records, seeds)
    scope = trust_gate.route_scope(results)
    rows, folded = trust_gate.endpoint_rows(records, scope)
    assert folded == 2  # R and P agree on one basin each
    assert rows[0]["state"] == "A"
    assert {r["min"] for r in rows} == {"m0", "m1"}
    distinct = [r for r in rows if r["verdict"] == "distinct minimum"]
    assert len(distinct) == 1 and distinct[0]["seed"] == 1
    assert distinct[0]["min"] == "m1"
    assert distinct[0]["Δ meV"] == 866.9
    assert distinct[0]["fingerprint"] == _FP_HI
    basin = [r for r in rows if r["verdict"] == "pass"]
    assert len(basin) == 2 and all(r["min"] == "m0" for r in basin)


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
    # count as current everywhere; the endpoint fail still demotes
    # (gr593139 distinct minimum — demotion is vintage-independent)
    blocked_37 = trust_gate.replay_barrier_gate(records, scope, engine="0.37.0")
    assert blocked_37 == ["R->A#s1#detachment"]
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
    """A stored summary minted under the pre-gr593139 rule still names the
    endpoint fail — the current replay drops it (stale AND a demoted
    distinct minimum), and the DISAGREES line says so."""
    seeds = [0, 1, 2]
    records = _records(seeds)
    for r in records:
        if r["id"] == "A#s1#endpoint_agreement":
            r["autocatpath_version"] = "0.37.0"
    results = _results(records, seeds)
    results["trust_summary"]["barrier"] = {
        "available": False,
        "blocked_by": ["A#s1#endpoint_agreement", "R->A#s1#detachment"],
    }
    body = trust_toon(_meta(results), pw_handle="pw1")
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
    # gr593139: the distinct-minimum endpoint fail demotes — only the
    # detachment still counts as a fatal on-route fail
    assert "verdict: BLOCKED · 1 fatal on-route fails" in body
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


@pytest.mark.parametrize(
    ("requested", "executed", "usable", "verdict", "note"),
    [
        pytest.param(
            [2, 3],
            [1],
            [1],
            "BLOCKED",
            "(requested [2,3], executed [1]); dispatch dropped or substituted seeds",
            id="substituted-single-seed",
        ),
        pytest.param(
            [2, 3],
            [2, 3],
            [2, 3],
            "PASS",
            "(requested [2,3], executed [2,3])",
            id="matching-two-seeds",
        ),
        pytest.param(
            [2, 3],
            [2, 3],
            [2],
            "BLOCKED",
            "(requested [2,3], executed [2,3])",
            id="started-is-not-usable",
        ),
        pytest.param(
            None,
            None,
            [2, 3],
            "PASS",
            "(requested unknown, executed unknown)",
            id="historical-unknown",
        ),
        pytest.param(
            [2, 3],
            [3, 2],
            [2, 3],
            "PASS",
            "(requested [2,3], executed [3,2])",
            id="order-is-not-substitution",
        ),
        pytest.param(
            [2, 3],
            [1, 2],
            [1, 2],
            "PASS",
            "(requested [2,3], executed [1,2]); dispatch dropped or substituted seeds",
            id="diagnostics-do-not-change-trust",
        ),
    ],
)
def test_trust_toon_reports_seed_dispatch_without_changing_independence(
    requested: list[int] | None,
    executed: list[int] | None,
    usable: list[int],
    verdict: str,
    note: str,
) -> None:
    """gr525725: starts diagnose dispatch; usable trust records decide independence."""
    records = _records(usable, hi_states=(), with_detachment=False)
    meta = _meta(_results(records, usable))
    if requested is not None:
        meta["requested_seeds"] = requested
    if executed is not None:
        meta["executed_seeds"] = executed

    body = trust_toon(meta, pw_handle="pw1")

    verdict_line = next(
        line for line in body.splitlines() if line.startswith("verdict:")
    )
    seed_line = next(
        line for line in body.splitlines() if line.startswith("seed independence")
    )
    assert f"verdict: {verdict}" in verdict_line
    assert note in verdict_line
    assert note in seed_line
    if verdict == "BLOCKED":
        assert f"seeds:1-independent {note}" in verdict_line
        assert "fail" in seed_line
    else:
        assert "pass" in seed_line
        assert "seeds:1-independent" not in body
    if "substituted seeds" not in note:
        assert "dispatch dropped or substituted seeds" not in body


def test_seed_execution_note_absent_metadata_is_unknown() -> None:
    assert (
        trust_gate.seed_execution_note(None) == "(requested unknown, executed unknown)"
    )
    assert trust_gate.seed_execution_note({"results": {"seeds": [2, 3]}}) == (
        "(requested unknown, executed unknown)"
    )


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


def test_trust_toon_stale_engine_row_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr518351 ruling 2: view='trust' answers like the harvest — a
    pathway whose ``autocatpath_version`` sits under the env-or-pin
    renders the ``engine staleness (precis)`` row failed, names
    ``engine:stale`` in blocked_by, and the verdict is BLOCKED even on
    clean records."""
    from precis.quest import compute as compute_mod

    monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.40.0")
    records = _records([0, 1], hi_states=(), with_detachment=False)
    body = trust_toon(_meta(_results(records, [0, 1])), pw_handle="pw1")
    assert "verdict: BLOCKED" in body
    assert "blocked_by: engine:stale" in body
    row = next(ln for ln in body.splitlines() if ln.startswith("engine staleness"))
    assert "fail" in row and "0.39.0" in row and "0.40.0" in row


def test_trust_toon_missing_engine_stamp_is_notrun() -> None:
    """A pre-stamp pathway carries no ``autocatpath_version`` — the row
    reads not-run (harvest parity: absence is not stale) and never
    blocks on its own."""
    records = _records([0, 1], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0, 1]))
    del meta["autocatpath_version"]
    body = trust_toon(meta, pw_handle="pw1")
    row = next(ln for ln in body.splitlines() if ln.startswith("engine staleness"))
    assert "not-run" in row
    assert "engine:stale" not in body


@pytest.mark.parametrize("version", ["0.50.0", None, "", 50])
def test_kinetics_staleness_prefers_record_version(version: Any) -> None:
    meta = {
        "autocatpath_version": "0.39.0",
        "results": {"kinetics_v2": {"engine_version": version}},
    }
    engine = trust_gate.kinetics_engine_version(meta)
    assert engine == ("0.50.0" if version == "0.50.0" else "0.39.0")
    assert trust_gate.engine_version_stale(engine, "0.50.0") is (version != "0.50.0")
    assert trust_gate.kinetics_engine_version({}) is None


def test_record_engine_recognizes_new_engine_version_stamp() -> None:
    assert (
        trust_gate.record_engine(
            {
                "engine_version": "0.50.0",
                "autocatpath_version": "0.39.0",
            }
        )
        == "0.50.0"
    )


def test_kinetics_recompute_does_not_refresh_barrier_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.quest import compute as compute_mod

    monkeypatch.setenv(compute_mod._AUTOCATPATH_VERSION_ENV, "0.50.0")
    records = _records([0, 1], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0, 1]))
    meta["results"]["kinetics_v2"] = {"engine_version": "0.50.0", "tof_mf": 1.0}
    body = trust_toon(meta, pw_handle="pw1")
    assert "verdict: BLOCKED" in body and "blocked_by: engine:stale" in body


def _ident(
    state: str,
    seed: int,
    e_ev: float,
    *,
    engine: str | None = None,
    fp: str = _FP_LO,
) -> dict[str, Any]:
    """One ``endpoint_identity`` record — the unit the union replay pools."""
    rec = _rec(
        "endpoint_identity",
        "pass",
        "warn",
        seed=seed,
        state=state,
        evidence={"e_eV": e_ev, "fingerprint": fp},
    )
    if engine is not None:
        rec["engine"] = engine
    return rec


def test_endpoint_minima_cluster_and_distinct_demotion() -> None:
    """gr593139: endpoint_minima groups each seed's lowest-energy landing
    by fingerprint; a seed provably in a non-basin minimum is
    ``distinct_minimum`` — demoted out of the blocked set — while a
    same-minimum spread keeps gating."""
    seeds = [0, 1, 2]
    records = _records(seeds)
    scope = trust_gate.route_scope({"route_steps": _ROUTE_STEPS})
    minima = trust_gate.endpoint_minima(records, scope)
    assert set(minima["A"]) == {_FP_LO, _FP_HI}
    assert minima["A"][_FP_LO] == {0: -10.0, 2: -10.0}
    assert minima["A"][_FP_HI] == {1: -9.1331}
    assert minima["R"] == {_FP_LO: {0: -10.0, 1: -10.0, 2: -10.0}}
    assert trust_gate.minimum_basin_fp(minima["A"]) == _FP_LO
    bad = next(r for r in records if r["id"] == "A#s1#endpoint_agreement")
    assert trust_gate.distinct_minimum(bad, minima)
    basin_rec = next(r for r in records if r["id"] == "A#s0#endpoint_agreement")
    assert not trust_gate.distinct_minimum(basin_rec, minima)


def test_step_landings_do_not_count_as_independent_seeds() -> None:
    """Two steps at one minimum retain evidence but cannot prove seed agreement."""
    records = [{**_ident("A", 0, -10.0), "step": step} for step in ("R->A", "A->P")]
    scope: tuple[None, set[str]] = (None, set())
    minima = trust_gate.endpoint_minima(records, scope)
    assert minima["A"][_FP_LO] == {(0, "R->A"): -10.0, (0, "A->P"): -10.0}
    blocker, text = trust_gate.endpoint_union_blocker(
        records, [], scope, union={0, 1}, run={0}, tol_eV=0.05
    )
    assert blocker == "endpoint:union-not-run"
    assert "no cross-seed pair on A" in text


def test_same_minimum_spread_still_gates() -> None:
    """The other half of the ruling: seeds sharing ONE fingerprint beyond
    tol are a real disagreement — the fail stays fatal in the replay."""
    scope = trust_gate.route_scope({"route_steps": _ROUTE_STEPS})
    records = [
        _ident("A", 0, -10.0),
        _ident("A", 1, -9.1),  # same fingerprint, 900 meV apart
        _rec(
            "endpoint_agreement",
            "fail",
            "fatal",
            seed=1,
            state="A",
            evidence={"delta_meV": 900.0, "tol_eV": 0.05},
        ),
    ]
    minima = trust_gate.endpoint_minima(records, scope)
    assert set(minima["A"]) == {_FP_LO}  # ONE minimum — this is a disagreement
    assert "A#s1#endpoint_agreement" in trust_gate.replay_barrier_gate(records, scope)


def _within_min_records() -> list[dict[str, Any]]:
    """Two seeds sharing ONE non-basin fingerprint but disagreeing inside
    it (−9.0 vs −8.8 eV against a −10.0 eV basin, tol 0.05): a 200 meV
    within-minimum failure the demotion must not hide."""
    return [
        *[_ident("R", s, -10.0) for s in (0, 1, 2)],
        _ident("A", 0, -10.0),  # basin m0
        _ident("A", 1, -9.0, fp=_FP_HI),  # m1's own reference
        _ident("A", 2, -8.8, fp=_FP_HI),  # m1, +200 meV inside it
        _rec(
            "endpoint_agreement",
            "fail",
            "fatal",
            seed=1,
            state="A",
            evidence={
                "e_eV": -9.0,
                "e_min_eV": -10.0,
                "delta_meV": 1000.0,
                "tol_eV": 0.05,
            },
        ),
        _rec(
            "endpoint_agreement",
            "fail",
            "fatal",
            seed=2,
            state="A",
            evidence={
                "e_eV": -8.8,
                "e_min_eV": -10.0,
                "delta_meV": 1200.0,
                "tol_eV": 0.05,
            },
        ),
    ]


def test_within_minimum_disagreement_still_gates() -> None:
    """gr593139's second half: ``distinct_minimum`` alone is not enough —
    demotion requires the seed's own minimum to AGREE. s2 sits 200 meV
    above m1's reference: a real disagreement that keeps gating, while
    s1 — at the m1 reference itself — demotes."""
    scope = trust_gate.route_scope({"route_steps": _ROUTE_STEPS})
    records = _within_min_records()
    minima = trust_gate.endpoint_minima(records, scope)
    assert set(minima["A"]) == {_FP_LO, _FP_HI}
    assert minima["A"][_FP_HI] == {1: -9.0, 2: -8.8}
    blocked = trust_gate.replay_barrier_gate(records, scope)
    assert "A#s2#endpoint_agreement" in blocked  # within-minimum failure
    assert "A#s1#endpoint_agreement" not in blocked  # m1 agrees internally


def test_mint_demotion_preserves_within_minimum_failures() -> None:
    """Same at mint: ``_demote_endpoint_minima`` recomputes every
    non-basin record against its OWN minimum's reference — s2's +200 meV
    inside m1 is a real fail with a rewritten delta, s1's reference
    landing demotes. The basin-judged values keep their provenance."""
    from precis_pathway.runner import _demote_endpoint_minima

    records = _within_min_records()

    class _Results:
        trust = [dict(r, evidence=dict(r["evidence"])) for r in records]

    results = _Results()
    _demote_endpoint_minima(results)
    by_id = {r["id"]: r for r in results.trust}
    s2 = by_id["A#s2#endpoint_agreement"]
    assert s2["verdict"] == "fail" and s2["severity"] == "fatal"
    assert s2["evidence"]["delta_meV"] == 200.0  # vs m1's own reference
    assert s2["evidence"]["e_min_eV"] == -9.0
    assert s2["evidence"]["basin_delta_meV"] == 1200.0
    assert s2["evidence"]["basin_e_min_eV"] == -10.0
    assert s2["evidence"]["minimum"] == "m1"
    assert "distinct_minimum" not in s2["evidence"]
    s1 = by_id["A#s1#endpoint_agreement"]
    assert s1["verdict"] == "marginal" and s1["severity"] == "warn"
    assert s1["evidence"]["distinct_minimum"] is True
    assert s1["evidence"]["delta_meV"] == 0.0
    assert s1["evidence"]["basin_delta_meV"] == 1000.0
    # and replay over the minted records agrees with the stored verdicts
    scope = trust_gate.route_scope({"route_steps": _ROUTE_STEPS})
    assert trust_gate.replay_barrier_gate(results.trust, scope) == [
        "A#s2#endpoint_agreement"
    ]


def test_endpoint_minima_payload_state_entries_and_barriers() -> None:
    """The artifact half of gr593139: ``_endpoint_minima_payload`` gives
    each non-basin minimum its own ``state@mK`` extxyz entry and groups
    every seed's step barrier by the ``mA->mB`` pair its endpoints sat
    in — the barrier-per-minimum the pooled edge cannot answer."""
    from precis_pathway.runner import _endpoint_minima_payload

    records = [
        _ident("R", 0, -10.0),
        _ident("R", 1, -10.0),
        _ident("A", 0, -10.0),
        _ident("A", 1, -9.1, fp=_FP_HI),
        _rec(
            "endpoint_agreement",
            "pass",
            "fatal",
            seed=0,
            state="A",
            evidence={"tol_eV": 0.05},
        ),
        _rec(
            "endpoint_agreement",
            "fail",
            "fatal",
            seed=1,
            state="A",
            evidence={"tol_eV": 0.05, "delta_meV": 900.0},
        ),
    ]

    class _Results:
        trust = records

    units = [
        {
            "seed": 0,
            "model": "emt",
            "model_index": 0,
            "partial": {
                "steps": {
                    "R->A": {
                        "reactant": "R",
                        "product": "A",
                        "barrier": 0.4,
                        "delta_e": -0.1,
                    }
                }
            },
            "structures": {
                "R": {"energy": -10.0, "extxyz": "R0"},
                "A": {"energy": -10.0, "extxyz": "A-basin"},
            },
        },
        {
            "seed": 1,
            "model": "emt",
            "model_index": 0,
            "partial": {
                "steps": {
                    "R->A": {
                        "reactant": "R",
                        "product": "A",
                        "barrier": 0.7,
                        "delta_e": 0.9,
                    }
                }
            },
            "structures": {
                "R": {"energy": -10.0, "extxyz": "R1"},
                "A": {"energy": -9.1, "extxyz": "A-second"},
            },
        },
    ]
    extra, block, wiring = _endpoint_minima_payload(_Results(), units)
    assert extra == {"A@m1": "A-second"}  # the basin keeps "A"
    assert block["model"] == "emt"
    a_minima = block["states"]["A"]
    assert a_minima["m0"]["fingerprint"] == _FP_LO
    assert a_minima["m0"]["basin"] is True
    assert a_minima["m1"]["fingerprint"] == _FP_HI
    assert a_minima["m1"]["seeds"] == [1]
    assert a_minima["m1"]["state_entry"] == "A@m1"
    pairs = block["barriers"]["R->A"]
    assert pairs["m0->m0"]["seeds"] == [0]
    assert pairs["m0->m0"]["barrier"] == 0.4
    assert pairs["m0->m1"]["seeds"] == [1]
    assert pairs["m0->m1"]["barrier"] == 0.7
    assert pairs["m0->m1"]["delta_e"] == 0.9


def test_step_identity_assigns_barrier_pairs() -> None:
    """Step-specific landings: the same state can relax differently on
    adjacent steps — seed 1's ``A`` lands in the non-basin fingerprint
    on ``R->A`` but the basin fingerprint on ``A->P`` (its collapsed
    lowest pick). The collapsed pick would mislabel ``R->A``'s barrier
    ``m0->m0``; the step's own ``endpoint_identity`` record says
    ``m0->m1``."""
    from precis_pathway.runner import _endpoint_minima_payload

    def ident(state: str, seed: int, e_ev: float, step: str, fp: Any = _FP_LO):
        rec = _ident(state, seed, e_ev, fp=fp)
        rec["step"] = step
        return rec

    records = [
        ident("R", 0, -10.0, "R->A"),
        ident("R", 1, -10.0, "R->A"),
        ident("A", 0, -10.0, "R->A"),
        ident("A", 1, -9.1, "R->A", fp=_FP_HI),  # non-basin on THIS step
        ident("A", 1, -10.0, "A->P"),  # basin pick collapsed (lower e)
        ident("P", 1, -10.0, "A->P"),
    ]

    class _Results:
        trust = records

    units = [
        {
            "seed": s,
            "model": "emt",
            "model_index": 0,
            "partial": {
                "states": {"R": -10.0, "A": -10.0 if s == 0 else -9.1, "P": -10.0},
                "steps": {
                    "R->A": {
                        "reactant": "R",
                        "product": "A",
                        "barrier": 0.4 if s == 0 else 0.7,
                        "delta_e": -0.1 if s == 0 else 0.9,
                    },
                    "A->P": {
                        "reactant": "A",
                        "product": "P",
                        "barrier": 0.5,
                        "delta_e": -0.2,
                    },
                },
            },
        }
        for s in (0, 1)
    ]
    _extra, block, _wiring = _endpoint_minima_payload(_Results(), units)
    pairs = block["barriers"]["R->A"]
    # judged by the step record, not the collapsed basin pick
    assert pairs["m0->m1"]["seeds"] == [1]
    assert pairs["m0->m1"]["barrier"] == 0.7
    assert pairs["m0->m0"]["seeds"] == [0]
    # and on A->P the SAME seed pairs basin->basin
    assert block["barriers"]["A->P"]["m0->m0"]["seeds"] == [1]


def test_wire_minima_network_repools_nodes_edges_graph() -> None:
    """The consumed-network half of the re-review: after demotion, the
    ``nodes``/``edges``/``graph_json`` summarize and kinetics read carry
    a ``A@m1`` node and a ``R->A@m1`` edge pooled over the second
    minimum's seeds alone — the canonical entries keep only what is
    left."""
    from precis_pathway.runner import (
        _endpoint_minima_payload,
        _wire_minima_network,
    )

    records = [
        _ident("R", 0, -10.0),
        _ident("R", 1, -10.0),
        _ident("A", 0, -10.0),
        _ident("A", 1, -9.1, fp=_FP_HI),
    ]

    class _Results:
        trust = records

    units = [
        {
            "seed": s,
            "model": "emt",
            "model_index": 0,
            "partial": {
                "states": {"R": -10.0, "A": -10.0 if s == 0 else -9.1},
                "steps": {
                    "R->A": {
                        "reactant": "R",
                        "product": "A",
                        "barrier": 0.4 if s == 0 else 0.7,
                        "delta_e": -0.1 if s == 0 else 0.9,
                    }
                },
            },
        }
        for s in (0, 1)
    ]
    _extra, _block, wiring = _endpoint_minima_payload(_Results(), units)
    results_json: dict[str, Any] = {
        "pathway": ["R"],
        "nodes": {
            "R": {
                "mean": -10.0,
                "std": 0.0,
                "n": 2,
                "values": [-10.0, -10.0],
                "low_confidence": False,
                "reasons": [],
            },
            "A": {
                "mean": -9.55,
                "std": 0.636,
                "n": 2,
                "values": [-10.0, -9.1],
                "low_confidence": False,
                "reasons": [],
            },
        },
        "edges": [
            {
                "name": "R->A",
                "reactant": "R",
                "product": "A",
                "barrier": {
                    "mean": 0.55,
                    "std": 0.212,
                    "n": 2,
                    "values": [0.4, 0.7],
                    "low_confidence": False,
                    "reasons": [],
                },
                "delta_e": {
                    "mean": 0.4,
                    "std": 0.707,
                    "n": 2,
                    "values": [-0.1, 0.9],
                    "low_confidence": False,
                    "reasons": [],
                },
            }
        ],
    }
    graph_json: dict[str, Any] = {
        "nodes": [
            {
                "id": "R",
                "energy": 0.0,
                "rel_energy": 0.0,
                "energy_std": 0.0,
                "low_confidence": False,
                "n_H": 0.0,
                "gas_note": "x",
            },
            {
                "id": "A",
                "energy": -9.55,
                "rel_energy": -9.55,
                "energy_std": 0.636,
                "low_confidence": False,
                "n_H": 0.0,
                "gas_note": "x",
            },
        ],
        "links": [
            {
                "kind": "reaction",
                "name": "R->A",
                "source": "R",
                "target": "A",
                "barrier": 0.55,
                "barrier_std": 0.212,
                "delta_e": 0.4,
                "delta_e_std": 0.707,
                "low_confidence": False,
            }
        ],
    }
    _wire_minima_network(results_json, graph_json, units, wiring, energy_thresh=0.3)

    # canonical entries re-pool to the basin members that remain
    assert results_json["nodes"]["A"]["values"] == [-10.0]
    assert results_json["nodes"]["A"]["n"] == 1
    assert results_json["nodes"]["R"]["values"] == [-10.0, -10.0]
    am1 = results_json["nodes"]["A@m1"]
    assert am1["mean"] == -9.1 and am1["values"] == [-9.1]
    by_name = {e["name"]: e for e in results_json["edges"]}
    assert by_name["R->A"]["barrier"]["values"] == [0.4]
    assert by_name["R->A"]["delta_e"]["values"] == [-0.1]
    new = by_name["R->A@m1"]
    assert new["reactant"] == "R" and new["product"] == "A@m1"
    assert new["barrier"]["mean"] == 0.7 and new["barrier"]["n"] == 1
    assert new["delta_e"]["mean"] == 0.9
    # single-member pools flag honestly, not a fabricated precise number
    assert new["barrier"]["low_confidence"] is True

    gnodes = {n["id"]: n for n in graph_json["nodes"]}
    assert gnodes["A@m1"]["energy"] == -9.1
    assert gnodes["A@m1"]["rel_energy"] == -9.1 - gnodes["R"]["energy"]
    assert gnodes["A@m1"]["n_H"] == 0.0  # canonical metadata carries over
    assert gnodes["A"]["energy"] == -10.0
    glinks = {l["name"]: l for l in graph_json["links"]}
    assert glinks["R->A"]["barrier"] == 0.4
    assert glinks["R->A@m1"]["target"] == "A@m1"
    assert glinks["R->A@m1"]["barrier"] == 0.7
    assert glinks["R->A@m1"]["delta_e"] == 0.9


def test_union_blocker_judges_per_minimum() -> None:
    """Union replay under gr593139: seeds in DISTINCT minima are no
    disagreement — but a pooled run that never shares a minimum is
    unprovable, and a spread inside one shared minimum still fails."""
    scope = trust_gate.route_scope({"route_steps": ["R->A", "A->P"]})
    own = [_ident(st, 0, -10.0) for st in _STATES]
    sib_distinct = [
        _ident("R", 1, -10.0),
        _ident("A", 1, -9.1, fp=_FP_HI),  # a SECOND minimum on A, not a split
        _ident("P", 1, -10.0),
    ]
    # run<2 and no same-minimum pair on A → unprovable, minima reported
    blk, text = trust_gate.endpoint_union_blocker(
        own, sib_distinct, scope, run={0}, union={0, 1}, engine="0.39.0", tol_eV=0.05
    )
    assert blk == "endpoint:union-not-run"
    assert "no cross-seed pair on A" in text and "2 minima found" in text
    # run≥2 → the engine's own agreement governs; minima still reported
    blk, text = trust_gate.endpoint_union_blocker(
        own,
        sib_distinct,
        scope,
        run={0, 1},
        union={0, 1},
        engine="0.39.0",
        tol_eV=0.05,
    )
    assert blk is None and "2 minima found on A" in text
    # a second minimum TWO seeds share and agree inside → it judges
    # internally (the singleton basin doesn't decide anything)
    blk, text = trust_gate.endpoint_union_blocker(
        own + [_ident("A", 2, -9.05, fp=_FP_HI)],
        sib_distinct,
        scope,
        run={0, 2},
        union={0, 1, 2},
        engine="0.39.0",
        tol_eV=0.05,
    )
    assert blk is None
    assert "3 route states agree" in text and "2 minima found on A" in text


def test_mint_demotion_stamps_minima_and_unblocks() -> None:
    """The mint pass ``runner._demote_endpoint_minima`` rewrites a
    distinct-minimum endpoint fail to marginal/warn — the stored
    trust_summary never blocks on it — and stamps the ``minimum``/
    ``n_minima`` evidence the trust view renders."""
    from precis_pathway.runner import _demote_endpoint_minima

    seeds = [0, 1, 2]
    records = _records(seeds)

    class _Results:
        trust = [dict(r, evidence=dict(r["evidence"])) for r in records]

    results = _Results()
    _demote_endpoint_minima(results)
    by_id = {r["id"]: r for r in results.trust}
    demoted = by_id["A#s1#endpoint_agreement"]
    assert demoted["verdict"] == "marginal" and demoted["severity"] == "warn"
    assert demoted["evidence"]["distinct_minimum"] is True
    assert demoted["evidence"]["n_minima"] == 2
    assert by_id["A#s0#endpoint_agreement"]["verdict"] == "pass"
    idents = {
        r["seed"]: r
        for r in results.trust
        if r["check"] == "endpoint_identity" and r["state"] == "A"
    }
    assert idents[1]["evidence"]["minimum"] == "m1"
    assert idents[0]["evidence"]["minimum"] == "m0"
    assert idents[0]["evidence"]["n_minima"] == 2


def test_trust_toon_split_reports_minima_not_blocked() -> None:
    """gr593139's whole point: a fingerprinted basin split no longer
    zeroes the barrier's trust — PASS with the minima named, never
    "split"."""
    records = _records([0, 1], hi_states=("A",), with_detachment=False)
    meta = _meta(_results(records, [0, 1]))
    body = trust_toon(meta, pw_handle="pw1")
    assert "verdict: PASS" in body
    assert "2 minima found on A" in body
    assert "distinct minimum" in body
    assert "split" not in body
    ea_line = next(
        ln for ln in body.splitlines() if ln.startswith("endpoint_agreement")
    )
    assert "warn" in ea_line


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


def test_engine_version_stale_orders_versions_and_rekeys_by_equality() -> None:
    """gr518351 ruling 7: both sides parse → ordering decides (an OLDER
    stamp is stale, a NEWER one keeps its verdicts); either side
    unparseable — an env override used as a re-key token, not a version —
    falls back to the pre-ruling equality compare so a re-key still
    re-mints."""
    stale = trust_gate.engine_version_stale
    assert stale("0.37.0", "0.39.0") is True
    assert stale("0.39.0", "0.39.0") is False
    assert stale("0.40.0", "0.39.0") is False
    assert stale("rekey-a", "rekey-b") is True
    assert stale("rekey-a", "rekey-a") is False
    # mixed shapes fall back to equality too — never an ordering guess
    assert stale("0.37.0", "rekey-b") is True
    assert stale("rekey-a", "0.39.0") is True


def test_trust_toon_newer_engine_row_passes() -> None:
    """Ruling 7, the other half: a pathway minted on an engine NEWER
    than the env-or-pin floor is not stale — the row passes
    ('not older than'), nothing names ``engine:stale``, and the verdict
    stays PASS."""
    records = _records([0, 1], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0, 1]))
    meta["autocatpath_version"] = "0.40.0"  # fleet ahead of the 0.39.0 pin
    body = trust_toon(meta, pw_handle="pw1")
    row = next(ln for ln in body.splitlines() if ln.startswith("engine staleness"))
    assert "pass" in row and "not older than" in row
    assert "engine:stale" not in body
    assert "verdict: PASS" in body


class _BatchStore:
    """``sibling_evidence_batch``'s one scan returns ``(ref_id, meta)``
    pairs, unlike ``_FakeStore``'s ref-id-only rows — the batch never
    calls ``fetch_refs_by_ids``."""

    def __init__(self, metas: dict[int, dict[str, Any]]) -> None:
        rows = list(metas.items())

        class _Conn:
            def __enter__(self) -> _Conn:
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

            def execute(self, *_a: Any, **_kw: Any) -> _Conn:
                return self

            def fetchall(self) -> list[tuple[int, dict[str, Any]]]:
                return rows

        class _Pool:
            def connection(self) -> _Conn:
                return _Conn()

        self.pool = _Pool()


def test_sibling_evidence_batch_matches_per_pathway_rules() -> None:
    """gr592278: view='compare' replays the seed gate for every row, so
    the batch must apply exactly ``sibling_evidence``'s rules — ready,
    full sibling_key match, self excluded — for one store scan instead of
    one per row."""
    ident = [_ident(st, 2, -10.0) for st in _STATES]
    stored: dict[int, dict[str, Any]] = {
        20: {**_SIB, "results": {"seeds": [0, 1]}},
        21: {**_SIB, "results": {"seeds": [3]}},
        30: {**_SIB, "results": {"seeds": [2], "trust": ident}},
        # a different candidate_ref: returned by the scan, re-checked out
        31: {**_SIB, "candidate_ref": 999999, "results": {"seeds": [7]}},
        # not ready: contributes nothing
        32: {**_SIB, "status": "computing", "results": {"seeds": [8]}},
    }
    store = _BatchStore(stored)
    out = trust_gate.sibling_evidence_batch(
        store, metas={20: stored[20], 21: stored[21]}
    )
    # each row pools the other's declared seed and sibling 30's records,
    # never its own, never the mis-keyed or unready rows
    assert out[20] == ({2, 3}, ident)
    # 21 also pools 20's declared seeds — pooling is symmetric
    assert out[21] == ({0, 1, 2}, ident)
    # a pathway whose key can't form pools with itself only
    out2 = trust_gate.sibling_evidence_batch(store, metas={40: {"status": "ready"}})
    assert out2 == {40: (set(), [])}


def test_gate_verdict_blocks_and_collapses_families() -> None:
    """gr592278, the gripe's shape: an on-route detachment fatal reads
    blocked, and the `why` collapses the record blocker to its check
    family."""
    records = _records([0])
    verdict, why = gate_verdict(_meta(_results(records, [0])))
    assert verdict == "blocked"
    assert why == "detachment"
    # a clean-but-1-seed run blocks on the seed gate — and the stored
    # available=true fails closed against the replay's blocker
    short = _records([0], hi_states=(), with_detachment=False)
    results = _results(short, [0])
    results["trust_summary"]["barrier"] = {"available": True, "blocked_by": []}
    verdict, why = gate_verdict(_meta(results))
    assert verdict == "blocked"
    assert why == "seeds:1-independent"


def test_gate_verdict_pass_and_lookup_failure() -> None:
    """A clean 2-seed run passes with an empty `why`; a sibling-lookup
    failure blocks (review item 10), never a silent pass."""
    records = _records([0, 1], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0, 1]))
    assert gate_verdict(meta) == ("pass", "")
    verdict, why = gate_verdict(meta, seed_lookup_failed=True)
    assert verdict == "blocked"
    assert why == "seeds:lookup-failed"


def test_gate_verdict_stale_engine_blocks_despite_clean_summary() -> None:
    """The pw452097 half of the gripe: stored summary clean, engine
    0.22.0 stale — the engine-staleness guard fails and the row blocks."""
    records = _records([0, 1], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0, 1]), autocatpath_version="0.22.0")
    verdict, why = gate_verdict(meta)
    assert verdict == "blocked"
    assert "engine staleness" in why


def test_gate_verdict_fails_closed_when_stored_summary_disagrees() -> None:
    """A stored ``available=true`` the record replay contradicts never
    reads pass here — the column picks candidates, so it fails closed
    (view='trust' prints the same disagreement as DISAGREES)."""
    records = _records([0])
    results = _results(records, [0])
    results["trust_summary"]["barrier"] = {"available": True, "blocked_by": []}
    verdict, _why = gate_verdict(_meta(results))
    assert verdict == "blocked"


def test_gate_verdict_na_without_trust_records() -> None:
    assert gate_verdict({"results": {}}) == ("n/a", "pre-trust-schema")
    meta = _meta(_results([], [0]))
    assert gate_verdict(meta) == ("n/a", "no trust records")


def test_row_anchor_renders_the_named_credit() -> None:
    """td625117 review: view='compare' must render each row's named credit
    anchor itself — the gates credited + the seed set — not footnote the
    reader off to a view='trust' lookup."""
    records = _records([0, 1], hi_states=(), with_detachment=False)
    meta = _meta(_results(records, [0, 1]))
    cell = row_anchor(meta)
    assert cell.startswith("gates ")
    assert "endpoint_agreement" in cell
    assert "seed independence (precis)" in cell
    assert "seeds [0, 1]" in cell
    assert "no credit" not in cell


def test_row_anchor_no_credit_names_the_failed_gates() -> None:
    records = _records([0])
    cell = row_anchor(_meta(_results(records, [0])))
    assert cell.startswith("no credit (")
    assert "detachment" in cell


def test_row_anchor_na_and_lookup_failure() -> None:
    assert row_anchor({"results": {}}) == "n/a"
    meta = _meta(_results([], [0]))
    assert row_anchor(meta) == "n/a"
    clean = _meta(
        _results(_records([0, 1], hi_states=(), with_detachment=False), [0, 1])
    )
    # an unverifiable sibling pool never anchors credit (review item 10)
    assert row_anchor(clean, seed_lookup_failed=True) == (
        "no credit (seed pool unverifiable)"
    )


def test_row_anchor_distinct_minima_credit_normally() -> None:
    """gr593139: two seeds sharing one higher minimum no longer ride the
    gr543165 credit-over mechanism at all — the distinct minimum demotes,
    the gates pass, and the anchor needs no ``over`` clause. The stored
    barrier rides the basin (m0) measurement."""
    records = _records([0, 1, 2], hi_states=("A",), with_detachment=False)
    # seed 2 joins seed 1's higher basin on A → a second, reproducible minimum
    for rec in records:
        if rec["check"] == "endpoint_identity" and rec.get("state") == "A":
            if rec["seed"] == 2:
                rec["evidence"] = {
                    "e_eV": round(-10.0 + 0.8669, 6),
                    "fingerprint": _FP_HI,
                }
    records.append(
        _rec(
            "endpoint_agreement",
            "fail",
            "fatal",
            seed=2,
            state="A",
            evidence={
                "e_eV": round(-10.0 + 0.8669, 6),
                "e_min_eV": -10.0,
                "delta_meV": 866.9,
                "n_runs": 3,
                "n_fingerprints": 2,
                "reason": "endpoint-mismatch",
                "tol_eV": 0.05,
            },
        )
    )
    cell = row_anchor(_meta(_results(records, [0, 1, 2])))
    assert cell.startswith("gates ")
    assert "seeds [0, 1, 2]" in cell
    assert "credited over" not in cell
    # endpoint_agreement reads warn (distinct minima), not a credited-over fail
    assert "endpoint_agreement" not in cell


def test_compare_toon_renders_anchor_column_and_legend() -> None:
    """The compare table carries the row's credit anchor in its own column
    (td625117 review) — an explicit 'no credit (…)' cell for blocked rows,
    never a blank, never a footnote pointing at view='trust'."""
    from precis.utils import reaction_graph as analysis

    meta = _meta(_results(_records([0]), [0]))
    graph = meta["graph"]
    root, target = analysis.roots(graph, meta["results"])
    candidates = [
        {
            "slug": "credited-run",
            "lever": "Pd",
            "graph": graph,
            "root": root,
            "target": target,
            "anchor": "gates endpoint_agreement, neb_convergence · seeds [0, 1]",
        },
        {
            "slug": "uncredited-run",
            "lever": "Pt",
            "graph": graph,
            "root": root,
            "target": target,
            "anchor": "no credit (detachment)",
        },
    ]
    out = compare_toon(candidates)
    assert "{cand\tcandidate\tgate\tanchor" in out  # schema carries the column
    assert "seeds [0, 1]" in out
    assert "no credit (detachment)" in out
    assert "named credit anchor" in out  # anchor legend names the cell


def test_compare_toon_renders_gate_column_and_legends() -> None:
    """The compare table names the gate per row and footnotes that conf
    is the rate-limiting step's spread only — the two readings the
    gr592278 route conflated."""
    from precis.utils import reaction_graph as analysis

    meta = _meta(_results(_records([0]), [0]))
    graph = meta["graph"]
    root, target = analysis.roots(graph, meta["results"])
    candidates = [
        {
            "slug": "blocked-run",
            "lever": "Pd",
            "graph": graph,
            "root": root,
            "target": target,
            "gate": "blocked (detachment · seeds:1-independent)",
        },
        {
            "slug": "clean-run",
            "lever": "Pt",
            "graph": graph,
            "root": root,
            "target": target,
        },
    ]
    out = compare_toon(candidates)
    assert "{cand\tcandidate\tgate" in out  # schema carries the column
    assert "blocked (detachment" in out
    assert "n/a" in out  # no gate supplied → n/a, never a blank
    assert "NOT the trust gate" in out
    assert "view='trust'" in out
