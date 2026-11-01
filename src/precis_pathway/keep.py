"""Keep-everything capture for one ``run_one_seed`` call (gr543776).

Reto 2026-10-31: "We should not throw anything away." The engine drops
the NEB band geometries, the ``images_energy`` profile and the TS
geometry inside ``pipeline._finish_band``, and a killed seed loses every
finished relax with it. The engine's only public seam is ``ts_collect``
(the saddle image per step); the rest reaches us by wrapping two module-
private pipeline functions for the duration of one
:func:`precis_pathway.runner.run_seed_partial` call — the same
monkeypatch seam the test suite already uses on
``autocatpath.pipeline``:

* ``pipeline._collect_saddle(bar, n_slab, sname, ts_collect)`` runs once
  per **reported** band — the serial path, the pooled flush, AND each
  segment of a hidden-intermediate split all end here — so wrapping it
  captures exactly the bands the seed's reported barriers were computed
  from, with ``bar.images`` (the relaxed band) and ``bar.images_energy``
  (the profile) still attached.
* ``pipeline._state_trust(records, st, sname, seed, res, geo, …)`` runs
  once per finished endpoint relax, so wrapping it captures
  ``res.atoms`` — the failed-seed salvage (a SIGKILLed child's finished
  relaxes) — keeping the lowest-energy geometry per state, mirroring
  ``run_one_seed``'s own ``collect`` rule.

Everything captured is recorded twice, through one ``_record`` helper:

* into ``artifacts`` — an in-memory ``{relpath: text}`` the seed child's
  result envelope carries back to the worker, which lands each entry in
  the job artifact store (:mod:`precis.store.job_artifacts`); and
* into ``checkpoint_dir`` — the detached scratch dir's ``checkpoints/``,
  written incrementally and atomically, so a SIGKILLed seed's finished
  measurements survive on disk for :func:`seed_job._reap_scratch` to
  harvest before the dir is reaped.

Both hook installs are feature-detected (``getattr`` on the pipeline
module): an engine that lacks either private name runs exactly as
before, minus the capture — the run never fails over retention.
"""

from __future__ import annotations

import inspect
import io
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    """A step/state name as one safe path segment (``N+O→NO+O`` becomes
    ``N_O_NO_O``); the checkpoint filenames all derive from it."""
    out = _SAFE_NAME_RE.sub("_", str(name)).strip("._")
    return out or "unnamed"


def _atoms_to_extxyz(atoms: Any) -> str:
    """One ASE ``Atoms`` → an extxyz string (mirrors
    :func:`runner._atoms_to_extxyz`; duplicated so this module stays
    importable without the runner)."""
    from ase.io import write as ase_write

    buf = io.StringIO()
    ase_write(buf, atoms, format="extxyz")
    return buf.getvalue()


def _frames_extxyz(images: list[Any]) -> str:
    """A whole NEB band → one multi-frame extxyz document."""
    from ase.io import write as ase_write

    buf = io.StringIO()
    ase_write(buf, images, format="extxyz")
    return buf.getvalue()


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def band_profile(bar: Any, *, seed: int, segment: int) -> dict[str, Any]:
    """The JSON-serialisable half of one ``BarrierResult`` — the
    ``images_energy`` profile (~2 kB) plus every scalar the entry used to
    carry as prose only. The small piece the ruling allows in the job
    result; the geometries ride ``artifacts``."""
    prof: dict[str, Any] = {
        "seed": seed,
        "segment": segment,
        "images_energy": [
            float(e) for e in (getattr(bar, "images_energy", None) or ())
        ],
        "n_images": len(getattr(bar, "images", None) or ()),
        "ts_index": getattr(bar, "ts_index", None),
        "ts_fmax": _float_or_none(getattr(bar, "ts_fmax", None)),
        "converged": bool(getattr(bar, "converged", False)),
        "method": getattr(bar, "method", None),
        "barrier": _float_or_none(getattr(bar, "barrier", None)),
        "delta_e": _float_or_none(getattr(bar, "delta_e", None)),
        "e_ts": _float_or_none(getattr(bar, "e_ts", None)),
        "desorbed_images": int(getattr(bar, "desorbed_images", 0) or 0),
        "collapsed_images": int(getattr(bar, "collapsed_images", 0) or 0),
        "optimizer": getattr(bar, "optimizer", None),
    }
    multistart = getattr(bar, "multistart", None)
    if isinstance(multistart, dict):
        prof["multistart"] = multistart
    return prof


def _record(
    artifacts: dict[str, str],
    checkpoint_dir: str | None,
    relpath: str,
    text: str,
) -> None:
    """One artifact, two homes: the result-envelope ``artifacts`` dict
    and (when set) ``checkpoint_dir``, written atomically so a SIGKILL
    mid-write never leaves a torn file for the harvest."""
    artifacts[relpath] = text
    if checkpoint_dir:
        dest = Path(checkpoint_dir) / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)


