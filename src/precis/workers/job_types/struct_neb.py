"""``struct_neb`` job_type — one CI-NEB edge between two agent-authored
endpoints, in-process on the GPU node (gr512126, slice 3,
``docs/backlog/neb-structure-builder.md``).

The ``structure`` handler's ``mode='neb'`` mint writes a ``struct_neb_runs``
row (status ``queued``) plus this job; the dispatch here loads both endpoint
designs, runs autocatpath's CI-NEB (``neb.align_endpoints`` +
``neb.neb_barrier``) in this process — mirroring ``struct_search`` (no
container, no NFS staging, the same ``has_gpaw`` capability token that names
"the node with torch/CUDA + the catalyst extra") — and writes the priced
record: barriers, the band (``struct_neb_images``), the per-check trust
records, and the TS image as a derived ``<is-slug>-ts-r<run>`` design.

**Trust is precis policy, not engine policy.** The runner seam mints the
engine-graded records (``saddle_verified``, ``detachment``) and the
slice-5 saddle check — a finite-difference Hessian over the TS image's
adsorbate atoms (ASE ``Vibrations``, ~6·n_ads force calls on the same
deadline-guarded calculator) returned as ``n_imag``/``imag_cm1``. This
module mints the rest of the spec's run-trust list itself —
``neb_convergence``, ``collapsed_band``, ``multistart`` (Reto 2026-10-25,
gr517804: a neb-tier barrier needs ≥ 2 independent same-version seeds
agreeing within 0.05 eV), the ``absurd_magnitude`` bound, and the
``frequency`` record (``n_imag == 1`` past 50i cm⁻¹, else ``marginal``;
``not-run`` when no Hessian ran — pre-slice-5 rows keep NULL columns and
read "frequency: not checked") — all in the ``autocatpath.trust``
record shape and each stamped ``engine=<autocatpath.__version__>`` so a
later engine bump renders them ``stale, rerun on current``
(:func:`precis.utils.trust_gate.is_stale`). ``trusted`` is then a pure
summary: priced AND every record ``pass``.

**Runner seam.** :data:`NEB_RUNNER` is the only autocatpath touch — swapped
for a stub in tests like ``struct_search.SEARCH_RUNNER`` (the test image
carries no ``[catalyst]`` extra). It returns the normalized outcome dict
:func:`_record_outcome` consumes. Wall time: the stub-engine dispatch is
~0.05 s end-to-end on the 42-atom fixture pair (2026-10-25, this tree's
test); the real-engine measurement lands here with the first prod edge —
spec estimate minutes–tens of minutes for one 42-atom edge × 7 images ×
2 seeds on one GPU.
"""

from __future__ import annotations

import copy
import logging
import os
import socket
import tempfile
import time
from typing import Any

import numpy as np
from psycopg.errors import UniqueViolation

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The struct_neb_runs row opened at mint — the job flips it
        # running → succeeded/failed; a queued row is how view='neb'
        # shows an in-flight edge and the inflight dedupe fires.
        "neb_run_id": {"type": "integer"},
        "is_ref_id": {"type": "integer"},
        "fs_ref_id": {"type": "integer"},
        # Design versions minted against — the drift contract (the job
        # re-reads geometry at claim time; same rule as struct_search's
        # on_version).
        "is_version": {"type": "integer"},
        "fs_version": {"type": "integer"},
        # The endpoint relax runs the band started from (audit columns).
        "is_run_id": {"type": ["integer", "null"]},
        "fs_run_id": {"type": ["integer", "null"]},
        # The IS slug, for the TS design name ``<is-slug>-ts-r<run>``.
        "is_slug": {"type": "string"},
        # 'ml' only (spec §Compute — no GPAW relax has ever completed in
        # prod); anything else fails the job honestly.
        "fidelity": {"type": "string"},
        "model": {"type": ["string", "null"]},
        # The normalized engine knob set (neb.normalize_neb_params).
        "params": {"type": "object"},
        "cache_key": {"type": "string"},
        "target_node": {"type": ["string", "null"]},
        "timeout_s": {"type": "number"},
    },
    "required": [
        "neb_run_id",
        "is_ref_id",
        "fs_ref_id",
        "is_version",
        "fs_version",
        "is_slug",
        "fidelity",
        "params",
        "cache_key",
    ],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Same token struct_relax/struct_search pin to — the node with torch/CUDA
