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

**Detached submit/poll (§H piece 4, gr187627).** ``_dispatch`` above stays
as the legacy blocking fallback (a mixed-version window where an older
``ssh_node`` build only knows ``dispatch`` still works), but ``ssh_node``
prefers ``_submit``/``_poll``/``_kill`` when both ``submit`` and ``poll``
are set: ``_submit`` parses/validates params exactly like ``_dispatch``
then launches :func:`precis_pathway.runner.submit_seed_partial_detached`
(a detached child, NOT docker — no autocatpath image exists, and the child
process already provides gr191351's MACE/CUDA isolation) and returns its
handle without blocking; ``_poll`` calls
:func:`~precis_pathway.runner.poll_seed_partial_detached` each pass and, on
a terminal result, does exactly ``_dispatch``'s post-run tail (same
``set_meta`` shape, same ``job_summary`` chunk) so a caller reading
``meta.partial`` can't tell which protocol produced it; ``_kill`` is the
wall-clock backstop ``ssh_node`` calls past ``meta.deadline``.
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
        # Provenance: the pathway ref this seed feeds (stamped onto the job's
        # own meta as ``pathway_ref`` so the pathway page can link the per-seed
        # run_log). Absent on pre-existing queued rows.
        "pathway_ref_id": {"type": ["integer", "null"]},
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


