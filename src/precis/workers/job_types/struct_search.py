"""``struct_search`` job_type — an AGOX/GOFEE surrogate search, in-process.

Global structure search slice 1 (shipped 2026-09-20; the spec's surviving
truth lives in `structure/search.py`'s docstring, slices 2/3 in
`docs/backlog/global-structure-search-slice-*`): one job
proposes N candidates instead of an LLM authoring one at a time. AGOX
searches a confined box on a seed slab with a fixed stoichiometry, using the
``ml`` rung's own MACE calculator as the oracle
(:mod:`precis.structure.search`, which owns the pure AGOX plumbing) — the
top-K distinct (symmetry-deduped) candidates land as ordinary `structure`
rows, `derived-from` the seed and tagged ``search:agox``.

**In-process only, unlike `struct_relax`.** There is no container contract
here — no staging, no NFS mutex, no GPU reset: AGOX runs entirely in this
worker process (mirroring `struct_relax._dispatch_inproc`'s ``ml`` rung,
never its ``gpaw`` container path), still pinned to the GPU node via the same
``requires={"has_gpaw"}`` capability token `struct_relax` uses (the node with
torch/CUDA + the MACE wheel — ``has_gpaw`` names the node's compute
provisioning, not literally GPAW).

Missing ``agox`` (the ``[struct-search]`` extra, kept separate from
``[catalyst-gpu]`` — see the backlog's decisions log) or a missing MLIP
backend are both **infra** failures, not a verdict on the seed/box/add
request — the quest loop reads ``failure_class`` to decide whether to rule a
request out, and a missing wheel is never a reason to do that.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
from pathlib import Path
from typing import Any

from precis.store import Tag
from precis.structure.search import (
    SearchResult,
    SearchSpec,
    SearchUnsupported,
    top_k_distinct,
)
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

#: A candidate structure minted by this job is tagged ``search:agox`` (open
#: namespace, lower-cased by ``Tag.open``) so it's findable/filterable
#: independent of the quest-candidate tag below.
_SEARCH_TAG = "search:agox"

#: Mirrors ``quest.compute._CANDIDATE_TAG`` — duplicated as a literal rather
#: than imported to avoid a fragile cross-package import into `quest/` from
#: a `workers/job_types/` module (`quest.compute` already imports
#: `precis.workers.executors`; importing back from here risks a cycle the
#: registry's lazy per-name loading otherwise avoids). Keep in sync by hand.
_CANDIDATE_TAG = "candidate"

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The slab/seed structure the search explores around.
        "seed_ref_id": {"type": "integer"},
        "on_version": {"type": "integer"},
        # Fractional confinement bounds in the seed's own cell:
        # [[fx0,fx1],[fy0,fy1],[fz0,fz1]].
        "box": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
            },
            "minItems": 3,
            "maxItems": 3,
        },
        # Fixed stoichiometry to place inside the box, e.g. {"Pd":2,"N":1,"O":1}.
        "add": {"type": "object", "additionalProperties": {"type": "integer"}},
        "algo": {"type": "string", "default": "gofee"},
        # 'ml' MLIP model name; null routes via relax.route_ml_model(seed).
        "model": {"type": ["string", "null"]},
        # Oracle single-point evaluations (AGOX N_iterations); hard-capped
        # at 1000 by SearchSpec.validate().
        "budget": {"type": "integer", "default": 200},
        # How many symmetry-distinct, lowest-energy candidates to write back.
        "top_k": {"type": "integer", "default": 10},
        "timeout_s": {"type": "number", "default": 7200},
        # When set, each written candidate also `serves` this quest and
        # carries the `candidate` tag — the frontier sees it like any other
        # quest candidate.
        "quest_id": {"type": ["integer", "null"]},
        # The GPU node this search pins itself to — same claim-gate pin as
        # struct_relax's own `target_node` (§23 #3).
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["seed_ref_id", "on_version", "box", "add"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Satisfied by EXECUTOR_PROVIDES['ssh_node'] == {'has_gpaw'} — the same
#: token struct_relax's `ml` rung pins to (the node with torch/CUDA/MACE).
REQUIRES = frozenset({"has_gpaw"})
DESCRIPTION = (
    "AGOX/GOFEE surrogate structure search on the GPU node; write back the "
    "top-K distinct candidates as structure rows."
)

#: The Linux DFT node mounts caspar's export at /shared (macOS nodes use
#: /opt/shared) — mirrors struct_relax._NFS_ROOT so search scratch lives
#: alongside relax scratch on the same shared root.
_NFS_ROOT = os.environ.get("PRECIS_DFT_NFS_ROOT", "/shared")


def _default_workdir(ref_id: int) -> Path:
    """A per-job scratch dir under the shared NFS root (mirrors
    ``struct_relax._default_stager``), falling back to a local temp dir when
    that root isn't writable (e.g. this worker isn't actually on the NFS
    mount, or a test host)."""
    base = Path(_NFS_ROOT) / "scratch" / f"precis-search-{ref_id}"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError:
        return Path(tempfile.mkdtemp(prefix=f"precis-search-{ref_id}-"))


def _default_search_runner(*, spec: SearchSpec, workdir: Path) -> SearchResult:
    """Run the real AGOX search. The one seam that touches AGOX/MACE — tests
    swap :data:`SEARCH_RUNNER` for a stub, mirroring
    ``struct_relax.ML_RUNNER``."""
    from precis.structure.relax import _ml_calculator
    from precis.structure.search import run_search

    return run_search(
        spec,
        calculator_factory=lambda: _ml_calculator(spec.model),
        workdir=workdir,
    )


#: Swapped for a stub in tests (no AGOX in `scripts/test`'s image).
SEARCH_RUNNER = _default_search_runner


def _dispatch(ctx: Any, _spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job. ``ctx``
    is a :class:`~precis.workers.executors._context.DispatchContext`."""
    from precis.structure.relax import RelaxUnsupported, route_ml_model

    params = (ctx.meta or {}).get("params") or {}
    try:
        seed_ref_id = int(params["seed_ref_id"])
        on_version = int(params["on_version"])
        box_raw = params["box"]
        box = tuple(tuple(float(v) for v in axis) for axis in box_raw)
        add = {str(k): int(v) for k, v in dict(params["add"]).items()}
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(
            f"struct_search: malformed params ({exc})", failure_class="infra"
        )
        return

    algo = str(params.get("algo") or "gofee")
    budget = int(params.get("budget", 200))
    top_k = int(params.get("top_k", 10))
    timeout_s = float(params.get("timeout_s", 7200))
    quest_id = params.get("quest_id")
    quest_id = int(quest_id) if quest_id is not None else None

    seed_ref = ctx.store.get_ref(kind="structure", id=seed_ref_id)
    if seed_ref is None:
        ctx.record_failure(
            f"struct_search: seed_ref_id={seed_ref_id} does not resolve to a "
            "live structure",
            failure_class="input",
        )
        return
    # The job carries only the seed's *identity* (struct_relax bakes the
    # POSCAR into its params; a search re-reads the geometry at claim time),
    # so a seed edited while the job sat in the queue would silently search
    # a different slab than the quest configured its box/add against. The
    # version stamped at dispatch is the contract; a drift fails fast.
    live_version = int(ctx.store.structure_version(seed_ref_id))
    if live_version != on_version:
        ctx.record_failure(
            f"struct_search: seed {seed_ref.slug} is at version {live_version}, "
            f"the job was minted against version {on_version} -- re-dispatch "
            "against the current geometry",
            failure_class="input",
        )
        return
    seed_scene, _handles = ctx.store.structure_load(seed_ref_id)

    model = params.get("model") or None
    if not model:
        model = route_ml_model(seed_scene)

    try:
        search_spec = SearchSpec(
            seed=seed_scene,
            box=box,  # type: ignore[arg-type]
            add=add,
            model=model,
            algo=algo,
            budget=budget,
            timeout_s=timeout_s,
        )
    except ValueError as exc:
        ctx.record_failure(f"struct_search: {exc}", failure_class="input")
        return

    workdir = _default_workdir(ctx.ref_id)
    host = os.environ.get("PRECIS_NODE") or socket.gethostname()
    ctx.append_chunk(
        "job_event",
        f"search[{algo}] in-process on {host}: model={model} budget={budget} "
        f"top_k={top_k} timeout_s={timeout_s:.0f} seed=structure:{seed_ref_id} "
        f"(on_version={on_version}) workdir={workdir}",
    )

    try:
        result = SEARCH_RUNNER(spec=search_spec, workdir=workdir)
    except SearchUnsupported as exc:
        ctx.record_failure(
            f"struct_search: AGOX has no backend on this host ({exc}) -- the "
            "job was routed here by the claim gate but AGOX isn't installed "
            "-- install the [struct-search] extra on the node",
            failure_class="infra",
        )
        return
    except RelaxUnsupported as exc:
        ctx.record_failure(
            f"struct_search: oracle model {model!r} has no backend on this "
            f"host ({exc}) -- install the [dft-ml] extra on the node",
            failure_class="infra",
        )
        return
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("struct_search: SEARCH_RUNNER raised", exc_info=True)
        ctx.record_failure(
            f"struct_search: search failed: {exc}", failure_class="infra"
        )
        return

    _record_search(
        ctx,
        result,
        seed_ref=seed_ref,
        seed_ref_id=seed_ref_id,
        on_version=on_version,
        algo=algo,
        model=model,
        top_k=top_k,
        quest_id=quest_id,
    )


