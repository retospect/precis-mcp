"""``autocatpath_explore`` job_type — run a autocatpath reaction-network exploration on
the pinned compute node and write the result back onto its `pathway` ref.

This is the routing seam (slice 1). The `pathway` handler mints one of these
jobs (`meta.executor='ssh_node'`, `meta.params.target_node=<node>`, parented on
the pathway ref via the compute lane, ADR 0044). The `ssh_node` worker pass on
that node — and only that node, per the target_node claim gate — claims it and
invokes `dispatch()` here, which runs autocatpath **in-process** (autocatpath +
the ML backend are installed in that node's worker venv, via the
``precis-mcp[catalyst-gpu]`` extra) and persists the artifact. So the heavy
relax/NEB runs where the hardware is; the gateway only mints the job.

Registered via the ``precis.job_types`` entry point
(``autocatpath_explore = precis_pathway.job:SPEC``). Needs no host capability
(``REQUIRES`` is empty); the node pin does the routing.

**§B-1 (gr180096, the spark wedge fix):** ``quest.compute.
dispatch_autocatpath`` no longer mints this job_type — it runs the whole
network x N seeds x full NEB as one ~90-min in-process blob, which overran
its lease and took the worker down (SIGTERM-deaf -> SIGKILL). It stays
registered so pre-existing queued/legacy rows don't error-loop. New
dispatches fan out ``autocatpath_seed`` (one per ``(model, seed)``, minutes-
scale) + ``autocatpath_aggregate`` (pure numpy, gated via the existing
``child_job_succeeded`` auto_check) — see ``quest.compute.
dispatch_autocatpath``'s docstring for the tree shape, and
``docs/proposals/gpu-priority.md`` Phase 1 for the design.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "autocatpath_explore"

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The pathway ref the result is written back onto.
        "pathway_ref_id": {"type": "integer"},
        # The authoritative config (parsed YAML) to run.
        "config": {"type": "object"},
        # An externally-prepared slab (the precis `structure` seam) as extxyz;
        # null → autocatpath builds an fcc(111) slab from the config label.
        "slab_extxyz": {"type": ["string", "null"]},
        # Provenance: the precis structure ref the slab came from (for linking).
        "structure_ref": {"type": ["integer", "null"]},
        # Backend override; null → run the config's own mlip.backend.
        "force_backend": {"type": ["string", "null"]},
        # Content address (matches the handler's regen cache key).
        "content_key": {"type": "string"},
        # The node this job pins itself to (claim gate → runs here).
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["pathway_ref_id", "config"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: autocatpath needs no special host capability (empty ⊆ any executor's PROVIDES);
#: the target_node pin routes it to the box with autocatpath + the backend installed.
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Run a autocatpath reaction-network exploration on the pinned node; "
    "write the graph + pooled-uncertainty result back onto the pathway ref."
)


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job. Runs the
    autocatpath pipeline in-process on this node and persists the artifact onto the
    pathway ref. ``ctx`` is a precis DispatchContext (store / meta / append_chunk
    / set_meta / set_status / record_failure)."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        pathway_ref_id = int(params["pathway_ref_id"])
        config = dict(params["config"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"autocatpath_explore: malformed params ({exc})")
        return
    force_backend = params.get("force_backend")
    slab_extxyz = params.get("slab_extxyz")
    backend = force_backend or (config.get("mlip") or {}).get("backend", "?")
    slab_src = "injected structure" if slab_extxyz else "label-built slab"
    ctx.append_chunk(
        "job_event",
        f"autocatpath_explore: {config.get('name', '?')} backend={backend} ({slab_src})",
    )

    try:
        from precis_pathway import runner

        artifact = runner.run_pathway(
            config, force_backend=force_backend, slab_extxyz=slab_extxyz
        )
    except Exception as exc:  # pragma: no cover - env/compute dependent
        log.warning("autocatpath_explore: run failed", exc_info=True)
        ctx.record_failure(f"autocatpath_explore: run failed: {exc}")
        return

    from precis_pathway._dispatch_common import finish

    finish(
        ctx,
        artifact,
        pathway_ref_id,
        pathway_slug=params.get("pathway_slug"),
        produced_by=NAME,
        extra_meta={"ran_on": params.get("target_node")},
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("autocatpath_explore runs via dispatch(), not run()")


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
