"""``hexfold_relax`` job_type — relax an assembled hexfold block on the
compute group and store per-seam/per-mouth strain before and after
(hexfold H3, docs/backlog/hexfold-nanoreactor-tube.md). A ``precis_se``
plugin spec (``precis.job_types`` entry point), so the core never
imports the hexfold atomic pipeline it dispatches.

The ``struct_relax`` job relaxes one structure at one rung into the
run-cube; an assembled tunnel wants the pipeline of
docs/backlog/hexfold-relax-pipeline.md instead — a cheap whole-block
polish (``ml``/``emt``/``xtb``, whichever the relax ladder supports —
GFN-FF has no backend in the tree) plus GFN2 on H-capped, buffer-frozen
junction fragments — and its own write-back: the relaxed geometry
becomes the structure's next version, the strain findings land on the
ref's ``meta["generated"]["relax"]``, and every compute pass records a
``struct_runs`` row like any other relax. All of it runs through
:func:`precis_se.atomic.relax_block.relax_block`, which in turn calls
the one ladder, :func:`precis.structure.relax.relax` — same rungs, same
calculators, no second stack.

Minted like every compute-lane job: ``put(kind='job',
job_type='hexfold_relax', executor='ssh_node',
parent_id=<structure ref>, params={'structure_ref_id': N, ...})`` —
``parent_id`` is the bound structure design the job builds on (the
compute lane's owner). ``params.target_node`` pins the claim the same
way ``struct_relax`` does; unset, any ``ssh_node`` worker may claim it —
a node missing tblite (the fragment rung's backend) fails the job
``infra``, naming the extra.

The runner's outer dispatch bound and initial lease read the type's
``wall_seconds`` callable: ``_relax_timeout_s`` is configured on the
claiming host, not stamped into the job's ``params.resources``. Without
that declaration the runner could fail at 7200s while the relaxer's
internal cap was still 14400s, abandoning a live in-process compute.

The compute runs in a supervised child process, not on the dispatch
thread (gr506395): a 5105-atom relax overran its bound in the worker's
daemon thread, the job was failed, and the thread kept ~44 GB of GPU
memory and the worker's shared, non-thread-safe MACE calculator for
hours. The child enforces the cap itself (the runner's outer bound is the
cap plus a margin, so it does not fire first), polls cancel and operator
kill, and is killed with its process group — confirmed gone — before the
job turns terminal. It also gives each job its own MACE calculator; the
cost is a model load per job, small against an hours-long relax.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from precis.workers.child_compute import (
    ChildDied,
    ChildStopped,
    job_stop_reason,
    run_in_child,
)
from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types.struct_relax import _relax_timeout_s

log = logging.getLogger(__name__)

#: Supervisor cadence (seconds) for the relax child: deadline / cancel /
#: kill checks, and the SIGTERM→SIGKILL grace.
_CHILD_POLL_S = 5.0
_CHILD_GRACE_S = 30.0

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The bound structure design minted by a hexfold generate — the
        # job relaxes it and writes the strain record onto its
        # meta['generated'].
        "structure_ref_id": {"type": "integer"},
        # The design version to relax (absent ⇒ the live version).
        "on_version": {"type": "integer"},
        # Whole-block polish rung: 'ml' (default, the T1 tier) | 'emt' |
        # 'xtb' | 'geo' | 'clean' — anything the relax ladder accepts.
        "polish": {"type": "string"},
        "model": {"type": ["string", "null"]},
        # Junction-fragment rung: 'xtb' (GFN2, the relaxer of record) or
        # 'none' (polish only).
        "fragment": {"type": "string"},
        "buffer_A": {"type": "number"},
        "core_A": {"type": "number"},
        "steps": {"type": "integer"},
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["structure_ref_id"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Satisfied by EXECUTOR_PROVIDES['ssh_node'] == {'has_gpaw'} — the
#: compute-group marker, same as ``struct_relax``.
REQUIRES = frozenset({"has_gpaw"})
DESCRIPTION = (
    "Relax an assembled hexfold block (whole-block polish + GFN2 junction "
    "fragments) and store per-seam/mouth strain before/after on the scene."
)

#: Polish rungs accepted here, mirrored from relax_block.POLISH_RUNGS —
#: a rung outside the ladder is refused at submit, not on the node.
_POLISH_RUNGS = ("ml", "emt", "xtb", "geo", "clean")
_FRAGMENT_RUNGS = ("xtb", "none")


def _validate_submit(
    store: Any, *, gripe_id: Any = None, params: Any = None
) -> str | None:
    """Refuse a mint the dispatch could only fail: unknown structure, no
    persisted strain bookkeeping, or a rung the ladder doesn't carry."""
    params = params or {}
    ref_id = params.get("structure_ref_id")
    if not isinstance(ref_id, int) or isinstance(ref_id, bool):
        return (
            "hexfold_relax needs params.structure_ref_id — the ref id of "
            "the structure design bound to the hexfold block"
        )
    polish = str(params.get("polish") or "ml")
    if polish not in _POLISH_RUNGS:
        return (
            f"hexfold_relax polish must be one of {list(_POLISH_RUNGS)}; got {polish!r}"
        )
    fragment = str(params.get("fragment") or "xtb")
    if fragment not in _FRAGMENT_RUNGS:
        return (
            f"hexfold_relax fragment must be one of {list(_FRAGMENT_RUNGS)}; "
            f"got {fragment!r}"
        )
    ref = store.get_ref(kind="structure", id=ref_id)
    if ref is None:
        return f"hexfold_relax: no structure ref {ref_id}"
    generated = (ref.meta or {}).get("generated") or {}
    if not generated.get("junctions"):
        return (
            f"structure ref {ref_id} has no generated.junctions "
            "bookkeeping — hexfold_relax scores blocks minted with the "
            "H3 strain record (tunnel-reservoirs scenes, fuse/opening "
            "builds); regenerate it first"
        )
    return None


