"""``autocatpath_modes`` job type: measure the normal modes a stored pathway
lacks (gr543723).

Engines before 0.43 reduced each state's normal-mode analysis to one number at
the run temperature and dropped the modes, so the kinetics sweep's re-T rule
refuses any other T on a ``thermo.mode: vib`` pathway. Those pathways do keep
each seed's relaxed geometries on the seed jobs (``meta.structures``), so the
measurement can be repeated on them without another relaxation: 6N single-point
forces per state, on the model that seed ran.

Reads the pathway's succeeded ``autocatpath_seed`` jobs and, per seed and state:

1. stores the whole measurement on that seed job's ``meta.partial.vib[state]``
   next to the original ``g``, so a later re-aggregate carries it;
2. stores the modes on the pathway's ``meta.results.thermo.state_modes``, the
   shape the aggregate writes, which is what the re-T rule reads;
3. compares the re-measured ``g`` with the stored one (``meta.modes_check``):
   the same geometry and potential should give the same number. The baked
   correction is never replaced; the modes only move it away from the run T.

Detached child like the seed job, with a gpu slot (``requires={"gpu": 1}`` at
the mint). Minted by ``put(kind='pathway', mode='measure_modes')``.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "autocatpath_modes"

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pathway_ref_id": {"type": "integer"},
        "pathway_slug": {"type": ["string", "null"]},
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["pathway_ref_id"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Measure the normal modes a stored vib-priced pathway lacks, from the "
    "relaxed geometries on its seed jobs; stores them on the seed jobs and on "
    "meta.results.thermo.state_modes so the kinetics sweep can vary T."
)

_WALL_S = 3600.0

#: Re-measured g this far from the stored one (eV) counts as a disagreement.
G_TOLERANCE_EV = 0.005


def seed_jobs(store: Any, pathway_ref_id: int) -> list[tuple[int, dict[str, Any]]]:
    """Every succeeded seed job of the pathway that kept geometry, as
    ``(job id, meta)``, oldest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.ref_id, j.meta FROM refs j
             WHERE j.kind = 'job' AND j.retired_at IS NULL
               AND j.meta->>'job_type' = 'autocatpath_seed'
               AND (j.meta->'params'->>'pathway_ref_id')::bigint = %s
               AND j.meta->'structures' <> '{}'::jsonb
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                        AND t.value = 'succeeded'
                   )
             ORDER BY j.ref_id ASC
            """,
            (pathway_ref_id,),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


def _unit(job_id: int, meta: dict[str, Any]) -> dict[str, Any]:
    """What the child needs from one seed job."""
    params = meta.get("params") or {}
    return {
        "job_id": job_id,
        "seed": meta.get("seed"),
        "model": meta.get("model"),
        "model_index": int(meta.get("model_index") or 0),
        "config": params.get("config") or {},
        "force_backend": params.get("force_backend"),
        "structures": {
            name: geo.get("extxyz")
            for name, geo in (meta.get("structures") or {}).items()
            if isinstance(geo, dict) and geo.get("extxyz")
        },
    }


def run_compute(units: list[dict[str, Any]]) -> dict[str, Any]:
    """Child entrypoint (``runner._subprocess_main`` mode ``modes_job``):
    ``{"units": [{"job_id", "vib": {state: <measurement>}, "failed": {state:
    reason}}], "notes": [...]}``. One calculator per model; a state that fails
    is recorded and the others still run."""
    from precis_pathway import runner

    calcs: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    for unit in units:
        calc, cfg = runner.modes_calculator(unit, calcs)
        vib, failed = runner.measure_unit_modes(unit, calc, cfg)
        out.append({"job_id": unit["job_id"], "vib": vib, "failed": failed})
        notes.append(
            f"autocatpath_modes: seed {unit.get('seed')} ({unit.get('model')}): "
            f"{len(vib)} states measured, {len(failed)} failed"
        )
    return {"units": out, "notes": notes}


def merge_measurements(
    pathway_thermo: dict[str, Any],
    seed_metas: dict[int, dict[str, Any]],
    measured: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[int, dict[str, Any]], dict[str, Any]]:
    """Fold the child's measurements in. Returns the pathway's new ``thermo``,
    each seed job's new ``partial`` and the ``modes_check`` summary. A seed
    job's original ``g`` stands, the re-measured value rides as ``g_remeasured``."""
    thermo = dict(pathway_thermo)
    state_modes: dict[str, list[dict[str, Any]]] = {}
    partials: dict[int, dict[str, Any]] = {}
    deltas: list[tuple[float, str, int]] = []
    failed: dict[str, list[str]] = {}
    for unit in measured:
        job_id = int(unit["job_id"])
        meta = seed_metas[job_id]
        partial = dict(meta.get("partial") or {})
        vib = dict(partial.get("vib") or {})
        for state, rec in (unit.get("vib") or {}).items():
            prior = vib.get(state)
            stored: dict[str, Any] = prior if isinstance(prior, dict) else {}
            if "g" in stored:
                deltas.append(
                    (abs(float(stored["g"]) - float(rec["g"])), state, job_id)
                )
                merged = {**rec, **stored, "g_remeasured": rec["g"]}
            else:
                merged = dict(rec)
            vib[state] = merged
            state_modes.setdefault(state, []).append(
                {
                    "seed": meta.get("seed"),
                    "model": meta.get("model"),
                    "hnu": rec["hnu"],
                    "hnu_imag": rec.get("hnu_imag") or [],
                }
            )
        for state, reason in (unit.get("failed") or {}).items():
            failed.setdefault(state, []).append(f"job {job_id}: {reason}")
        partial["vib"] = vib
        partials[job_id] = partial
    thermo["state_modes"] = state_modes
    corrected = set(thermo.get("state_corrections") or {})
    fallback = set(thermo.get("vib_fallback") or [])
    worst = max(deltas, default=(0.0, "", 0))
    check = {
        "n_measured": len(deltas),
        "max_abs_dg_eV": worst[0],
        "worst_state": worst[1] or None,
        "worst_job": worst[2] or None,
        "n_over_tolerance": sum(1 for d in deltas if d[0] > G_TOLERANCE_EV),
        "tolerance_eV": G_TOLERANCE_EV,
        "states_without_modes": sorted(corrected - fallback - set(state_modes)),
        "failed": failed,
    }
    return thermo, partials, check


