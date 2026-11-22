"""``electronic_analysis`` job_type — a DFT single point that stamps
orbital-level descriptors onto a structure (gr592999).

The screening ladder (``ml``/``xtb`` relax, autocatpath barriers) produces
energetics, never *mechanism*: the ML potential has no charge response,
so questions like "does Ta win because its d-band centre shifts?" had no
compute to answer them. This job is the descriptor pass: it stages a
structure's **stored** geometry, runs one GPAW self-consistent single
point on the DFT node inside the ``precis-dft`` container's
``gpaw-analysis`` subcommand, and writes back Bader charges, the d-band
centre/width of the dopant and surface atoms, the dopant's projected
d-DOS, and pCOHP + ICOHP(E_F) for the named bonds.

**Same lane, different cargo.** Staging, the ``precis-job-<ref>``
container-name mutex, stale-container pre-clean, GPU-card fencing, the
wall-clock cap and the kill/reset escalations are *literally*
``struct_relax``'s — imported, not re-implemented, so one code path owns
the cluster boundary. What differs is the tail: where ``_record_run``
sinks an energy envelope into the §23.16 run-cube, this job records a
``struct_runs`` row of fidelity ``'electronic'`` carrying the descriptor
payload — Bader in the ``charges`` column (the one migration 0084 left
NULL for exactly this rung), the rest under ``params.descriptors`` — and
stamps a ``meta['last_electronic']`` summary on the design so
``view='electronic'`` and the frontier's ``mechanism_evidence`` flag have
something cheap to read.

**Deliberately ``energy=None``.** A single point has a total energy, but
the frontier's "energy" measure is *the converged relax's* energy —
picking this row there would silently swap an MLIP relax number for a
single-point RPBE one (the ``guard_energy_comparable`` class of lie).
``E_tot``/``fermi`` ride the run's ``params`` for audit; the typed
``energy`` column stays NULL so ``_is_energy_run`` /
``_latest_converged_relax_run`` / the run-cube cache never mistake this
row for a measurement.

**Honest absence.** The container reports every descriptor as
``computed`` or ``unavailable`` + reason (no ``bader`` binary in the
image, an LCAO API drift, …). The host stores what computed — a run
with two descriptors present is still evidence — and the view renders
the absences with their reasons rather than hiding them. A run whose
families all came back ``unavailable``, or one describing a superseded
geometry (``on_version`` behind the design's), is *not* mechanism
evidence: the stamp records it, but the frontier flag and the view's
verdict read it as absent.

Mint: ``put(kind='structure', id=<slug>, mode='analyze', args={...})``
on the structure handler snapshots the geometry and mints this job —
the same derived-compute lane as a dispatched relax.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from precis.structure.relax_dispatch import phase
from precis.utils.container_limits import (
    KILLED_FOR_MEMORY,
    container_cap_mb,
    job_estimate_mb,
    memory_cap_killed,
)
from precis.workers.executors._common import handle_memory_kill
from precis.workers.job_types import JobTypeSpec, struct_relax

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "structure_ref_id": {"type": "integer"},
        "on_version": {"type": "integer"},
        "structure_sha": {"type": "string"},
        # The mint-time request identity (spec + dft + label map +
        # version) — stamped into last_electronic so the handler's
        # completed-run dedupe reuses only same-version evidence.
        "request_sha": {"type": "string"},
        # canonical_order(scene) — audit context for the label map.
        "order": {"type": "array", "items": {"type": "string"}},
        # Labels in POSCAR row order — the container resolves dopant /
        # surface / bond selectors against them.
        "poscar_labels": {"type": "array", "items": {"type": "string"}},
        "poscar": {"type": "string"},
        "target_node": {"type": ["string", "null"]},
        # The descriptor spec, passed through to the container verbatim:
        # {"dopant": [labels|el:X], "surface": [...],
        #  "bonds": [{"name": "Pd-N", "a": "el:Pd", "b": "el:N"}],
        #  "bond_cutoff": 1.3}
        "analysis": {"type": "object"},
        # Optional DFT knob overrides (same block gpaw-relax reads:
        # functional, kpts, smearing, h, spin_polarized).
        "dft": {"type": "object"},
    },
    "required": [
        "structure_ref_id",
        "on_version",
        "structure_sha",
        "poscar_labels",
        "poscar",
    ],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Same token struct_relax carries — "the node with the precis-dft image".
REQUIRES = frozenset({"has_gpaw"})
DESCRIPTION = (
    "DFT single point on a stored geometry; stamp Bader charges, d-band "
    "moments, dopant PDOS and bond COHPs onto the structure."
)

#: struct_runs.fidelity label for descriptor rows. Distinct from every
#: relax rung so `view='runs'` reads it as what it is, the frontier's
#: `mechanism_evidence` flag finds it, and no consumer that keys
#: ``fidelity == 'ml'`` (pair_check) mistakes it for a relax.
FIDELITY = "electronic"

#: The container subcommand this job invokes — see
#: ``src/precis_dft/_container/gpaw_analysis.py``.
_COMMAND = "gpaw-analysis"


def _validate(store: Any, params: dict[str, Any]) -> None:
    """Submit-time check (``JobTypeSpec.validate``): refuse a job whose
    structure ref doesn't exist — a queued zombie otherwise."""
    from precis.errors import BadInput

    try:
        ref_id = int(params["structure_ref_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BadInput(
            f"electronic_analysis needs params.structure_ref_id (int): {exc}",
            next="mint via put(kind='structure', id='<slug>', mode='analyze', "
            "args={'dopant': ['<label>'], 'bonds': [...]})",
        ) from exc
    if store.get_ref(kind="structure", id=ref_id) is None:
        raise BadInput(
            f"no structure ref #{ref_id} — nothing to analyse",
            next="check the handle with get(kind='structure', id='<slug>')",
        )


#: Overridable hooks (tests monkeypatch these). The runner is the cluster
#: boundary, the stager the NFS one — both default to struct_relax's
#: implementations so the container contract has exactly one definition.
RUNNER = struct_relax._default_runner
STAGER = struct_relax._default_stager


def _params_for_container(
    params: dict[str, Any], poscar_labels: list[str]
) -> dict[str, Any]:
    """The ``params.json`` the container reads: the analysis spec, the
    label map (POSCAR row order), and any DFT knob overrides."""
    return {
        "fidelity": "gpaw",
        "analysis": params.get("analysis") or {},
        "labels": poscar_labels,
        **({"dft": params["dft"]} if params.get("dft") else {}),
    }


def _descriptor_present(descriptors: dict[str, Any], key: str) -> bool:
    """True when a descriptor block has at least one computed entry —
    dband/cohp are dicts-of-entries, bader/pdos are single envelopes."""
    block = descriptors.get(key)
    if not isinstance(block, dict):
        return False
    if block.get("status") == "computed":
        return True
    return any(
        isinstance(v, dict) and v.get("status") == "computed" for v in block.values()
    )


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job. Stages
    the geometry, runs ``precis-dft-run gpaw-analysis`` on the DFT node,
    parses the result, and records the descriptor run."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        structure_ref_id = int(params["structure_ref_id"])
        on_version = int(params["on_version"])
        poscar_labels = list(params["poscar_labels"])
        poscar = str(params["poscar"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(
            f"electronic_analysis: malformed params ({exc})", failure_class="infra"
        )
        return

    node = params.get("target_node") or struct_relax._NODE
    if node is None:
        ctx.record_failure(
            "electronic_analysis: no target node — params carry no "
            "target_node and PRECIS_DFT_NODE is unset on this host (deploy "
            "renders it from topology precis_capabilities.dft)",
            failure_class="infra",
        )
        return

    # Same deterministic-name mutex as the relax lane: a leftover
    # ``precis-job-<ref>`` container collides on rc=125 forever unless
    # cleared first (struct_relax gripe 310809, defect C — the container
    # name is per-structure, shared across both job types on purpose so a
    # relax and an analysis on one design can never overlap).
    container_name = f"{struct_relax._CONTAINER_PREFIX}{structure_ref_id}"
    local = node == os.environ.get("PRECIS_NODE")
    if struct_relax._container_still_present(
        container_name,
        target=node,
        local=local,
        container_cmd=struct_relax._CONTAINER_CMD,
    ):
        log.warning(
            "electronic_analysis: pre-clean found a stale container %s on %s "
            "— force-removing before dispatch",
            container_name,
            node,
        )
        if not struct_relax.kill_container(structure_ref_id, node=node):
            ctx.record_failure(
                f"electronic_analysis: stale container {container_name} on "
                f"{node} is un-removable — operator cleanup required "
                "(docker rm -f, or a host reboot) before this job can run",
                failure_class="infra",
            )
            return

    in_dir, out_dir = STAGER(structure_ref_id)
    Path(in_dir, "POSCAR").write_text(poscar, encoding="utf-8")
    Path(in_dir, "params.json").write_text(
        json.dumps(_params_for_container(params, poscar_labels), sort_keys=True),
        encoding="utf-8",
    )

    raw_card = (ctx.meta.get("reserved") or {}).get("gpu_card")
    gpu_card = int(raw_card) if raw_card is not None else None
    argv = struct_relax.build_run_argv(
        ref_id=structure_ref_id,
        in_dir=in_dir,
        out_dir=out_dir,
        gpu_card=gpu_card,
        command=_COMMAND,
        mem_mb=job_estimate_mb(ctx.meta),
    )
    ctx.append_chunk("job_event", f"analysis[{FIDELITY}] on {node}: {' '.join(argv)}")

    timeout = struct_relax._relax_timeout_s()
    try:
        rc, output = RUNNER(
            argv, node=node, in_dir=in_dir, out_dir=out_dir, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "electronic_analysis: single point exceeded %.0fs wall-clock cap "
            "— self-aborting",
            timeout,
        )
        kill_ok = struct_relax.kill_container(structure_ref_id, node=node)
        reset_ok = struct_relax.reset_gpu(node=node)
        container_note = (
            "container force-removed"
            if kill_ok
            else f"container {container_name} NOT removed — it survives on "
            f"{node}; operator docker rm -f (or a host reboot) is required "
            "before a retry can run"
        )
        gpu_note = (
            "nvidia-smi --gpu-reset attempted (succeeded)"
            if reset_ok
            else "nvidia-smi --gpu-reset attempted (failed)"
        )
        ctx.append_chunk(
            "job_event",
            f"analysis[{FIDELITY}] on {node}: exceeded {timeout:.0f}s "
            f"wall-clock cap — self-aborted ({container_note}; {gpu_note})",
        )
        ctx.record_failure(
            f"electronic_analysis: single point on {node} exceeded the "
            f"{timeout:.0f}s wall-clock cap — self-aborted "
            f"({container_note}; {gpu_note})",
            failure_class="infra",
        )
        return
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("electronic_analysis: runner raised", exc_info=True)
        ctx.record_failure(
            f"electronic_analysis: runner failed: {exc}", failure_class="infra"
        )
        return
    ctx.append_chunk("job_event", f"container rc={rc}\n{output[-2000:]}")

    result_path = Path(out_dir) / struct_relax._RESULT_FILE
    if rc != 0 or not result_path.exists():
        killed = memory_cap_killed(rc)
        if killed:
            # gr595537/538: the --memory cap's OOM kill — stamp the
            # recognisable outcome, then requeue once with the footprint
            # estimate bumped (or, on the second kill, park for
            # memory_triage) instead of recording a failure.
            ctx.set_meta(outcome=KILLED_FOR_MEMORY)
        if killed and handle_memory_kill(
            ctx.store, ctx.ref_id, cap_mb=container_cap_mb(argv)
        ):
            return
        ctx.record_failure(
            f"electronic_analysis: container rc={rc}"
            f"{' (killed-for-memory)' if killed else ''}, no "
            f"{struct_relax._RESULT_FILE} — see the log event",
            failure_class="infra",
        )
        return
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        ctx.record_failure(
            f"electronic_analysis: malformed {struct_relax._RESULT_FILE}: {exc}",
            failure_class="infra",
        )
        return
    if not result.get("ok"):
        ctx.record_failure(
            "electronic_analysis: single point reported failure: "
            f"{result.get('error', 'unknown')}",
            failure_class="non-convergence",
        )
        return
    _record_analysis(
        ctx, result, structure_ref_id=structure_ref_id, on_version=on_version
    )


def _record_analysis(
    ctx: Any,
    result: dict[str, Any],
    *,
    structure_ref_id: int,
    on_version: int,
) -> None:
    """Sink a finished descriptor pass into ``struct_runs`` + the design's
    ``meta['last_electronic']`` stamp — the write-back counterpart of
    ``struct_relax._record_run``."""
    scalars = result.get("scalars") or {}
    descriptors = result.get("descriptors") or {}
    if "E_tot" not in scalars:
        ctx.record_failure(
            "electronic_analysis: result.json missing scalars.E_tot",
            failure_class="infra",
        )
        return

    bader = descriptors.get("bader") or {}
    charges = None
    if bader.get("status") == "computed":
        charges = {
            "bader": bader.get("charges") or bader.get("counts"),
            **({"note": bader["note"]} if bader.get("note") else {}),
        }

    run_params: dict[str, Any] = {
        # Audit scalars — deliberately NOT the typed `energy` column (see
        # the module docstring: the frontier's relax-energy channel must
        # never read this row).
        "E_tot": scalars.get("E_tot"),
        "fermi": scalars.get("fermi"),
        "descriptors": descriptors,
        "functional": result.get("functional"),
        "gpaw_version": result.get("gpaw_version"),
        "analysis": ((ctx.meta or {}).get("params") or {}).get("analysis"),
    }

    present = [
        k
        for k in ("bader", "dband", "pdos", "cohp")
        if _descriptor_present(descriptors, k)
    ]
    absent = [
        k
        for k in ("bader", "dband", "pdos", "cohp")
        if not _descriptor_present(descriptors, k)
    ]

    with phase(structure_ref_id, "record_analysis"):
        run_id = ctx.store.structure_record_run(
            structure_ref_id,
            fidelity=FIDELITY,
            on_version=on_version,
            # ``converged`` here means "the SCF + descriptor pass ran to
            # completion" — the run is usable evidence. energy stays NULL
            # on purpose (module docstring).
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=None,
            model=str(result.get("functional") or "gpaw"),
            params=run_params,
            charges=charges,
        )
    job_params = (ctx.meta or {}).get("params") or {}
    ctx.store.structure_stamp_electronic(
        structure_ref_id,
        on_version=on_version,
        summary={
            "run_id": run_id,
            "on_version": on_version,
            "structure_sha": job_params.get("structure_sha"),
            "request_sha": job_params.get("request_sha"),
            "analysis": job_params.get("analysis"),
            "dft": job_params.get("dft") or {},
            "present": present,
            "absent": absent,
            "functional": result.get("functional"),
        },
    )
    # A converged single point whose every descriptor came back
    # ``unavailable`` is a finished run, not evidence — the frontier flag
    # and the view's verdict require a computed descriptor on the current
    # geometry.
    ctx.set_meta(
        struct_run_id=run_id,
        mechanism_evidence="present" if present else "absent",
    )
    bits = ", ".join(f"{k} ✓" for k in present) or "none"
    missing = f" · unavailable: {', '.join(absent)}" if absent else ""
    verdict = (
        "'mechanism evidence' is now present for this candidate."
        if present
        else "no descriptor computed — 'mechanism evidence' stays absent."
    )
    ctx.append_chunk(
        "job_summary",
        f"electronic_analysis r{run_id}: single point at stored geometry, "
        f"E_tot={scalars['E_tot']:.4f} eV — descriptors present: {bits}"
        f"{missing}. view='electronic' shows them; {verdict}",
    )
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name="electronic_analysis",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
    validate=_validate,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "DESCRIPTION",
    "FIDELITY",
    "RUNNER",
    "SPEC",
    "STAGER",
    "load",
]
