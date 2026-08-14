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
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store.protocols import PoolStore

log = logging.getLogger(__name__)

NAME = "autocatpath_aggregate"

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
                          AND j.deleted_at IS NULL
             WHERE t.parent_id = %s AND t.kind = 'todo' AND t.deleted_at IS NULL
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

    seed_meta = _collect_seed_results(ctx.store, agg_todo_id)
    seed_results: list[dict[str, Any]] = []
    for m in seed_meta:
        partial = m.get("partial")
        if not isinstance(partial, dict):
            continue
        seed_results.append(
            {
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
        )
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

    from precis_pathway._dispatch_common import finish

    finish(
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
