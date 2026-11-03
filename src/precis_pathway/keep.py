"""Keep-everything capture for one ``run_one_seed`` call (gr543776).

Reto 2026-10-31: "We should not throw anything away." The engine drops
the NEB band geometries, the ``images_energy`` profile and the TS
geometry inside ``pipeline._finish_band``, and a killed seed loses every
finished relax with it. The engine's only public seam is ``ts_collect``
(the saddle image per step); the rest reaches us by wrapping module-
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

gr543776 items 4–6 add a scope stack over the same seam — wrappers on
the enclosing private functions push a labelled scope, and wrappers on
the leaf calls record under it:

* ``pipeline._run_neb`` (item 5, serial path) scopes one step's NEB:
  every ``pipeline.neb_barrier`` call inside it is indexed (call 1 is
  the primary band, call ≥2 the auto-retry), and every
  ``neb._neb_attempt`` rung inside a call is kept as
  ``att-<step>-<calltag>-s<init_seed>-<opt>-r<rung>.{extxyz,attempt.json}``
  — the raw band and the energy profile the engine discards for
  multistart starts, denser-band retries, optimizer-fallback reruns and
  auto-retry bands. The ``neb`` module is resolved from
  ``pipeline.neb_barrier.__module__`` so the inner rung seam follows the
  engine's own import.
* ``pipeline._split_and_stamp`` (item 4) scopes the hidden-intermediate
  repair: ``pipeline.relax`` is additionally wrapped inside it to keep
  ``mres.atoms`` (the newly found minimum), the two segment
  ``neb_barrier`` calls rung-capture as ``seg1``/``seg2``, and a
  ``split-<step>.json`` index records the dip image and outcome.
* ``pipeline._relax_state_bound`` (item 6) scopes one endpoint's
  bind-or-reseat: inside it ``pipeline._relax_state`` keeps the plain
  (desorbed/failed) geometry, ``pipeline._dissolve_reseat`` keeps each
  failed attempt's geometry, and ``pipeline._adsorption_barrier`` pushes
  an ``adsorption`` scope so the spring-free NEB band rungs are kept;
  the returned ``diag`` (per-attempt anchor/worst heights and
  ``ads_barrier``) lands as ``reseat-<state>.json`` — the heights as
  data, not only warning text.
* ``pipeline._pool_run_nebs`` (item 5, pooled path) scopes the pooled
  flush and wraps ``pipeline.neb_barrier_pool`` inside it: each call's
  returned bands are kept per job (mapped back to ``sname`` by endpoint
  identity against the scope's ``jobs``), the first call as ``pool`` and
  later calls as ``poolretry<n>``. Bands relaxed inside the pool's own
  tick loop never pass through ``_neb_attempt``, so its intra-pool
  densify discards stay engine-internal — the pool-level discards
  (replaced first attempts) are the kept ones.

Everything captured is recorded twice, through one :func:`record`
helper:

* into ``artifacts`` — an in-memory ``{relpath: text}`` the seed child's
  result envelope carries back to the worker, which lands each entry in
  the job artifact store (:mod:`precis.store.job_artifacts`); and
* into ``checkpoint_dir`` — the detached scratch dir's ``checkpoints/``,
  written incrementally and atomically, so a SIGKILLed seed's finished
  measurements survive on disk for :func:`seed_job._reap_scratch` to
  harvest before the dir is reaped.

All hook installs are feature-detected (``getattr`` on the pipeline
module): an engine that lacks any of the private names runs exactly as
before, minus the capture — the run never fails over retention.
"""

from __future__ import annotations

import inspect
import io
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
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