def _record_search(
    ctx: Any,
    result: SearchResult,
    *,
    seed_ref: Any,
    seed_ref_id: int,
    on_version: int,
    algo: str,
    model: str,
    top_k: int,
    quest_id: int | None,
) -> None:
    """Write back the top-K distinct candidates and the population summary —
    the one seam both a real and a stubbed :data:`SEARCH_RUNNER` land on."""
    from precis.handlers.structure import card_text

    ranked = top_k_distinct(result.candidates, k=top_k)
    if not ranked:
        ctx.append_chunk(
            "job_event",
            "search: 0 candidates survived symmetry dedup -- an empty "
            "population is a result, not an infra failure",
        )
        ctx.set_meta(
            search={
                "count": 0,
                "best_energy_eV": result.best_energy_eV,
                "budget_used": result.budget_used,
                "wall_s": result.wall_s,
                "database_path": result.database_path,
                "candidate_ref_ids": [],
            }
        )
        ctx.append_chunk(
            "job_summary",
            f"search[{algo}]: 0 distinct candidates from {result.budget_used} "
            f"oracle calls ({result.wall_s:.0f}s) -- nothing to write back.",
        )
        ctx.set_status("succeeded")
        return

    candidate_ref_ids: list[int] = []
    for rank, cand in enumerate(ranked, start=1):
        slug = f"{seed_ref.slug}-search-{ctx.ref_id}-{rank}"
        title = f"{seed_ref.title or seed_ref.slug} search #{rank}"
        description = (
            f"AGOX {algo} search candidate, rank {rank}, {cand.oracle_energy_eV:.3f} eV"
        )
        text = card_text(title, cand.scene, description)
        ref, _created = ctx.store.structure_save(
            slug=slug,
            title=title,
            scene=cand.scene,
            version=1,
            card_text=text,
            description=description,
        )
        ctx.store.add_link(
            src_ref_id=ref.id,
            dst_ref_id=seed_ref_id,
            relation="derived-from",
            set_by="system",
        )
        search_meta = {
            "algo": algo,
            "model": model,
            "budget_used": result.budget_used,
            "iteration": cand.iteration,
            "oracle_energy_eV": cand.oracle_energy_eV,
            "surrogate_energy_eV": cand.surrogate_energy_eV,
            "rank": rank,
            "job_ref_id": ctx.ref_id,
            "seed_version": on_version,
        }
        ctx.store.stamp_ref_meta(ref.id, {"search": search_meta})
        ctx.store.add_tag(ref.id, Tag.open(_SEARCH_TAG), set_by="system")
        # Deliberately no relax-cache participation (backlog "Explicitly NOT
        # in scope") -- cache_key/structure_sha stay NULL. structure_
        # find_cached_run's lookup is `WHERE cache_key = %s`, which a NULL
        # column can never satisfy (SQL NULL = <anything> is never true), so
        # this row is structurally unreachable from the relax-cache path,
        # never a silent false hit.
        ctx.store.structure_record_run(
            ref.id,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=cand.oracle_energy_eV,
            model=model,
            params={"search": search_meta},
            cache_key=None,
            structure_sha=None,
        )
        if quest_id is not None:
            ctx.store.add_link(
                src_ref_id=ref.id,
                dst_ref_id=quest_id,
                relation="serves",
                set_by="system",
            )
            ctx.store.add_tag(ref.id, Tag.open(_CANDIDATE_TAG), set_by="system")
        candidate_ref_ids.append(int(ref.id))

    # ``SearchResult.best_energy_eV`` is optional (a runner may leave it
    # unset); the written population is the authority for the summary.
    best = result.best_energy_eV
    if best is None and ranked:
        best = ranked[0].oracle_energy_eV
    ctx.set_meta(
        search={
            "count": len(candidate_ref_ids),
            "best_energy_eV": best,
            "budget_used": result.budget_used,
            "wall_s": result.wall_s,
            "database_path": result.database_path,
            "candidate_ref_ids": candidate_ref_ids,
        }
    )
    best_txt = f"{best:.3f} eV" if best is not None else "n/a"
    ctx.append_chunk(
        "job_summary",
        f"search[{algo}]: {len(candidate_ref_ids)} distinct candidates from "
        f"{result.budget_used} oracle calls ({result.wall_s:.0f}s); best "
        f"{best_txt} -> {candidate_ref_ids}.",
    )
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name="struct_search",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SEARCH_RUNNER", "SPEC", "load"]
