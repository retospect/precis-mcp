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
"""

from __future__ import annotations

import logging
import os
from typing import Any

from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types.struct_relax import _relax_timeout_s

log = logging.getLogger(__name__)

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


def _dispatch(ctx: Any, spec: Any) -> None:
    """Load the bound structure, run the relax pipeline in-process, and
    write the relaxed geometry + strain record back.

    No container and no staging — the rungs compute where the claim put
    the job, like ``struct_relax``'s in-process path. Backend-missing and
    wall-clock overruns record ``infra`` honestly (the geometry is fine;
    the host is not provisioned or the rung is too big), a record with no
    junction bookkeeping fails as a params error, and a genuine relax
    fault lands as ``non-convergence`` only when the backend ran and
    reported one."""
    from precis.structure.relax import RelaxUnsupported
    from precis_se.atomic.relax_block import relax_block

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

    ctx.append_chunk(
        "job_event",
        f"hexfold_relax[{polish}+{fragment}] on "
        f"{os.environ.get('PRECIS_NODE') or 'this host'}: "
        f"{len(scene.atoms)} atoms, buffer={buffer_A:g}A core={core_A:g}A "
        f"steps={steps} cap={_relax_timeout_s():.0f}s",
    )
    try:
        report = relax_block(
            scene,
            generated,
            polish=polish,
            model=model,
            fragment=fragment,
            buffer_A=buffer_A,
            core_A=core_A,
            steps=steps,
        )
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
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
