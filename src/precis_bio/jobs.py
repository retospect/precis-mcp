"""The ``fold`` job_type — predict a structure on the compute lane (ADR 0056).

A *derived* compute-lane job (ADR 0044): it parents on the ``protein`` ref
(via ``KindSpec.can_own_jobs``), is content-addressed (``cache_key``), and
runs off the request path. It reuses the ``ssh_node`` executor — the same one
``struct_relax`` / ``retrosynth`` use to run a container on a pinned node —
routed by ``params.target_node`` (``PRECIS_FOLD_NODE``).

The dispatch branches on the engine's **transport** (ADR 0056 §4):

* **inprocess** (``stub``) — runs inside the dispatch, writes the fold back.
* **container** (``alphafold3``) — run through the ``RUNNER``/``STAGER`` hooks:
  the dispatch stages the AF3 input JSON, ssh's a ``docker run`` to the fold
  node, and parses the mmCIF + summary-confidences it drops (the
  ``struct_relax`` container pattern). There is no ``service`` transport —
  AF3 is a one-shot GPU container, not a long-running HTTP planner.

All the cluster/NFS boundaries are hooks, so tests stub them to run the whole
orchestration + write-back without a GPU or the image.

Registered via the ``precis.job_types`` entry-point group (see the plugin
entry points in ``pyproject.toml``); ``get_job_type('fold')`` resolves it
once precis-mcp is installed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from precis.workers.job_types import JobTypeSpec
from precis_bio.alphafold import INPUT_FILE
from precis_bio.engine import DEFAULT_SEEDS, resolve_engine
from precis_bio.ir import ProteinFold, normalize_sequence
from precis_bio.persist import apply_fold_result

log = logging.getLogger(__name__)

# ── container contract (mirrors struct_relax / retrosynth) ────────────────
_NODE = os.environ.get("PRECIS_FOLD_NODE", "")
_CONTAINER_CMD = os.environ.get("PRECIS_FOLD_CONTAINER_CMD", "docker")
#: The shared scratch root the claiming worker + the fold node both see.
_NFS_ROOT = os.environ.get("PRECIS_FOLD_NFS_ROOT", "/shared")
#: The fold node's model directory, mounted read-only into the container.
_MODELS_DIR = os.environ.get("PRECIS_FOLD_MODELS_DIR", "")
#: Wall-clock for one fold (~10 min/157aa + first-run XLA compile; generous).
_FOLD_TIMEOUT_S = int(os.environ.get("PRECIS_FOLD_TIMEOUT_S", "3600"))


def _default_stager(ref_id: int, *, nfs_root: str = _NFS_ROOT) -> tuple[str, str]:
    """``(in_dir, out_dir)`` under the shared scratch tree, created. On the
    shared FS so the same paths resolve on the worker and on the node."""
    base = Path(nfs_root) / "scratch" / f"precis-fold-{ref_id}"
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
    :data:`RUNNER` for a stub that writes a fake output tree into out_dir."""
    local = node == os.environ.get("PRECIS_NODE")
    cmd = argv if local else ["ssh", node, *argv]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout + proc.stderr