def _load(ctx: Any) -> tuple[int, list[tuple[int, dict[str, Any]]]] | None:
    params = (ctx.meta or {}).get("params") or {}
    try:
        pathway_ref_id = int(params["pathway_ref_id"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"autocatpath_modes: malformed params ({exc})")
        return None
    jobs = seed_jobs(ctx.store, pathway_ref_id)
    if not jobs:
        ctx.record_failure(
            f"autocatpath_modes: pathway #{pathway_ref_id} has no succeeded seed "
            "job with stored geometry"
        )
        return None
    return pathway_ref_id, jobs


def _finalize(
    ctx: Any,
    pathway_ref_id: int,
    jobs: list[tuple[int, dict[str, Any]]],
    measured: list[dict[str, Any]],
) -> bool:
    from precis_pathway.persist import _json_finite

    ref = ctx.store.get_ref(kind="pathway", id=pathway_ref_id)
    if ref is None:
        ctx.record_failure(f"autocatpath_modes: pathway #{pathway_ref_id} not found")
        return False
    meta = dict(ref.meta or {})
    results = dict(meta.get("results") or {})
    thermo, partials, check = merge_measurements(
        dict(results.get("thermo") or {}), dict(jobs), measured
    )
    for job_id, partial in partials.items():
        ctx.store.stamp_ref_meta(job_id, {"partial": _json_finite(partial)})
    results["thermo"] = thermo
    ctx.store.stamp_ref_meta(pathway_ref_id, {"results": _json_finite(results)})
    ctx.set_meta(modes_check=check)
    ctx.append_chunk(
        "job_event",
        f"autocatpath_modes: {check['n_measured']} states compared with the stored "
        f"g; worst {check['max_abs_dg_eV']:.4f} eV ({check['worst_state']}), "
        f"{check['n_over_tolerance']} over {G_TOLERANCE_EV} eV",
    )
    missing = check["states_without_modes"]
    if missing:
        ctx.record_failure(
            "autocatpath_modes: no modes for "
            f"{', '.join(missing)}; the sweep still refuses other temperatures "
            f"(failures: {check['failed']})"
        )
        return False
    return True


def _submit(ctx: Any, spec: Any) -> Any:
    loaded = _load(ctx)
    if loaded is None:
        return None
    pathway_ref_id, jobs = loaded
    ctx.append_chunk(
        "job_event",
        f"autocatpath_modes: measuring modes for pathway #{pathway_ref_id} from "
        f"{len(jobs)} seed jobs (detached submit)",
    )
    try:
        from precis_pathway import runner

        params = (ctx.meta or {}).get("params") or {}
        cpuset = (params.get("resources") or {}).get("cpuset")
        return runner.submit_modes_job_detached(
            [_unit(job_id, meta) for job_id, meta in jobs],
            **({"cpuset": cpuset} if cpuset else {}),
        )
    except Exception as exc:  # pragma: no cover - spawn/env dependent
        log.warning("autocatpath_modes: submit failed", exc_info=True)
        ctx.record_failure(f"autocatpath_modes: submit failed: {exc}")
        return None


def _poll(ctx: Any, handle: Any) -> bool:
    from precis_pathway import runner
    from precis_pathway.seed_job import _reap_scratch

    status = runner.poll_seed_partial_detached(handle, defer_cleanup=True)
    state = status.get("state")
    if state == "running":
        return False
    ctx.set_meta(compute_handle_released=True)
    if state == "failed":
        reason = f"autocatpath_modes: compute failed: {status.get('error')}"
        stderr_tail = status.get("stderr_tail") or ""
        if stderr_tail:
            reason = f"{reason}\n--- stderr (warnings removed) ---\n{stderr_tail}"
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
    pathway_ref_id, jobs = loaded
    envelope = cast("dict[str, Any]", status["result"])
    for line in envelope.get("notes") or []:
        ctx.append_chunk("job_event", str(line))
    ok = _finalize(ctx, pathway_ref_id, jobs, envelope.get("units") or [])
    if ok:
        ctx.set_status("succeeded")
    _reap_scratch(ctx, handle, failed=not ok)
    return True


def _kill(ctx: Any, handle: Any) -> None:
    from precis_pathway import seed_job

    seed_job._kill(ctx, handle)


def _reap(ctx: Any, handle: Any) -> None:
    from precis_pathway import seed_job

    seed_job._reap(ctx, handle)


def _wall_seconds() -> float:
    return _WALL_S


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    submit=_submit,
    poll=_poll,
    kill=_kill,
    reap=_reap,
    wall_seconds=_wall_seconds,
)


def load() -> JobTypeSpec:
    return SPEC


__all__: list[str] = [
    "NAME",
    "SPEC",
    "load",
    "merge_measurements",
    "run_compute",
    "seed_jobs",
]