def _relax_child(
    scene: Any, generated: dict[str, Any], kwargs: dict[str, Any], cuda_requested: bool
) -> tuple[Any, Any]:
    """The compute, run in the supervised child: report the MLIP device
    (``ml`` polish only, and only when the record will pass
    ``relax_block``'s own checks, so a bad record still fails as params
    before a model loads), then relax. Returns the relaxed scene with the
    report — the child's mutation of ``scene`` does not reach the parent."""
    from precis.workers.child_compute import emit
    from precis_se.atomic.relax_block import relax_block, strain_bookkeeping

    if kwargs["polish"] == "ml" and strain_bookkeeping(generated) is not None:
        from precis.structure.relax import ml_device_check

        device = ml_device_check(
            kwargs["model"] or "mace_mp", cuda_requested=cuda_requested
        )
        if device is not None:
            emit("ml_device", device)
    report = relax_block(scene, generated, **kwargs)
    return scene, report


def _on_child_event(ctx: Any, kind: str, payload: Any) -> None:
    """Record the child's MLIP device on ``meta.ml_device``; a CPU fallback
    also lands as a ``job_event`` warning."""
    if kind != "ml_device":
        return
    ctx.set_meta(ml_device=payload)
    warning = payload.get("warning")
    if warning:
        log.warning("hexfold_relax job %s: %s", ctx.ref_id, warning)
        ctx.append_chunk("job_event", f"hexfold_relax: warning — {warning}")


def _terminalize_stopped(ctx: Any, stop: ChildStopped, cap: float) -> None:
    """Terminal state for a child the supervisor stopped. Runs after the
    kill, so the job never reads terminal while its compute still runs; an
    unverified kill is named in the reason."""
    pid = stop.handle.get("pid")
    killed = (
        f"relax process pid {pid} killed"
        if stop.handle.get("kill_verified")
        else f"relax process pid {pid} signalled but NOT confirmed gone "
        "(survived SIGKILL)"
    )
    if stop.reason == "cancel":
        ctx.append_chunk("job_event", f"hexfold_relax: cancel requested — {killed}")
        ctx.set_status("cancelled")
    elif stop.reason == "kill":
        ctx.record_failure(f"hexfold_relax: killed by operator — {killed}")
    else:
        ctx.record_failure(
            f"hexfold_relax: relax exceeded its {cap:.0f}s cap "
            "(PRECIS_DFT_RELAX_TIMEOUT_S) — " + killed,
            failure_class="timeout",
        )


