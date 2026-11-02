"""``autocatpath_kinetics`` job type (kinetics v2 slice 4, D7/D9).

Registered as ``autocatpath_kinetics = precis_pathway.kinetics_job:SPEC``
in ``pyproject.toml``. Reads the aggregated pathway's ``meta.results`` and
the job's ``params.config`` (the effective config, or — for
``put(kind='pathway', mode='recompute_kinetics')`` — that config with the
stored ``environment``/``objective``/``kinetics.sweep`` laid over it:
explicit args where given, the kinetics record's echo otherwise) and
computes, in this order:

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
``_kill``/``_reap`` bound and clean it up. The child mirrors each step note
into ``progress.log`` next to ``result.json`` as it happens, and ``_poll``
drains that file into job events (plus one heartbeat per quiet poll) so a
long sweep no longer reads as stalled (gr549807). ``ssh_node`` claims
``limit=1`` and a blocking dispatch held its worker thread for up to an
hour; the detached job holds none, and ``requires={}`` takes no gpu slot,
so it may share pollux/spark with seed and aggregate work. ``_dispatch``
stays as the blocking fallback for an executor build that only knows
``dispatch``.

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

import copy
import json
import logging
import math
import os
import time
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

#: The per-step ceiling (s) when ``PRECIS_AUTOCATPATH_KINETICS_SECONDS`` is
#: unset, the same default as ``runner._kinetics_timeout_s``.
_DEFAULT_STEP_S = 900
#: The live step-note file the detached child mirrors every ``note()``
#: line into, next to ``result.json`` (gr549807): ``_poll`` drains it so a
#: long sweep stops reading as a stalled job.
PROGRESS_NAME = "progress.log"
#: The sweep gets this many step budgets: 48 points with an order stencil per
#: gas plus the optimum search outran one on 511428 (job 544260).
_SWEEP_STEPS = 3
#: TPD check and kinetics solve take one step each.
_N_STEPS = 2 + _SWEEP_STEPS
_DEFAULT_WALL_S = _N_STEPS * _DEFAULT_STEP_S + 300


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


def _sweep_budget_s() -> int:
    """The sweep child's ceiling; 0 (unbounded steps) stays 0."""
    try:
        step = int(os.environ.get("PRECIS_AUTOCATPATH_KINETICS_SECONDS", ""))
    except ValueError:
        step = _DEFAULT_STEP_S
    return max(0, step) * _SWEEP_STEPS


def _step_seconds() -> int:
    """The per-step ceiling the compute children run under — the same
    ``PRECIS_AUTOCATPATH_KINETICS_SECONDS`` read
    ``runner._kinetics_timeout_s`` makes (0 = unbounded steps). Stamped on
    submit so the running job card can show the budget a silent step may
    still be inside (gr549807)."""
    try:
        return max(0, int(os.environ.get("PRECIS_AUTOCATPATH_KINETICS_SECONDS", "")))
    except ValueError:
        return _DEFAULT_STEP_S


def _tpd_family(config: dict[str, Any]) -> Any:
    """The reference family the TPD check would compare against."""
    return ((config.get("kinetics") or {}).get("tpd") or {}).get("family")


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


# ── the stored environment/objective/sweep a no-arg recompute reuses ──────