def _provenance_meta(params: dict[str, Any]) -> dict[str, Any]:
    """``{"pathway_ref": <int>}`` when the mint carried ``pathway_ref_id``,
    else empty — pre-existing queued rows (minted before the param existed)
    stamp nothing rather than a null."""
    pref = params.get("pathway_ref_id")
    return {"pathway_ref": pref} if isinstance(pref, int) else {}


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job. Runs one
    ``run_one_seed`` unit and writes the partial onto this job's OWN meta —
    the aggregate job reads it back via the seed-todo tree, not a shared
    file.

    Only the detached submit/poll protocol (``_poll`` below) persists a
    ``run_log`` chunk from the child's captured output — this blocking
    fallback runs in-process (``run_seed_partial_subprocess``) and doesn't
    go through :func:`~precis_pathway.runner.poll_seed_partial_detached`'s
    scratch-dir tail capture."""
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
    except runner.ChildKilledError as exc:
        # INFRA-class (parked-leaf-recovery, docs/backlog/
        # parked-leaf-recovery.md): the child died by signal (SIGKILL/OOM)
        # or exited without writing its result envelope — the compute
        # never ran. Tag at this generic call site (not per job_type) so
        # the bounded infra-retry in ``_job_bubble.bubble_job_failure``
        # applies instead of an immediate content-class latch.
        log.warning("autocatpath_seed: run failed (child killed)", exc_info=True)
        ctx.record_failure(
            f"autocatpath_seed: run failed: {exc}", open_tag="infra:child-killed"
        )
        return
    except Exception as exc:  # pragma: no cover - env/compute dependent
        log.warning("autocatpath_seed: run failed", exc_info=True)
        ctx.record_failure(f"autocatpath_seed: run failed: {exc}")
        return

    partial = result["partial"]
    # The aggregate job's whole input: seed/model identity + the raw
    # run_one_seed partial + any lattice this unit relaxed + the per-state
    # geometry (``structures``, model_index==0 units only — this is how the
    # relaxed extxyz crosses the job boundary to the aggregate's
    # ``structures_extxyz`` merge; dropping it here is exactly how pathway
    # geometry silently vanished from the whole fan-out era). Top-level job
    # meta (not params) — mirrors autocatpath_explore's own contract.
    ctx.set_meta(
        content_key=params.get("content_key"),
        seed=seed,
        model_index=model_index,
        model=result["model"],
        partial=partial,
        lattice=result.get("lattice") or {},
        structures=result.get("structures") or {},
        **_provenance_meta(params),
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


def _submit(ctx: Any, spec: Any) -> dict[str, Any] | None:
    """Detached submit half (§H piece 4) — same param parsing/validation as
    ``_dispatch``; malformed params fail the SAME way, ``ctx.record_failure``
    + return ``None``. ``ssh_node._run_one`` still stamps
    ``meta.compute_handle=None`` + a deadline right after a submit that
    returns ``None``, but that's harmless: ``record_failure`` already drove
    the row to ``STATUS:failed``, so it drops out of ``_polling_jobs``'
    ``STATUS:running`` selection and nothing ever polls the null handle.

    Must NOT call ``ctx.set_status`` on the happy path (the executor
    contract, ``JobTypeSpec.submit``'s docstring) — the row stays
    ``STATUS:running`` (already set by the claim) until ``_poll`` drives it
    terminal."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        config = dict(params["config"])
        seed = int(params["seed"])
        model_index = int(params["model_index"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"autocatpath_seed: malformed params ({exc})")
        return None
    force_backend = params.get("force_backend")
    slab_extxyz = params.get("slab_extxyz")

    ctx.append_chunk(
        "job_event",
        f"autocatpath_seed: {config.get('name', '?')} seed={seed} "
        f"model#{model_index} (detached submit)",
    )

    try:
        from precis_pathway import runner

        return runner.submit_seed_partial_detached(
            config,
            seed,
            model_index,
            force_backend=force_backend,
            slab_extxyz=slab_extxyz,
        )
    except Exception as exc:  # pragma: no cover - spawn/env dependent
        log.warning("autocatpath_seed: submit failed", exc_info=True)
        ctx.record_failure(f"autocatpath_seed: submit failed: {exc}")
        return None


def _poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half (§H piece 4) — on "done", does exactly
    ``_dispatch``'s post-run tail (same ``set_meta`` fields, same
    ``job_summary`` chunk shape) so the aggregate job's read of
    ``meta.partial`` can't tell which protocol produced it. ``content_key``/
    ``seed``/``model_index`` come from ``ctx.meta['params']`` — the executor
    rebuilds ``ctx`` fresh from the DB row every poll tick
    (``ssh_node._polling_jobs``), so this always sees the row's current
    params, not a submit-time snapshot."""
    from precis_pathway import runner

    status = runner.poll_seed_partial_detached(handle)
    state = status.get("state")
    if state == "running":
        return False

    if state == "failed":
        tail = status.get("tail") or ""
        reason = f"autocatpath_seed: run failed: {status.get('error')}"
        if tail:
            reason = f"{reason}\n--- tail ---\n{tail}"
        # INFRA-class (parked-leaf-recovery, docs/backlog/
        # parked-leaf-recovery.md): ``poll_seed_partial_detached`` sets
        # ``"infra"`` only on its no-envelope branch (the child exited
        # without writing result.json) — mirrors the blocking dispatch
        # path's ``ChildKilledError`` above, just observed from the
        # detached side where a returncode was never captured.
        open_tag = "infra:child-killed" if status.get("infra") else None
        ctx.record_failure(reason, open_tag=open_tag)
        return True

    # state == "done"
    params = (ctx.meta or {}).get("params") or {}
    seed = int(params["seed"])
    model_index = int(params["model_index"])
    result = status["result"]
    partial = result["partial"]
    ctx.set_meta(
        content_key=params.get("content_key"),
        seed=seed,
        model_index=model_index,
        model=result["model"],
        partial=partial,
        lattice=result.get("lattice") or {},
        structures=result.get("structures") or {},  # see _dispatch's note
        **_provenance_meta(params),
    )
    tail = (status.get("tail") or "")[-4000:]
    if tail:
        ctx.append_chunk(
            "run_log",
            f"run log (seed={seed}, model#{model_index}, last {len(tail)} chars):\n{tail}",
        )
    n_states = len(partial.get("states") or {})
    n_warnings = len(partial.get("warnings") or [])
    ctx.append_chunk(
        "job_summary",
        f"autocatpath_seed: seed={seed} model={result['model']}: "
        f"{n_states} state(s), {n_warnings} warning(s).",
    )
    ctx.set_status("succeeded")
    return True


def _kill(ctx: Any, handle: Any) -> None:
    """``ssh_node`` wall-clock kill hook (§H piece 2) — best-effort SIGKILL
    of the detached child's process group plus scratch-dir cleanup."""
    from precis_pathway import runner

    runner.kill_seed_partial_detached(handle)


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
    submit=_submit,
    poll=_poll,
    kill=_kill,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["NAME", "SPEC", "load"]