#: + the [catalyst] extra (autocatpath is a GPU-node install; the module
#: lazy-imports it inside the runner seam so the package loads without it).
REQUIRES = frozenset({"has_gpaw"})
DESCRIPTION = (
    "One CI-NEB edge between two structure endpoints on the GPU node; "
    "sink the band, the trust records and the TS design."
)

#: Wall-clock cap handed to the runner seam — PRECIS_STRUCT_NEB_TIMEOUT_S,
#: default 7200 s (spec §Compute; the ssh_node lease is sized
#: max(7200, wall + margin) at mint).
_TIMEOUT_S_DEFAULT = 7200.0


def _timeout_s() -> float:
    raw = os.environ.get("PRECIS_STRUCT_NEB_TIMEOUT_S")
    try:
        return max(60.0, float(raw)) if raw else _TIMEOUT_S_DEFAULT
    except ValueError:
        return _TIMEOUT_S_DEFAULT


class NebUnsupported(RuntimeError):
    """autocatpath is not importable on this host — an infra failure."""


class NebTimeout(RuntimeError):
    """The runner self-aborted on the wall-clock cap (failure_class
    ``timeout``, same contract as struct_relax's _RelaxDeadline)."""


class _DeadlineCalc:
    """ASE-calculator proxy enforcing the NEB wall-clock cap inside the
    band's own energy/force calls — the ``struct_relax._RelaxDeadline``
    pattern: enforce in-process, because a lease-expiry kill would strand
    the ``struct_neb_runs`` row ``running`` (and before
    :meth:`_dispatch` wrapped the write-back, let the job be re-claimed
    for a second GPU run on the same edge).

    ASE accepts a duck-typed ``atoms.calc`` (``Atoms.calc``'s setter just
    stores the object and calls ``set_atoms`` when present), so this
    wraps the calculator :func:`_default_neb_runner`'s ``_make_calc``
    factory produces: each evaluation entry point delegates to the inner
    calculator bracketed by a clock check. ``results`` and everything
    else — ``implemented_properties``, ``parameters``,
    ``calculation_required`` — fall through to the inner object.

    The check fires only between calculator calls — a call in flight
    can't be preempted, so one long or hung call overruns the cap by
    that call's own length plus the lease's +1 h margin. A hung call
    ends as a lease-expiry zombie — dead job, live ``running`` row —
    that ``struct_neb_reap_dead`` clears (gr518100)."""

    def __init__(self, inner: Any, deadline: float, timeout_s: float) -> None:
        self._inner = inner
        self._deadline = deadline
        self._timeout_s = timeout_s

    def _check(self) -> None:
        if time.monotonic() > self._deadline:
            raise NebTimeout(f"edge exceeded the {self._timeout_s:.0f}s wall-clock cap")

    @property
    def results(self) -> dict[str, Any]:
        out: dict[str, Any] = self._inner.results
        return out

    def get_property(
        self,
        name: str,
        atoms: Any = None,
        allow_calculation: bool = True,
    ) -> Any:
        self._check()
        out = self._inner.get_property(name, atoms, allow_calculation)
        self._check()
        return out

    def get_potential_energy(
        self, atoms: Any = None, force_consistent: bool = False
    ) -> Any:
        self._check()
        out = self._inner.get_potential_energy(atoms, force_consistent)
        self._check()
        return out

    def get_forces(self, atoms: Any = None) -> Any:
        self._check()
        out = self._inner.get_forces(atoms)
        self._check()
        return out

    def get_stress(self, atoms: Any = None) -> Any:
        self._check()
        out = self._inner.get_stress(atoms)
        self._check()
        return out

    def calculate(
        self,
        atoms: Any = None,
        properties: Any = ("energy",),
        system_changes: Any = None,
    ) -> None:
        self._check()
        self._inner.calculate(atoms, properties, system_changes)
        self._check()

    def __getattr__(self, name: str) -> Any:
        # Everything non-evaluative — set_atoms, implemented_properties,
        # check_state, parameters — delegates to the wrapped calculator.
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)


