"""``autocatpath_kinetics`` job type (kinetics v2 slice 4, D7/D9).

Registered as ``autocatpath_kinetics = precis_pathway.kinetics_job:SPEC``
in ``pyproject.toml``. Runs on a CPU worker (``ssh_node``, empty
``REQUIRES`` — the GPU seed lane is never touched): reads the just-
aggregated pathway's ``meta.results`` + ``meta.config``, runs the
``autocatpath.kinetics.sweep`` (T, p_total) MF grid out-of-process, and
persists ``meta.kinetics_sweep`` (the competitive coverage map +
optimum; from engine 0.36 also ``kmc_points`` — the lattice KMC at the
corners, centre and optimum, each with ``tof_kmc`` and
``correlation_effect``) and the refreshed ``meta.kinetics_v2`` onto the
pathway ref. From engine 0.38 (slice 10, D10) it also restamps
``meta.validation_manifest``/``meta.manifest_id`` on the ref and
``manifest_id`` on its own meta and the D8 record.  From engine 0.39
(slice 9, D11) it first runs the TPD check (``meta.kinetics_tpd``): an
uncorrected check folds its comparisons onto ``kinetics_v2.tpd``; a check
that applied ``tpd_corrected`` offsets re-runs the kinetics solve on the
corrected states (provider ``vacuum+tpd``), and the sweep prices them too.
Minted by ``autocatpath_aggregate``'s dispatch on every verify-tier
pathway — never user-facing.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

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
#: CPU worker pool — empty ⊆ any executor's PROVIDES, same seam as the
#: aggregate job; the GPU seed lane is never claimed.
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Run the kinetics v2 mean-field (T, p_total) sweep over a just-"
    "aggregated verify-tier pathway (autocatpath.kinetics.sweep); writes "
    "meta.kinetics_sweep + refreshed meta.kinetics_v2 onto the pathway ref."
)


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        pathway_ref_id = int(params["pathway_ref_id"])
        config = dict(params["config"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"autocatpath_kinetics: malformed params ({exc})")
        return

    from precis_pathway import runner

    # The pathway's persisted results carry the aggregated nodes/edges/
    # score/thermo the sweep reprices from.
    ref = ctx.store.get_ref(kind="pathway", id=pathway_ref_id)
    if ref is None:
        ctx.record_failure(f"autocatpath_kinetics: pathway #{pathway_ref_id} not found")
        return
    results = dict((ref.meta or {}).get("results") or {})
    if not results.get("nodes") or not results.get("edges"):
        ctx.record_failure(
            f"autocatpath_kinetics: pathway #{pathway_ref_id} has no "
            "aggregated results to sweep"
        )
        return

    if runner.has_tpd():
        _tpd_leg(ctx, runner, config, results)
    ctx.append_chunk(
        "job_event",
        f"autocatpath_kinetics: sweeping (T, p_total) for "
        f"{params.get('pathway_slug') or pathway_ref_id}",
    )
    runner.run_kinetics_sweep_subprocess(config, results)
    if "kinetics_sweep_error" in results:
        ctx.append_chunk(
            "job_event",
            f"autocatpath_kinetics: {results['kinetics_sweep_error']}",
        )
        # a sweep failure is not a pathway-fatal: the v2 record + the error
        # both persist so the surface stays honest.
    sweep = results.get("kinetics_sweep") or {}
    n_pts = len(sweep.get("points") or [])
    optimum = sweep.get("optimum")
    if optimum:
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
    # (a sibling job may have touched it), merge the sweep block into
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

        if cur.get("config_snapshot_yaml"):
            manifest = vm.build_safely(
                config=dict(cur.get("config") or config),
                config_snapshot_yaml=str(cur["config_snapshot_yaml"]),
                results={**(cur.get("results") or {}), **results},
                autocatpath_version=str(cur.get("autocatpath_version") or ""),
            )
            manifest_id = vm.stamp_results(results, manifest)
            if manifest is not None:
                cur["validation_manifest"] = manifest
                cur["manifest_id"] = manifest_id
        res_meta = dict(cur.get("results") or {})
        res_meta.update(
            {
                k: results[k]
                for k in (
                    "kinetics_sweep",
                    "kinetics_v2",
                    "kinetics_sweep_error",
                    "kinetics",
                    "kinetics_error",
                    "kinetics_tpd",
                    "kinetics_tpd_error",
                )
                if k in results
            }
        )
        cur["results"] = res_meta
        ctx.store.stamp_ref_meta(pathway_ref_id, cur)
    except Exception as exc:  # pragma: no cover - persistence is best-effort
        log.warning("autocatpath_kinetics: meta persist failed", exc_info=True)
        ctx.record_failure(
            f"autocatpath_kinetics: could not persist onto pathway "
            f"#{pathway_ref_id}: {exc}"
        )
        return
    if "kinetics_sweep_error" in results:
        # the error is saved above for the record; the job itself failed
        ctx.record_failure(results["kinetics_sweep_error"])
        return

    # stamp the job's own meta with the lifted scalars so the harvest lane
    # reads them without opening the artifact.  The aggregate's v1 block
    # goes in too: _kinetics_scalars lifts nothing (not even the sweep
    # optimum) without it, so the slice-4 stamp carried n_sweep_points only.
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


def _tpd_leg(
    ctx: Any, runner: Any, config: dict[str, Any], results: dict[str, Any]
) -> None:
    """The D11 TPD check, then — when it applied offsets — the kinetics
    solve again on the corrected states.  A failure is a job note, never a
    pathway-fatal (the sweep still runs on the uncorrected states)."""
    ctx.append_chunk("job_event", "autocatpath_kinetics: TPD check")
    runner.run_kinetics_tpd_subprocess(config, results)
    if "kinetics_tpd_error" in results:
        ctx.append_chunk(
            "job_event", f"autocatpath_kinetics: TPD {results['kinetics_tpd_error']}"
        )
        return
    tpd = results["kinetics_tpd"]
    parts = [
        f"{ads} {rec.get('status')}"
        for ads, rec in sorted((tpd.get("tpd") or {}).items())
    ]
    ctx.append_chunk(
        "job_event",
        f"autocatpath_kinetics: TPD ({tpd.get('family') or 'no family'}): "
        + (", ".join(parts) or "no desorbing adsorbate"),
    )
    if not tpd.get("offsets"):
        runner.fold_tpd(results)
        return
    artifact = {"results_json": results}
    runner.run_kinetics_subprocess(config, artifact, tpd=tpd)
    if "kinetics_error" in results:
        ctx.append_chunk(
            "job_event",
            f"autocatpath_kinetics: corrected re-solve failed: {results['kinetics_error']}",
        )


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
    wall_seconds=lambda: float(
        int(__import__("os").environ.get("PRECIS_AUTOCATPATH_KINETICS_SECONDS", "3600"))
    ),
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["NAME", "SPEC", "load"]
