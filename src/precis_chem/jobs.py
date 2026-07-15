"""The ``retrosynth`` job_type — plan a route on the compute lane (ADR 0056).

A *derived* compute-lane job (ADR 0044): it parents on the ``route`` ref
(via ``KindSpec.can_own_jobs``), is content-addressed (``cache_key``), and
runs off the request path. It reuses the ``ssh_node`` executor — the same
one ``struct_relax`` uses to run a container on a pinned node — routed by
``params.target_node`` (``PRECIS_CHEM_ROUTE_NODE``).

Slice 1a: the in-process ``stub`` engine runs inside the dispatch and writes
the route back. Slice 1b swaps in AiZynthFinder by having this dispatch build
a ``podman run`` argv from the engine's image (the ``struct_relax`` container
pattern) and parse the container's ``result.json`` — no change to the kind or
the verb surface.

Registered via the ``precis.job_types`` entry-point group (see the plugin
entry points in ``pyproject.toml``); ``get_job_type('retrosynth')`` resolves
it once precis-mcp is installed.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec
from precis_chem.engine import DEFAULT_MAX_STEPS, resolve_engine
from precis_chem.ir import RouteGraph
from precis_chem.persist import apply_route_result

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The route ref this plan is written back onto.
        "route_ref_id": {"type": "integer"},
        # Target molecule (SMILES).
        "target": {"type": "string"},
        # Engine selector + its version (image digest in prod) — part of the
        # content address, so a model bump invalidates the cache.
        "engine": {"type": "string"},
        "engine_version": {"type": "string"},
        # The content address (ADR 0007) — same key ⇒ zero recompute.
        "cache_key": {"type": "string"},
        "max_steps": {"type": "integer"},
        # The node this plan pins itself to (the claim gate): only that node's
        # worker claims it. NULL ⇒ any node (used only by the in-process stub).
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["route_ref_id", "target", "cache_key"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: No node capability required — routing is by ``target_node``, and the engine
#: (stub in-process, or a container the node builds) needs no advertised
#: ``EXECUTOR_PROVIDES`` flag. Empty ⊆ any executor's provides, so ``ssh_node``
#: (which provides ``has_gpaw``) is compatible.
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = "Plan a retrosynthesis route to a target molecule on the compute lane."


def run_retrosynth(params: dict[str, Any]) -> RouteGraph:
    """Plan a route from job params — the engine call, decoupled from any
    store/executor so it is unit-testable.

    For an in-process engine (``stub``) this runs the planner directly. For a
    container engine (``aizynth``) the engine's ``plan`` raises — slice 1b wires
    the ``podman run`` argv here; until then a container engine selected in prod
    fails the job cleanly (the dispatch surfaces the message) rather than
    silently producing nothing.
    """
    target = str(params["target"])
    engine = resolve_engine(params.get("engine"))
    max_steps = int(params.get("max_steps") or DEFAULT_MAX_STEPS)
    return engine.plan(target, max_steps=max_steps)


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed retrosynth job.

    Runs the planner, writes the route back onto the ``route`` ref, and marks
    the job succeeded. ``ctx`` is a
    :class:`~precis.workers.executors._context.DispatchContext`.
    """
    params = (ctx.meta or {}).get("params") or {}
    try:
        route_ref_id = int(params["route_ref_id"])
        target = str(params["target"])
        cache_key = str(params["cache_key"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"retrosynth: malformed params ({exc})")
        return

    try:
        graph = run_retrosynth(params)
    except NotImplementedError as exc:
        # A container engine selected before slice 1b wired the container.
        ctx.record_failure(str(exc))
        return
    except Exception as exc:  # engine blew up — bubble, don't crash the worker.
        ctx.record_failure(f"retrosynth: engine failed on {target!r} ({exc})")
        return

    apply_route_result(ctx.store, route_ref_id, graph, cache_key=cache_key)

    state = "solved" if graph.solved else "unsolved (no buyable leaves)"
    ctx.set_meta(cache_key=cache_key, solved=graph.solved)
    ctx.append_chunk(
        "job_summary",
        f"route[{graph.engine}] {state}: {len(graph.steps)} step(s) → "
        f"{target} (cache_key {cache_key[:12]}…). The next identical plan is a "
        f"zero-compute cache hit.",
    )
    ctx.set_status("succeeded")


def _run_placeholder(*_a: Any, **_kw: Any) -> None:
    """``JobTypeSpec.run`` is required but ``ssh_node`` uses ``dispatch``; this
    is never called (mirrors ``struct_relax._run``)."""
    raise RuntimeError("retrosynth runs via dispatch(), not run()")


RETROSYNTH_SPEC = JobTypeSpec(
    name="retrosynth",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run_placeholder,
    dispatch=_dispatch,
)
