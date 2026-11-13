"""``struct_md`` job_type — one NVT molecular-dynamics run on a held or
free structure, in-process on the GPU node (gr606465).

The ``structure`` handler's ``mode='md'`` mint writes a ``struct_md_runs``
row (status ``queued``) plus this job; the dispatch here loads the design
at the minted version, runs an ASE Langevin NVT trajectory on the rung's
calculator in this process — mirroring ``struct_neb`` (no container, no
NFS staging, the same ``has_gpaw`` capability token that names "the node
with torch/CUDA + the MLIP extras") — and writes the priced record onto
the run row: the extXYZ trajectory, the MSD and RDF summaries, and the
sampled E_pot/T timeseries.

**Rungs (gr606465).** ``ml`` = MACE-MP-0 / CHGNet via
:func:`precis.structure.relax._ml_calculator` — the same backend the
in-process ``struct_relax`` rung and the ``struct_neb`` band ride.
``emt`` = ASE EMT, the always-on rung of the relax ladder: it needs no
GPU and no extra, so it is the honest smoke rung for an MD setup (and
the one the acceptance run — a 1 ps NVT on a held slab — exercises
end-to-end in tests). A rung outside the pair fails the job honestly,
never a different physics than was asked for (gr346449's rule).

**Runner seam.** :data:`MD_RUNNER` is the only calculator touch — swapped
for a stub in tests like ``struct_neb.NEB_RUNNER``. It returns the
normalized outcome dict :func:`_record_outcome` consumes
(:func:`precis.structure.md.run_nvt`'s shape).
"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any

from psycopg.errors import UniqueViolation

from precis.structure.md import MdTimeout, mobile_indices, run_nvt
from precis.structure.relax import RelaxUnsupported
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The struct_md_runs row opened at mint — the job flips it
        # running → succeeded/failed; a queued row is how view='md'
        # shows an in-flight run and the inflight dedupe fires.
        "md_run_id": {"type": "integer"},
        "structure_ref_id": {"type": "integer"},
        # Design version minted against — the drift contract (the job
        # re-reads geometry at claim time; same rule as struct_neb's
        # is_version/fs_version).
        "on_version": {"type": "integer"},
        "is_slug": {"type": "string"},
        # 'ml' (MACE/CHGNet) | 'emt' (the always-on smoke rung); anything
        # else fails the job honestly.
        "fidelity": {"type": "string"},
        "model": {"type": ["string", "null"]},
        # The normalized engine knob set (md.normalize_md_params).
        "params": {"type": "object"},
        "cache_key": {"type": "string"},
        "target_node": {"type": ["string", "null"]},
        "timeout_s": {"type": "number"},
    },
    "required": [
        "md_run_id",
        "structure_ref_id",
        "on_version",
        "is_slug",
        "fidelity",
        "params",
        "cache_key",
    ],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Same token struct_relax/struct_neb pin to — the node with torch/CUDA
#: + the [dft-ml] extra (the MLIP lazy-imports inside the runner seam so
#: the module loads without it).
REQUIRES = frozenset({"has_gpaw"})
DESCRIPTION = (
    "One NVT Langevin MD run on a held or free structure on the GPU node; "
    "sink the extXYZ trajectory and the MSD/RDF summaries."
)

#: Rungs this job computes. ``ml`` is the proposal's MACE-MP-0/CHGNet
#: ML-MD rung; ``emt`` is the always-on ASE EMT smoke rung (the relax
#: ladder's owned tier — no GPU, no extra) that a membrane/interface MD
#: setup gets validated on before GPU spend.
_MD_FIDELITIES = frozenset({"ml", "emt"})

#: Wall-clock cap handed to the runner seam — PRECIS_STRUCT_MD_TIMEOUT_S,
#: default 3600 s (a 1 ps slab NVT is minutes on the GPU node; the
#: ssh_node lease is sized wall + margin at mint).
_TIMEOUT_S_DEFAULT = 3600.0


def _timeout_s() -> float:
    raw = os.environ.get("PRECIS_STRUCT_MD_TIMEOUT_S")
    try:
        return max(60.0, float(raw)) if raw else _TIMEOUT_S_DEFAULT
    except ValueError:
        return _TIMEOUT_S_DEFAULT


class MdUnsupported(RuntimeError):
    """The rung's backend is not available on this host — an infra
    failure (missing wheel, or an element set outside EMT's closed
    coverage)."""


def _default_md_runner(
    *,
    atoms: Any,
    fidelity: str,
    model: str | None,
    params: dict[str, Any],
    timeout_s: float,
    device: str | None = None,
) -> dict[str, Any]:
    """Run the real trajectory: attach the rung's calculator, then one
    Langevin NVT via :func:`precis.structure.md.run_nvt`.

    Lazy imports throughout — the MLIP is the ``[dft-ml]`` extra on the
    GPU node and must never be imported off it; EMT is plain ASE. The
    fidelity gate upstream already refused anything outside
    :data:`_MD_FIDELITIES`. Returns :func:`run_nvt`'s normalized outcome.

    ``device`` is the footprint-admission card binding (``cuda:<i>``):
    it reaches the MLIP calculator's constructor, whose cache keys on
    it — a card-bound run can never land on a calculator parked on
    another card (td655172 review, take two). ``None`` keeps the
    backend's own default. EMT is CPU-only and ignores it.
    """
    if fidelity == "emt":
        from ase.calculators.emt import EMT

        from precis.structure.relax import EMT_ELEMENTS

        outside = set(atoms.get_chemical_symbols()) - EMT_ELEMENTS
        if outside:
            raise MdUnsupported(
                f"rung 'emt' has no parameters for {sorted(outside)} — "
                "EMT's coverage is closed (use fidelity='ml')"
            )
        atoms.calc = EMT()
    else:
        from precis.structure.relax import _ml_calculator

        atoms.calc = _ml_calculator(model or "mace_mp", device=device)
    return run_nvt(atoms, params, timeout_s=timeout_s)


#: Swapped for a stub in tests, mirroring ``struct_neb.NEB_RUNNER``.
MD_RUNNER = _default_md_runner


def _dispatch(ctx: Any, _spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job. ``ctx``
    is a :class:`~precis.workers.executors._context.DispatchContext`."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        run_id = int(params["md_run_id"])
        structure_ref_id = int(params["structure_ref_id"])
        on_version = int(params["on_version"])
        fidelity = str(params["fidelity"])
        engine_params = dict(params["params"])
        cache_key = str(params["cache_key"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(
            f"struct_md: malformed params ({exc})", failure_class="infra"
        )
        return
    model = params.get("model") or None
    timeout_s = float(params.get("timeout_s") or _timeout_s())

    def _fail(reason: str, failure_class: str) -> None:
        """Terminalize both rows honestly — the run row is what
        ``view='md'`` renders, the job row is what the requester polls."""
        ctx.store.struct_md_update_run(run_id, status="failed")
        ctx.record_failure(reason, failure_class=failure_class)

    if fidelity not in _MD_FIDELITIES:
        _fail(
            f"struct_md: no backend for fidelity {fidelity!r} — the rung "
            f"set is {sorted(_MD_FIDELITIES)}. Refusing to run a different "
            "rung than was requested",
            failure_class="infra",
        )
        return

    ref = ctx.store.get_ref(kind="structure", id=structure_ref_id)
    if ref is None:
        _fail(
            "struct_md: the design ref no longer resolves to a live "
            f"structure (st{structure_ref_id})",
            failure_class="input",
        )
        return
    live = int(ctx.store.structure_version(structure_ref_id))
    if live != on_version:
        _fail(
            f"struct_md: design drift — at version {live} (minted "
            f"{on_version}); re-dispatch against the current geometry",
            failure_class="input",
        )
        return

    scene, _ = ctx.store.structure_load(structure_ref_id, version=on_version)
    if not scene.atoms:
        _fail(
            "struct_md: the design has no atoms — nothing to propagate",
            failure_class="input",
        )
        return

    from precis.structure.neb import neb_atoms, slab_first_order

    order, _n_slab = slab_first_order(scene)
    try:
        atoms = neb_atoms(scene, order)
    except Exception as exc:  # pragma: no cover - defensive
        _fail(f"struct_md: scene → ASE Atoms failed: {exc}", "infra")
        return
    mobile = mobile_indices(scene, order)
    if not mobile:
        _fail(
            "struct_md: every atom is held fixed — an all-held trajectory "
            "is a tautology, not a run",
            failure_class="input",
        )
        return
    if model is None:
        if fidelity == "emt":
            model = "emt"
        else:
            from precis.structure.relax import route_ml_model

            model = route_ml_model(scene)

    ctx.store.struct_md_update_run(run_id, status="running")
    host = os.environ.get("PRECIS_NODE") or socket.gethostname()
    ctx.append_chunk(
        "job_event",
        f"md[{fidelity}] in-process on {host}: model={model} "
        f"{engine_params.get('steps')} steps × "
        f"{engine_params.get('timestep_fs')} fs @ "
        f"{engine_params.get('temperature_k')} K · "
        f"{len(mobile)}/{len(order)} mobile · run={run_id} "
        f"cap={timeout_s:.0f}s",
    )

    from precis.workers.footprint_admission import bound_device

    t0 = time.monotonic()
    try:
        outcome = MD_RUNNER(
            atoms=atoms,
            fidelity=fidelity,
            model=model,
            params=engine_params,
            timeout_s=timeout_s,
            device=bound_device(ctx.meta),
        )
    except MdTimeout as exc:
        _fail(
            f"struct_md: run exceeded the {timeout_s:.0f}s wall-clock cap "
            f"({exc}) — raise PRECIS_STRUCT_MD_TIMEOUT_S, lower steps, or "
            "coarsen traj_every",
            "timeout",
        )
        return
    except (MdUnsupported, RelaxUnsupported) as exc:
        _fail(
            f"struct_md: no backend on this host ({exc}) — the job was "
            "routed here by the claim gate but the rung's backend is "
            "missing; install the extra that carries it on the node "
            "([dft-ml] for 'ml')",
            "infra",
        )
        return
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("struct_md: MD_RUNNER raised", exc_info=True)
        _fail(f"struct_md: run failed: {exc}", "infra")
        return

    # A runner that returns nothing on a full-cap wall-clock burn is a
    # timeout, not a verdict (gr518100's rule).
    if not outcome.get("n_steps") and time.monotonic() - t0 >= timeout_s:
        _fail(
            f"struct_md: run burned the {timeout_s:.0f}s wall-clock cap "
            "without producing a frame — raise PRECIS_STRUCT_MD_TIMEOUT_S "
            "or lower steps",
            "timeout",
        )
        return

    try:
        _record_outcome(
            ctx,
            outcome,
            run_id=run_id,
            cache_key=cache_key,
            fidelity=fidelity,
        )
    except UniqueViolation:
        # Two runs of one cache_key both finished — the partial unique
        # index struct_md_runs_key_succeeded refused the second. The
        # request is already answered; this row fails as the duplicate.
        _fail(
            "struct_md: an identical request already has a succeeded run "
            "— this row fails as the duplicate; the earlier run is the "
            "answer",
            "infra",
        )
    except Exception as exc:
        # A write-back fault after status=running must still terminalize
        # the row — else it lies 'running' forever.
        log.warning("struct_md: outcome write-back failed", exc_info=True)
        _fail(f"struct_md: outcome write-back failed: {exc}", "infra")


def _record_outcome(
    ctx: Any,
    outcome: dict[str, Any],
    *,
    run_id: int,
    cache_key: str,
    fidelity: str,
) -> None:
    """Sink a finished trajectory — the one write-back both a real and a
    stubbed :data:`MD_RUNNER` land on: the extXYZ frames, the MSD/RDF
    summaries and the sampled thermodynamic scalars onto the run row."""
    msd = dict(outcome.get("msd") or {})
    ctx.store.struct_md_update_run(
        run_id,
        status="succeeded",
        n_steps=int(outcome["n_steps"]),
        n_frames=int(outcome["n_frames"]),
        temperature_k=outcome.get("temperature_k"),
        temp_mean_k=outcome.get("temp_mean_k"),
        e_pot_mean_ev=outcome.get("e_pot_mean_ev"),
        e_pot_std_ev=outcome.get("e_pot_std_ev"),
        msd=msd,
        rdf=dict(outcome.get("rdf") or {}),
        trajectory=outcome.get("extxyz"),
        wall_s=outcome.get("wall_s"),
    )
    ctx.set_meta(
        struct_md_run_id=run_id,
        cache_key=cache_key,
        msd_final_A2=msd.get("final_A2"),
    )
    msd_txt = f"{msd['final_A2']:.3f} Å²" if msd.get("final_A2") is not None else "n/a"
    ctx.append_chunk(
        "job_summary",
        f"md[{fidelity}] run {run_id}: {outcome['n_steps']} steps @ "
        f"{outcome.get('temperature_k')} K — {outcome['n_frames']} frames, "
        f"MSD(final) {msd_txt} over {outcome.get('n_mobile')} mobile atom(s) "
        f"· trajectory on the run row: get(kind='structure', "
        f"view='md', args={{'run': {run_id}, 'trajectory': true}})",
    )
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name="struct_md",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "MD_RUNNER",
    "SPEC",
    "MdUnsupported",
    "load",
]
