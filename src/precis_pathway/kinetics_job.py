"""``autocatpath_kinetics`` job type (kinetics v2 slice 4, D7/D9).

Registered as ``autocatpath_kinetics = precis_pathway.kinetics_job:SPEC``
in ``pyproject.toml``. Reads the aggregated pathway's ``meta.results`` and
the job's ``params.config`` (the effective config, or — for
``put(kind='pathway', mode='recompute_kinetics')`` — that config with a
caller-supplied ``environment``/``objective``/``kinetics.sweep`` laid over
it) and computes, in this order:

1. the D11 TPD check (``meta.kinetics_tpd``, engine >= 0.39);
2. the kinetics solve on the CURRENT engine under ``params.config``,
   refreshing ``meta.kinetics``/``meta.kinetics_v2`` — on the TPD-corrected
   states (provider ``vacuum+tpd``) when the check applied offsets, else
   the check's comparisons fold onto ``kinetics_v2.tpd``. Without this step a
   job on an old pathway left the aggregate's stale ``tof_mf`` in place;
3. the ``autocatpath.kinetics.sweep`` (T, p_total) MF grid
   (``meta.kinetics_sweep``: the competitive coverage map + optimum; from
   engine 0.36 also ``kmc_points`` with ``tof_kmc`` and
   ``correlation_effect``).

From engine 0.38 (slice 10, D10) the persist step restamps
``meta.validation_manifest``/``meta.manifest_id`` on the ref and
``manifest_id`` on the job's own meta and the D8 record.

**Detached submit/poll (non-blocking).** The compute runs as a detached
child (:func:`precis_pathway.runner.submit_kinetics_job_detached`), exactly
the seed job's protocol: ``_submit`` launches it and returns the handle,
``_poll`` reads its ``result.json`` and does the persistence, the seed job's
``_kill``/``_reap`` bound and clean it up. ``ssh_node`` claims ``limit=1``
and a blocking dispatch held its worker thread for up to an hour; the
detached job holds none, and ``requires={}`` takes no gpu slot, so it may
share pollux/spark with seed and aggregate work. ``_dispatch`` stays as the
blocking fallback for an executor build that only knows ``dispatch``.

Gas references: the solve, TPD and sweep rebuild the net and call
``autocatpath.pipeline.ledger_refs``, which re-measures gas / bare-slab
energies with the config's MLIP backend. The host therefore needs that
backend loadable (pollux and spark do). A reference that fails to measure
is omitted with a log line and the term falls back to the legacy zero
bookkeeping — the same behaviour the aggregate and the sweep already have.

Minted by ``autocatpath_aggregate``'s dispatch on every verify- and
neb-tier pathway, and by ``put(kind='pathway', mode='recompute_kinetics')``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from .types import PathwayArtifact

log = logging.getLogger(__name__)

NAME = "autocatpath_kinetics"

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pathway_ref_id": {"type": "integer"},
        "pathway_slug": {"type": ["string", "null"]},
        "config": {"type": "object"},
        "content_key": {"type": ["string", "null"]},
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["pathway_ref_id", "config"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Empty ⊆ any executor's PROVIDES, same seam as the aggregate job. The
#: detached child takes no gpu slot.
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Refresh a verify/neb-tier pathway's kinetics: TPD check, kinetics "
    "re-solve on the current engine and the mean-field (T, p_total) sweep "
    "(autocatpath.kinetics); writes meta.kinetics_v2 + meta.kinetics_sweep "
    "onto the pathway ref. Detached child, no gpu slot."
)

#: What the engine reads off the stored ``meta.results``.
_INPUT_KEYS = ("nodes", "edges", "score", "thermo")
#: What the compute produces; the persist step merges exactly these.
_OUTPUT_KEYS = (
    "kinetics_sweep",
    "kinetics_v2",
    "kinetics_sweep_error",
    "kinetics",
    "kinetics_error",
    "kinetics_tpd",
    "kinetics_tpd_error",
)
#: Fresh success key -> the stale error key it clears from the stored record.
_CLEARS = {
    "kinetics_sweep": "kinetics_sweep_error",
    "kinetics_v2": "kinetics_error",
    "kinetics": "kinetics_error",
    "kinetics_tpd": "kinetics_tpd_error",
}

_DEFAULT_WALL_S = 3600
#: TPD check, kinetics solve and sweep each run under their own ceiling.
_N_STEPS = 3


def _wall_seconds() -> float:
    """The job's wall budget. ``PRECIS_AUTOCATPATH_KINETICS_SECONDS`` is the
    per-step ceiling the three child steps each run under
    (:func:`precis_pathway.runner._kinetics_timeout_s`), so when it is set
    the job gets ``3 x`` that plus headroom — ``ssh_node`` sizes the lease and
    the kill deadline from this (plus its own margin). Unset/invalid/0
    (unbounded steps) keeps the 1 h default."""
    try:
        step = int(os.environ.get("PRECIS_AUTOCATPATH_KINETICS_SECONDS", ""))
    except ValueError:
        return float(_DEFAULT_WALL_S)
    if step <= 0:
        return float(_DEFAULT_WALL_S)
    return float(max(_DEFAULT_WALL_S, _N_STEPS * step + 300))


def overlay_recompute_config(
    config: dict[str, Any],
    *,
    environment: dict[str, Any] | None = None,
    objective: dict[str, Any] | None = None,
    sweep: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``config`` with an ``environment``/``objective``/``kinetics.sweep``
    laid over it for ``mode='recompute_kinetics'``. Engine-free.

    A new ``environment`` is the single source of truth for the gas table,
    so the stored ``conditions.pressures`` and ``kinetics.sticking`` (the
    priced / sticking views of the OLD environment) are dropped — the engine
    refuses a config where they disagree. Its ``T`` must equal the run
    temperature (node energies are priced there; other temperatures are the
    sweep's job) — the engine says so at job time."""
    out = dict(config)
    if environment is not None:
        out["environment"] = dict(environment)
        cond = dict(out.get("conditions") or {})
        cond.pop("pressures", None)
        out["conditions"] = cond
        kin = dict(out.get("kinetics") or {})
        kin.pop("sticking", None)
        out["kinetics"] = kin
    if objective is not None:
        out["objective"] = dict(objective)
    if sweep is not None:
        kin = dict(out.get("kinetics") or {})
        kin["sweep"] = dict(sweep)
        out["kinetics"] = kin
    return out


