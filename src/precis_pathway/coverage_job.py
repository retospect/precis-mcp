"""``surface_coverage_scan`` job_type — compute one model's coverage anchors
on a slab and fold them into the inner CHE sweep (surface-Pourbaix slice 1,
``docs/backlog/surface-pourbaix-staircase-optimizer.md``).

One job = one ``(slab config, model)`` scan: catpath's ab-initio
thermodynamics coverage scan (``autocatpath.coverage.scan``) relaxes the
clean slab and each requested adsorbate at each requested coverage, prices
every termination against the engine's reservoirs, and returns
``gamma(theta)`` per adsorbate — the **anchors** the outer loop pays for
once per slab. The job then:

1. stamps the anchor footing onto its meta — ``anchor_key`` (slab +
   adsorbates + coverages + conditions + engine version; the model is NOT
   in the key so several models pool under one), ``model``,
   ``engine_version``, ``corrections`` (the correction set the engine ran
   with, ``None`` on an engine that records none) — so every anchor says
   what it was measured on (slice-1 acceptance: one footing);
2. pools this scan with every earlier succeeded scan on the same
   ``anchor_key`` (one per model, newest wins) and runs
   :func:`precis_pathway.surface_pourbaix.sweep` over them — the resting
   envelope along U, every boundary with its propagated band and, with two
   or more models, its model-form band — into ``meta.che_sweep`` and a
   ``job_summary`` chunk.

Scope (Reto's 2026-10-07 rulings): the engine scans the clean
``config.slab`` it builds itself, (111) only — ``coverage.facets`` must be
empty and ``slab.miller`` unset or (1,1,1). A doped candidate or a β-PdH
slab cannot be scanned until catpath's scan honours the prebuilt-slab side
channel (briefed to the catpath session); the job refuses ``slab_extxyz``
with ``failure_class="input"`` rather than silently scanning the wrong
surface.

Registered via the ``precis.job_types`` entry point
(``surface_coverage_scan = precis_pathway.coverage_job:SPEC``). Same
executor seam as ``autocatpath_seed``: ``ssh_node`` with a ``target_node``
pin to the box that has the engine + backend, the compute in a fresh,
killable child (:func:`precis_pathway.runner.run_coverage_scan_subprocess`)
under ``resources.wall_seconds``.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "surface_coverage_scan"

#: Slice-1 default anchor set: the terminations the resting-state map needs
#: (transfer prompt §3: "bare, H, O, OH, oxide, plus every intermediate";
#: the oxide is a bulk phase, Part A's business; intermediates come from
#: the pathway run itself).
DEFAULT_ADSORBATES: tuple[str, ...] = ("H", "O", "OH")
DEFAULT_COVERAGES: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)
DEFAULT_U_WINDOW: tuple[float, float] = (-1.0, 0.5)
DEFAULT_GRID = 151
DEFAULT_PH = 7.0

_RANGE = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The catpath config (same shape the pathway lane runs — slab, mlip,
        # conditions, thermo). Its ``coverage`` block is filled with the
        # slice-1 defaults when absent.
        "config": {"type": "object"},
        # Which cfg.mlip.specs() entry this scan runs.
        "model_index": {"type": "integer", "minimum": 0},
        # Backend override; null -> the config's own mlip.backend.
        "force_backend": {"type": ["string", "null"]},
        # Content address of the anchor set (runner.coverage_anchor_key);
        # recomputed at dispatch when absent.
        "anchor_key": {"type": ["string", "null"]},
        # The node this job pins itself to (claim gate -> runs here).
        "target_node": {"type": ["string", "null"]},
        # Sweep framing: the operating point (V vs RHE) the summary names,
        # the U window, the grid, and the pH that labels the SHE view.
        "point_U_RHE": {"type": ["number", "null"]},
        "window_U_RHE": _RANGE,
        "grid": {"type": "integer", "minimum": 2, "maximum": 2001},
        "pH": {"type": "number"},
        # Per-anchor energy bar (eV) for the propagated band; default =
        # config.search.energy_thresh, else 0.05.
        "sigma_e_eV": {"type": "number", "minimum": 0},
        # Refused (see the module docstring) — listed so the refusal is a
        # typed input error, not a silent drop.
        "slab_extxyz": {"type": ["string", "null"]},
    },
    "required": ["config"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Compute one MLIP model's adsorbate coverage anchors on a slab "
    "(autocatpath coverage scan) and fold them into the inner CHE sweep "
    "(surface-Pourbaix slice 1)."
)


def with_defaults(config: dict[str, Any]) -> dict[str, Any]:
    """``config`` with the slice-1 ``coverage`` defaults filled in (a copy;
    caller-set keys win)."""
    cfg = dict(config)
    cov = dict(cfg.get("coverage") or {})
    cov.setdefault("adsorbates", list(DEFAULT_ADSORBATES))
    cov.setdefault("coverages", list(DEFAULT_COVERAGES))
    cov.setdefault("facets", [])
    cfg["coverage"] = cov
    return cfg


def check_params(params: dict[str, Any]) -> str | None:
    """The reason a params dict is unusable, or ``None``. Scope rules of the
    module docstring: (111) only, no extra facets, no prebuilt slab."""
    config = params.get("config")
    if not isinstance(config, dict) or not config:
        return "params.config must be a non-empty catpath config object"
    if params.get("slab_extxyz"):
        return (
            "slab_extxyz is not scannable: catpath's coverage scan builds its own "
            "clean slab from config.slab and ignores a prebuilt slab (doped and "
            "hydride slabs wait on the catpath brief)"
        )
    cov = config.get("coverage") or {}
    if cov.get("facets"):
        return "coverage.facets must be empty: slice 1 scans the (111) facet only"
    miller = (config.get("slab") or {}).get("miller")
    if miller is not None and [int(i) for i in miller] != [1, 1, 1]:
        return f"slab.miller={miller!r} is out of scope: slice 1 scans (111) only"
    w = params.get("window_U_RHE")
    if w is not None and not float(w[0]) < float(w[1]):
        return "window_U_RHE must be [lo, hi] with lo < hi"
    return None


def find_sibling_scans(
    store: Any, anchor_key: str, *, exclude: int | None = None
) -> dict[str, dict[str, Any]]:
    """``{model: coverage payload}`` from the newest succeeded
    ``surface_coverage_scan`` job per model on ``anchor_key`` (``exclude``
    drops the calling job's own row). The lookup mirrors
    ``pourbaix_bulk.find_cached_entries``: newest ``ref_id`` first, a job
    that did not reach ``STATUS:succeeded`` is skipped."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->>'model', meta->'coverage' FROM refs "
            "WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'job_type' = %(jt)s "
            "AND meta->>'anchor_key' = %(key)s "
            "ORDER BY ref_id DESC LIMIT 50",
            {"jt": NAME, "key": anchor_key},
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for ref_id, model, coverage in rows:
        if exclude is not None and int(ref_id) == exclude:
            continue
        if not model or not isinstance(coverage, dict) or model in out:
            continue
        if any(str(t) == "STATUS:succeeded" for t in store.tags_for(int(ref_id))):
            out[str(model)] = coverage
    return out


def _sigma_e(params: dict[str, Any], config: dict[str, Any]) -> float:
    from .surface_pourbaix import DEFAULT_SIGMA_E_EV

    if params.get("sigma_e_eV") is not None:
        return float(params["sigma_e_eV"])
    thresh = (config.get("search") or {}).get("energy_thresh")
    return (
        float(thresh)
        if isinstance(thresh, int | float) and thresh > 0
        else DEFAULT_SIGMA_E_EV
    )


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job: one scan
    in a killable child, then the pooled sweep onto this job's own meta."""
    params = (ctx.meta or {}).get("params") or {}
    bad = check_params(params)
    if bad is not None:
        ctx.record_failure(f"{NAME}: {bad}", failure_class="input")
        return
    config = with_defaults(dict(params["config"]))
    model_index = int(params.get("model_index", 0) or 0)
    resources = params.get("resources") or {}
    try:
        timeout_s = int(resources.get("wall_seconds") or 0)
    except (TypeError, ValueError):
        timeout_s = 0
    cpuset = resources.get("cpuset")

    from . import runner, surface_pourbaix

    anchor_key = str(params.get("anchor_key") or runner.coverage_anchor_key(config))
    ctx.append_chunk(
        "job_event",
        f"{NAME}: {config.get('name', '?')} model#{model_index} "
        f"adsorbates={config['coverage']['adsorbates']} "
        f"coverages={config['coverage']['coverages']} key={anchor_key[:12]}…",
    )
    try:
        kw: dict[str, Any] = {"force_backend": params.get("force_backend")}
        if timeout_s > 0:
            kw["timeout"] = timeout_s
        if cpuset:
            kw["cpuset"] = cpuset
        result = runner.run_coverage_scan_subprocess(config, model_index, **kw)
    except runner.ChildKilledError as exc:
        log.warning("%s: run failed (child killed)", NAME, exc_info=True)
        ctx.record_failure(f"{NAME}: run failed: {exc}", open_tag="infra:child-killed")
        return
    except Exception as exc:  # pragma: no cover - env/compute dependent
        log.warning("%s: run failed", NAME, exc_info=True)
        ctx.record_failure(f"{NAME}: run failed: {exc}")
        return

    model = str(result["model"])
    coverage = result["coverage"]
    own_id = getattr(ctx, "ref_id", None)
    payloads = find_sibling_scans(
        ctx.store, anchor_key, exclude=int(own_id) if isinstance(own_id, int) else None
    )
    payloads[model] = coverage  # this run wins over an older scan of the same model
    window = params.get("window_U_RHE") or list(DEFAULT_U_WINDOW)
    point = params.get("point_U_RHE")
    che = surface_pourbaix.sweep(
        payloads,
        u_window=(float(window[0]), float(window[1])),
        grid=int(params.get("grid") or DEFAULT_GRID),
        ph=float(params.get("pH", DEFAULT_PH)),
        point_u_rhe=None if point is None else float(point),
        sigma_e_ev=_sigma_e(params, config),
    )
    ctx.set_meta(
        anchor_key=anchor_key,
        model=model,
        model_index=model_index,
        engine_version=result["engine_version"],
        corrections=result.get("corrections"),
        coverage=coverage,
        che_sweep=che,
    )
    warnings = list(coverage.get("warnings") or [])
    text = surface_pourbaix.render_text(che)
    if warnings:
        text += "\nscan warnings: " + "; ".join(str(w) for w in warnings[:6])
    ctx.append_chunk("job_summary", text)
    ctx.set_status("succeeded")


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError(f"{NAME} runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "NAME",
    "SPEC",
    "check_params",
    "find_sibling_scans",
    "load",
    "with_defaults",
]
