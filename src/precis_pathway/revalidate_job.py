"""``autocatpath_revalidate`` job type (kinetics v2 slice 10, D10).

Registered as ``autocatpath_revalidate = precis_pathway.revalidate_job:SPEC``
in ``pyproject.toml``. Input: a pathway handle and the ``manifest_id`` its
``meta.validation_manifest`` carries. The job

1. refuses — naming the mismatch, never substituting — when this host
   cannot satisfy the manifest's lock hash or engine version/commit
   (``autocatpath.manifest.unsatisfied``), or when the id does not match
   the stored record;
2. re-runs the CPU legs from the stored inputs and seeds: the TPD check
   (slice 9; first, because its applied offsets feed the others), the
   chain/MF kinetics solve and, when the result carries one, the (T,
   p_total) sweep with its seeded lattice KMC at ``kmc_points``.  The MC sensitivity band is
   not an observable and is skipped (``n_samples = 0``: its draw count is
   wall-clock calibrated, so it could never be a reproducible input);
3. judges every observable against the manifest's own tolerance table —
   the same-platform column when arch, OS and accelerator match the stamp,
   else the cross-platform column — and appends ``{platform_then,
   platform_now, column, per-observable drift, verdict}`` to
   ``meta.revalidation[<manifest_id>]`` on the pathway ref.

Not re-run here: the min-energy-path leg (relax/NEB/refine needs the GPU
seed lane), and the TPD leg of a result stored without a TPD record. They
land in ``not_run``, so the verdict is at most ``reproduced_partial(energies)``
with ``complete: false`` — a result is validated only when ``complete`` is
true. An engine without a
stamped commit (either side) adds a caveat with the same effect.

Only an explicit request mints this job (``put(kind='pathway', id=<handle>,
mode='revalidate', args={'manifest': <id>})`` or a direct job put); nothing
auto-mints it. No LLM executor may run it (D10: validation makes no LLM
call) — the executor set is asserted against ``ZERO_LLM_EXECUTORS`` at
import.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from precis.workers.executors import ZERO_LLM_EXECUTORS
from precis.workers.job_types import JobTypeSpec
from precis_pathway import manifest as vm

if TYPE_CHECKING:
    from precis_pathway.types import PathwayArtifact

log = logging.getLogger(__name__)

NAME = "autocatpath_revalidate"

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pathway_ref_id": {"type": "integer"},
        "pathway_slug": {"type": ["string", "null"]},
        "manifest_id": {"type": "string"},
    },
    "required": ["pathway_ref_id", "manifest_id"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: D10: the validation jobs have no LLM executor (import-time guard).
if not COMPATIBLE_EXECUTORS <= ZERO_LLM_EXECUTORS:  # pragma: no cover
    raise RuntimeError(
        f"{NAME}: executors {sorted(COMPATIBLE_EXECUTORS - ZERO_LLM_EXECUTORS)} can spend "
        "LLM budget; validation runs only on ZERO_LLM_EXECUTORS"
    )
#: CPU worker pool (same seam as autocatpath_kinetics); the GPU seed lane
#: is never claimed.
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Re-validate a pathway result from its validation manifest (kinetics v2 "
    "D10): refuse on a lock-hash mismatch, re-run the CPU kinetics/sweep legs "
    "from the stored inputs and seeds, judge drift per observable against the "
    "manifest's tolerances; writes meta.revalidation[<manifest_id>]."
)

#: ``(config, then_results, legs) -> (now_results, failed_legs)``
Rerun = Callable[
    [dict[str, Any], dict[str, Any], tuple[str, ...]],
    tuple[dict[str, Any], tuple[str, ...]],
]


class Refused(Exception):
    """The manifest cannot be satisfied on this host; ``str()`` names why."""


def rerun_cpu_legs(
    config: dict[str, Any], then: dict[str, Any], legs: tuple[str, ...]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Re-run the CPU legs over the stored nodes/edges (the min-energy-path
    leg's outputs are this re-run's inputs)."""
    from precis_pathway import runner

    cfg = copy.deepcopy(config)
    cfg.setdefault("kinetics", {})["n_samples"] = 0
    now: dict[str, Any] = {
        k: then.get(k) for k in ("nodes", "edges", "score", "thermo")
    }
    now["network_digest"] = then.get("network_digest")
    failed: list[str] = []
    tpd: dict[str, Any] | None = None
    if "tpd" in legs:
        runner.run_kinetics_tpd_subprocess(cfg, now)
        if "kinetics_tpd_error" in now:
            failed.append("tpd")
        else:
            tpd = now["kinetics_tpd"]
    offsets = tpd if isinstance(tpd, dict) and tpd.get("offsets") else None
    if "kinetics" in legs:
        # run_kinetics_subprocess reads and writes only results_json
        artifact = cast("PathwayArtifact", {"results_json": now})
        runner.run_kinetics_subprocess(cfg, artifact, tpd=offsets)
        if "kinetics_error" in now:
            failed.append("kinetics")
    runner.fold_tpd(now)
    if "sweep" in legs and then.get("kinetics_sweep"):
        runner.run_kinetics_sweep_subprocess(cfg, now)
        if "kinetics_sweep_error" in now:
            failed.append("sweep")
    return now, tuple(failed)


