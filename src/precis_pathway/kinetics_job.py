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
pathway ref.
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
    try:
        cur = dict(
            (ctx.store.get_ref(kind="pathway", id=pathway_ref_id) or ref).meta or {}
        )
        res_meta = dict(cur.get("results") or {})
        res_meta.update(
            {
                k: results[k]
                for k in ("kinetics_sweep", "kinetics_v2", "kinetics_sweep_error")
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
        ctx.set_meta({**scalars, "n_sweep_points": n_pts})
    except Exception:
        log.warning("autocatpath_kinetics: scalar stamp failed", exc_info=True)


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