def record(
    artifacts: dict[str, str],
    checkpoint_dir: str | None,
    relpath: str,
    text: str,
) -> None:
    """One artifact, two homes: the result-envelope ``artifacts`` dict
    and (when set) ``checkpoint_dir``, written atomically so a SIGKILL
    mid-write never leaves a torn file for the harvest. Public: the
    runner records the step-retry full-network partial through it too."""
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
                record(
                    artifacts, checkpoint_dir, f"{stem}.extxyz", _frames_extxyz(images)
                )
            record(
                artifacts,
                checkpoint_dir,
                f"{stem}.profile.json",
                json.dumps(prof),
            )
            saddle = (tc or {}).get(sname) if isinstance(tc, dict) else None
            if saddle is not None:
                record(
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
            record(artifacts, checkpoint_dir, f"{stem}.extxyz", _atoms_to_extxyz(atoms))
            record(
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

    # ── items 4–6: the scope stack ────────────────────────────────────
    # Enclosing wrappers push a labelled scope; the leaf wrappers
    # (``pipeline.neb_barrier`` call indexing, ``neb._neb_attempt`` rung
    # capture) read the stack top to label each kept band by attempt
    # kind and index.
    scopes: list[dict[str, Any]] = []

    def _call_tag(scope_kind: str, call: int) -> str:
        """The attempt kind a ``neb_barrier`` call index means inside one
        scope: first call of a step is the primary band, later calls the
        auto-retry escalation; inside a split it is the segment number."""
        if scope_kind == "step":
            return "a1" if call <= 1 else f"retry{call}"
        if scope_kind == "split":
            return f"seg{call}"
        if scope_kind == "adsorption":
            return f"ads{call}"
        if scope_kind == "pool":
            return f"call{call}"
        return f"c{call}"

    def _match_pool_job(jobs: Any, r: Any, p: Any) -> Any:
        """One pool job dict back from the ``(reactant, product)`` pair a
        ``neb_barrier``/``neb_barrier_pool`` call was handed — identity
        match, the same Atoms objects the job dicts hold."""
        if r is None:
            return None
        for j in jobs or ():
            if (
                isinstance(j, Mapping)
                and j.get("r") is r
                and (p is None or j.get("p") is p)
            ):
                return j
        return None

    def _unique_stem(base: str) -> str:
        """``base`` or ``base-dup<N>`` — never a stem already recorded:
        a repeated attempt (a second pool flush, an unscoped call)
        keeps its own artifact rather than overwriting the first."""
        stem, dup = base, 0
        while any(
            f"{stem}{sfx}" in artifacts
            for sfx in (".extxyz", ".json", ".attempt.json", ".profile.json")
        ):
            dup += 1
            stem = f"{base}-dup{dup}"
        return stem

    orig_neb = getattr(pipeline, "neb_barrier", None)
    if callable(orig_neb):
        nsig = inspect.signature(orig_neb)

        def _neb_hook(*args: Any, **kwargs: Any) -> Any:
            scope = scopes[-1] if scopes else None
            name = scope.get("name") if scope is not None else None
            call = 0
            if scope is not None:
                scope["calls"] = scope.get("calls", 0) + 1
                call = scope["calls"]
                if scope["kind"] == "pool":
                    try:
                        b = _bind(nsig, args, kwargs)
                        j = _match_pool_job(
                            scope.get("jobs"), b.get("reactant"), b.get("product")
                        )
                    except Exception:
                        j = None
                    if j is not None:
                        name = j.get("sname") or name
            frame: dict[str, Any] = {
                "kind": "call",
                "scope": scope["kind"] if scope is not None else "misc",
                "call": call,
                "name": name,
                "rungs": {},
            }
            scopes.append(frame)
            try:
                return orig_neb(*args, **kwargs)
            finally:
                scopes.pop()

        pipeline.neb_barrier = _neb_hook
        unpatch.append(lambda: setattr(pipeline, "neb_barrier", orig_neb))

        # The rung seam lives in the engine's neb module, resolved off
        # the function's own __module__ so an aliased import still finds
        # it; ``_neb_attempt`` is the one place every serial band —
        # multistart start, denser retry, fallback rerun, auto-retry,
        # split segment, adsorption band — passes through. The module's
        # own ``neb_barrier`` name gets the same wrapper: the pool's
        # sequential fallback (neb-ode) calls it through there, not
        # through the pipeline alias.
        neb_mod = sys.modules.get(getattr(orig_neb, "__module__", "") or "")
        if neb_mod is not None and getattr(neb_mod, "neb_barrier", None) is orig_neb:
            setattr(neb_mod, "neb_barrier", _neb_hook)
            unpatch.append(lambda: setattr(neb_mod, "neb_barrier", orig_neb))
        orig_attempt = getattr(neb_mod, "_neb_attempt", None)
        if callable(orig_attempt) and neb_mod is not None:
            asig = inspect.signature(orig_attempt)

            def _attempt_hook(*args: Any, **kwargs: Any) -> Any:
                out = orig_attempt(*args, **kwargs)
                try:
                    es, converged, images = out
                    _capture_attempt(_bind(asig, args, kwargs), es, converged, images)
                except Exception:
                    log.warning("keep: attempt capture failed", exc_info=True)
                return out

            def _capture_attempt(
                bound: dict[str, Any], es: Any, converged: Any, images: Any
            ) -> None:
                frame = (
                    scopes[-1] if scopes and scopes[-1].get("kind") == "call" else None
                )
                scope_kind = (
                    frame["scope"]
                    if frame is not None
                    else (scopes[-1]["kind"] if scopes else "misc")
                )
                call = frame["call"] if frame is not None else 0
                base = (frame.get("name") if frame is not None else None) or "run"
                init_seed = int(bound.get("init_seed") or 0)
                opt = str(bound.get("optimizer") or "bfgs")
                rungs = frame["rungs"] if frame is not None else {}
                key = (init_seed, opt)
                rung = rungs.get(key, 0) + 1
                rungs[key] = rung
                if frame is not None:
                    fallback = opt != frame.setdefault("primary_opt", opt)
                else:
                    fallback = False
                tag = _call_tag(scope_kind, call)
                stem = _unique_stem(
                    f"att-{safe_name(base)}-{tag}-s{init_seed}-{safe_name(opt)}-r{rung}"
                )
                meta = {
                    "kind": "neb-attempt",
                    "seed": seed,
                    "scope": scope_kind,
                    "name": str(base),
                    "call": call,
                    "call_tag": tag,
                    "init_seed": init_seed,
                    "rung": rung,
                    "optimizer": opt,
                    "fallback": fallback,
                    "converged": bool(converged),
                    "n_images_requested": bound.get("n_images"),
                    "max_steps": bound.get("max_steps"),
                    "fmax": _float_or_none(bound.get("fmax")),
                    "n_band_images": len(images or ()),
                    "images_energy": [float(e) for e in (es or ())],
                }
                record(
                    artifacts,
                    checkpoint_dir,
                    f"{stem}.attempt.json",
                    json.dumps(meta),
                )
                if images:
                    record(
                        artifacts,
                        checkpoint_dir,
                        f"{stem}.extxyz",
                        _frames_extxyz(list(images)),
                    )

            setattr(neb_mod, "_neb_attempt", _attempt_hook)
            unpatch.append(lambda: setattr(neb_mod, "_neb_attempt", orig_attempt))

    orig_run_neb = getattr(pipeline, "_run_neb", None)
    if callable(orig_run_neb):
        rsig = inspect.signature(orig_run_neb)

        def _run_neb_hook(*args: Any, **kwargs: Any) -> Any:
            try:
                sname = _bind(rsig, args, kwargs).get("sname")
            except Exception:
                sname = None
            scopes.append({"kind": "step", "name": str(sname), "calls": 0})
            try:
                return orig_run_neb(*args, **kwargs)
            finally:
                scopes.pop()

        pipeline._run_neb = _run_neb_hook
        unpatch.append(lambda: setattr(pipeline, "_run_neb", orig_run_neb))

    orig_split = getattr(pipeline, "_split_and_stamp", None)
    if callable(orig_split):
        ssig = inspect.signature(orig_split)

        def _split_hook(*args: Any, **kwargs: Any) -> Any:
            try:
                bound = _bind(ssig, args, kwargs)
            except Exception:
                bound = {}
            sname = str(bound.get("sname"))
            dip = bound.get("dip")
            scopes.append({"kind": "split", "name": sname, "calls": 0})
            held: dict[str, Any] = {}
            orig_relax = getattr(pipeline, "relax", None)
            if callable(orig_relax):

                def _relax_hook(*a: Any, **k: Any) -> Any:
                    res = orig_relax(*a, **k)
                    # serialise NOW — the engine may mutate the Atoms in
                    # place before _split_and_stamp returns.
                    try:
                        held.setdefault(
                            "min_xyz", _atoms_to_extxyz(getattr(res, "atoms", None))
                        )
                        held.setdefault("mres", res)
                    except Exception:
                        log.warning("keep: split-min capture failed", exc_info=True)
                    return res

                pipeline.relax = _relax_hook
            try:
                out = orig_split(*args, **kwargs)
            finally:
                if callable(orig_relax):
                    pipeline.relax = orig_relax
                scopes.pop()
            try:
                _capture_split(sname, dip, held, out)
            except Exception:
                log.warning("keep: split capture failed", exc_info=True)
            return out

        def _capture_split(
            sname: str, dip: Any, held: dict[str, Any], out: Any
        ) -> None:
            """The hidden-intermediate finding: the relaxed dip image
            (``mres.atoms``, the newly found minimum — segment bands ride
            the rung/saddle captures) and the split index."""
            mres = held.get("mres")
            if held.get("min_xyz") is not None:
                record(
                    artifacts,
                    checkpoint_dir,
                    f"split-{safe_name(sname)}-min.extxyz",
                    held["min_xyz"],
                )
            idx = {
                "kind": "hidden_intermediate_split",
                "seed": seed,
                "step": sname,
                "dip_image": (dip + 1 if isinstance(dip, int) else dip),
                "intermediate_energy": _float_or_none(getattr(mres, "energy", None)),
                "stamped": bool(out),
            }
            record(
                artifacts,
                checkpoint_dir,
                f"split-{safe_name(sname)}.json",
                json.dumps(idx, default=str),
            )

        pipeline._split_and_stamp = _split_hook
        unpatch.append(lambda: setattr(pipeline, "_split_and_stamp", orig_split))

    orig_rsb = getattr(pipeline, "_relax_state_bound", None)
    if callable(orig_rsb):
        rbsig = inspect.signature(orig_rsb)

        def _rsb_hook(*args: Any, **kwargs: Any) -> Any:
            try:
                bound = _bind(rbsig, args, kwargs)
            except Exception:
                bound = {}
            state = bound.get("state")
            sname = str(getattr(state, "name", state))
            scopes.append({"kind": "reseat", "name": sname, "calls": 0})
            held: dict[str, Any] = {"plain": None, "attempts": []}
            installed: list[tuple[str, Any]] = []

            def _patch(name: str, wrap: Callable[..., Any]) -> None:
                orig = getattr(pipeline, name, None)
                if callable(orig):
                    setattr(pipeline, name, wrap(orig))
                    installed.append((name, orig))

            def _wrap_relax_state(orig: Any) -> Any:
                def h(*a: Any, **k: Any) -> Any:
                    out = orig(*a, **k)
                    # serialise NOW — later mutation of the held Atoms
                    # would corrupt what was kept.
                    try:
                        if held["plain"] is None:
                            res, geo = out
                            held["plain"] = (
                                _atoms_to_extxyz(getattr(res, "atoms", None)),
                                bool(getattr(geo, "ok", True)),
                            )
                    except Exception:
                        log.warning("keep: plain capture failed", exc_info=True)
                    return out

                return h

            def _wrap_dissolve(orig: Any) -> Any:
                def h(*a: Any, **k: Any) -> Any:
                    out = orig(*a, **k)
                    try:
                        res2, geo2 = out
                        held["attempts"].append(
                            (
                                _atoms_to_extxyz(getattr(res2, "atoms", None)),
                                bool(getattr(geo2, "ok", False)),
                            )
                        )
                    except Exception:
                        log.warning("keep: reseat capture failed", exc_info=True)
                    return out

                return h

            def _wrap_adsorption(orig: Any) -> Any:
                def h(*a: Any, **k: Any) -> Any:
                    scopes.append({"kind": "adsorption", "name": sname, "calls": 0})
                    try:
                        return orig(*a, **k)
                    finally:
                        scopes.pop()

                return h

            _patch("_relax_state", _wrap_relax_state)
            _patch("_dissolve_reseat", _wrap_dissolve)
            _patch("_adsorption_barrier", _wrap_adsorption)
            try:
                out = orig_rsb(*args, **kwargs)
            finally:
                for name, orig in installed:
                    setattr(pipeline, name, orig)
                scopes.pop()
            try:
                _capture_reseat(sname, held, out)
            except Exception:
                log.warning("keep: reseat capture failed", exc_info=True)
            return out

        def _capture_reseat(sname: str, held: dict[str, Any], out: Any) -> None:
            """Desorbed and failed reseat geometries plus the ``diag``
            trace (per-attempt anchor/worst heights, ``ads_barrier``) as
            data. The adsorption band itself rides the ``att-…-ads``
            rung capture under the adsorption scope."""
            res, geo, bound_out, diag = out
            attempts_diag = (diag or {}).get("attempts") or []
            if not attempts_diag and bound_out:
                return  # bound on the plain relax — nothing dropped
            record(
                artifacts,
                checkpoint_dir,
                f"reseat-{safe_name(sname)}.json",
                json.dumps(
                    {
                        "kind": "reseat",
                        "seed": seed,
                        "state": sname,
                        "bound": bool(bound_out),
                        "diag": diag,
                    },
                    default=str,
                ),
            )
            plain = held.get("plain")
            if plain is not None:
                plain_xyz, plain_ok = plain
                if plain_xyz is not None and not plain_ok:
                    record(
                        artifacts,
                        checkpoint_dir,
                        f"reseat-{safe_name(sname)}-plain.extxyz",
                        plain_xyz,
                    )
            for i, attempt in enumerate(held["attempts"]):
                xyz, geo2_ok = attempt
                ok = (
                    bool(attempts_diag[i].get("ok"))
                    if i < len(attempts_diag)
                    else geo2_ok
                )
                if ok or xyz is None:
                    continue  # the bound reseat rides the relax capture
                record(
                    artifacts,
                    checkpoint_dir,
                    f"reseat-{safe_name(sname)}-fail-{i + 1}.extxyz",
                    xyz,
                )

        pipeline._relax_state_bound = _rsb_hook
        unpatch.append(lambda: setattr(pipeline, "_relax_state_bound", orig_rsb))

    orig_pool_nebs = getattr(pipeline, "_pool_run_nebs", None)
    if callable(orig_pool_nebs):
        psig = inspect.signature(orig_pool_nebs)

        def _pool_hook(*args: Any, **kwargs: Any) -> Any:
            try:
                jobs = _bind(psig, args, kwargs).get("jobs") or []
            except Exception:
                jobs = []
            scope: dict[str, Any] = {
                "kind": "pool",
                "name": "pool",
                "calls": 0,
                "pool_calls": 0,
                "jobs": jobs,
            }
            scopes.append(scope)
            orig_pb = getattr(pipeline, "neb_barrier_pool", None)
            installed = False
            if callable(orig_pb):

                def _pb_hook(jobpairs: Any, *a: Any, **k: Any) -> Any:
                    scope["pool_calls"] += 1
                    call_n = scope["pool_calls"]
                    bars = orig_pb(jobpairs, *a, **k)
                    try:
                        _capture_pool_bars(scope, jobpairs, bars, call_n)
                    except Exception:
                        log.warning("keep: pool capture failed", exc_info=True)
                    return bars

                pipeline.neb_barrier_pool = _pb_hook
                installed = True
            try:
                return orig_pool_nebs(*args, **kwargs)
            finally:
                if installed:
                    pipeline.neb_barrier_pool = orig_pb
                scopes.pop()

        def _capture_pool_bars(
            scope: dict[str, Any], jobpairs: Any, bars: Any, call_n: int
        ) -> None:
            tag = "pool" if call_n <= 1 else f"poolretry{call_n}"
            for i, bar in enumerate(bars or ()):
                pair = jobpairs[i] if i < len(jobpairs or ()) else None
                r = pair[0] if pair else None
                p = pair[1] if pair and len(pair) > 1 else None
                j = _match_pool_job(scope.get("jobs"), r, p)
                sname = str(j.get("sname")) if j is not None else f"job{i}"
                stem = _unique_stem(f"att-{safe_name(sname)}-{tag}-{i}")
                prof = band_profile(bar, seed=seed, segment=call_n)
                prof["kind"] = "pool-attempt"
                prof["pool_call"] = call_n
                prof["job_index"] = i
                images = list(getattr(bar, "images", None) or ())
                if images:
                    record(
                        artifacts,
                        checkpoint_dir,
                        f"{stem}.extxyz",
                        _frames_extxyz(images),
                    )
                record(
                    artifacts,
                    checkpoint_dir,
                    f"{stem}.json",
                    json.dumps(prof, default=str),
                )

        pipeline._pool_run_nebs = _pool_hook
        unpatch.append(lambda: setattr(pipeline, "_pool_run_nebs", orig_pool_nebs))

    def restore() -> None:
        for undo in unpatch:
            undo()

    return restore