# ── compute (runs in the detached child, or inline for the fallback) ──────


def compute(
    config: dict[str, Any],
    results: dict[str, Any],
    note: Callable[[str], None],
) -> None:
    """TPD check, kinetics re-solve, sweep — mutates ``results`` in place
    with the ``_OUTPUT_KEYS`` it produced. Never raises (each step lands its
    own ``*_error``); ``note`` receives the progress lines."""
    from precis_pathway import runner

    # Only the inputs: a stale kinetics_v2 must never be folded or kept.
    for key in _OUTPUT_KEYS:
        results.pop(key, None)

    tpd: dict[str, Any] | None = None
    if runner.has_tpd():
        tpd = _tpd_check(runner, config, results, note)

    note("autocatpath_kinetics: kinetics solve")
    artifact = cast("PathwayArtifact", {"results_json": results})
    if tpd is not None and tpd.get("offsets"):
        # offsets applied: solve on the corrected states
        runner.run_kinetics_subprocess(config, artifact, tpd=tpd)
    else:
        runner.run_kinetics_subprocess(config, artifact)
        if tpd is not None:
            runner.fold_tpd(results)
    if "kinetics_error" in results:
        note(
            f"autocatpath_kinetics: kinetics solve failed: {results['kinetics_error']}"
        )
    else:
        tof = (results.get("kinetics_v2") or {}).get("tof_mf")
        note(f"autocatpath_kinetics: kinetics solved (tof_mf={tof})")

    note("autocatpath_kinetics: sweeping (T, p_total)")
    runner.run_kinetics_sweep_subprocess(config, results)
    if "kinetics_sweep_error" in results:
        # a sweep failure is not a pathway-fatal: the v2 record + the error
        # both persist so the surface stays honest.
        note(f"autocatpath_kinetics: {results['kinetics_sweep_error']}")


