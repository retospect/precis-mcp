"""Validation manifest glue (kinetics v2 slice 10, D10).

The engine owns the record and the drift judge (``autocatpath.manifest``,
engine >= 0.38); this module gathers what only precis knows — its own git
sha, the lock file the host runs from, the pathway's stored config,
snapshot and results — builds the record, and lifts the observables a
re-validation compares.

Stamps (D10 "Storage"): the pathway ref carries ``meta.validation_manifest``
(the record) and ``meta.manifest_id``; every job that produced part of the
result carries ``meta.manifest_id``; the D8 ``kinetics_v2`` record carries
``manifest_id``.  The id does not depend on any output, so the aggregate and
the kinetics job stamp the same id for the same config, lock and code.

Engine-free reads (``manifest_of``, the view) work on hosts without the
``catalyst`` extra; building or judging needs the engine.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Override for the lock file this host runs from (tests, odd layouts).
LOCK_ENV = "PRECIS_UV_LOCK"
#: Where deploy lands the uv.lock export on cluster hosts
#: (deploy/playbooks/03-precis-constraints.yml) — the lock's projection on a
#: pip-from-git venv, which has no uv.lock of its own.
HOST_CONSTRAINTS = "/opt/precis/constraints.txt"

#: Legs this slice re-runs on a CPU worker.  ``energies`` (relax/NEB/refine)
#: needs the GPU seed lane and ``tpd`` needs slice 9; both are reported as
#: not run, never as passed.
CPU_LEGS = ("kinetics", "kmc", "sweep")
NOT_RUN_LEGS = {
    "energies": "the min-energy-path leg runs on the GPU seed lane; not re-run by this job",
    "tpd": "no TPD record before kinetics v2 slice 9",
}


def engine_manifest() -> Any | None:
    """``autocatpath.manifest`` when the installed engine has it (>= 0.38)."""
    try:
        from autocatpath import manifest
    except ImportError:
        return None
    return manifest


def precis_sha() -> str | None:
    """The precis code this process runs: the image-baked ``PRECIS_GIT_SHA``,
    else the live checkout / installed dist (same chain as the status
    surface and nanopub provenance)."""
    sha = os.environ.get("PRECIS_GIT_SHA", "").strip() or None
    if sha and sha.lower() != "unknown":
        return sha
    try:
        from precis.handlers.skill import _DIST_GIT_INFO, _SOURCE_GIT_INFO

        return _SOURCE_GIT_INFO.get("git_sha") or _DIST_GIT_INFO.get("git_sha") or None
    except Exception:  # pragma: no cover - status surface unavailable
        return None


def lock_file() -> tuple[Path, str] | None:
    """The lock this host resolves from, and what kind it is: ``$PRECIS_UV_LOCK``,
    the source checkout's ``uv.lock`` (dev, CI, the image), else the deploy's
    ``constraints.txt`` export.  A uv.lock and its export hash differently,
    so a re-run on the other kind refuses — it never substitutes."""
    env = os.environ.get(LOCK_ENV, "").strip()
    if env:
        p = Path(env)
        return (p, p.name) if p.is_file() else None
    repo = Path(__file__).resolve().parents[2] / "uv.lock"
    if repo.is_file():
        return repo, "uv.lock"
    host = Path(HOST_CONSTRAINTS)
    if host.is_file():
        return host, "constraints.txt"
    return None


def lock_sha256() -> tuple[str | None, str | None]:
    found = lock_file()
    if found is None:
        return None, None
    m = engine_manifest()
    if m is None:  # pragma: no cover - callers check the engine first
        return None, found[1]
    path, source = found
    if source == "constraints.txt":
        # uv export's header comment names the (temporary) -o path, so the
        # same lock exports to different bytes; hash the pins only
        pins = [
            ln.strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        return m.sha256_text("\n".join(pins)), source
    return m.sha256_file(path), source


def _mlips(config: dict[str, Any], m: Any) -> tuple[Any, ...]:
    mlip = config.get("mlip") or {}
    try:
        from precis_pathway import runner

        specs = runner.model_specs(config)
    except Exception:  # config the engine cannot parse: record the block as given
        specs = [(str(mlip.get("backend") or "emt"), mlip.get("model"))]
    out = []
    for backend, model in specs:
        path = m.resolve_weights_path(backend, model)
        out.append(
            m.Mlip(
                backend=backend,
                model=model,
                weights_sha256=m.weights_sha256(path) if path else None,
                task=mlip.get("task"),
                dtype=mlip.get("dtype"),
            )
        )
    return tuple(out)


def build(
    config: dict[str, Any],
    *,
    config_snapshot_yaml: str,
    results: dict[str, Any],
    autocatpath_version: str,
    slab_extxyz: str | None = None,
) -> dict[str, Any] | None:
    """The stored form (``to_json``) of the manifest for one pathway result,
    or ``None`` when the engine predates the manifest (< 0.38)."""
    m = engine_manifest()
    if m is None:
        return None
    import autocatpath
    from autocatpath import corrections

    env = config.get("environment")
    v2 = results.get("kinetics_v2") or {}
    uv_sha, source = lock_sha256()
    rec = m.ValidationManifest(
        code=m.Code(
            precis_sha=precis_sha(),
            autocatpath_version=autocatpath_version,
            autocatpath_sha=getattr(autocatpath, "__git_sha__", None),
        ),
        lock=m.Lock(uv_lock_sha256=uv_sha, lock_source=source, mlip=_mlips(config, m)),
        platform=m.capture_platform(),
        seeds=m.seeds_from_config(config),
        inputs=m.Inputs(
            slab_extxyz_sha256=m.sha256_text(slab_extxyz) if slab_extxyz else None,
            network={
                "digest": results.get("network_digest"),
                "template": config.get("network"),
            },
            environment=env,
            environment_sha256=m.sha256_obj(env) if env is not None else None,
            provider={
                "id": v2.get("provider") or "vacuum",
                "params": (env or {}).get("electrochemical"),
            },
            config_snapshot_sha256=m.sha256_text(config_snapshot_yaml),
            # the policy tuple (pathway-correction-basis.md) is a projection
            # of the config, which config_snapshot_sha256 already pins
            corrections={"version": getattr(corrections, "VERSION", None)},
        ),
    )
    return dict(rec.to_json())


def stamp_results(
    results: dict[str, Any], manifest: dict[str, Any] | None
) -> str | None:
    """Fold the manifest id into the D8 record (``kinetics_v2.manifest_id``)
    in place; returns the id."""
    if not manifest:
        return None
    mid = str(manifest["manifest_id"])
    v2 = results.get("kinetics_v2")
    if isinstance(v2, dict):
        v2["manifest_id"] = mid
    return mid


def build_safely(**kw: Any) -> dict[str, Any] | None:
    """:func:`build` that never raises — a stamp is provenance riding on a
    finished result and must never fail it."""
    try:
        return build(**kw)
    except Exception:
        log.warning("validation manifest: build failed", exc_info=True)
        return None


def manifest_of(meta: dict[str, Any]) -> dict[str, Any] | None:
    rec = meta.get("validation_manifest")
    return rec if isinstance(rec, dict) else None


def observables_of(results: dict[str, Any]) -> dict[str, Any]:
    """The D10 observables of a stored ``meta.results`` (engine needed)."""
    m = engine_manifest()
    if m is None:
        raise RuntimeError("autocatpath >= 0.38 (autocatpath.manifest) is required")
    return dict(m.observables(results))


def manifest_text(meta: dict[str, Any]) -> str:
    """``view='manifest'``: the record, its tolerance contract and every
    re-validation verdict, engine-free."""
    rec = manifest_of(meta)
    if rec is None:
        return (
            "No validation manifest on this pathway — results aggregated on "
            "autocatpath >= 0.38 carry one (meta.validation_manifest)."
        )
    code, lock, plat = (
        rec.get("code") or {},
        rec.get("lock") or {},
        rec.get("platform") or {},
    )
    seeds, inputs = rec.get("seeds") or {}, rec.get("inputs") or {}
    mid = rec.get("manifest_id")
    reval = meta.get("revalidation") or {}
    runs = reval.get(mid) if isinstance(reval, dict) else None
    validated = any(r.get("complete") is True for r in runs or [])
    lines = [
        f"manifest_id: {mid}",
        "validated: yes (a complete re-validation reproduced every leg)"
        if validated
        else "validated: NO — no complete re-validation; a reproduced_partial "
        "verdict (legs not run, or an unstamped engine) does not validate",
        f"code: precis {code.get('precis_sha') or '?'} · autocatpath "
        f"{code.get('autocatpath_version')} ("
        + (
            code.get("autocatpath_sha")
            or "ENGINE COMMIT NOT STAMPED — the version string is reused across "
            "commits, so this result cannot be validated"
        )
        + ")",
        f"lock: {lock.get('lock_source') or '?'} {lock.get('uv_lock_sha256') or 'none'}",
    ]
    for ml in lock.get("mlip") or []:
        lines.append(
            f"  mlip {ml.get('backend')}:{ml.get('model')} weights "
            f"{ml.get('weights_sha256') or 'not located when stamped'}"
        )
    lines += [
        f"platform (recorded, not hashed): {plat.get('os')}/{plat.get('arch')}/"
        f"{plat.get('accelerator')} · numpy {plat.get('numpy')} · numba "
        f"{plat.get('numba')} · torch {plat.get('torch')} · blas {plat.get('blas')}",
        f"seeds: search {seeds.get('search_seeds')} · kinetics {seeds.get('kinetics_seed')}"
        f" · kmc {seeds.get('kmc_seed')} ({len(seeds.get('kmc_replica_seeds') or [])} replicas)"
        f" · lhs {seeds.get('lhs_seed')}",
        f"inputs: config_snapshot {inputs.get('config_snapshot_sha256')} · network "
        f"{(inputs.get('network') or {}).get('template')}@"
        f"{(inputs.get('network') or {}).get('digest')} · environment "
        f"{inputs.get('environment_sha256') or 'legacy (none)'} · corrections "
        f"{(inputs.get('corrections') or {}).get('version')}",
        "",
        "tolerances (observable · unit · same platform · cross-platform):",
    ]
    for t in rec.get("tolerances") or []:

        def cell(c: dict[str, Any]) -> str:
            v = c.get("value")
            return c.get("rule", "?") + ("" if v is None else f" ≤ {v:g}")

        lines.append(
            f"  {t.get('observable')} · {t.get('unit')} · {cell(t.get('same_platform') or {})}"
            f" · {cell(t.get('cross_platform') or {})}"
        )
    lines.append("")
    if not runs:
        lines.append(
            "re-validations: none — put(kind='pathway', id=<handle>, mode='revalidate', "
            f"args={{'manifest': '{mid}'}})"
        )
    else:
        lines.append("re-validations:")
        for r in runs:
            pt, pn = r.get("platform_then") or {}, r.get("platform_now") or {}
            lines.append(
                f"  {r.get('at')} job {r.get('job_id')}: {r.get('verdict')} "
                f"(complete: {'true' if r.get('complete') is True else 'false'}) "
                f"[{r.get('column')}] then {pt.get('os')}/{pt.get('arch')}/{pt.get('accelerator')}"
                f" → now {pn.get('os')}/{pn.get('arch')}/{pn.get('accelerator')}"
            )
            for o in r.get("observables") or []:
                if not o.get("ok"):
                    lines.append(
                        f"    drift {o.get('observable')}: Δ {o.get('delta')} > "
                        f"{o.get('tolerance')} ({o.get('rule')}) {o.get('note') or ''}".rstrip()
                    )
            why = r.get("not_run_why") or {}
            for leg in r.get("not_run") or []:
                lines.append(
                    f"    not run: {leg} — {why.get(leg, 'not re-run on this host')}"
                )
            for c in r.get("caveats") or []:
                lines.append(f"    caveat: {c}")
    return "\n".join(lines)


__all__ = [
    "CPU_LEGS",
    "NOT_RUN_LEGS",
    "build",
    "build_safely",
    "engine_manifest",
    "lock_sha256",
    "manifest_of",
    "manifest_text",
    "observables_of",
    "precis_sha",
    "stamp_results",
]