def _default_neb_runner(
    *,
    is_atoms: Any,
    fs_atoms: Any,
    n_slab: int,
    model: str,
    params: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """Run the real edge: align the FS onto the IS (permutation +
    per-fragment MIC), then one serial CI-NEB via
    ``autocatpath.neb.neb_barrier``, graded by ``autocatpath.trust``.

    Lazy imports throughout — autocatpath is the ``[catalyst]`` extra on
    the GPU node and must never be imported off it. Returns the
    normalized outcome :func:`_record_outcome` consumes: the engine's
    priced scalars, the band as frac-indexed image rows, the engine-minted
    trust records, and the engine version stamp.
    """
    try:
        import autocatpath
        from autocatpath import neb as ac_neb
        from autocatpath import trust as ac_trust
    except ImportError as exc:
        raise NebUnsupported(f"autocatpath not importable: {exc}") from exc

    from precis.structure.relax import _ml_calculator

    aligned_fs = ac_neb.align_endpoints(is_atoms, fs_atoms, n_slab)

    deadline = time.monotonic() + timeout_s

    def _make_calc() -> Any:
        return _DeadlineCalc(_ml_calculator(model), deadline, timeout_s)

    bar = ac_neb.neb_barrier(
        is_atoms,
        aligned_fs,
        _make_calc,
        n_images=int(params["images"]),
        fmax=float(params["fmax"]),
        max_steps=int(params["max_steps"]),
        climb=True,
        retries=int(params["retries"]),
        n_slab=n_slab,
        tether=bool(params.get("tether", True)),
        optimizer=str(params.get("optimizer", "bfgs")),
        path_seeds=int(params.get("path_seeds", 1)),
    )

    engine = getattr(autocatpath, "__version__", None)
    records: list[dict[str, Any]] = []
    for check, graded in (
        ("saddle_verified", ac_trust.grade_saddle(bar, float(params["fmax"]))),
        ("detachment", ac_trust.grade_band(bar, n_slab)),
    ):
        if graded is None:
            continue
        verdict, severity, ev = graded
        records.append(
            ac_trust.record(check, verdict, severity, seed=0, step=None, evidence=ev)
        )

    images = []
    for idx, img in enumerate(bar.images or []):
        images.append(
            {
                "idx": idx,
                "energy_ev": (
                    float(bar.images_energy[idx])
                    if bar.images_energy and idx < len(bar.images_energy)
                    else None
                ),
                "max_force": None,
                "positions": {
                    "frac": [
                        list(map(float, row)) for row in img.get_scaled_positions()
                    ],
                    "lattice": [list(map(float, row)) for row in img.cell],
                },
            }
        )

    n_imag, imag_cm1, freq_skip = _ts_frequencies(bar, n_slab, _make_calc)

    return {
        "converged": bool(bar.converged),
        "collapsed_images": int(bar.collapsed_images or 0),
        "desorbed_images": int(bar.desorbed_images or 0),
        "e_reactant": bar.e_reactant,
        "e_product": bar.e_product,
        "e_ts": bar.e_ts,
        "ts_index": bar.ts_index,
        "ts_fmax": bar.ts_fmax,
        "images": images,
        "multistart": bar.multistart,
        "records": records,
        "n_imag": n_imag,
        "imag_cm1": imag_cm1,
        "freq_skip_reason": freq_skip,
        "engine": engine,
        "wall_s": None,
    }


#: The slice-5 saddle check counts only modes past this magnitude — the
#: finite-difference noise floor, not a chemistry bound (spec §Validation:
#: "exactly one imaginary mode, above 50i cm⁻¹").
_IMAG_CUTOFF_CM1 = 50.0


def _count_imaginary(freqs: Any) -> tuple[int, float | None]:
    """``(n_imag, imag_cm1)`` off an ASE frequency array (cm⁻¹).

    Imaginary modes surface two ways depending on the ASE version — a
    complex ``1j·f`` component or a negative real — and either counts once
    past :data:`_IMAG_CUTOFF_CM1`. ``imag_cm1`` is the largest imaginary
    magnitude, stored NEGATIVE (ASE's own imaginary-mode convention)."""
    f = np.asarray(freqs, dtype=complex).ravel()
    mag = np.maximum(np.abs(f.imag), np.abs(np.minimum(f.real, 0.0)))
    imaginary = mag[mag > _IMAG_CUTOFF_CM1]
    if len(imaginary) == 0:
        return 0, None
    return len(imaginary), float(-np.max(imaginary))


def _ts_frequencies(
    bar: Any, n_slab: int, make_calc: Any
) -> tuple[int | None, float | None, str | None]:
    """The slice-5 saddle check: a finite-difference Hessian over the
    ADSORBATE atoms of the TS image (ASE ``Vibrations`` with ``indices`` —
    ~6·n_ads force calls; the slab is held fixed the way the band tethered
    it). Returns ``(n_imag, imag_cm1, skip_reason)`` — ``(None, None,
    None)`` when there is no TS image to check or the frequency pass
    itself fails, and ``(None, None, "freq_check_budget_exhausted")`` on a
    deadline overrun: the band's calculator is deadline-wrapped, so a
    :class:`NebTimeout` here means the edge's wall-clock cap was spent
    mid-Hessian — the record must say the check never got its budget,
    which must not lose the already-priced band to a diagnostic.

    The TS image's own calculator is reused when the band left it
    attached — it is already the deadline-wrapped calculator the edge
    ran on, and a fresh ``make_calc()`` would re-load the ML model for
    a check whose remaining budget may not cover it."""
    ts_index = getattr(bar, "ts_index", None)
    images = getattr(bar, "images", None) or []
    if ts_index is None or not (0 <= int(ts_index) < len(images)):
        return None, None, None
    try:
        from ase.vibrations import Vibrations

        img = images[int(ts_index)]
        ts = img.copy()
        ts.calc = getattr(img, "calc", None) or make_calc()
        indices = list(range(n_slab, len(ts)))  # adsorbate atoms only
        if not indices:
            return None, None, None
        with tempfile.TemporaryDirectory(prefix="precis-neb-vib-") as tmp:
            vib = Vibrations(ts, indices=indices, name=f"{tmp}/vib")
            vib.run()
            n_imag, imag_cm1 = _count_imaginary(vib.get_frequencies())
            return n_imag, imag_cm1, None
    except NebTimeout:
        return None, None, "freq_check_budget_exhausted"
    except Exception:
        log.warning("struct_neb: TS frequency check failed", exc_info=True)
        return None, None, None


#: Swapped for a stub in tests, mirroring ``struct_search.SEARCH_RUNNER``.
NEB_RUNNER = _default_neb_runner


def _rec(
    check: str,
    verdict: str,
    severity: str,
    evidence: dict[str, Any],
    *,
    engine: str | None,
) -> dict[str, Any]:
    """One precis-side trust record in the ``autocatpath.trust`` shape
    (check/verdict/severity/seed/step/evidence/id) + the ``engine`` stamp
    the same-version staleness rule reads."""
    rec: dict[str, Any] = {
        "id": f"precis-{check}",
        "check": check,
        "verdict": verdict,
        "severity": severity,
        "seed": 0,
        "step": None,
        "evidence": evidence,
    }
    if engine:
        rec["engine"] = engine
    return rec


def _precis_records(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """The run-trust checks that are precis policy (spec §Validation's
    run list minus the engine-graded saddle/band rows): convergence, the
    collapsed band, the ≥2-same-version-seeds rule (gr517804) and the
    absurd-magnitude bound."""
    from precis.quest.frontier import _BARRIER_ABSURD_EV

    engine = outcome.get("engine")
    records: list[dict[str, Any]] = []
    converged = bool(outcome.get("converged"))
    records.append(
        _rec(
            "neb_convergence",
            "pass" if converged else "fail",
            "fatal",
            {"converged": converged},
            engine=engine,
        )
    )
    collapsed = int(outcome.get("collapsed_images") or 0)
    records.append(
        _rec(
            "collapsed_band",
            "pass" if collapsed == 0 else "fail",
            "fatal" if collapsed else "warn",
            {
                "collapsed_images": collapsed,
                "desorbed_images": int(outcome.get("desorbed_images") or 0),
                "reason": "a collapsed band has no transition state — not priced",
            },
            engine=engine,
        )
    )
    ms = outcome.get("multistart") or {}
    n_valid = int(ms.get("n_valid") or 0)
    n_at_min = int(ms.get("n_at_min") or 0)
    seeds_ok = n_valid >= 2 and n_at_min >= 2
    evidence: dict[str, Any] = {
        "n_valid": n_valid,
        "n_at_min": n_at_min,
        "tol_eV": 0.05,
    }
    if not seeds_ok:
        evidence["reason"] = (
            "fewer than 2 independent same-version seeds agree — untrusted (gr517804)"
        )
    records.append(
        _rec(
            "multistart",
            "pass" if seeds_ok else "marginal",
            "warn",
            evidence,
            engine=engine,
        )
    )
    e_ts, e_r = outcome.get("e_ts"), outcome.get("e_reactant")
    ea = (float(e_ts) - float(e_r)) if e_ts is not None and e_r is not None else None
    absurd = ea is not None and abs(ea) > float(_BARRIER_ABSURD_EV)
    records.append(
        _rec(
            "absurd_magnitude",
            "fail" if absurd else ("pass" if ea is not None else "not-run"),
            "warn",
            {"barrier_eV": ea, "bound_eV": float(_BARRIER_ABSURD_EV)},
            engine=engine,
        )
    )
    # Slice 5: the TS-image saddle check. Exactly one imaginary mode past
    # the noise floor proves a first-order saddle — a minimum (n_imag=0)
    # or a higher-order saddle (n_imag≥2) keeps the barrier but never
    # trusts it; no frequencies at all is not-run (the spec's
    # "frequency: not checked" read).
    n_imag = outcome.get("n_imag")
    freq_ev: dict[str, Any] = {
        "n_imag": n_imag,
        "imag_cm1": outcome.get("imag_cm1"),
        "cutoff_cm1": _IMAG_CUTOFF_CM1,
    }
    if n_imag is None:
        freq_verdict = "not-run"
        freq_ev["reason"] = outcome.get("freq_skip_reason") or (
            "no TS-image frequencies — the Hessian did not run"
        )
    elif int(n_imag) == 1:
        freq_verdict = "pass"
    else:
        freq_verdict = "marginal"
        freq_ev["reason"] = f"n_imag={int(n_imag)}"
    records.append(_rec("frequency", freq_verdict, "warn", freq_ev, engine=engine))
    return records


#: The run-trust checks a priced band must PASS to be ``trusted`` —
#: presence, not just no-fails (gr512126 review #3): the engine grader may
#: return ``None`` for ``saddle_verified``/``detachment``, and a check that
#: emitted no record is ``not-run``, never ``pass`` (trust_gate's rule).
#: ``absurd_magnitude`` isn't listed — it always emits a record and any
#: ``fail`` already blocks via the no-fails clause. ``frequency`` is:
#: slice 5 makes a first-order-saddle proof a trust requirement, so a
#: ``marginal`` (n_imag≠1) or ``not-run`` (no Hessian) record blocks via
#: the presence rule — old runs predate the check and keep their stored
#: verdict, the column staying NULL.
_NEB_REQUIRED_CHECKS: tuple[str, ...] = (
    "neb_convergence",
    "collapsed_band",
    "multistart",
    "saddle_verified",
    "detachment",
    "frequency",
)


def _priced(outcome: dict[str, Any]) -> bool:
    """The band produced a transition state worth recording — the inverse
    of the spec's "not priced" failure (collapsed band or no TS)."""
    return (
        int(outcome.get("collapsed_images") or 0) == 0
        and outcome.get("e_ts") is not None
    )


def _untrusted_digest(records: list[dict[str, Any]]) -> str:
    """Why the gate refused, for the job summary and the harvest warnings
    (gr522897): every non-``pass`` record as ``check verdict — measured
    (vs tol) — where``, plus each REQUIRED check the engine never graded
    as ``not-run`` — a silent check blocks trust by the presence rule, so
    it must be named, not left implicit."""
    from precis.utils.trust_gate import record_line

    bits = [
        record_line(r)
        for r in records
        if isinstance(r, dict) and r.get("verdict") not in (None, "pass")
    ]
    seen = {r.get("check") for r in records if isinstance(r, dict)}
    for check in _NEB_REQUIRED_CHECKS:
        if check not in seen:
            bits.append(f"{check} not-run — the check emitted no record")
    return "; ".join(bits)


def _dispatch(ctx: Any, _spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job. ``ctx``
    is a :class:`~precis.workers.executors._context.DispatchContext`."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        run_id = int(params["neb_run_id"])
        is_ref_id = int(params["is_ref_id"])
        fs_ref_id = int(params["fs_ref_id"])
        is_version = int(params["is_version"])
        fs_version = int(params["fs_version"])
        is_slug = str(params["is_slug"])
        fidelity = str(params["fidelity"])
        engine_params = dict(params["params"])
        cache_key = str(params["cache_key"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(
            f"struct_neb: malformed params ({exc})", failure_class="infra"
        )
        return
    model = params.get("model") or None
    timeout_s = float(params.get("timeout_s") or _timeout_s())

    def _fail(reason: str, failure_class: str) -> None:
        """Terminalize both rows honestly — the run row is what
        ``view='neb'`` renders, the job row is what the requester polls."""
        ctx.store.struct_neb_update_run(run_id, status="failed")
        ctx.record_failure(reason, failure_class=failure_class)

    if fidelity != "ml":
        _fail(
            f"struct_neb: no backend for fidelity {fidelity!r} — the rung is "
            "'ml' only (spec §Compute: no GPAW relax has ever completed in "
            "prod). Refusing to run a different rung than was requested",
            failure_class="infra",
        )
        return

    is_ref = ctx.store.get_ref(kind="structure", id=is_ref_id)
    fs_ref = ctx.store.get_ref(kind="structure", id=fs_ref_id)
    if is_ref is None or fs_ref is None:
        _fail(
            "struct_neb: an endpoint ref no longer resolves to a live "
            f"structure (is={is_ref_id}, fs={fs_ref_id})",
            failure_class="input",
        )
        return
    live_is = int(ctx.store.structure_version(is_ref_id))
    live_fs = int(ctx.store.structure_version(fs_ref_id))
    if live_is != is_version or live_fs != fs_version:
        _fail(
            f"struct_neb: endpoint drift — is at version {live_is} (minted "
            f"{is_version}), fs at {live_fs} (minted {fs_version}); "
            "re-dispatch against the current geometries",
            failure_class="input",
        )
        return

    is_scene, _ = ctx.store.structure_load(is_ref_id, version=is_version)
    fs_scene, _ = ctx.store.structure_load(fs_ref_id, version=fs_version)

    from precis.structure.neb import neb_atoms, slab_first_order

    order, n_slab = slab_first_order(is_scene)
    try:
        is_atoms = neb_atoms(is_scene, order)
        fs_atoms = neb_atoms(fs_scene, order)
    except Exception as exc:  # pragma: no cover - defensive
        _fail(f"struct_neb: endpoint → ASE Atoms failed: {exc}", "infra")
        return

    if model is None:
        from precis.structure.relax import route_ml_model

        model = route_ml_model(is_scene)

    ctx.store.struct_neb_update_run(run_id, status="running")
    host = os.environ.get("PRECIS_NODE") or socket.gethostname()
    ctx.append_chunk(
        "job_event",
        f"neb[ml] in-process on {host}: model={model} "
        f"images={engine_params.get('images')} "
        f"seeds={engine_params.get('path_seeds')} "
        f"fmax={engine_params.get('fmax')} "
        f"steps={engine_params.get('max_steps')} "
        f"n_slab={n_slab} run={run_id} cap={timeout_s:.0f}s",
    )

    t0 = time.monotonic()
    try:
        outcome = NEB_RUNNER(
            is_atoms=is_atoms,
            fs_atoms=fs_atoms,
            n_slab=n_slab,
            model=model,
            params=engine_params,
            timeout_s=timeout_s,
        )
    except NebTimeout as exc:
        _fail(
            f"struct_neb: edge exceeded the {timeout_s:.0f}s wall-clock cap "
            f"({exc}) — raise PRECIS_STRUCT_NEB_TIMEOUT_S, lower max_steps, "
            "or fewer images",
            "timeout",
        )
        return
    except NebUnsupported as exc:
        _fail(
            f"struct_neb: no backend on this host ({exc}) — the job was "
            "routed here by the claim gate but autocatpath isn't installed; "
            "install the [catalyst] extra on the node",
            "infra",
        )
        return
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("struct_neb: NEB_RUNNER raised", exc_info=True)
        _fail(f"struct_neb: edge failed: {exc}", "infra")
        return

    # The engine's retry loop can swallow the per-attempt NebTimeout and
    # return an unpriced band — a burned budget then records as "not
    # priced" (non-convergence). An unpriced outcome after a full-cap
    # wall-clock burn is a timeout, not a convergence verdict (gr518100).
    if not _priced(outcome) and time.monotonic() - t0 >= timeout_s:
        _fail(
            f"struct_neb: edge burned the {timeout_s:.0f}s wall-clock cap "
            "without pricing (the engine's retry loop swallowed the "
            "timeout) — raise PRECIS_STRUCT_NEB_TIMEOUT_S, lower "
            "max_steps, or fewer images",
            "timeout",
        )
        return

    try:
        _record_outcome(
            ctx,
            outcome,
            run_id=run_id,
            is_ref_id=is_ref_id,
            is_slug=is_slug,
            is_scene=is_scene,
            order=order,
            cache_key=cache_key,
            model=model,
        )
    except UniqueViolation as exc:
        # Two runs of one cache_key both priced — the partial unique index
        # struct_neb_runs_key_succeeded refused the second. The edge is
        # already answered; this row is the duplicate and fails honestly.
        _fail(
            "struct_neb: an identical edge already has a succeeded run "
            f"({exc.diag.constraint_name or exc}) — this row fails as the "
            "duplicate; the earlier run is the answer",
            "infra",
        )
    except Exception as exc:
        # Any write-back fault (structure_save, add_link, trust record)
        # after status=running must still terminalize the row — else the
        # edge lies 'running' forever (the inflight join mitigates reads,
        # but the row itself must tell the truth).
        log.warning("struct_neb: outcome write-back failed", exc_info=True)
        _fail(f"struct_neb: outcome write-back failed: {exc}", "infra")


def _record_outcome(
    ctx: Any,
    outcome: dict[str, Any],
    *,
    run_id: int,
    is_ref_id: int,
    is_slug: str,
    is_scene: Any,
    order: list[str],
    cache_key: str,
    model: str | None,
) -> None:
    """Sink a finished edge — the one write-back both a real and a stubbed
    :data:`NEB_RUNNER` land on. A collapsed band (or one that never found
    a TS) stores NULL barriers and fails "not priced"; a priced band
    stores the scalars, the images, every trust record, and the TS
    design."""
    records = list(outcome.get("records") or [])
    engine = outcome.get("engine")
    for rec in records:
        if engine and "engine" not in rec:
            rec["engine"] = engine
    records += _precis_records(outcome)

    priced = _priced(outcome)
    e_r, e_p, e_ts = (
        outcome.get("e_reactant"),
        outcome.get("e_product"),
        outcome.get("e_ts"),
    )
    barrier_fwd = (
        float(e_ts) - float(e_r)
        if priced and e_ts is not None and e_r is not None
        else None
    )
    barrier_rev = (
        float(e_ts) - float(e_p)
        if priced and e_ts is not None and e_p is not None
        else None
    )
    delta_e = (
        (float(e_p) - float(e_r))
        if priced and e_p is not None and e_r is not None
        else None
    )

    images = list(outcome.get("images") or [])
    if images:
        ctx.store.struct_neb_record_images(run_id, images)

    # Trust is a fixed-set pass requirement, not just "everything present
    # passed": a check the engine never graded emitted no record and must
    # read not-run — a band with no saddle record is NOT trusted.
    passed = {r.get("check") for r in records if r.get("verdict") == "pass"}
    trusted = (
        priced
        and not any(r.get("verdict") == "fail" for r in records)
        and all(check in passed for check in _NEB_REQUIRED_CHECKS)
    )

    ts_ref_id: int | None = None
    ts_index = outcome.get("ts_index")
    if priced and ts_index is not None and 0 <= int(ts_index) < len(images):
        ts_ref_id = _mint_ts_design(
            ctx,
            is_scene,
            is_ref_id,
            is_slug,
            order,
            images[int(ts_index)],
            run_id,
            barrier_fwd,
            trusted,
        )

    ctx.store.struct_neb_update_run(
        run_id,
        status="succeeded" if priced else "failed",
        converged=bool(outcome.get("converged")),
        barrier_fwd_ev=barrier_fwd,
        barrier_rev_ev=barrier_rev,
        delta_e_ev=delta_e,
        ts_index=int(ts_index) if priced and ts_index is not None else None,
        ts_fmax=outcome.get("ts_fmax") if priced else None,
        n_imag=(
            int(outcome["n_imag"])
            if priced and outcome.get("n_imag") is not None
            else None
        ),
        imag_cm1=outcome.get("imag_cm1") if priced else None,
        ts_ref_id=ts_ref_id,
        trust=records,
        trusted=trusted,
        multistart=outcome.get("multistart"),
        wall_s=outcome.get("wall_s"),
    )

    ctx.set_meta(
        struct_neb_run_id=run_id,
        cache_key=cache_key,
        barrier_fwd_ev=barrier_fwd,
        trusted=trusted,
        ts_ref_id=ts_ref_id,
    )
    if not priced:
        collapsed = int(outcome.get("collapsed_images") or 0)
        why = (
            f"band collapsed ({collapsed} collapsed images)"
            if collapsed
            else "no transition state found"
        )
        gate = _untrusted_digest(records)
        ctx.append_chunk(
            "job_summary",
            f"neb[ml] run {run_id}: {why} — not priced (NULL barriers)"
            + (f"; gate: {gate}" if gate else "")
            + "; try a denser band (more images), more seeds, or fix the "
            "endpoint geometry",
        )
        ctx.record_failure(
            f"struct_neb: edge not priced — {why}",
            failure_class="non-convergence",
        )
        return
    ea_txt = f"{barrier_fwd:.3f}" if barrier_fwd is not None else "n/a"
    summary = f"neb[ml] run {run_id}: E_a fwd {ea_txt} eV"
    if barrier_rev is not None:
        summary += f" rev {barrier_rev:.3f} eV"
    if delta_e is not None:
        summary += f" ΔE {delta_e:.3f} eV"
    summary += f" · {'trusted' if trusted else 'untrusted'}"
    if ts_ref_id:
        summary += f" · TS → st{ts_ref_id}"
    if priced and not trusted:
        # gr522897: a refused gate must name each failing check, its
        # measured value, tolerance and which image/seed — a bare
        # "untrusted" is why qu164903 could not tell a real endpoint
        # disagreement from tolerance noise.
        digest = _untrusted_digest(records)
        if digest:
            summary += f" · untrusted: {digest}"
    ctx.append_chunk("job_summary", summary)
    ctx.set_status("succeeded")


def _mint_ts_design(
    ctx: Any,
    is_scene: Any,
    is_ref_id: int,
    is_slug: str,
    order: list[str],
    ts_image: dict[str, Any],
    run_id: int,
    barrier_fwd: float | None,
    trusted: bool,
) -> int | None:
    """The TS image as a citable design: ``<is-slug>-ts-r<run>``, labels
    preserved (the IS scene with the TS image's frac rows written back by
    band order), linked ``derived-from`` the IS."""
    from precis.structure.card import card_text

    positions = (ts_image.get("positions") or {}).get("frac") or []
    if len(positions) != len(order):
        ctx.append_chunk(
            "job_event",
            "warn: TS image position/label count mismatch — run row keeps "
            "the scalars but no TS design is minted",
        )
        return None
    ts_scene = copy.deepcopy(is_scene)
    for i, label in enumerate(order):
        ts_scene.atoms[label].frac = ts_scene.cell.wrap(
            np.asarray(positions[i], dtype=float)
        )
    slug = f"{is_slug}-ts-r{run_id}"
    ea_txt = f"{barrier_fwd:.3f} eV" if barrier_fwd is not None else "unpriced"
    title = f"{is_slug} TS (neb run {run_id})"
    description = (
        f"NEB transition-state design from run {run_id}: E_a {ea_txt}, "
        f"{'trusted' if trusted else 'untrusted'} — cite this handle"
    )
    ref, _created = ctx.store.structure_save(
        slug=slug,
        title=title,
        scene=ts_scene,
        version=1,
        card_text=card_text(title, ts_scene, description),
        description=description,
    )
    ctx.store.add_link(
        src_ref_id=ref.id,
        dst_ref_id=is_ref_id,
        relation="derived-from",
        set_by="system",
    )
    return int(ref.id)


SPEC = JobTypeSpec(
    name="struct_neb",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "NEB_RUNNER",
    "SPEC",
    "NebTimeout",
    "NebUnsupported",
    "load",
]