def _tpd_check(
    runner: Any,
    config: dict[str, Any],
    results: dict[str, Any],
    note: Callable[[str], None],
) -> dict[str, Any] | None:
    """The D11 TPD check; a failure is a job note, never a pathway-fatal
    (the solve and sweep still run on the uncorrected states). Returns the
    ``kinetics_tpd`` payload, ``None`` when the check failed."""
    note("autocatpath_kinetics: TPD check")
    runner.run_kinetics_tpd_subprocess(config, results)
    if "kinetics_tpd_error" in results:
        note(f"autocatpath_kinetics: TPD {results['kinetics_tpd_error']}")
        return None
    tpd: dict[str, Any] = results["kinetics_tpd"]
    parts = [
        f"{ads} {rec.get('status')}"
        for ads, rec in sorted((tpd.get("tpd") or {}).items())
    ]
    note(
        f"autocatpath_kinetics: TPD ({tpd.get('family') or 'no family'}): "
        + (", ".join(parts) or "no desorbing adsorbate")
    )
    return tpd


def run_compute(config: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    """Child entrypoint (``runner._subprocess_main`` mode ``kinetics_job``):
    the :func:`compute` over ``inputs``, returned as the JSON envelope
    ``{"results": <output keys>, "notes": [<job_event lines>]}``."""
    results = {k: inputs.get(k) for k in _INPUT_KEYS}
    notes: list[str] = []
    compute(config, results, notes.append)
    return {
        "results": {k: results[k] for k in _OUTPUT_KEYS if k in results},
        "notes": notes,
    }


# ── persistence (shared by _dispatch and _poll) ───────────────────────────


def _load(ctx: Any) -> tuple[int, dict[str, Any], Any, dict[str, Any]] | None:
    """Parse params and read the pathway; ``None`` after recording the
    failure. Returns ``(pathway_ref_id, config, ref, inputs)``."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        pathway_ref_id = int(params["pathway_ref_id"])
        config = dict(params["config"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"autocatpath_kinetics: malformed params ({exc})")
        return None
    # The pathway's persisted results carry the aggregated nodes/edges/
    # score/thermo the sweep reprices from.
    ref = ctx.store.get_ref(kind="pathway", id=pathway_ref_id)
    if ref is None:
        ctx.record_failure(f"autocatpath_kinetics: pathway #{pathway_ref_id} not found")
        return None
    results = dict((ref.meta or {}).get("results") or {})
    if not results.get("nodes") or not results.get("edges"):
        ctx.record_failure(
            f"autocatpath_kinetics: pathway #{pathway_ref_id} has no "
            "aggregated results to sweep"
        )
        return None
    inputs = {k: results.get(k) for k in _INPUT_KEYS}
    return pathway_ref_id, config, ref, inputs


def _finalize(
    ctx: Any,
    pathway_ref_id: int,
    config: dict[str, Any],
    ref: Any,
    results: dict[str, Any],
) -> bool:
    """Persist ``results`` (inputs + computed outputs) onto the pathway ref
    and the job's meta; ``False`` after ``ctx.record_failure`` (the record is
    saved first, so a failed step stays visible on the surface)."""
    sweep = results.get("kinetics_sweep") or {}
    n_pts = len(sweep.get("points") or [])
    optimum = sweep.get("optimum")
    if optimum and optimum.get("value") is None:
        # the objective had no value at any point: that T and p are the first
        # row of the grid, not an optimum
        ctx.append_chunk(
            "job_event",
            f"autocatpath_kinetics: {n_pts} points; objective "
            f"{optimum.get('objective')} had no value at any point",
        )
    elif optimum:
        ctx.append_chunk(
            "job_event",
            f"autocatpath_kinetics: {n_pts} points; optimum "
            f"T={optimum.get('T')} K, p={optimum.get('p_total')} bar",
        )
    else:
        ctx.append_chunk(
            "job_event", f"autocatpath_kinetics: {n_pts} points; no optimum"
        )
    # slice 5 (D7): the lattice KMC at kmc_points (engine >= 0.36)
    kmc_points = [p for p in (sweep.get("kmc_points") or []) if isinstance(p, dict)]
    if kmc_points:
        ran = sum(1 for p in kmc_points if isinstance(p.get("tof_kmc"), dict))
        ctx.append_chunk(
            "job_event",
            f"autocatpath_kinetics: lattice KMC at {ran} of {len(kmc_points)} kmc_points",
        )

    # persist onto the pathway ref: re-read the meta just before writing
    # (a sibling job may have touched it), merge the computed blocks into
    # meta.results — the same place the aggregate's kinetics record lives
    # — and stamp wholesale (persist.py:129).
    manifest_id: str | None = None
    try:
        cur = dict(
            (ctx.store.get_ref(kind="pathway", id=pathway_ref_id) or ref).meta or {}
        )
        # slice 10 (D10): the manifest is built from the STORED effective
        # config + snapshot — the same inputs the aggregate stamped from, so
        # the id agrees — and folded into the refreshed D8 record.
        from precis_pathway import manifest as vm
        from precis_pathway.persist import _json_finite

        if cur.get("config_snapshot_yaml"):
            # The stored mlip weight pins ride along verbatim — they were
            # stamped by the seed lane and this host cannot locate the
            # checkpoints, so rebuilding without them would mint a
            # DIFFERENT manifest_id and clobber the aggregate's record.
            stored_mlip = (
                (cur.get("validation_manifest") or {}).get("lock") or {}
            ).get("mlip") or []
            # A recompute's overriding environment is what this record was
            # solved under, so the manifest must name it, not the stored one.
            manifest_config = dict(cur.get("config") or config)
            if "environment" in config:
                manifest_config["environment"] = config["environment"]
            manifest = vm.build_safely(
                config=manifest_config,
                config_snapshot_yaml=str(cur["config_snapshot_yaml"]),
                results={**(cur.get("results") or {}), **results},
                autocatpath_version=str(cur.get("autocatpath_version") or ""),
                seed_weights={
                    i: {ml.get("weights_sha256")}
                    for i, ml in enumerate(stored_mlip)
                    if isinstance(ml, dict)
                },
            )
            manifest_id = vm.stamp_results(results, manifest)
            if manifest is not None:
                cur["validation_manifest"] = manifest
                cur["manifest_id"] = manifest_id
        res_meta = dict(cur.get("results") or {})
        for ok_key, err_key in _CLEARS.items():
            if ok_key in results:
                res_meta.pop(err_key, None)  # a fresh success retires its error
        res_meta.update({k: results[k] for k in _OUTPUT_KEYS if k in results})
        cur["results"] = res_meta
        # a failed solve's rate-control sum is NaN, which Jsonb refuses
        ctx.store.stamp_ref_meta(pathway_ref_id, _json_finite(cur))
    except Exception as exc:  # pragma: no cover - persistence is best-effort
        log.warning("autocatpath_kinetics: meta persist failed", exc_info=True)
        ctx.record_failure(
            f"autocatpath_kinetics: could not persist onto pathway "
            f"#{pathway_ref_id}: {exc}"
        )
        return False

    errors: list[str] = []
    if "kinetics_error" in results and "kinetics_v2" not in results:
        errors.append(
            f"kinetics re-solve failed (stored record left as it was): "
            f"{results['kinetics_error']}"
        )
    if "kinetics_sweep_error" in results:
        errors.append(str(results["kinetics_sweep_error"]))
    elif "kinetics_sweep" in results and not n_pts:
        # the engine turns each dead point into a warning and returns an
        # empty sweep: without this the job reads as a success
        why = next(iter(sweep.get("warnings") or []), "no reason given")
        errors.append(f"sweep solved no point: {why}")
    if errors:
        # the errors are saved above for the record; the job itself failed
        ctx.record_failure("autocatpath_kinetics: " + "; ".join(errors))
        return False

    # stamp the job's own meta with the lifted scalars so the harvest lane
    # reads them without opening the artifact.  The v2 record alone is a
    # complete input to _kinetics_scalars (the D8 reader flip); the
    # aggregate's v1 block still rides in as the fallback for fields the
    # record lacks.
    try:
        from precis_pathway._dispatch_common import _kinetics_scalars

        scalars = _kinetics_scalars(
            {
                "kinetics": results.get("kinetics"),
                "kinetics_error": results.get("kinetics_error"),
                "kinetics_v2": results.get("kinetics_v2"),
                "kinetics_sweep": sweep,
            }
        )
        stamp = {**scalars, "n_sweep_points": n_pts}
        if manifest_id:
            stamp["manifest_id"] = manifest_id
        ctx.set_meta(stamp)
    except Exception:
        log.warning("autocatpath_kinetics: scalar stamp failed", exc_info=True)
    return True


def _dispatch(ctx: Any, spec: Any) -> None:
    """Blocking fallback invoked by ``ssh_node`` builds that predate the
    detached protocol: the same compute inline, then the same persistence."""
    loaded = _load(ctx)
    if loaded is None:
        return
    pathway_ref_id, config, ref, inputs = loaded
    params = (ctx.meta or {}).get("params") or {}
    ctx.append_chunk(
        "job_event",
        f"autocatpath_kinetics: recomputing kinetics for "
        f"{params.get('pathway_slug') or pathway_ref_id}",
    )
    results = dict(inputs)
    compute(config, results, lambda line: ctx.append_chunk("job_event", line))
    _finalize(ctx, pathway_ref_id, config, ref, results)


# ── detached protocol ─────────────────────────────────────────────────────


def _submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half — validates like ``_dispatch`` (malformed params
    fail the same way: ``record_failure`` + ``None``), launches the compute
    child and returns its handle without blocking. Must not set a status on
    the happy path (the row stays ``running`` until ``_poll`` ends it)."""
    loaded = _load(ctx)
    if loaded is None:
        return None
    pathway_ref_id, config, _ref, inputs = loaded
    params = (ctx.meta or {}).get("params") or {}
    ctx.append_chunk(
        "job_event",
        f"autocatpath_kinetics: recomputing kinetics for "
        f"{params.get('pathway_slug') or pathway_ref_id} (detached submit)",
    )
    try:
        from precis_pathway import runner

        cpuset = (params.get("resources") or {}).get("cpuset")
        return runner.submit_kinetics_job_detached(
            config, inputs, **({"cpuset": cpuset} if cpuset else {})
        )
    except Exception as exc:  # pragma: no cover - spawn/env dependent
        log.warning("autocatpath_kinetics: submit failed", exc_info=True)
        ctx.record_failure(f"autocatpath_kinetics: submit failed: {exc}")
        return None


