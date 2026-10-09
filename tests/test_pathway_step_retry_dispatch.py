"""Aggregate-side step retry: ``precis_pathway.aggregate_job._dispatch_step_retries``
with a fake mint (no executor, no engine, no DFT) and, once, the real
``JobHandler`` mint. Store-seeded trees mirror what
``quest.compute.dispatch_autocatpath`` creates: candidate -> T_agg -> seed
todo -> succeeded ``autocatpath_seed`` job carrying ``params`` + ``partial``.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store, Tag
from precis_pathway import aggregate_job, step_retry

BASE_KEY = "a" * 64
CONFIG: dict[str, Any] = {
    "name": "demo",
    "substrate": "R",
    "target": "S",
    "slab": {"element": "Pd"},
    "search": {"seeds": [0, 1]},
}


class _Ctx:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.chunks: list[tuple[str, str]] = []
        self.status: str | None = None
        self.failure: str | None = None
        self.meta: dict[str, Any] = {}
        self.meta_updates: dict[str, Any] = {}

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_status(self, value: str) -> None:
        self.status = value

    def record_failure(self, reason: str, *, open_tag: str | None = None) -> None:
        self.failure, self.status = reason, "failed"

    def set_meta(self, **fields: Any) -> None:
        self.meta_updates.update(fields)


def _record(seed: int, *, step: str = "R->M", verdict: str = "fail") -> dict[str, Any]:
    return {
        "id": f"{step}#s{seed}#neb_convergence",
        "step": step,
        "seed": seed,
        "check": "neb_convergence",
        "verdict": verdict,
        "severity": "fatal",
        "evidence": {},
        "model": "emt",
    }


def _partial(seed: int, *, fail_rm: bool = False) -> dict[str, Any]:
    return {
        "seed": seed,
        "model": "emt",
        "states": {"*": -1.0, "R": 0.0, "M": 0.5, "S": 0.9},
        "steps": {
            "R->M": {"reactant": "R", "product": "M", "barrier": 1.0, "delta_e": 0.5},
            "M->S": {"reactant": "M", "product": "S", "barrier": 0.7, "delta_e": 0.4},
        },
        "trust": [
            {**_record(seed, verdict="fail" if fail_rm else "pass"), "model": None},
            {**_record(seed, step="M->S", verdict="pass"), "model": None},
        ],
        "warnings": [],
        "refs": {},
        "shed": {},
    }


def _blocked_results(*seeds: int) -> dict[str, Any]:
    ids = [f"R->M#s{s}#neb_convergence" for s in seeds]
    return {
        "trust_schema": 2,
        "trust": [_record(s) for s in seeds]
        + [_record(0, step="M->S", verdict="pass")],
        "trust_summary": {
            "barrier": {"available": not ids, "blocked_by": ids},
            "selectivity": {"available": True, "blocked_by": []},
        },
    }


def _agg_params(pathway_ref_id: int) -> dict[str, Any]:
    return {
        "pathway_ref_id": pathway_ref_id,
        "pathway_slug": "demo-rx-aaaaaaaaaa",
        "config": CONFIG,
        "force_backend": "emt",
        "content_key": BASE_KEY,
        "target_node": "spark",
        "resources": {"wall_seconds": 5400},
    }


def _seed_params(seed: int, model_index: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "config": CONFIG,
        "slab_extxyz": "2\nLattice\nPd 0 0 0\nPd 1 1 1\n",
        "seed": seed,
        "model_index": model_index,
        "force_backend": "emt",
        "content_key": f"seed{seed}",
        "target_node": "spark",
        "pathway_ref_id": 0,
        "resources": {"wall_seconds": 5400, "cpuset": "0-4"},
        **extra,
    }


def _tree(
    store: Store, *, seeds: tuple[int, ...] = (0, 1), with_params: bool = True
) -> tuple[int, int, int]:
    """candidate stand-in -> T_agg -> seed todos -> succeeded seed jobs; plus
    the pathway ref. Returns ``(candidate_id, agg_todo_id, pathway_ref_id)``."""
    with store.tx() as c:
        cand = store.insert_ref(kind="todo", slug=None, title="cand", meta={}, conn=c)
        pw = store.insert_ref(
            kind="pathway",
            slug="demo-rx-aaaaaaaaaa",
            title="t",
            meta={"content_key": BASE_KEY, "status": "computing"},
            conn=c,
        )
        agg = store.insert_ref(
            kind="todo",
            slug=None,
            title="agg",
            meta={
                "executor": "ssh_node",
                "job_type": "autocatpath_aggregate",
                "content_key": BASE_KEY,
                "params": _agg_params(pw.id),
            },
            parent_id=cand.id,
            conn=c,
        )
        for seed in seeds:
            st = store.insert_ref(
                kind="todo",
                slug=None,
                title=f"seed {seed}",
                meta={"content_key": f"seed{seed}"},
                parent_id=agg.id,
                conn=c,
            )
            meta: dict[str, Any] = {
                "job_type": "autocatpath_seed",
                "seed": seed,
                "model_index": 0,
                "model": "emt",
                "partial": _partial(seed, fail_rm=seed == 0),
                "lattice": {},
                "structures": {},
            }
            if with_params:
                meta["params"] = _seed_params(seed, pathway_ref_id=pw.id)
            job = store.insert_ref(
                kind="job",
                slug=None,
                title="seed job",
                meta=meta,
                parent_id=st.id,
                conn=c,
            )
            store.add_tag(
                job.id, Tag.closed("STATUS", "succeeded"), set_by="system", conn=c
            )
    return int(cand.id), int(agg.id), int(pw.id)


def _run(
    store: Store,
    ctx: _Ctx,
    *,
    agg_id: int,
    pw_id: int,
    results: dict[str, Any],
    mint: Any,
    params: dict[str, Any] | None = None,
    max_retries: int | None = 2,
) -> list[dict[str, Any]]:
    return aggregate_job._dispatch_step_retries(
        ctx,
        results=results,
        seed_meta=aggregate_job._collect_seed_results(store, agg_id),
        params=params or _agg_params(pw_id),
        agg_todo_id=agg_id,
        pathway_ref_id=pw_id,
        mint=mint,
        max_retries=max_retries,
    )


def _children(store: Store, parent_id: int, kind: str) -> list[Any]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE parent_id = %s AND kind = %s "
            "AND retired_at IS NULL ORDER BY ref_id",
            (parent_id, kind),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


def test_blocked_step_is_redispatched_once_with_a_fresh_seed_and_logged(
    store: Store,
) -> None:
    cand, agg, pw = _tree(store)
    minted: list[tuple[int, str, dict[str, Any]]] = []

    def mint(parent: int, idem: str, params: dict[str, Any]) -> int:
        minted.append((parent, idem, params))
        return 4242

    ctx = _Ctx(store)
    events = _run(
        store, ctx, agg_id=agg, pw_id=pw, results=_blocked_results(0), mint=mint
    )

    # exactly one new seed job: the blocked step, a seed nobody has used,
    # its own idem key, the base seed's params cloned (slab included)
    assert len(minted) == 1
    parent, idem, p = minted[0]
    skey = step_retry.retry_content_key(BASE_KEY, step="R->M", seed=2, model_index=0)
    assert idem == f"autocatpath_seed:{skey}"
    assert p["seed"] == 2 and p["model_index"] == 0
    assert p["only_steps"] == ["R->M"]
    assert p["step_retry"] == {"step": "R->M", "from_seed": 0}
    assert p["content_key"] == skey
    assert p["slab_extxyz"] == _seed_params(0)["slab_extxyz"]
    assert p["resources"] == {"wall_seconds": 5400, "cpuset": "0-4"}

    # tree shape: a NEW aggregate todo on the candidate (not under T_agg),
    # inheriting T_agg's partials, with the seed todo under it
    retry_todos = [t for t in _children(store, cand, "todo") if t[0] != agg]
    assert len(retry_todos) == 1
    retry_id, retry_meta = retry_todos[0]
    assert retry_meta["executor"] == "ssh_node"
    assert retry_meta["job_type"] == "autocatpath_aggregate"
    assert retry_meta["content_key"] == f"{BASE_KEY}:step-retry:1"
    assert retry_meta["params"]["base_agg_todo_ids"] == [agg]
    assert retry_meta["params"]["pathway_ref_id"] == pw
    seed_todos = _children(store, retry_id, "todo")
    assert [t[0] for t in seed_todos] == [parent]
    assert seed_todos[0][1]["step_retry"] == {"step": "R->M", "from_seed": 0}
    assert seed_todos[0][1]["auto_check"] == {"type": "child_job_succeeded"}

    # the event log on the pathway: what was retried and why
    assert len(events) == 1
    ref = store.fetch_refs_by_ids({pw}).get(pw)
    assert ref is not None
    (event,) = ref.meta[step_retry.EVENTS_META_KEY]
    assert event["step"] == "R->M" and event["from_seed"] == 0 and event["to_seed"] == 2
    assert event["record_id"] == "R->M#s0#neb_convergence"
    assert event["check"] == "neb_convergence"
    assert event["job"] == 4242 and event["round"] == 1 and event["attempt"] == 1
    assert event["seed_todo"] == parent and event["retry_todo"] == retry_id
    assert event["at"].endswith("+00:00")
    assert any("step retry 1/2 for R->M" in text for _, text in ctx.chunks)

    # idempotent: the same aggregate re-run mints nothing and logs nothing new
    assert (
        _run(store, ctx, agg_id=agg, pw_id=pw, results=_blocked_results(0), mint=mint)
        == []
    )
    assert len(minted) == 1
    ref = store.fetch_refs_by_ids({pw}).get(pw)
    assert ref is not None and len(ref.meta[step_retry.EVENTS_META_KEY]) == 1


def test_retry_cap_is_durable_across_rounds_then_the_blocker_stands(
    store: Store,
) -> None:
    _cand, agg, pw = _tree(store)
    minted: list[dict[str, Any]] = []

    def mint(parent: int, idem: str, params: dict[str, Any]) -> int:
        minted.append(params)
        return 100 + len(minted)

    ctx = _Ctx(store)
    # round 1 (seed 0 failed) -> retry at seed 2; round 2 (the retry failed
    # too) -> retry at seed 3; round 3 -> cap spent, no mint, named in a chunk
    _run(store, ctx, agg_id=agg, pw_id=pw, results=_blocked_results(0), mint=mint)
    _run(store, ctx, agg_id=agg, pw_id=pw, results=_blocked_results(2), mint=mint)
    assert [p["seed"] for p in minted] == [2, 3]
    ref = store.fetch_refs_by_ids({pw}).get(pw)
    assert ref is not None
    log = ref.meta[step_retry.EVENTS_META_KEY]
    assert [(e["round"], e["attempt"], e["from_seed"], e["to_seed"]) for e in log] == [
        (1, 1, 0, 2),
        (2, 2, 2, 3),
    ]
    ctx.chunks.clear()
    assert (
        _run(store, ctx, agg_id=agg, pw_id=pw, results=_blocked_results(3), mint=mint)
        == []
    )
    assert len(minted) == 2
    assert any(
        "cap (2) spent for R->M" in text and "R->M#s3#neb_convergence stands" in text
        for _, text in ctx.chunks
    )
    # a different step still has its own budget
    other = _blocked_results()
    other["trust"].append(_record(1, step="M->S"))
    other["trust_summary"]["barrier"] = {
        "available": False,
        "blocked_by": ["M->S#s1#neb_convergence"],
    }
    _run(store, ctx, agg_id=agg, pw_id=pw, results=other, mint=mint)
    assert minted[-1]["only_steps"] == ["M->S"] and minted[-1]["seed"] == 4


@pytest.mark.parametrize(
    "results",
    [
        # budget / job-error strings cited as blockers: never a step retry
        {
            "trust_schema": 2,
            "trust": [_record(0)],
            "trust_summary": {
                "barrier": {"available": False, "blocked_by": ["cost-cap"]}
            },
        },
        {
            "trust_schema": 2,
            "trust": [_record(0)],
            "trust_summary": {
                "barrier": {"available": False, "blocked_by": ["spend-limit"]}
            },
        },
        # an available quantity has nothing to retry
        {
            "trust_schema": 2,
            "trust": [_record(0, verdict="pass")],
            "trust_summary": {"barrier": {"available": True, "blocked_by": []}},
        },
        # pre-trust-schema artifact: no gate is fabricated
        {"warnings": ["[R->M] NEB not converged"]},
    ],
)
def test_no_retry_without_a_trust_check_verdict(
    store: Store, results: dict[str, Any]
) -> None:
    cand, agg, pw = _tree(store)
    ctx = _Ctx(store)

    def mint(*_a: Any) -> int:
        raise AssertionError("must not mint")

    assert _run(store, ctx, agg_id=agg, pw_id=pw, results=results, mint=mint) == []
    assert [t[0] for t in _children(store, cand, "todo")] == [agg]
    ref = store.fetch_refs_by_ids({pw}).get(pw)
    assert ref is not None and step_retry.EVENTS_META_KEY not in ref.meta


def test_retry_disabled_by_cap_zero(store: Store) -> None:
    _cand, agg, pw = _tree(store)
    ctx = _Ctx(store)
    assert (
        _run(
            store,
            ctx,
            agg_id=agg,
            pw_id=pw,
            results=_blocked_results(0),
            mint=lambda *_a: 1,
            max_retries=0,
        )
        == []
    )


def test_legacy_seed_rows_without_params_are_refused_not_guessed(store: Store) -> None:
    """A retry must measure the candidate's exported slab; a seed job minted
    before ``params`` carried it cannot be cloned."""
    cand, agg, pw = _tree(store, with_params=False)
    ctx = _Ctx(store)
    minted: list[Any] = []
    assert (
        _run(
            store,
            ctx,
            agg_id=agg,
            pw_id=pw,
            results=_blocked_results(0),
            mint=lambda *a: minted.append(a),
        )
        == []
    )
    assert minted == []
    assert any("no base seed params to clone" in text for _, text in ctx.chunks)


def test_default_mint_goes_through_the_job_handler(store: Store) -> None:
    """The real seam: ``JobHandler.put`` validates the new ``only_steps`` /
    ``step_retry`` params against ``autocatpath_seed``'s schema and parents
    the job on the retry seed todo."""
    cand, agg, pw = _tree(store)
    ctx = _Ctx(store)
    events = _run(
        store, ctx, agg_id=agg, pw_id=pw, results=_blocked_results(0), mint=None
    )
    assert len(events) == 1 and isinstance(events[0]["job"], int)
    jobs = _children(store, events[0]["seed_todo"], "job")
    assert [j[0] for j in jobs] == [events[0]["job"]]
    meta = jobs[0][1]
    assert meta["job_type"] == "autocatpath_seed"
    assert meta["params"]["only_steps"] == ["R->M"]
    assert meta["params"]["step_retry"] == {"step": "R->M", "from_seed": 0}
    assert meta["params"]["seed"] == 2
    assert meta["requires"] == {"gpu": 1}
    assert "STATUS:queued" in {str(t) for t in store.tags_for(events[0]["job"])}


def test_retry_round_aggregate_collects_base_partials_and_replaces_the_step(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The T_retry aggregate: inherits T_agg's partials via
    ``base_agg_todo_ids``, folds the narrowed retry in, prunes the failed
    measurement, and persists onto the same pathway — the whole-network
    engine call itself is faked."""
    pytest.importorskip("autocatpath")
    from precis_pathway import runner

    cand, agg, pw = _tree(store)
    # the retry round's own tree: T_retry (direct child of the candidate)
    # with one succeeded retry seed narrowed to R->M at seed 2
    retry_params = {
        **_agg_params(pw),
        "content_key": f"{BASE_KEY}:r1",
        "base_agg_todo_ids": [agg],
    }
    with store.tx() as c:
        t_retry = store.insert_ref(
            kind="todo",
            slug=None,
            title="retry",
            meta={
                "executor": "ssh_node",
                "job_type": "autocatpath_aggregate",
                "params": retry_params,
            },
            parent_id=cand,
            conn=c,
        )
        st = store.insert_ref(
            kind="todo",
            slug=None,
            title="retry seed",
            meta={},
            parent_id=t_retry.id,
            conn=c,
        )
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="retry seed job",
            meta={
                "job_type": "autocatpath_seed",
                "seed": 2,
                "model_index": 0,
                "model": "emt",
                "partial": _partial(2),  # whole network: an older child did not narrow
                "lattice": {},
                "structures": {},
                "step_retry": {"step": "R->M", "from_seed": 0},
                "params": _seed_params(2, only_steps=["R->M"]),
            },
            parent_id=st.id,
            conn=c,
        )
        store.add_tag(
            job.id, Tag.closed("STATUS", "succeeded"), set_by="system", conn=c
        )

    seen: list[list[dict[str, Any]]] = []

    def fake_aggregate(config: Any, seed_results: Any, **_kw: Any) -> dict[str, Any]:
        seen.append([dict(r) for r in seed_results])
        return {
            "content_key": BASE_KEY,
            "autocatpath_version": "0.0-test",
            "config": CONFIG,
            "config_snapshot_yaml": "name: demo\n",
            "results_json": {
                "substrate": "R",
                "target": "S",
                "backend": "emt",
                "n_samples": len(seed_results),
                "nodes": ["*", "R", "M", "S"],
                "edges": [],
                "trust_schema": 2,
                "trust": [],
                "trust_summary": {"barrier": {"available": True, "blocked_by": []}},
            },
            "graph_json": {"directed": True, "nodes": [], "links": []},
            "methods_md": "# Methods\n",
            "structures_extxyz": {},
            "warnings": [],
        }

    monkeypatch.setattr(runner, "aggregate_seed_partials", fake_aggregate)
    monkeypatch.setattr(runner, "run_kinetics_subprocess", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "root_state", lambda _cfg: "*")

    ctx = _Ctx(store)
    ctx.meta = {"params": retry_params, "dispatched_from_todo": int(t_retry.id)}
    aggregate_job._dispatch(ctx, aggregate_job.SPEC)

    assert ctx.failure is None, ctx.failure
    assert ctx.status == "succeeded"
    (partials,) = seen
    assert [r["seed"] for r in partials] == [0, 1, 2]
    assert set(partials[0]["partial"]["steps"]) == {"M->S"}  # the failed R->M is gone
    assert not any(t["step"] == "R->M" for t in partials[0]["partial"]["trust"])
    assert set(partials[1]["partial"]["steps"]) == {"R->M", "M->S"}
    assert set(partials[2]["partial"]["steps"]) == {"R->M"}  # narrowed here
    assert set(partials[2]["partial"]["states"]) == {"*", "R", "M"}
    ref = store.fetch_refs_by_ids({pw}).get(pw)
    assert ref is not None
    assert ref.meta["status"] == "ready"
    assert ref.meta["results"]["trust_summary"]["barrier"]["available"] is True
    assert ref.meta["n_seed_partials"] == 3
    # a clean round plans nothing further
    assert step_retry.EVENTS_META_KEY not in ref.meta