def _bar(raw: Any) -> float | None:
    """``float(raw)`` when it parses finite — the engine's acceptance for a
    gas table entry (``_table`` and ``from_legacy`` both use a bare
    ``float()``: ints, floats and numeric strings count); ``None`` for
    anything the engine itself would refuse."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _block_gas_table(environment: dict[str, Any]) -> dict[str, float]:
    """The positive gas partials an ``environment:`` block declares:
    ``inputs`` + a mapping ``outputs`` + ``inerts``. The ``far`` preset
    contributes nothing — the engine's ``Environment._table_sum``
    semantics, engine-free."""
    gases = environment.get("gases")
    table: dict[str, float] = {}
    if isinstance(gases, dict):
        for key in ("inputs", "outputs", "inerts"):
            entry = gases.get(key)
            if isinstance(entry, dict):
                for gas, p in entry.items():
                    v = _bar(p)
                    if v is not None and v > 0:
                        table[str(gas)] = v
    return table


def _effective_gas_table(config: dict[str, Any]) -> dict[str, float]:
    """The gas table the job's environment prices from: the
    ``environment:`` block's own when present (the engine re-derives
    ``conditions.pressures`` from it — it is the single source), else the
    legacy ``conditions.pressures``."""
    env = config.get("environment")
    if env is not None:
        return _block_gas_table(env) if isinstance(env, dict) else {}
    cond = config.get("conditions")
    pressures = cond.get("pressures") if isinstance(cond, dict) else None
    if not isinstance(pressures, dict):
        return {}
    return {
        str(g): v for g, p in pressures.items() if (v := _bar(p)) is not None and v > 0
    }


def _environment_refusal(config: dict[str, Any]) -> str | None:
    """``environment.p_total`` stated while the block's gas table is empty —
    the one inconsistency this job refuses at its start instead of inside
    a child's ``_prep``: the engine's own ``Environment.from_dict`` raises
    it, so a config carrying it can never compute. Everything else runs —
    with no ``environment:`` block the config prices the legacy conditions
    (unstated gases at the 1 bar fallback), the aggregate-minted job's
    normal shape (gr550976)."""
    env = config.get("environment")
    if not isinstance(env, dict):
        return None
    p_total = _bar(env.get("p_total"))
    if p_total is not None and p_total > 0 and not _block_gas_table(env):
        return (
            "environment.p_total is set but the gas table is empty: "
            "there is no composition to scale"
        )
    return None


def _record_environment_block(record: dict[str, Any]) -> dict[str, Any]:
    """A stored ``kinetics_v2.environment`` — the resolved block the solve
    wrote — back into an ``environment:`` block a config accepts. The
    resolved tables are already in bar (``scaled()`` applied), so
    ``p_total`` is never restated (the env's total is the table's sum
    either way) and the ``composition``/``p_total_stated``/``legacy``
    views are dropped; a ``far`` preset maps back onto ``outputs: 'far'``.
    Only keys the record actually carries are emitted — a missing or empty
    value is left out."""
    gases = record.get("gases")
    gases = gases if isinstance(gases, dict) else {}
    outputs = gases.get("outputs")
    gas_block: dict[str, Any] = {}
    for key in ("inputs", "inerts"):
        entry = gases.get(key)
        if isinstance(entry, dict) and entry:
            gas_block[key] = dict(entry)
    if gases.get("preset") == "far":
        gas_block["outputs"] = "far"
    elif isinstance(outputs, dict) and outputs:
        gas_block["outputs"] = dict(outputs)
    if gases.get("unlisted") is not None:
        gas_block["unlisted"] = str(gases["unlisted"])
    sticking = record.get("sticking")
    block: dict[str, Any] = {}
    if record.get("T") is not None:
        block["T"] = record["T"]
    if gas_block:
        block["gases"] = gas_block
    if isinstance(sticking, dict) and sticking:
        block["sticking"] = dict(sticking)
    if record.get("electrochemical") is not None:
        block["electrochemical"] = copy.deepcopy(record["electrochemical"])
    return block


def stored_environment(
    meta: dict[str, Any], config: dict[str, Any], results: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """The stored environment a no-arg ``recompute_kinetics`` reuses, as
    ``(block_to_overlay | None, source_label | None)``. ``(None, label)``
    means the env exists but the config already prices it (nothing to lay
    over); ``(None, None)`` means the pathway has no usable stored
    environment and the door refuses.

    Order: the last solve's own record env (``results.kinetics_v2`` —
    what the current record was priced under, so a re-solve matches it),
    the config's declared ``environment:``, a ``meta.environment`` stamp,
    then the legacy ``conditions.pressures`` the config already carries.
    A ``legacy``-flagged record is skipped: the same gases are already
    priced byte-faithfully by the legacy keys, and de-resolving it would
    drop the flag that suppresses the uncited-sticking warning. A
    candidate counts only with a non-empty gas table — the (T, p_total)
    sweep needs a composition to scale; an empty-table env solves at the
    fallback and dies at the sweep, the exact burn gr550976 reported."""
    rec = next(
        (
            v2["environment"]
            for v2 in (results.get("kinetics_v2"), meta.get("kinetics_v2"))
            if isinstance(v2, dict) and isinstance(v2.get("environment"), dict)
        ),
        None,
    )
    if rec is not None and not rec.get("legacy"):
        block = _record_environment_block(rec)
        if _block_gas_table(block):
            return block, "kinetics_v2 record"
    env = config.get("environment")
    if isinstance(env, dict) and _block_gas_table(env):
        return None, "config"
    meta_env = meta.get("environment")
    if isinstance(meta_env, dict) and _block_gas_table(meta_env):
        return dict(meta_env), "meta.environment"
    if _effective_gas_table(config):
        return None, "legacy conditions"
    return None, None


def stored_objective(
    meta: dict[str, Any], config: dict[str, Any], results: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """The stored objective a no-arg recompute reuses, same
    ``(overlay | None, label | None)`` contract as
    :func:`stored_environment`. The config's declared ``objective:`` wins
    (it is complete); the sweep record's echo — ``optimum.objective`` plus
    the ``competing`` pair — is the rescue when the config never stated
    one (``subject_to`` is not on the record, so it cannot be rebuilt)."""
    obj = config.get("objective")
    if isinstance(obj, dict) and obj:
        return None, "config"
    record = results.get("kinetics_sweep")
    record = record if isinstance(record, dict) else {}
    optimum = record.get("optimum")
    maximise = optimum.get("objective") if isinstance(optimum, dict) else None
    if isinstance(maximise, str) and maximise:
        out: dict[str, Any] = {"maximise": maximise}
        competing = record.get("competing")
        pair = competing.get("pair") if isinstance(competing, dict) else None
        if isinstance(pair, list) and len(pair) == 2:
            out["competing"] = [str(p) for p in pair]
        return out, "kinetics_sweep record"
    return None, None


def stored_sweep(
    meta: dict[str, Any], config: dict[str, Any], results: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """The stored sweep a no-arg recompute reuses, same
    ``(overlay | None, label | None)`` contract as
    :func:`stored_environment`. The config's declared ``kinetics.sweep``
    wins (it is complete: ``n``, ``log``, ``seed``, ``kmc_points``); the
    ``kinetics_sweep`` record's ``axes``/``design``/``composition`` echo
    is the rescue when the config never stated one — the engine's
    defaults fill the rest."""
    kin = config.get("kinetics")
    declared = kin.get("sweep") if isinstance(kin, dict) else None
    if isinstance(declared, dict) and declared:
        return None, "config"
    record = results.get("kinetics_sweep")
    record = record if isinstance(record, dict) else {}
    out = {k: record[k] for k in ("axes", "design", "composition") if k in record}
    return (out, "kinetics_sweep record") if out else (None, None)


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
    if not _tpd_family(config):
        # with no family every adsorbate ends as no_reference and no offset can
        # apply, so the ramp would spend its whole budget to produce nothing
        note("autocatpath_kinetics: TPD skipped (no kinetics.tpd.family)")
    elif runner.has_tpd():
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
    runner.run_kinetics_sweep_subprocess(config, results, timeout=_sweep_budget_s())
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


def run_compute(
    config: dict[str, Any],
    inputs: dict[str, Any],
    *,
    progress_path: str | None = None,
) -> dict[str, Any]:
    """Child entrypoint (``runner._subprocess_main`` mode ``kinetics_job``):
    the :func:`compute` over ``inputs``, returned as the JSON envelope
    ``{"results": <output keys>, "notes": [<job_event lines>]}``. Every
    note also lands on ``progress_path`` appended and flushed per line, so
    the detached ``_poll`` streams step notes while the compute still
    runs — the envelope only travels at exit otherwise (gr549807)."""
    results = {k: inputs.get(k) for k in _INPUT_KEYS}
    notes: list[str] = []

    def note(line: str) -> None:
        notes.append(line)
        if progress_path:
            try:
                # one JSON record per line — a note's own newlines stay
                # inside the record, so _drain_progress counts NOTES
                with open(progress_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line) + "\n")
            except OSError:
                pass  # the envelope copy still lands on done

    compute(config, results, note)
    return {
        "results": {k: results[k] for k in _OUTPUT_KEYS if k in results},
        "notes": notes,
    }


# ── persistence (shared by _dispatch and _poll) ───────────────────────────


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _degraded_keys(stored: dict[str, Any], results: dict[str, Any]) -> dict[str, str]:
    """``{output key: rejection reason}`` for each record key ``results``
    produced but must not store — judged key by key, so a mixed run keeps
    its good half: a finite ``tof_mf`` still lands when only the sweep
    degraded, and vice versa.

    ``kinetics_v2`` degrades when the new ``tof_mf`` is not finite while
    the stored one is; ``kinetics_sweep`` degrades when the new sweep has
    no ``optimum`` while the stored one does — a solve on a backend-less
    host writes exactly this and used to clobber a good record wholesale
    (pathway 527720 lost its v2 record to a one-point, optimum-less
    sweep). Keys absent from ``results`` aren't judged (the ``*_error``
    tail keeps the stored record on its own), and a stored record with
    nothing to protect is overwritten as before.
    """
    out: dict[str, str] = {}
    if "kinetics_v2" in results:
        v2 = results.get("kinetics_v2")
        old = stored.get("kinetics_v2")
        if not (isinstance(v2, dict) and _finite(v2.get("tof_mf"))) and (
            isinstance(old, dict) and _finite(old.get("tof_mf"))
        ):
            out["kinetics_v2"] = (
                "kinetics_v2 degraded: no finite tof_mf; stored record kept"
            )
    if "kinetics_sweep" in results:
        sweep = results.get("kinetics_sweep")
        old = stored.get("kinetics_sweep")
        if not (isinstance(sweep, dict) and sweep.get("optimum")) and (
            isinstance(old, dict) and old.get("optimum")
        ):
            out["kinetics_sweep"] = (
                "kinetics_sweep degraded: no optimum; stored record kept"
            )
    return out


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
        sweep["optimum"] = None
        sweep.setdefault("warnings", []).append(
            f"objective {optimum.get('objective')} had no value at any point; no optimum"
        )
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
    degraded: dict[str, str] = {}
    try:
        cur = dict(
            (ctx.store.get_ref(kind="pathway", id=pathway_ref_id) or ref).meta or {}
        )
        # slice 10 (D10): the manifest is built from the STORED effective
        # config + snapshot — the same inputs the aggregate stamped from, so
        # the id agrees — and folded into the refreshed D8 record.
        from precis_pathway import manifest as vm
        from precis_pathway.persist import _json_finite

        res_meta = dict(cur.get("results") or {})
        # Per-key degradation guard: a produced record with no real answer
        # must not clobber a stored one that has it — judged key by key,
        # so a finite tof still lands when only the sweep degraded. The
        # rejected output is kept under ``<key>_rejected`` with the
        # reason; the stored key stands.
        degraded = _degraded_keys(res_meta, results)
        accepted = {
            k: results[k] for k in _OUTPUT_KEYS if k in results and k not in degraded
        }

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
                results={**(cur.get("results") or {}), **accepted},
                autocatpath_version=str(cur.get("autocatpath_version") or ""),
                seed_weights={
                    i: {ml.get("weights_sha256")}
                    for i, ml in enumerate(stored_mlip)
                    if isinstance(ml, dict)
                },
            )
            manifest_id = vm.stamp_results(accepted, manifest)
            if manifest is not None:
                cur["validation_manifest"] = manifest
                cur["manifest_id"] = manifest_id
        for ok_key, err_key in _CLEARS.items():
            if ok_key in accepted:
                res_meta.pop(err_key, None)  # a fresh success retires its error
                res_meta.pop(f"{ok_key}_rejected", None)  # and its stale rejection
        if (
            "kinetics_sweep_error" in results
            and "kinetics_sweep" not in accepted
            and "kinetics_sweep" not in degraded
        ):
            # the stored sweep was solved under another request: kept, but it
            # must not read as this request's answer
            if "kinetics_sweep" in res_meta:
                res_meta["kinetics_sweep_previous"] = res_meta.pop("kinetics_sweep")
        # the prior record a fresh success replaces moves under *_previous —
        # never lost, one write per finalize (paired with the rejection
        # guard above: whatever this merge drops is recoverable either way)
        for ok_key in ("kinetics_v2", "kinetics_sweep"):
            if ok_key in accepted and ok_key in res_meta:
                res_meta[f"{ok_key}_previous"] = res_meta.pop(ok_key)
        res_meta.update(accepted)
        for key, why in degraded.items():
            rejected = {key: results[key], "reason": why}
            rej_err_key = _CLEARS.get(key)
            if rej_err_key is not None and rej_err_key in results:
                rejected[rej_err_key] = results[rej_err_key]
            res_meta[f"{key}_rejected"] = rejected
        cur["results"] = res_meta
        # a failed solve's rate-control sum is NaN, which Jsonb refuses
        ctx.store.stamp_ref_meta(pathway_ref_id, _json_finite(cur))
        if degraded:
            try:
                ctx.store.append_event(
                    pathway_ref_id,
                    source=NAME,
                    event="kinetics-rejected",
                    payload={
                        "reason": "; ".join(degraded[k] for k in sorted(degraded)),
                        "keys": sorted(degraded),
                        "job_id": getattr(ctx, "ref_id", None),
                    },
                )
            except Exception:
                log.warning(
                    "autocatpath_kinetics: rejected event append failed",
                    exc_info=True,
                )
    except Exception as exc:  # pragma: no cover - persistence is best-effort
        log.warning("autocatpath_kinetics: meta persist failed", exc_info=True)
        ctx.record_failure(
            f"autocatpath_kinetics: could not persist onto pathway "
            f"#{pathway_ref_id}: {exc}"
        )
        return False

    errors: list[str] = []
    if "kinetics_error" in results and "kinetics_v2" not in accepted:
        errors.append(
            f"kinetics re-solve failed (stored record left as it was): "
            f"{results['kinetics_error']}"
        )
    if "kinetics_sweep_error" in results:
        errors.append(str(results["kinetics_sweep_error"]))
    elif "kinetics_sweep" in accepted and not n_pts:
        # the engine turns each dead point into a warning and returns an
        # empty sweep: without this the job reads as a success
        why = next(iter(sweep.get("warnings") or []), "no reason given")
        errors.append(f"sweep solved no point: {why}")
    errors.extend(degraded[k] for k in sorted(degraded))
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
    why = _environment_refusal(config)
    if why is not None:
        # an env the engine itself refuses — fail here, not inside _prep
        ctx.record_failure(f"autocatpath_kinetics: {why}")
        return
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
    why = _environment_refusal(config)
    if why is not None:
        # an env the engine itself refuses — fail before the child launches
        ctx.record_failure(f"autocatpath_kinetics: {why}")
        return None
    params = (ctx.meta or {}).get("params") or {}
    ctx.append_chunk(
        "job_event",
        f"autocatpath_kinetics: recomputing kinetics for "
        f"{params.get('pathway_slug') or pathway_ref_id} (detached submit)",
    )
    try:
        from precis_pathway import runner

        cpuset = (params.get("resources") or {}).get("cpuset")
        handle = runner.submit_kinetics_job_detached(
            config, inputs, **({"cpuset": cpuset} if cpuset else {})
        )
    except Exception as exc:  # pragma: no cover - spawn/env dependent
        log.warning("autocatpath_kinetics: submit failed", exc_info=True)
        ctx.record_failure(f"autocatpath_kinetics: submit failed: {exc}")
        return None
    # the per-step ceiling the children run under — the running card shows
    # it so a silent step's budget is visible (gr549807)
    ctx.set_meta(kinetics_step_s=_step_seconds())
    return handle


def _drain_progress(ctx: Any, handle: Any) -> tuple[int, int]:
    """Append the progress records the detached child wrote since the last
    read as ``job_event``s — one JSON-encoded note per line, so a note
    carrying its own newlines (a ``kinetics_error`` reason with a stderr
    tail) still counts and lands once. Returns ``(total, new)`` — the
    running count of emitted notes (the done-envelope skips the ones
    already shown) and how many this call added. The byte offset and
    count ride on ``meta`` so a worker restart resumes mid-file.
    Best-effort: an absent or unreadable file emits nothing, and a record
    that doesn't decode is skipped rather than counted."""
    meta = ctx.meta if isinstance(ctx.meta, dict) else {}
    offset = int(meta.get("kinetics_progress_offset") or 0)
    emitted = int(meta.get("kinetics_progress_notes") or 0)
    try:
        path = os.path.join(str(handle["dir"]), PROGRESS_NAME)
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except (OSError, KeyError, TypeError):
        return emitted, 0
    cut = data.rfind(b"\n") + 1  # a trailing partial line waits for its flush
    if not cut:
        return emitted, 0
    new = 0
    for raw in data[:cut].decode("utf-8", "replace").split("\n"):
        if not raw:
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            continue  # not a progress record — never emitted, never counted
        ctx.append_chunk("job_event", str(line))
        new += 1
    offset += cut
    emitted += new
    ctx.set_meta(kinetics_progress_offset=offset, kinetics_progress_notes=emitted)
    meta["kinetics_progress_offset"] = offset
    meta["kinetics_progress_notes"] = emitted
    return emitted, new


