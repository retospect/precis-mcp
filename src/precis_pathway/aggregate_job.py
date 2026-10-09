"""``autocatpath_aggregate`` job_type — combine the §B-1 seed fan-out's N
partials (pure numpy, no ML backend) and write the pathway's graph + pooled-
uncertainty result back, exactly like ``autocatpath_explore`` used to in one
shot.

**Minting is entirely the existing dispatch worker's job**, not this
module's: ``quest.compute.dispatch_autocatpath`` creates the aggregate todo
(``T_agg``) with ``meta.executor``/``meta.job_type='autocatpath_aggregate'``/
``meta.params`` set but NO job minted yet, plus N per-seed todos (each
already wrapping its own ``autocatpath_seed`` job) parented ON ``T_agg``.
The dispatch worker's ordinary candidate-selection query already excludes a
parent todo with a live (non-done) child todo — so ``T_agg`` only becomes a
dispatch candidate once every seed todo under it has resolved
(``child_job_succeeded`` on each), at which point the dispatch worker mints
THIS job under ``T_agg`` the same way it mints any other todo's child job.
No new coordinator, no Yield/Done state machine — see ``quest.compute.
dispatch_autocatpath``'s docstring for the full tree shape and why the two-
level nesting (seed job -> seed todo -> T_agg) is load-bearing (a bare seed
job as T_agg's direct child would satisfy ``child_job_succeeded`` on the
FIRST seed's success, not the aggregate's).

Registered via the ``precis.job_types`` entry point (``autocatpath_aggregate
= precis_pathway.aggregate_job:SPEC``). Runs on any node (pure numpy) —
``target_node``, if set, only carries forward for provenance (``ran_on``),
it does not gate the claim the way it does for the seed/monolith jobs.

**Step-level retry (the one place this module DOES mint).** Right after the
result is persisted, :func:`_dispatch_step_retries` reads the artifact's
``trust_summary`` — computed here, so the retry decision sits next to the
evidence — and, for every quantity-blocking fatal convergence record
(:mod:`precis_pathway.step_retry`), re-queues ONLY that step at a fresh seed:
a new ``autocatpath_seed`` job (distinct seed, distinct idem key,
``params.only_steps``/``params.step_retry``) under a seed todo of a NEW
aggregate todo ``T_retry`` parented on the candidate — not under ``T_agg``:
a deterministic parent with a succeeded child job is never re-dispatched
(``dispatch._job_blocks_dispatch_sql``), so the just-finished ``T_agg`` cannot
host a second round. ``T_retry.params.base_agg_todo_ids`` names the tree(s)
whose partials it inherits; its aggregate job (minted by the dispatch worker
once the retry seed resolves) collects base + retry partials, applies
:func:`~precis_pathway.step_retry.apply_step_replacements`, and persists onto
the SAME pathway ref, so the harvest reads the merged verdict exactly like a
first aggregate. Each dispatched retry is appended to the pathway ref's
``meta.step_retries`` event log (the durable per-step counter and the
reader's "what was retried and why"); past the cap the blocker stands and
surfaces as before, with a ``job_event`` line saying so. Infra/budget
failures never reach this path — only trust-check verdicts do.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.workers.job_types import JobTypeSpec

from . import step_retry

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.protocols import PoolStore

log = logging.getLogger(__name__)

NAME = "autocatpath_aggregate"

#: Mint seam for the step retry: ``(parent_seed_todo_id, idem_key, params)
#: -> job ref id`` (``None`` when the mint was refused). The default goes
#: through ``JobHandler.put`` exactly like ``dispatch_autocatpath``; tests
#: pass a fake so no executor/engine is involved.
MintSeedJob = Callable[[int, str, dict[str, Any]], int | None]

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The pathway ref the merged result is written back onto.
        "pathway_ref_id": {"type": "integer"},
        "pathway_slug": {"type": ["string", "null"]},
        # The (base) reaction config — aggregate_seed_partials rebuilds the
        # network topology from this (cheap, rule-based, no ML).
        "config": {"type": "object"},
        "force_backend": {"type": ["string", "null"]},
        # Content address (matches dispatch_autocatpath's base key).
        "content_key": {"type": "string"},
        "target_node": {"type": ["string", "null"]},
        # Step-retry round (T_retry): the aggregate todo(s) whose seed
        # partials this round inherits, oldest first. Absent on T_agg.
        "base_agg_todo_ids": {"type": ["array", "null"], "items": {"type": "integer"}},
    },
    "required": ["pathway_ref_id", "config"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Combine the autocatpath seed fan-out's partials (pure numpy) into the "
    "pathway's graph + pooled-uncertainty result; write it back onto the "
    "pathway ref."
)


def _collect_seed_results(store: PoolStore, agg_todo_id: int) -> list[dict[str, Any]]:
    """Every succeeded ``autocatpath_seed`` job under a seed-todo child of
    ``agg_todo_id`` — the partials this aggregate combines.

    Walks TWO levels (aggregate todo -> seed todo -> seed job) rather than
    reading direct children of ``agg_todo_id`` — see the module docstring
    for why the seed jobs are nested one level deeper than the aggregate's
    own eventual job.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.meta FROM refs t
              JOIN refs j ON j.parent_id = t.ref_id AND j.kind = 'job'
                          AND j.retired_at IS NULL
             WHERE t.parent_id = %s AND t.kind = 'todo' AND t.retired_at IS NULL
               AND j.meta->>'job_type' = 'autocatpath_seed'
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags tg ON tg.tag_id = rt.tag_id
                      WHERE rt.ref_id = j.ref_id AND tg.namespace = 'STATUS'
                        AND tg.value = 'succeeded'
                   )
             ORDER BY j.ref_id ASC
            """,
            (agg_todo_id,),
        ).fetchall()
    return [dict(r[0] or {}) for r in rows]


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job. Gathers
    the sibling seed partials, aggregates them in-process (pure numpy), and
    persists onto the pathway ref — same tail as ``autocatpath_explore``
    (shared via ``precis_pathway._dispatch_common``)."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        pathway_ref_id = int(params["pathway_ref_id"])
        config = dict(params["config"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"autocatpath_aggregate: malformed params ({exc})")
        return
    force_backend = params.get("force_backend")

    # The dispatch worker stamps `dispatched_from_todo` = the parent todo's
    # own ref_id on every job it mints (workers/dispatch.py) — that parent
    # IS T_agg here, our route back to the sibling seed todos.
    agg_todo_id = (ctx.meta or {}).get("dispatched_from_todo")
    if not isinstance(agg_todo_id, int):
        ctx.record_failure(
            "autocatpath_aggregate: no dispatched_from_todo on job meta "
            "(must be minted under the aggregate todo by the dispatch worker)"
        )
        return

    base_ids = [
        int(b)
        for b in (params.get("base_agg_todo_ids") or [])
        if isinstance(b, int) and not isinstance(b, bool)
    ]
    seed_meta: list[dict[str, Any]] = []
    for tree_id in [*base_ids, agg_todo_id]:
        seed_meta.extend(_collect_seed_results(ctx.store, tree_id))
    seed_results: list[dict[str, Any]] = []
    for m in seed_meta:
        partial = m.get("partial")
        if not isinstance(partial, dict):
            continue
        entry: dict[str, Any] = {
            "seed": m.get("seed"),
            "model": m.get("model"),
            "model_index": m.get("model_index"),
            "partial": partial,
            "lattice": m.get("lattice") or {},
            # per-state geometry (model_index==0 units) — feeds
            # aggregate_seed_partials' min-energy structures_extxyz
            # merge; omitting this key silently strips geometry from
            # every fan-out pathway (the persist gate just skips).
            "structures": m.get("structures") or {},
        }
        if isinstance(m.get("step_retry"), dict):
            entry["step_retry"] = m["step_retry"]
        seed_results.append(entry)
    if any(step_retry.step_retry_of(r) for r in seed_results):
        # A retry round: the retry seed's narrowed step replaces the failed
        # measurement it names (step_retry module docstring).
        try:
            from precis_pathway import runner

            root = runner.root_state(config)
        except Exception:  # pragma: no cover - engine/config dependent
            log.warning("autocatpath_aggregate: root state unavailable", exc_info=True)
            root = None
        seed_results = step_retry.apply_step_replacements(seed_results, root=root)
    if not seed_results:
        ctx.record_failure(
            "autocatpath_aggregate: no succeeded seed partials found under "
            f"todo #{agg_todo_id}"
        )
        return

    ctx.append_chunk(
        "job_event",
        f"autocatpath_aggregate: combining {len(seed_results)} seed "
        f"partial(s) for {config.get('name', '?')}",
    )

    try:
        from precis_pathway import runner

        artifact = runner.aggregate_seed_partials(
            config, seed_results, force_backend=force_backend
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("autocatpath_aggregate: aggregate failed", exc_info=True)
        ctx.record_failure(f"autocatpath_aggregate: aggregate failed: {exc}")
        return

    # Microkinetics is a diagnostic bonus riding on the just-aggregated
    # pathway — `run_kinetics_subprocess` never raises (any failure, including
    # a deployed engine that predates the `kinetics` module, a solve that
    # throws, or the wall-clock ceiling firing, lands as
    # `results_json["kinetics_error"]`), so this can never fail the aggregate
    # itself.
    #
    # Out-of-process and BOUNDED, not in-process as it was: this job wrote its
    # "combining N seed partial(s)" chunk four times between 2026-09-25 18:00Z
    # and 2026-09-26 07:25Z and never got further, holding a worker that grew
    # to ~117 GB, while the same work on the same inputs measures 33s. A
    # ceiling cannot fix a cause nobody has established yet, but it stops a
    # diagnostic from eating a node, and it turns the next occurrence into a
    # legible `kinetics timed out` instead of silence.
    runner.run_kinetics_subprocess(artifact["config"], artifact)
    r = artifact["results_json"]
    if isinstance(r.get("kinetics"), dict):
        tof = r["kinetics"].get("tof")
        tof_s = f"{tof:.3e} /site/s" if isinstance(tof, (int, float)) else "no TOF"
        ctx.append_chunk("job_event", f"autocatpath_aggregate: kinetics -> TOF {tof_s}")
    elif r.get("kinetics_error"):
        ctx.append_chunk(
            "job_event",
            f"autocatpath_aggregate: kinetics skipped ({r['kinetics_error']})",
        )

    from precis_pathway._dispatch_common import finish

    ok = finish(
        ctx,
        artifact,
        pathway_ref_id,
        pathway_slug=params.get("pathway_slug"),
        produced_by=NAME,
        extra_meta={
            "ran_on": params.get("target_node"),
            "n_seed_partials": len(seed_results),
        },
    )
    if not ok:
        return
    # The persisted verdict stands as-is; a retry only ever ADDS a seed job.
    # Isolated so a retry-planning fault can never fail a finished aggregate.
    try:
        _dispatch_step_retries(
            ctx,
            results=r,
            seed_meta=seed_meta,
            params=params,
            agg_todo_id=agg_todo_id,
            pathway_ref_id=pathway_ref_id,
        )
    except Exception:
        log.warning("autocatpath_aggregate: step retry planning failed", exc_info=True)
        ctx.append_chunk(
            "job_event",
            "autocatpath_aggregate: step retry planning failed (see worker log); "
            "the trust verdict above stands",
        )


def _mint_seed_job_via_handler(
    store: Store, parent_todo_id: int, idem_key: str, params: dict[str, Any]
) -> int | None:
    """Default :data:`MintSeedJob`: the same ``JobHandler.put`` shape
    ``quest.compute.dispatch_autocatpath`` uses for a base seed (``ssh_node``,
    ``requires={"gpu": 1}`` so seeds serialise one-at-a-time per GPU)."""
    from precis.dispatch import Hub
    from precis.handlers.job import JobHandler

    resp = JobHandler(hub=Hub(store=store)).put(
        job_type="autocatpath_seed",
        executor="ssh_node",
        parent_id=parent_todo_id,
        idem_key=idem_key,
        requires={"gpu": 1},
        params=params,
    )
    return int(resp.ref_id) if resp.ref_id is not None else None


def _find_child_todo(store: Store, parent_id: int, content_key: str) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND parent_id = %s "
            "AND retired_at IS NULL AND meta->>'content_key' = %s "
            "ORDER BY ref_id LIMIT 1",
            (parent_id, content_key),
        ).fetchone()
    return int(row[0]) if row else None


def _ensure_todo(
    store: Store, *, parent_id: int, content_key: str, title: str, meta: dict[str, Any]
) -> tuple[int, bool]:
    """Content-addressed get-or-create for one retry-tree node (mirrors
    ``quest.compute._ensure_autocatpath_todo``: raw ``insert_ref`` — these
    are compute-lane nodes under a ``structure``, outside the human intent
    tree — tagged ``ephemeral``). Returns ``(ref_id, created)``; the flag
    is what keeps a re-run of the same aggregate from logging a retry
    twice."""
    from precis.store import Tag

    existing = _find_child_todo(store, parent_id, content_key)
    if existing is not None:
        return existing, False
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta={**meta, "content_key": content_key},
            parent_id=parent_id,
            conn=conn,
        )
        store.add_tag(ref.id, Tag.open("ephemeral"), set_by="system", conn=conn)
    return int(ref.id), True


def _pathway_events(store: Store, pathway_ref_id: int) -> list[dict[str, Any]]:
    ref = store.fetch_refs_by_ids({pathway_ref_id}).get(pathway_ref_id)
    events = (ref.meta or {}).get(step_retry.EVENTS_META_KEY) if ref else None
    return (
        [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    )


def _template_seed_params(
    seed_meta: list[dict[str, Any]], model_index: int
) -> dict[str, Any] | None:
    """The params of a base seed job for ``model_index`` — the retry clones
    them (config, exported slab, backend, node pin, wall budget) and changes
    only seed/content_key/only_steps/step_retry. ``None`` when no seed
    carried params (legacy rows): without ``slab_extxyz`` the retry would
    measure a default slab, not the candidate — refuse rather than guess."""
    for m in seed_meta:
        p = m.get("params")
        if (
            m.get("model_index") == model_index
            and isinstance(p, dict)
            and isinstance(p.get("config"), dict)
            and not isinstance(m.get("step_retry"), dict)
        ):
            return dict(p)
    return None


def _dispatch_step_retries(
    ctx: Any,
    *,
    results: dict[str, Any],
    seed_meta: list[dict[str, Any]],
    params: dict[str, Any],
    agg_todo_id: int,
    pathway_ref_id: int,
    mint: MintSeedJob | None = None,
    max_retries: int | None = None,
) -> list[dict[str, Any]]:
    """Plan and mint this round's step retries; return the events appended.

    See the module docstring for the tree shape. Idempotent on a re-run of
    the same aggregate: the retry todo and seed todos are content-addressed
    on ``(base key, round, step, model, seed)``, and only a NEWLY created
    seed todo is logged and minted.
    """
    store = ctx.store
    events = _pathway_events(store, pathway_ref_id)
    model_index_of: dict[str | None, int] = {}
    seeds_in_use: set[int] = set()
    for m in seed_meta:
        if isinstance(m.get("model_index"), int) and isinstance(m.get("model"), str):
            model_index_of.setdefault(m["model"], m["model_index"])
        if isinstance(m.get("seed"), int):
            seeds_in_use.add(m["seed"])
    if len(set(model_index_of.values())) == 1:
        # A record without a model tag can only mean the one model that ran.
        model_index_of.setdefault(None, next(iter(model_index_of.values())))
    cfg_seeds = (params.get("config") or {}).get("search", {}).get("seeds") or []
    seeds_in_use.update(int(s) for s in cfg_seeds if isinstance(s, int))

    cap = step_retry.max_step_retries() if max_retries is None else max_retries
    exhausted = step_retry.exhausted_blockers(
        results, events=events, model_index_of=model_index_of, max_retries=cap
    )
    for rec in exhausted:
        ctx.append_chunk(
            "job_event",
            f"autocatpath_aggregate: step retry cap ({cap}) spent for "
            f"{rec['step']} (model {rec.get('model')}); blocker {rec['id']} stands",
        )
    planned = step_retry.plan_step_retries(
        results,
        events=events,
        model_index_of=model_index_of,
        seeds_in_use=seeds_in_use,
        max_retries=cap,
    )
    if not planned:
        return []

    base_key = str(params.get("content_key") or "")
    base_ids = [
        int(b)
        for b in (params.get("base_agg_todo_ids") or [])
        if isinstance(b, int) and not isinstance(b, bool)
    ]
    round_no = 1 + max((int(e.get("round", 0) or 0) for e in events), default=0)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT parent_id FROM refs WHERE ref_id = %s", (agg_todo_id,)
        ).fetchone()
    structure_id = int(row[0]) if row and row[0] is not None else None
    if structure_id is None:
        ctx.append_chunk(
            "job_event",
            "autocatpath_aggregate: step retry skipped — aggregate todo has no parent",
        )
        return []

    retry_key = f"{base_key}:step-retry:{round_no}"
    retry_todo_id, _ = _ensure_todo(
        store,
        parent_id=structure_id,
        content_key=retry_key,
        title=f"autocatpath step retry round {round_no}: {params.get('pathway_slug')}",
        meta={
            "executor": "ssh_node",
            "job_type": NAME,
            "step_retry_round": round_no,
            "params": {
                **params,
                "content_key": retry_key,
                "base_agg_todo_ids": [*base_ids, agg_todo_id],
            },
        },
    )
    mint_fn: MintSeedJob = mint or (
        lambda parent, key, p: _mint_seed_job_via_handler(store, parent, key, p)
    )
    appended: list[dict[str, Any]] = []
    for plan in planned:
        template = _template_seed_params(seed_meta, plan.model_index)
        if template is None:
            ctx.append_chunk(
                "job_event",
                f"autocatpath_aggregate: step retry skipped for {plan.step} — no base "
                f"seed params to clone for model#{plan.model_index}",
            )
            continue
        skey = step_retry.retry_content_key(
            base_key, step=plan.step, seed=plan.to_seed, model_index=plan.model_index
        )
        seed_todo_id, created = _ensure_todo(
            store,
            parent_id=retry_todo_id,
            content_key=skey,
            title=(
                f"autocatpath step retry {plan.step} seed {plan.to_seed} "
                f"model#{plan.model_index}: {params.get('pathway_slug')}"
            ),
            meta={
                "auto_check": {"type": "child_job_succeeded"},
                "step_retry": {"step": plan.step, "from_seed": plan.from_seed},
            },
        )
        if not created:
            continue
        seed_params = {
            **template,
            "seed": plan.to_seed,
            "content_key": skey,
            "only_steps": [plan.step],
            "step_retry": {"step": plan.step, "from_seed": plan.from_seed},
        }
        job_id = mint_fn(seed_todo_id, f"autocatpath_seed:{skey}", seed_params)
        event = plan.event(
            round=round_no,
            retry_todo=retry_todo_id,
            seed_todo=seed_todo_id,
            job=job_id,
            at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        appended.append(event)
        ctx.append_chunk(
            "job_event",
            f"autocatpath_aggregate: step retry {plan.attempt}/{cap} for {plan.step} "
            f"(model {plan.model}) seed {plan.from_seed} -> {plan.to_seed}, blocked by "
            f"{plan.record_id} -> job {job_id}",
        )
    if appended:
        store.stamp_ref_meta(
            pathway_ref_id, {step_retry.EVENTS_META_KEY: [*events, *appended]}
        )
    return appended


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("autocatpath_aggregate runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["NAME", "SPEC", "load"]