def _bind(
    sig: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Call-site args resolved to the engine function's parameter names —
    robust to positional-vs-keyword drift across engine versions."""
    return dict(sig.bind(*args, **kwargs).arguments)


def install_hooks(
    pipeline: Any,
    *,
    seed: int,
    bands: dict[str, list[dict[str, Any]]],
    artifacts: dict[str, str],
    checkpoint_dir: str | None = None,
    progress: Callable[..., None] | None = None,
) -> Callable[[], None]:
    """Wrap ``pipeline``'s private collect points so one seed's bands,
    saddles and finished relaxes are captured. Returns a restore
    callable — :func:`runner.run_seed_partial` invokes it in ``finally``
    so a second in-process call never double-wraps.

    The hook reads the ``ts_collect`` dict the engine was handed straight
    off the wrapped call's bound args: after the engine's own
    ``_collect_saddle`` updates it, it holds the lowest saddle seen so
    far for the step — which the hook then serialises (overwrites keep
    the file at the running best). ``bands``/``artifacts`` are the
    caller's accumulators (``bands`` for the JSON profile list per step,
    ``artifacts`` for the byte payloads). ``progress`` is the caller's
    log callable (the child prints); capture failures go to the module
    logger, never the run."""
    say = progress or (lambda *a, **k: None)
    unpatch: list[Callable[[], None]] = []
    relax_best: dict[str, float] = {}

    orig_saddle = getattr(pipeline, "_collect_saddle", None)
    if callable(orig_saddle):
        sig = inspect.signature(orig_saddle)

        def _saddle_hook(*args: Any, **kwargs: Any) -> Any:
            out = orig_saddle(*args, **kwargs)
            try:
                bound = _bind(sig, args, kwargs)
                bar = bound.get("bar")
                sname = bound.get("sname")
                tc = bound.get("ts_collect")
                _capture_band(bar, sname, tc)
            except Exception:
                log.warning("keep: band capture failed", exc_info=True)
            return out

        def _capture_band(bar: Any, sname: Any, tc: Any) -> None:
            step = str(sname)
            profiles = bands.setdefault(step, [])
            segment = len(profiles) + 1
            prof = band_profile(bar, seed=seed, segment=segment)
            profiles.append(prof)
            stem = f"band-{safe_name(step)}"
            if segment > 1:
                stem = f"{stem}-{segment}"
            images = list(getattr(bar, "images", None) or ())
            if images:
                _record(
                    artifacts, checkpoint_dir, f"{stem}.extxyz", _frames_extxyz(images)
                )
            _record(
                artifacts,
                checkpoint_dir,
                f"{stem}.profile.json",
                json.dumps(prof),
            )
            saddle = (tc or {}).get(sname) if isinstance(tc, dict) else None
            if saddle is not None:
                _record(
                    artifacts,
                    checkpoint_dir,
                    f"ts-{safe_name(step)}.extxyz",
                    _atoms_to_extxyz(saddle),
                )

        pipeline._collect_saddle = _saddle_hook
        unpatch.append(lambda: setattr(pipeline, "_collect_saddle", orig_saddle))
    else:
        say("keep: engine has no pipeline._collect_saddle — no band capture")

    orig_trust = getattr(pipeline, "_state_trust", None)
    if callable(orig_trust):
        tsig = inspect.signature(orig_trust)

        def _trust_hook(*args: Any, **kwargs: Any) -> Any:
            out = orig_trust(*args, **kwargs)
            try:
                bound = _bind(tsig, args, kwargs)
                _capture_relax(bound.get("st"), bound.get("res"))
            except Exception:
                log.warning("keep: relax capture failed", exc_info=True)
            return out

        def _capture_relax(st: Any, res: Any) -> None:
            name = getattr(st, "name", None)
            atoms = getattr(res, "atoms", None)
            if name is None or atoms is None:
                return
            energy = _float_or_none(getattr(res, "energy", None))
            best = relax_best.get(str(name))
            # mirror `collect`'s min-energy rule: the file always holds the
            # best finished relax for the state seen so far.
            if best is not None and (energy is None or energy >= best):
                return
            if energy is not None:
                relax_best[str(name)] = energy
            stem = f"relax-{safe_name(name)}"
            _record(
                artifacts, checkpoint_dir, f"{stem}.extxyz", _atoms_to_extxyz(atoms)
            )
            _record(
                artifacts,
                checkpoint_dir,
                f"{stem}.json",
                json.dumps(
                    {
                        "energy": energy,
                        "converged": bool(getattr(res, "converged", False)),
                        "seed": seed,
                    }
                ),
            )

        pipeline._state_trust = _trust_hook
        unpatch.append(lambda: setattr(pipeline, "_state_trust", orig_trust))

    def restore() -> None:
        for undo in unpatch:
            undo()

    return restore