def _heartbeat(ctx: Any, handle: Any, runner: Any) -> None:
    """One liveness line for a quiet poll — child pid, alive/dead and
    elapsed — so a running job does not read as stalled (gr549807)."""
    pid = handle.get("pid")
    probe = getattr(runner, "_process_alive", None)
    alive = True
    if callable(probe) and isinstance(pid, int):
        try:
            alive = bool(probe(pid))
        except OSError:
            alive = True
    started = handle.get("started_at")
    elapsed = (
        f"{max(0.0, time.time() - float(started)):.0f}"
        if isinstance(started, (int, float))
        else "?"
    )
    ctx.append_chunk(
        "job_event",
        f"autocatpath_kinetics: heartbeat — pid {pid} "
        f"{'alive' if alive else 'dead'}, {elapsed} s elapsed",
    )


def _poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half: ``False`` while the child runs — draining the
    child's ``progress.log`` into job events, with one heartbeat per poll
    when nothing new arrived; on a terminal state does ``_dispatch``'s
    persistence from the child's envelope and drives the job terminal."""
    from precis_pathway import runner
    from precis_pathway.seed_job import _reap_scratch

    status = runner.poll_seed_partial_detached(handle, defer_cleanup=True)
    state = status.get("state")
    emitted, new = _drain_progress(ctx, handle)
    if state == "running":
        meta = ctx.meta if isinstance(ctx.meta, dict) else {}
        if meta.get("kinetics_step_s") is None:
            # a job submitted before the stamp existed
            step = _step_seconds()
            ctx.set_meta(kinetics_step_s=step)
            meta["kinetics_step_s"] = step
        if not new:
            _heartbeat(ctx, handle, runner)
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
    # the first ``emitted`` notes already landed via the progress drain
    for line in (envelope.get("notes") or [])[emitted:]:
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


__all__ = [
    "NAME",
    "SPEC",
    "compute",
    "load",
    "overlay_recompute_config",
    "run_compute",
    "stored_environment",
    "stored_objective",
    "stored_sweep",
]