def _poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half: ``False`` while the child runs; on a terminal
    state does ``_dispatch``'s persistence from the child's envelope and
    drives the job terminal."""
    from precis_pathway import runner
    from precis_pathway.seed_job import _reap_scratch

    status = runner.poll_seed_partial_detached(handle, defer_cleanup=True)
    state = status.get("state")
    if state == "running":
        return False
    # the runner observed completion: a finished pid must never authorize an
    # orphan kill after its process-group number is reused.
    ctx.set_meta(compute_handle_released=True)

    if state == "failed":
        reason = f"autocatpath_kinetics: compute failed: {status.get('error')}"
        stderr_tail = status.get("stderr_tail") or ""
        if stderr_tail:
            reason = f"{reason}\n--- stderr (warnings removed) ---\n{stderr_tail}"
        tail = status.get("tail") or ""
        if tail:
            reason = f"{reason}\n--- tail ---\n{tail}"
        ctx.record_failure(
            reason,
            open_tag="infra:child-killed" if status.get("infra") else None,
            failure_class="infra" if status.get("infra") else None,
        )
        _reap_scratch(ctx, handle, failed=True)
        return True

    loaded = _load(ctx)
    if loaded is None:
        _reap_scratch(ctx, handle, failed=True)
        return True
    pathway_ref_id, config, ref, inputs = loaded
    envelope = cast("dict[str, Any]", status["result"])
    for line in envelope.get("notes") or []:
        ctx.append_chunk("job_event", str(line))
    results = {**inputs, **(envelope.get("results") or {})}
    ok = _finalize(ctx, pathway_ref_id, config, ref, results)
    if ok:
        ctx.set_status("succeeded")
    _reap_scratch(ctx, handle, failed=not ok)
    return True


def _kill(ctx: Any, handle: Any) -> None:
    """Wall-clock kill: SIGKILL the child's process group (the seed job's
    hook — the handle shape is the same)."""
    from precis_pathway import seed_job

    seed_job._kill(ctx, handle)


def _reap(ctx: Any, handle: Any) -> None:
    """Post-terminal scratch removal (the seed job's hook)."""
    from precis_pathway import seed_job

    seed_job._reap(ctx, handle)


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
    submit=_submit,
    poll=_poll,
    kill=_kill,
    reap=_reap,
    wall_seconds=_wall_seconds,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["NAME", "SPEC", "compute", "load", "overlay_recompute_config", "run_compute"]
