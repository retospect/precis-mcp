"""The ``retrosynth`` job_type — plan a route on the compute lane (ADR 0056).

A *derived* compute-lane job (ADR 0044): it parents on the ``route`` ref
(via ``KindSpec.can_own_jobs``), is content-addressed (``cache_key``), and
runs off the request path. It reuses the ``ssh_node`` executor — the same
one ``struct_relax`` uses to run a container on a pinned node — routed by
``params.target_node`` (``PRECIS_CHEM_ROUTE_NODE``).

An in-process engine (``stub``) runs inside the dispatch and writes the route
back. A container engine (``aizynth``) is run through the ``RUNNER``/``STAGER``
hooks below — the dispatch stages the target, ssh's a ``podman run`` to the
route node, and parses the container's ``trees.json`` into the same IR (the
``struct_relax`` container pattern). The hooks are the single cluster/NFS
boundary, so tests stub them to run the whole orchestration + write-back
without a cluster.

Registered via the ``precis.job_types`` entry-point group (see the plugin
entry points in ``pyproject.toml``); ``get_job_type('retrosynth')`` resolves
it once precis-mcp is installed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from precis.workers.job_types import JobTypeSpec
from precis_chem.aizynth import TREES_FILE, build_aizynth_argv, parse_aizynth_trees
from precis_chem.engine import DEFAULT_MAX_STEPS, resolve_engine
from precis_chem.ir import RouteGraph
from precis_chem.normalize import ROUTE_FILE, parse_syngraph
from precis_chem.persist import apply_route_result

log = logging.getLogger(__name__)

# ── container contract (mirrors struct_relax) ────────────────────────────
_NODE = os.environ.get("PRECIS_CHEM_ROUTE_NODE", "")
_CONTAINER_CMD = os.environ.get("PRECIS_CHEM_CONTAINER_CMD", "podman")
#: The shared scratch root the claiming worker + the route node both see.
_NFS_ROOT = os.environ.get("PRECIS_CHEM_NFS_ROOT", "/shared")


def _default_stager(ref_id: int, *, nfs_root: str = _NFS_ROOT) -> tuple[str, str]:
    """``(in_dir, out_dir)`` under the shared scratch tree, created. On the
    shared FS so the same paths resolve on the worker and on the node."""
    base = Path(nfs_root) / "scratch" / f"precis-route-{ref_id}"
    in_dir, out_dir = base / "in", base / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(in_dir), str(out_dir)


def _default_runner(
    argv: list[str], *, node: str, timeout: int | None = None
) -> tuple[int, str]:
    """Run the container ``argv`` on ``node``; return ``(returncode, output)``.
    Runs directly when this worker *is* the node (the claim gate co-locates
    them), else ssh's. The single execution boundary — tests swap
    :data:`RUNNER` for a stub that writes a fake ``trees.json`` into out_dir."""
    local = node == os.environ.get("PRECIS_NODE")
    cmd = argv if local else ["ssh", node, *argv]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout + proc.stderr


#: Overridable hooks (tests monkeypatch these). Runner = cluster boundary,
#: stager = NFS boundary — same seam as ``struct_relax``.
RUNNER = _default_runner
STAGER = _default_stager

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

    For an in-process engine (``stub``) this runs the planner directly. A
    container engine (``aizynth``) is not run here — the dispatch routes it
    through :func:`_run_container` instead — so ``engine.plan`` raising is the
    correct guard for a caller that reaches this by mistake.
    """
    target = str(params["target"])
    engine = resolve_engine(params.get("engine"))
    max_steps = int(params.get("max_steps") or DEFAULT_MAX_STEPS)
    return engine.plan(target, max_steps=max_steps)


def _run_container(ctx: Any, params: dict[str, Any], engine: Any) -> RouteGraph | None:
    """Run a container engine (AiZynth) on the route node + parse its output.

    Stages the target SMILES to the shared scratch dir, ssh's the ``podman
    run`` to the node via :data:`RUNNER`, and parses ``trees.json`` from the
    bound out-dir into a :class:`RouteGraph`. Records the failure and returns
    ``None`` on any error (bad node, rc≠0, missing/garbled output) so the job
    bubbles cleanly instead of crashing the worker. Mirrors
    ``struct_relax._dispatch``'s container half.
    """
    ref_id = int(params["route_ref_id"])
    smiles = str(params["target"])
    node = str(params.get("target_node") or _NODE)
    if not node:
        ctx.record_failure(
            "retrosynth: container engine needs a route node — set "
            "PRECIS_CHEM_ROUTE_NODE (and build the wrapper image)"
        )
        return None

    in_dir, out_dir = STAGER(ref_id)
    Path(in_dir, "target.smi").write_text(smiles)
    argv = build_aizynth_argv(
        ref_id=ref_id,
        in_dir=in_dir,
        out_dir=out_dir,
        smiles=smiles,
        image=engine.image,
        container_cmd=_CONTAINER_CMD,
        models_dir=os.environ.get("PRECIS_CHEM_MODELS_DIR") or None,
    )
    ctx.append_chunk("job_event", f"route[{engine.name}] on {node}: {' '.join(argv)}")

    try:
        rc, output = RUNNER(argv, node=node)
    except Exception as exc:
        log.warning("retrosynth: runner raised", exc_info=True)
        ctx.record_failure(f"retrosynth: runner failed: {exc}")
        return None
    ctx.append_chunk("job_event", f"container rc={rc}\n{output[-2000:]}")

    # Prefer the LinChemIn-normalized route.json (slice 2, engine-agnostic + route
    # descriptors); fall back to the engine's native trees.json when the shim
    # skipped normalization (older image / normalizer error). Either produces the
    # same RouteGraph.
    route = Path(out_dir) / ROUTE_FILE
    trees = Path(out_dir) / TREES_FILE
    if rc != 0 or not (route.exists() or trees.exists()):
        ctx.record_failure(
            f"retrosynth: container rc={rc}, no {ROUTE_FILE}/{TREES_FILE} — see the log event"
        )
        return None
    if route.exists():
        try:
            return parse_syngraph(
                route.read_text(), target=smiles, engine_version=engine.version
            )
        except Exception as exc:
            # A garbled route.json still lets us fall back to trees.json below.
            log.warning(
                "retrosynth: bad %s, trying %s", ROUTE_FILE, TREES_FILE, exc_info=True
            )
            ctx.append_chunk(
                "job_event", f"bad {ROUTE_FILE} ({exc}); falling back to {TREES_FILE}"
            )
    try:
        return parse_aizynth_trees(
            trees.read_text(), target=smiles, engine_version=engine.version
        )
    except Exception as exc:
        ctx.record_failure(f"retrosynth: malformed {TREES_FILE}: {exc}")
        return None


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

    engine = resolve_engine(params.get("engine"))
    if getattr(engine, "is_container", False):
        graph = _run_container(ctx, params, engine)
        if graph is None:
            return  # failure already recorded
    else:
        try:
            graph = run_retrosynth(params)
        except Exception as exc:  # engine blew up — bubble, don't crash worker.
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
