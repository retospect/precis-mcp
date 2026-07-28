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
"""

from __future__ import annotations

import logging
import math
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


def _summarize(artifact: dict[str, Any]) -> dict[str, Any]:
    """Reduce a run artifact to the scalar summary a caller ranks on.

    Computes the rate-limiting **barrier** (eV), the energetic **span**, and the
    **low_confidence** flag from the reaction graph (``analysis.summarize`` — pure
    graph math, no ML). This is the seam the precis quest harvests: it lifts the
    barrier onto the candidate structure's meta and ranks the design on it. Empty
    on any failure — a missing summary must never fail the run/persist.
    """
    try:
        from precis_pathway import analysis

        graph = artifact.get("graph_json") or {}
        results = artifact.get("results_json") or {}
        root, target = analysis.roots(graph, results)
        summ = analysis.summarize(graph, root, target)
        out: dict[str, Any] = {}
        ea = (summ.get("rate_limiting") or {}).get("ea")
        if (
            isinstance(ea, (int, float))
            and not isinstance(ea, bool)
            and math.isfinite(ea)
        ):
            out["barrier"] = float(ea)
        span = summ.get("span")
        if (
            isinstance(span, (int, float))
            and not isinstance(span, bool)
            and math.isfinite(span)
        ):
            out["span"] = float(span)
        if "low_confidence" in summ:
            out["low_confidence"] = bool(summ["low_confidence"])
        return out
    except Exception:  # pragma: no cover - defensive
        log.warning("autocatpath_explore: summary failed", exc_info=True)
        return {}


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

    summary = _summarize(artifact)  # {barrier, span, low_confidence} or {}

    try:
        from precis_pathway.persist import persist_result

        pathway_extra: dict[str, Any] = {
            "produced_by": NAME,
            "slice": 1,
            "ran_on": params.get("target_node"),
        }
        # Self-describe the pathway with the scalar summary (rate_Ea alias) so a
        # by-total / by-intermediate view need not recompute it from the graph.
        if "barrier" in summary:
            pathway_extra["rate_Ea"] = summary["barrier"]
        for k in ("span", "low_confidence"):
            if k in summary:
                pathway_extra[k] = summary[k]
        persist_result(
            ctx.store,
            pathway_ref_id,
            artifact,
            pathway_slug=params.get("pathway_slug"),
            extra_meta=pathway_extra,
        )
    except Exception as exc:
        log.warning("autocatpath_explore: persist failed", exc_info=True)
        ctx.record_failure(f"autocatpath_explore: persist failed: {exc}")
        return

    r = artifact["results_json"]
    # Emit the scalar summary + the pathway ref onto the JOB meta — the contract
    # the precis quest harvest reads (barrier → candidate meta → frontier).
    ctx.set_meta(
        content_key=artifact["content_key"],
        n_states=len(r["nodes"]),
        pathway_ref=pathway_ref_id,
        **summary,
    )
    b_s = f", barrier {summary['barrier']:g} eV" if "barrier" in summary else ""
    ctx.append_chunk(
        "job_summary",
        f"autocatpath: {len(r['nodes'])} states, {len(r['edges'])} steps "
        f"({r['n_samples']} samples, backend {r['backend']}) → pathway "
        f"#{pathway_ref_id}{b_s}.",
    )
    ctx.set_status("succeeded")


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