def revalidate(
    meta: dict[str, Any],
    manifest_id: str,
    *,
    rerun: Rerun = rerun_cpu_legs,
    lock: tuple[str | None, str | None] | None = None,
    platform_now: Any = None,
    legs: tuple[str, ...] = vm.CPU_LEGS,
    engine: tuple[str, str | None] | None = None,
) -> dict[str, Any]:
    """Re-validate one stored result; returns the ``meta.revalidation``
    entry.  Raises :class:`Refused` (naming the mismatch) before any compute
    when the record is missing, edited, not the one asked for, or its lock
    hash or engine version/commit cannot be satisfied here.  ``engine`` is
    the host's ``(autocatpath version, commit)`` (default: the installed
    engine's ``__version__``/``__git_sha__``)."""
    m = vm.engine_manifest()
    if m is None:
        raise Refused(
            "this host's autocatpath predates the validation manifest (< 0.38)"
        )
    rec = vm.manifest_of(meta)
    if rec is None:
        raise Refused("the pathway carries no meta.validation_manifest")
    if rec.get("manifest_id") != manifest_id:
        raise Refused(
            f"manifest_id mismatch: asked {manifest_id[:12]}, the pathway carries "
            f"{str(rec.get('manifest_id'))[:12]}"
        )
    try:
        manifest = m.ValidationManifest.from_dict(rec)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    uv_sha, source = lock if lock is not None else vm.lock_sha256()
    if engine is None:
        import autocatpath

        engine = (autocatpath.__version__, getattr(autocatpath, "__git_sha__", None))
    # no MLIP is loaded by the CPU legs, so weights are not checked here
    misses = m.unsatisfied(
        manifest,
        uv_lock_sha256=uv_sha,
        lock_source=source,
        autocatpath_version=engine[0],
        autocatpath_sha=engine[1],
    )
    if misses:
        raise Refused("; ".join(misses))

    then = dict(meta.get("results") or {})
    then_obs = m.observables(then)
    # a result stored without a TPD record has nothing the TPD leg could be
    # judged against: not run (named), never a silent pass
    stored_tpd = then.get("kinetics_tpd")
    no_tpd = "tpd" in legs and not (
        isinstance(stored_tpd, dict) and stored_tpd.get("tpd")
    )
    run_legs = tuple(leg for leg in legs if not (leg == "tpd" and no_tpd))
    now, failed = rerun(dict(meta.get("config") or {}), then, run_legs)
    now_obs = m.observables(now)
    platform_now = platform_now or m.capture_platform()
    column = m.column_for(manifest.platform, platform_now)
    not_run = {
        leg: vm.NOT_RUN_LEGS.get(leg, "not requested")
        for leg in (*vm.NOT_RUN_LEGS, *vm.CPU_LEGS)
        if leg not in legs
    }
    if no_tpd:
        not_run["tpd"] = vm.NO_TPD_RECORD
    report = m.compare(
        then_obs,
        now_obs,
        manifest.tolerances,
        column=column,
        failed_legs=failed,
        skip_legs=tuple(not_run),
        caveats=tuple(m.caveats(manifest, autocatpath_sha=engine[1])),
    )
    # report.as_dict carries verdict, complete, not_run, caveats
    return {
        "manifest_id": manifest_id,
        "platform_then": manifest.platform.as_dict(),
        "platform_now": platform_now.as_dict(),
        **report.as_dict(),
        "not_run_why": not_run,
    }


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        pathway_ref_id = int(params["pathway_ref_id"])
        manifest_id = str(params["manifest_id"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"{NAME}: malformed params ({exc})")
        return
    ref = ctx.store.get_ref(kind="pathway", id=pathway_ref_id)
    if ref is None:
        ctx.record_failure(f"{NAME}: pathway #{pathway_ref_id} not found")
        return
    ctx.set_meta(manifest_id=manifest_id)
    try:
        entry = revalidate(dict(ref.meta or {}), manifest_id, rerun=rerun_cpu_legs)
    except Refused as exc:
        ctx.record_failure(f"{NAME}: refused — {exc}")
        return
    entry["at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["job_id"] = getattr(ctx, "ref_id", None)
    try:
        cur = dict(
            (ctx.store.get_ref(kind="pathway", id=pathway_ref_id) or ref).meta or {}
        )
        reval = dict(cur.get("revalidation") or {})
        reval[manifest_id] = [*(reval.get(manifest_id) or []), entry]
        cur["revalidation"] = reval
        ctx.store.stamp_ref_meta(pathway_ref_id, cur)
    except Exception as exc:  # pragma: no cover - persistence is best-effort
        log.warning("%s: meta persist failed", NAME, exc_info=True)
        ctx.record_failure(
            f"{NAME}: could not persist onto pathway #{pathway_ref_id}: {exc}"
        )
        return
    ctx.set_meta(
        manifest_id=manifest_id,
        verdict=entry["verdict"],
        complete=entry["complete"],
        column=entry["column"],
    )
    bad = [o["observable"] for o in entry["observables"] if not o["ok"]]
    ctx.append_chunk(
        "job_summary",
        f"{NAME}: {entry['verdict']} [{entry['column']}] on "
        f"{len(entry['observables'])} observable(s)"
        + (f"; drifted: {', '.join(bad)}" if bad else "")
        + (f"; not run: {', '.join(entry['not_run'])}" if entry["not_run"] else "")
        + (f"; caveats: {', '.join(entry['caveats'])}" if entry["caveats"] else "")
        + ("" if entry["complete"] else " — NOT validated (complete: false)"),
    )
    if entry["verdict"].startswith("failed"):
        ctx.record_failure(f"{NAME}: {entry['verdict']}")
        return
    ctx.set_status("succeeded")


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


__all__ = ["NAME", "SPEC", "Refused", "load", "rerun_cpu_legs", "revalidate"]
