"""``autocatpath_seed`` job_type — run ONE ``(model, seed)`` unit of a
pathway exploration and stash the resulting JSON partial on its own job
meta.

§B-1 (gr180096, the spark wedge fix): ``autocatpath_explore`` ran the whole
network x N seeds x full NEB as one ~90-min in-process blob, un-
interruptible and SIGTERM-deaf — it overran its lease and took the worker
down. This job_type is the fan-out unit instead: minutes-scale (one seed of
one model), so a kill loses only that seed and the worker stays
SIGTERM-responsive by construction — no new mechanism needed, it falls out
of the jobs being short.

Minted directly (synchronously) by ``quest.compute.dispatch_autocatpath``,
one per ``(model, seed)`` pair, each parented on its own per-seed todo
(``meta.auto_check={'type': 'child_job_succeeded'}``) so the sibling
``autocatpath_aggregate`` node can gate on "every seed todo under me is
done" via the dispatch worker's existing live-child-todo candidacy gate —
see ``quest.compute.dispatch_autocatpath``'s docstring for the full tree
shape and why the per-seed todo layer (not a bare job) is load-bearing.

Registered via the ``precis.job_types`` entry point (``autocatpath_seed =
precis_pathway.seed_job:SPEC``). Needs no host capability; the node pin
(``target_node``, same seam as ``autocatpath_explore``) routes it to a box
with autocatpath + the backend installed.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "autocatpath_seed"

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The (base) reaction config; run_seed_partial re-derives the
        # per-model spec at model_index from it.
        "config": {"type": "object"},
        # An externally-prepared slab (the precis `structure` seam) as
        # extxyz; null -> autocatpath builds an fcc(111) slab from the
        # config label.
        "slab_extxyz": {"type": ["string", "null"]},
        # Which cfg.search.seeds entry this unit runs.
        "seed": {"type": "integer"},
        # Which cfg.mlip.specs() entry this unit runs.
        "model_index": {"type": "integer"},
        # Backend override; null -> run the config's own mlip.backend.
        "force_backend": {"type": ["string", "null"]},
        # Content address (matches dispatch_autocatpath's per-seed key).
        "content_key": {"type": "string"},
        # The node this job pins itself to (claim gate -> runs here).
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["config", "seed", "model_index"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Same as autocatpath_explore: empty ⊆ any executor's PROVIDES; the
#: target_node pin routes it to the box with the backend installed.
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Run one (model, seed) unit of a pathway exploration "
    "(autocatpath run_one_seed); stash the JSON partial for the sibling "
    "autocatpath_aggregate job."
)


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job. Runs one
    ``run_one_seed`` unit and writes the partial onto this job's OWN meta —
    the aggregate job reads it back via the seed-todo tree, not a shared
    file."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        config = dict(params["config"])
        seed = int(params["seed"])
        model_index = int(params["model_index"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"autocatpath_seed: malformed params ({exc})")
        return
    force_backend = params.get("force_backend")
    slab_extxyz = params.get("slab_extxyz")
    # Kill the compute at its OWN declared wall budget (default 5400s). The
    # ssh_node lease sits a full margin above this (max(7200, wall+3600)), so a
    # timeout-kill always precedes any reclaim/double-claim window.
    resources = params.get("resources") or {}
    try:
        timeout_s = int(resources.get("wall_seconds") or 0)
    except (TypeError, ValueError):
        timeout_s = 0

    ctx.append_chunk(
        "job_event",
        f"autocatpath_seed: {config.get('name', '?')} seed={seed} model#{model_index}",
    )

    try:
        from precis_pathway import runner

        # Out-of-process (gr191351): loading MACE/CUDA in the long-lived worker
        # deadlocks on the GPU node and wedges every system pass (cast_audio
        # included) until SIGKILL; a fresh child process loads it in seconds and
        # a genuine hang is killed at the wall budget instead of holding the pass
        # for the whole lease horizon.
        kw: dict[str, Any] = {
            "force_backend": force_backend,
            "slab_extxyz": slab_extxyz,
        }
        if timeout_s > 0:
            kw["timeout"] = timeout_s
        result = runner.run_seed_partial_subprocess(config, seed, model_index, **kw)
    except Exception as exc:  # pragma: no cover - env/compute dependent
        log.warning("autocatpath_seed: run failed", exc_info=True)
        ctx.record_failure(f"autocatpath_seed: run failed: {exc}")
        return

    partial = result["partial"]
    # The aggregate job's whole input: seed/model identity + the raw
    # run_one_seed partial + any lattice this unit relaxed. Top-level job
    # meta (not params) — mirrors autocatpath_explore's own contract.
    ctx.set_meta(
        content_key=params.get("content_key"),
        seed=seed,
        model_index=model_index,
        model=result["model"],
        partial=partial,
        lattice=result.get("lattice") or {},
    )
    n_states = len(partial.get("states") or {})
    n_warnings = len(partial.get("warnings") or [])
    ctx.append_chunk(
        "job_summary",
        f"autocatpath_seed: seed={seed} model={result['model']}: "
        f"{n_states} state(s), {n_warnings} warning(s).",
    )
    ctx.set_status("succeeded")


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("autocatpath_seed runs via dispatch(), not run()")


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
