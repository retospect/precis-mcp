"""The ``retrosynth`` job_type — plan a route on the compute lane (ADR 0056).

A *derived* compute-lane job (ADR 0044): it parents on the ``route`` ref
(via ``KindSpec.can_own_jobs``), is content-addressed (``cache_key``), and
runs off the request path. It reuses the ``ssh_node`` executor — the same
one ``struct_relax`` uses to run a container on a pinned node — routed by
``params.target_node`` (``PRECIS_CHEM_ROUTE_NODE``).

The dispatch branches on the engine's **transport** (ADR 0056 §4):

* **inprocess** (``stub``) — runs inside the dispatch, writes the route back.
* **container** (``aizynth``) — run through the ``RUNNER``/``STAGER`` hooks: the
  dispatch stages the target, ssh's a ``podman run`` to the route node, and
  reads the shim-normalized ``route.json`` (falling back to the engine's native
  output) into the same IR (the ``struct_relax`` container pattern).
* **service** (``askcos``) — POST the target to a running deployment's REST API
  (the ``SERVICE_CALLER`` hook), then normalize the returned paths to
  ``route.json`` with the standalone LinChemIn normalizer container (the
  ``NORMALIZER`` hook) → the *same* :func:`parse_syngraph`.

All the cluster/NFS/network boundaries are hooks, so tests stub them to run the
whole orchestration + write-back without a cluster.

Registered via the ``precis.job_types`` entry-point group (see the plugin
entry points in ``pyproject.toml``); ``get_job_type('retrosynth')`` resolves
it once precis-mcp is installed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from precis.workers.job_types import JobTypeSpec
from precis_chem.askcos import (
    TREE_SEARCH_PATH,
    build_treebuilder_request,
    extract_paths,
)
from precis_chem.engine import DEFAULT_MAX_STEPS, resolve_engine
from precis_chem.ir import RouteGraph
from precis_chem.normalize import ROUTE_FILE, parse_syngraph
from precis_chem.persist import apply_route_result

log = logging.getLogger(__name__)

# ── container / service contract (mirrors struct_relax) ───────────────────
_NODE = os.environ.get("PRECIS_CHEM_ROUTE_NODE", "")
_CONTAINER_CMD = os.environ.get("PRECIS_CHEM_CONTAINER_CMD", "podman")
#: The shared scratch root the claiming worker + the route node both see.
_NFS_ROOT = os.environ.get("PRECIS_CHEM_NFS_ROOT", "/shared")
#: Raw planner output staged for the normalizer container (service transport).
RAW_FILE = "raw.json"
#: HTTP timeout for a synchronous ASKCOS tree search (its MCTS wall-clock plus
#: slack — the endpoint blocks until the search completes).
_SERVICE_TIMEOUT_S = int(os.environ.get("PRECIS_CHEM_SERVICE_TIMEOUT_S", "600"))


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


def _default_service_caller(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST ``payload`` to a service engine's REST endpoint; return parsed JSON.

    The URL is **operator-configured trusted infra** (``PRECIS_ASKCOS_URL`` —
    like the DB DSN / LLM base URL), not an agent-supplied URL, so it is exempt
    from the ``safe_fetch`` SSRF guard (whose block-list would reject the
    private cluster IP the service lives on). Tests swap :data:`SERVICE_CALLER`.
    """
    import httpx

    resp = httpx.post(url, json=payload, timeout=_SERVICE_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def _build_normalizer_argv(
    *,
    ref_id: int,
    in_dir: str,
    out_dir: str,
    input_format: str,
    engine: str,
    engine_version: str,
    image: str,
    container_cmd: str,
) -> list[str]:
    """The ``run`` argv for the standalone LinChemIn normalizer container —
    raw planner paths in (``/work/in/raw.json``) → ``route.json`` out."""
    return [
        container_cmd,
        "run",
        "--rm",
        "--name",
        f"precis-normalize-{ref_id}",
        "-v",
        f"{in_dir}:/work/in:ro",
        "-v",
        f"{out_dir}:/work/out",
        image,
        "python",
        "/usr/local/lib/to_route.py",
        "--input-format",
        input_format,
        f"/work/in/{RAW_FILE}",
        f"/work/out/{ROUTE_FILE}",
        engine,
        engine_version,
    ]


def _default_normalizer(
    *,
    raw_json: str,
    input_format: str,
    engine: str,
    engine_version: str,
    node: str,
    ref_id: int,
    image: str,
) -> str | None:
    """Normalize a service engine's raw paths → ``route.json`` **text**.

    Stages ``raw.json`` onto the shared tree, runs the ``precis-normalizer``
    container (LinChemIn) on the route node via :data:`RUNNER`, and returns the
    emitted ``route.json`` text (``None`` on any failure). Tests swap
    :data:`NORMALIZER` for a stub that returns a canned ``route.json``."""
    in_dir, out_dir = STAGER(ref_id)
    Path(in_dir, RAW_FILE).write_text(raw_json)
    argv = _build_normalizer_argv(
        ref_id=ref_id,
        in_dir=in_dir,
        out_dir=out_dir,
        input_format=input_format,
        engine=engine,
        engine_version=engine_version,
        image=image,
        container_cmd=_CONTAINER_CMD,
    )
    rc, _output = RUNNER(argv, node=node)
    route = Path(out_dir) / ROUTE_FILE
    if rc != 0 or not route.exists():
        return None
    return route.read_text()


#: Overridable hooks (tests monkeypatch these). Runner = cluster boundary,
#: stager = NFS boundary (same seam as ``struct_relax``); service caller =
#: network boundary; normalizer = the standalone LinChemIn container.
RUNNER = _default_runner
STAGER = _default_stager
SERVICE_CALLER = _default_service_caller
NORMALIZER = _default_normalizer

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
    """Run a container engine on the route node + parse its output.

    Engine-driven (not aizynth-specific): stages the target, ssh's the
    ``engine.run_argv`` container to the node via :data:`RUNNER`, and reads the
    shim-normalized ``route.json`` (slice 2, engine-agnostic) — falling back to
    the engine's native output (``engine.native_output`` +
    ``engine.native_parser``) when the shim skipped normalization. Records the
    failure + returns ``None`` on any error so the job bubbles cleanly. Mirrors
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
    argv = engine.run_argv(
        ref_id=ref_id,
        in_dir=in_dir,
        out_dir=out_dir,
        smiles=smiles,
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

    native_name = getattr(engine, "native_output", None)
    route = Path(out_dir) / ROUTE_FILE
    native = Path(out_dir) / native_name if native_name else None
    if rc != 0 or not (route.exists() or (native and native.exists())):
        want = ROUTE_FILE + (f"/{native_name}" if native_name else "")
        ctx.record_failure(
            f"retrosynth: container rc={rc}, no {want} — see the log event"
        )
        return None
    if route.exists():
        try:
            return parse_syngraph(
                route.read_text(), target=smiles, engine_version=engine.version
            )
        except Exception as exc:
            # A garbled route.json still lets us fall back to the native output.
            log.warning("retrosynth: bad %s, trying native", ROUTE_FILE, exc_info=True)
            ctx.append_chunk(
                "job_event", f"bad {ROUTE_FILE} ({exc}); falling back to {native_name}"
            )
    if native is not None and native.exists() and hasattr(engine, "native_parser"):
        try:
            return engine.native_parser(
                native.read_text(), target=smiles, engine_version=engine.version
            )
        except Exception as exc:
            ctx.record_failure(f"retrosynth: malformed {native_name}: {exc}")
            return None
    ctx.record_failure(f"retrosynth: no readable {ROUTE_FILE} and no native parser")
    return None


def _run_service(ctx: Any, params: dict[str, Any], engine: Any) -> RouteGraph | None:
    """Run a service engine (ASKCOS) over its REST API + normalize the result.

    POSTs the target to the deployment's Tree-Builder endpoint (the
    :data:`SERVICE_CALLER` hook), extracts the returned paths, and normalizes
    them to ``route.json`` with the standalone LinChemIn container (the
    :data:`NORMALIZER` hook) → the same :func:`parse_syngraph`. Records the
    failure + returns ``None`` on any error so the job bubbles cleanly.
    """
    ref_id = int(params["route_ref_id"])
    smiles = str(params["target"])
    node = str(params.get("target_node") or _NODE)
    endpoint = getattr(engine, "endpoint", None)
    if not endpoint:
        ctx.record_failure(
            "retrosynth: service engine needs an endpoint — set PRECIS_ASKCOS_URL "
            "to a running ASKCOS v2 deployment"
        )
        return None
    if not node:
        ctx.record_failure(
            "retrosynth: service engine needs a route node to run the normalizer "
            "container — set PRECIS_CHEM_ROUTE_NODE"
        )
        return None

    max_steps = int(params.get("max_steps") or DEFAULT_MAX_STEPS)
    url = endpoint.rstrip("/") + TREE_SEARCH_PATH
    payload = build_treebuilder_request(smiles, max_steps=max_steps)
    ctx.append_chunk("job_event", f"route[{engine.name}] POST {url}")
    try:
        response = SERVICE_CALLER(url, payload)
    except Exception as exc:
        log.warning("retrosynth: service call raised", exc_info=True)
        ctx.record_failure(f"retrosynth: ASKCOS call failed: {exc}")
        return None

    paths = extract_paths(response)
    ctx.append_chunk("job_event", f"ASKCOS returned {len(paths)} path(s)")
    if not paths:
        # No route found — a legitimate unsolved result, not an error.
        return RouteGraph(
            target=smiles,
            engine=engine.name,
            engine_version=engine.version,
            steps=[],
            solved=False,
            provenance={"engine": engine.name, "n_routes": 0},
        )

    try:
        route_text = NORMALIZER(
            raw_json=json.dumps(paths),
            input_format=engine.input_format,
            engine=engine.name,
            engine_version=engine.version,
            node=node,
            ref_id=ref_id,
            image=engine.image,
        )
    except Exception as exc:
        log.warning("retrosynth: normalizer raised", exc_info=True)
        ctx.record_failure(f"retrosynth: normalizer failed: {exc}")
        return None
    if not route_text:
        ctx.record_failure("retrosynth: normalizer produced no route.json")
        return None
    try:
        return parse_syngraph(route_text, target=smiles, engine_version=engine.version)
    except Exception as exc:
        ctx.record_failure(f"retrosynth: malformed normalized {ROUTE_FILE}: {exc}")
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
    transport = getattr(engine, "transport", "inprocess")
    if transport == "container":
        graph = _run_container(ctx, params, engine)
        if graph is None:
            return  # failure already recorded
    elif transport == "service":
        graph = _run_service(ctx, params, engine)
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