def _dispatch(ctx: Any, spec: Any) -> None:
    """Load the bound structure, run the relax pipeline in a supervised
    child process, and write the relaxed geometry + strain record back.

    No container and no staging — the rungs compute on the claiming host,
    like ``struct_relax``'s in-process path, but in a child
    (:func:`precis.workers.child_compute.run_in_child`) whose pid lands on
    ``meta.compute_child``. Past the cap, on cancel or on an operator kill
    the child's process group is killed and confirmed gone before the job
    turns terminal. Backend-missing and a child that dies without a result
    record ``infra`` honestly (the geometry is fine; the host is not
    provisioned or the rung is too big), a cap overrun records
    ``timeout``, a record with no junction bookkeeping fails as a params
    error."""
    from precis.structure.relax import RelaxUnsupported

    params = (ctx.meta or {}).get("params") or {}
    try:
        structure_ref_id = int(params["structure_ref_id"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(
            f"hexfold_relax: malformed params ({exc})", failure_class="infra"
        )
        return
    polish = str(params.get("polish") or "ml")
    model = params.get("model") or None
    fragment = str(params.get("fragment") or "xtb")
    buffer_A = float(params.get("buffer_A") or 12.0)
    core_A = float(params.get("core_A") or 4.0)
    steps = int(params.get("steps") or 200)
    on_version = params.get("on_version")

    ref = ctx.store.get_ref(kind="structure", id=structure_ref_id)
    if ref is None:
        ctx.record_failure(
            f"hexfold_relax: no structure ref {structure_ref_id}",
            failure_class="infra",
        )
        return
    generated = (ref.meta or {}).get("generated") or {}
    try:
        scene, _handles = ctx.store.structure_load(
            structure_ref_id,
            version=int(on_version) if on_version is not None else None,
        )
    except Exception as exc:
        ctx.record_failure(
            f"hexfold_relax: structure_load failed: {exc}", failure_class="infra"
        )
        return
    loaded_version = int((ref.meta or {}).get("version", 0))

    cap = _relax_timeout_s()
    ctx.append_chunk(
        "job_event",
        f"hexfold_relax[{polish}+{fragment}] on "
        f"{os.environ.get('PRECIS_NODE') or 'this host'}: "
        f"{len(scene.atoms)} atoms, buffer={buffer_A:g}A core={core_A:g}A "
        f"steps={steps} cap={cap:.0f}s",
    )
    kwargs = {
        "polish": polish,
        "model": model,
        "fragment": fragment,
        "buffer_A": buffer_A,
        "core_A": core_A,
        "steps": steps,
    }
    cuda_requested = bool(((ctx.meta or {}).get("requires") or {}).get("gpu"))
    try:
        scene, report = run_in_child(
            _relax_child,
            (scene, generated, kwargs, cuda_requested),
            label=f"hexfold_relax job {ctx.ref_id}",
            timeout_s=cap,
            should_stop=lambda: job_stop_reason(ctx),
            on_start=lambda handle: ctx.set_meta(compute_child=handle),
            on_event=lambda kind, payload: _on_child_event(ctx, kind, payload),
            on_end=lambda handle: ctx.set_meta(compute_child=handle),
            poll_s=_CHILD_POLL_S,
            grace_s=_CHILD_GRACE_S,
        )
    except ChildStopped as stop:
        _terminalize_stopped(ctx, stop, cap)
        return
    except ChildDied as exc:
        code = exc.handle.get("exitcode")
        ctx.record_failure(
            f"hexfold_relax: relax process {exc} — no result",
            failure_class="infra",
            open_tag=(
                "infra:child-killed" if isinstance(code, int) and code < 0 else None
            ),
        )
        return
    except RelaxUnsupported as exc:
        ctx.record_failure(
            f"hexfold_relax: a rung has no backend on this host ({exc}) — "
            "install the extra that carries it on the node ([dft-ml] for "
            "'ml', [estimate] for 'xtb')",
            failure_class="infra",
        )
        return
    except ValueError as exc:
        ctx.record_failure(f"hexfold_relax: {exc}", failure_class="params")
        return
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("hexfold_relax: relax pipeline raised", exc_info=True)
        ctx.record_failure(
            f"hexfold_relax: relax pipeline failed: {exc}", failure_class="infra"
        )
        return

    # Write-back: the relaxed geometry is the scene's next version; the
    # before/after strain lands on the generated record; each compute
    # pass is a struct_runs row like any other relax.
    version = loaded_version + 1
    relax_record = {
        "job_id": int(ctx.ref_id),
        "polish": {
            "rung": report.polish.rung,
            "model": report.polish.model,
            "converged": report.polish.converged,
            "n_steps": report.polish.n_steps,
            "energy": report.polish.energy,
            "max_force": report.polish.max_force,
        },
        "fragments": [
            {
                "junction": f.name,
                "rung": f.result.rung,
                "atoms": f.atoms,
                "h_caps": f.h_caps,
                "frozen": f.frozen,
                "converged": f.result.converged,
                "n_steps": f.result.n_steps,
                "energy": f.result.energy,
                "max_force": f.result.max_force,
            }
            for f in report.fragments
        ],
        "strain": {"before": report.before, "after": report.after},
    }
    generated_next = dict(generated)
    generated_next["relax"] = relax_record
    title = str(ref.title or ref.slug)
    ctx.store.structure_save(
        slug=str(ref.slug),
        title=title,
        scene=scene,
        version=version,
        card_text=(
            f"{title} (atomistic structure). hexfold_relax "
            f"polish={polish} fragment={fragment}: {len(scene.atoms)} "
            f"atoms, {len(scene.bonds)} bonds."
        ),
        description=str((ref.meta or {}).get("description") or ""),
        relax_summary={
            "rung": f"hexfold:{polish}+{fragment}",
            "converged": report.polish.converged
            and all(f.result.converged for f in report.fragments),
            "n_steps": report.polish.n_steps
            + sum(f.result.n_steps for f in report.fragments),
            "max_disp": report.polish.max_disp,
            "energy": report.polish.energy,
            "max_force": report.polish.max_force,
            "model": report.polish.model,
        },
        meta_extra={"generated": generated_next},
    )
    ctx.store.structure_record_run(
        structure_ref_id,
        fidelity=polish,
        on_version=loaded_version,
        converged=report.polish.converged,
        n_steps=report.polish.n_steps,
        max_disp=report.polish.max_disp,
        energy=report.polish.energy,
        max_force=report.polish.max_force,
        model=report.polish.model,
        curve=report.polish.curve,
        params={"job": int(ctx.ref_id), "part": "hexfold_relax:polish"},
    )
    for f in report.fragments:
        ctx.store.structure_record_run(
            structure_ref_id,
            fidelity=f.result.rung,
            on_version=loaded_version,
            converged=f.result.converged,
            n_steps=f.result.n_steps,
            max_disp=f.result.max_disp,
            energy=f.result.energy,
            max_force=f.result.max_force,
            model=f.result.model,
            curve=f.result.curve,
            params={
                "job": int(ctx.ref_id),
                "part": f"hexfold_relax:fragment:{f.name}",
                "atoms": f.atoms,
                "h_caps": f.h_caps,
                "frozen": f.frozen,
            },
        )
    ctx.set_meta(
        hexfold_relax={
            "structure_ref_id": structure_ref_id,
            "version": version,
            "polish": polish,
            "fragment": fragment,
        }
    )
    n_after = len(report.after)
    ctx.append_chunk(
        "job_summary",
        f"hexfold_relax[{polish}+{fragment}] → version {version}: polish "
        f"{'converged' if report.polish.converged else 'NOT converged'} in "
        f"{report.polish.n_steps} steps"
        + (
            f"; {len(report.fragments)} junction fragment(s) relaxed "
            f"({sum(1 for f in report.fragments if f.result.converged)} converged)"
            if report.fragments
            else ""
        )
        + f"; {n_after} strain finding(s) re-scored on the relaxed atoms — "
        "before/after on the ref's meta.generated.relax.",
    )
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name="hexfold_relax",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
    validate_submit=_validate_submit,
    wall_seconds=_relax_timeout_s,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