#: Overridable hooks (tests monkeypatch these). Runner = cluster boundary,
#: stager = NFS boundary — the same seam as ``struct_relax`` / ``retrosynth``.
RUNNER = _default_runner
STAGER = _default_stager

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The protein ref this fold is written back onto.
        "protein_ref_id": {"type": "integer"},
        # The name (for the AF3 input + output labelling) + the sequence.
        "name": {"type": "string"},
        "sequence": {"type": "string"},
        # Engine selector + its version (image digest in prod) — part of the
        # content address, so a weights bump invalidates the cache.
        "engine": {"type": "string"},
        "engine_version": {"type": "string"},
        "mode": {"type": "string"},
        "seeds": {"type": "array", "items": {"type": "integer"}},
        # The content address (ADR 0007) — same key ⇒ zero recompute.
        "cache_key": {"type": "string"},
        # The node this fold pins itself to (the claim gate): only that node's
        # worker claims it. NULL ⇒ any node (used only by the in-process stub).
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["protein_ref_id", "sequence", "cache_key"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: No node capability required — routing is by ``target_node``, and the engine
#: (stub in-process, or the AF3 container the node holds) needs no advertised
#: ``EXECUTOR_PROVIDES`` flag. Empty ⊆ any executor's provides.
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = "Predict a protein structure from its sequence on the compute lane."


def _seeds(params: dict[str, Any]) -> list[int]:
    raw = params.get("seeds")
    if isinstance(raw, list) and raw:
        try:
            return [int(s) for s in raw]
        except (TypeError, ValueError):
            pass
    return list(DEFAULT_SEEDS)


def run_fold(params: dict[str, Any]) -> ProteinFold:
    """Fold from job params — the engine call, decoupled from any store/executor
    so it is unit-testable.

    For an in-process engine (``stub``) this runs the predictor directly. A
    container engine (``alphafold3``) is not run here — the dispatch routes it
    through :func:`_run_container` instead — so ``engine.fold`` raising is the
    correct guard for a caller that reaches this by mistake.
    """
    engine = resolve_engine(params.get("engine"))
    return engine.fold(str(params["sequence"]), seeds=_seeds(params))


def _run_container(ctx: Any, params: dict[str, Any], engine: Any) -> ProteinFold | None:
    """Run a container engine on the fold node + parse its output.

    Stages the AF3 input JSON, ssh's the ``engine.run_argv`` container to the
    node via :data:`RUNNER`, and reads the emitted mmCIF + summary-confidences
    via ``engine.parse``. Records the failure + returns ``None`` on any error so
    the job bubbles cleanly. Mirrors ``retrosynth._run_container``.
    """
    ref_id = int(params["protein_ref_id"])
    sequence = normalize_sequence(str(params["sequence"]))
    name = str(params.get("name") or f"protein-{ref_id}")
    seeds = _seeds(params)
    node = str(params.get("target_node") or _NODE)
    models_dir = _MODELS_DIR
    if not node:
        ctx.record_failure(
            "fold: container engine needs a fold node — set PRECIS_FOLD_NODE"
        )
        return None
    if not models_dir:
        ctx.record_failure(
            "fold: container engine needs the model weights — set "
            "PRECIS_FOLD_MODELS_DIR to the node's AF3 models dir"
        )
        return None

    in_dir, out_dir = STAGER(ref_id)
    Path(in_dir, INPUT_FILE).write_text(
        json.dumps(engine.build_input(name=name, sequence=sequence, seeds=seeds))
    )
    argv = engine.run_argv(
        ref_id=ref_id,
        in_dir=in_dir,
        out_dir=out_dir,
        name=name,
        sequence=sequence,
        seeds=seeds,
        container_cmd=_CONTAINER_CMD,
        models_dir=models_dir,
    )
    ctx.append_chunk("job_event", f"fold[{engine.name}] on {node}: {' '.join(argv)}")

    try:
        rc, output = RUNNER(argv, node=node, timeout=_FOLD_TIMEOUT_S)
    except Exception as exc:
        log.warning("fold: runner raised", exc_info=True)
        ctx.record_failure(f"fold: runner failed: {exc}")
        return None
    ctx.append_chunk("job_event", f"container rc={rc}\n{output[-2000:]}")

    if rc != 0:
        ctx.record_failure(f"fold: container rc={rc} — see the log event")
        return None
    try:
        fold = engine.parse(out_dir, name=name, sequence=sequence, seeds=seeds)
    except Exception as exc:
        log.warning("fold: parse raised", exc_info=True)
        ctx.record_failure(f"fold: could not parse AF3 output: {exc}")
        return None
    if not fold.folded:
        ctx.record_failure(
            "fold: container produced no model mmCIF — see the log event"
        )
        return None
    return fold


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed fold job.

    Runs the predictor, writes the fold back onto the ``protein`` ref, and
    marks the job succeeded. ``ctx`` is a
    :class:`~precis.workers.executors._context.DispatchContext`.
    """
    params = (ctx.meta or {}).get("params") or {}
    try:
        protein_ref_id = int(params["protein_ref_id"])
        sequence = normalize_sequence(str(params["sequence"]))
        cache_key = str(params["cache_key"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"fold: malformed params ({exc})")
        return

    engine = resolve_engine(params.get("engine"))
    transport = getattr(engine, "transport", "inprocess")
    if transport == "container":
        fold = _run_container(ctx, params, engine)
        if fold is None:
            return  # failure already recorded
    else:
        try:
            fold = run_fold(params)
        except Exception as exc:  # engine blew up — bubble, don't crash worker.
            ctx.record_failure(f"fold: engine failed on {sequence[:24]!r}… ({exc})")
            return

    apply_fold_result(ctx.store, protein_ref_id, fold, cache_key=cache_key)

    plddt = f"{fold.plddt_mean:.1f}" if fold.plddt_mean is not None else "?"
    ctx.set_meta(cache_key=cache_key, folded=fold.folded, plddt_mean=fold.plddt_mean)
    ctx.append_chunk(
        "job_summary",
        f"fold[{fold.engine}] {fold.mode}: {fold.n_residues} residue(s), "
        f"mean pLDDT {plddt}, pTM {fold.ptm} (cache_key {cache_key[:12]}…). The "
        f"next identical fold is a zero-compute cache hit.",
    )
    ctx.set_status("succeeded")


def _run_placeholder(*_a: Any, **_kw: Any) -> None:
    """``JobTypeSpec.run`` is required but ``ssh_node`` uses ``dispatch``; this
    is never called (mirrors ``struct_relax._run`` / ``retrosynth``)."""
    raise RuntimeError("fold runs via dispatch(), not run()")


FOLD_SPEC = JobTypeSpec(
    name="fold",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run_placeholder,
    dispatch=_dispatch,
)
