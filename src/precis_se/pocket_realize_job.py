"""``se_pocket_realize`` job_type — the pocket realize loop's GFN2
candidate relaxes off the MCP request path (pocket-from-molecule open
question 13, gr519389; Reto ruling 2026-10-25: run the surface read and
the realize relaxes as a JOB, and add the tblite wheel to the MCP image
so small reads still run inline).

The ``pocket_realize`` op ranks posed candidate walls at GFN2 — one
cluster relax (up to :data:`precis_se.pocket_realize.MAX_COMPLEX_ATOMS`
atoms, :data:`~precis_se.pocket_realize.RELAX_STEPS` FIRE steps) plus
single points per candidate. On a host without tblite the op cannot
compute that itself — the prod MCP image is linux-aarch64, which PyPI
has no wheel for — so it mints this job instead of echoing one
``join failed`` line per candidate. Even where tblite IS installed, a
multi-candidate relax is more work than an MCP call should hold open;
the op stays inline for now per the ruling ("small reads run inline")
and this job is the lane that makes the prod posture honest.

Minted like every compute-lane job: ``put(kind='job',
job_type='se_pocket_realize', executor='ssh_node',
parent_id=<se design ref>, params={'se_ref_id': N, 'wall': ...,
'candidates': [...]})``. ``params.target_node`` pins the claim the
same way ``struct_relax`` does — the mint resolves it through
:func:`precis.workers.job_types.struct_relax.target_node_for` with
rung ``'xtb'`` so the job lands on a compute-group host that carries
the wheelhouse tblite build. A claimed node still missing tblite fails
the job ``infra`` (``_require_gfn2``'s ``Unsupported``), naming the
backend — the same honesty ``hexfold_relax`` keeps.

The result IS the answer, not a write-back: the loop's ranking lands
as a ``job_summary`` chunk (the same ``_report`` text the op echoes
inline), the full :class:`RealizeResult` as a ``job_result`` JSON
chunk — the conventional job-result kind, readable via
``get(kind='job', id=<job ref>)`` (the default card caps it at a head;
``view='result'`` returns it in full) — and a compact summary on the
job's meta.

Params re-resolve everything at dispatch — the tree is reloaded with
:func:`precis_se.persist.load_tree`, the wall block and pocket are
selected exactly as the op does, and every candidate slug is reloaded
through ``_candidate_from_structure`` — so a stale block name, a gone
pocket or a dead candidate structure fails the job by name instead of
silently scoring nothing. ``wall_seconds`` reads the shared relax cap
(:func:`struct_relax._relax_timeout_s`) so the runner's outer bound
and initial lease match the kind of compute inside.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types.struct_relax import _relax_timeout_s

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

JOB_TYPE = "se_pocket_realize"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The se design the pocket lives on — the job reloads its tree.
        "se_ref_id": {"type": "integer"},
        # Echo only (log lines + summary); the ref is the truth.
        "slug": {"type": ["string", "null"]},
        # The block the pocket lives on + its name (absent ⇒ the wall's
        # single pocket, same rule as the op).
        "wall": {"type": "string", "minLength": 1},
        "pocket": {"type": ["string", "null"]},
        # Structure design slugs — each reloaded and re-posed by the
        # wall block's transform at dispatch.
        "candidates": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        "cap": {"type": ["integer", "null"], "minimum": 1},
        # The catpath dft config — verbatim into provenance (Q5 seam).
        "dft": {"type": ["object", "null"]},
        "target_node": {"type": ["string", "null"]},
    },
    "required": ["se_ref_id", "wall", "candidates"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Satisfied by EXECUTOR_PROVIDES['ssh_node'] == {'has_gpaw'} — the
#: compute-group marker, same as ``struct_relax``/``hexfold_relax``.
#: Those hosts carry the wheelhouse tblite this job's GFN2 rung needs.
REQUIRES = frozenset({"has_gpaw"})
DESCRIPTION = (
    "Rank pocket candidates at GFN2 on the compute group (the enqueued "
    "half of the se pocket_realize op, Q13); store the ranked report."
)


def _validate_submit(
    store: Any, *, gripe_id: Any = None, params: Any = None
) -> str | None:
    """Refuse a mint the dispatch could only fail: no se design, no
    wall name, or an empty candidate list."""
    params = params or {}
    ref_id = params.get("se_ref_id")
    if not isinstance(ref_id, int) or isinstance(ref_id, bool):
        return (
            "se_pocket_realize needs params.se_ref_id — the ref id of "
            "the se design the pocket lives on"
        )
    if store.get_ref(kind="se", id=ref_id) is None:
        return f"se_pocket_realize: no se ref {ref_id}"
    if not isinstance(params.get("wall"), str) or not params["wall"].strip():
        return "se_pocket_realize needs params.wall — the block the pocket lives on"
    raw = params.get("candidates")
    if not isinstance(raw, list) or not raw:
        return "se_pocket_realize needs params.candidates — a non-empty slug list"
    return None


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    try:
        se_ref_id = int(params["se_ref_id"])
        wall = str(params["wall"]).strip()
        slugs = [str(c).strip() for c in params["candidates"]]
        if not wall or not slugs or any(not s for s in slugs):
            raise ValueError("wall/candidates must be non-empty")
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(
            f"se_pocket_realize: malformed params ({exc})", failure_class="infra"
        )
        return
    pocket_name = params.get("pocket")
    if pocket_name is not None:
        pocket_name = str(pocket_name)
    cap = params.get("cap")
    cap = int(cap) if cap is not None else None
    dft = params.get("dft")
    slug = str(params.get("slug") or se_ref_id)

    from precis.errors import Unsupported
    from precis_se import persist as se_persist
    from precis_se import pocket_realize

    try:
        tree = se_persist.load_tree(ctx.store, se_ref_id)
    except Exception as exc:
        ctx.record_failure(f"se_pocket_realize: {slug}: tree load failed: {exc}")
        return
    wall_node = tree.blocks.get(wall)
    if wall_node is None:
        ctx.record_failure(
            f"se_pocket_realize: {slug}: no block {wall!r} "
            f"(have: {', '.join(sorted(tree.blocks)) or 'none'})"
        )
        return
    pockets = wall_node.pockets
    if pocket_name is None:
        if len(pockets) != 1:
            ctx.record_failure(
                f"se_pocket_realize: {slug}: {wall!r} has {len(pockets)} "
                "pockets — params.pocket must name one"
            )
            return
        pocket_name = next(iter(pockets))
    pocket = pockets.get(pocket_name)
    if pocket is None:
        ctx.record_failure(
            f"se_pocket_realize: {slug}: no pocket {pocket_name!r} on {wall!r} "
            f"(have: {', '.join(sorted(pockets)) or 'none'})"
        )
        return
    candidates = []
    for c in slugs:
        got = pocket_realize._candidate_from_structure(ctx.store, c, wall_node)
        if isinstance(got, str):
            ctx.record_failure(f"se_pocket_realize: {slug}: candidate {c!r}: {got}")
            return
        candidates.append(got)

    ctx.append_chunk(
        "job_event",
        f"se_pocket_realize: {slug}.{wall} pocket {pocket_name!r} — "
        f"{len(candidates)} candidate(s), cap {cap or 'none'}",
    )
    try:
        result = pocket_realize.realize_pocket(
            tree, wall, pocket, candidates, cap=cap, dft=dft
        )
    except Unsupported as exc:
        # The node lacks tblite — infra, not a bad request: name the
        # backend so the lane/wheelhouse gap is the thing that gets fixed.
        ctx.record_failure(f"se_pocket_realize: {exc}", failure_class="infra")
        return
    except Exception as exc:
        ctx.record_failure(f"se_pocket_realize: {slug}.{wall}: {exc}")
        return

    # The stored result (Q13's "stored result serves reads too big for
    # inline"): the report text the op would echo, plus the full ranking
    # as JSON so a later read gets the structured numbers, not a re-run.
    ctx.append_chunk("job_summary", pocket_realize._report(result))
    try:
        ctx.append_chunk("job_result", json.dumps(asdict(result)))
    except (TypeError, ValueError) as exc:  # never fail a finished loop
        log.warning("se_pocket_realize: result JSON not serializable: %s", exc)
    best = result.results[0] if result.results else None
    ctx.set_meta(
        pocket=result.pocket,
        evaluated=result.evaluated,
        unevaluated=result.unevaluated,
        solved=result.solved,
        best=best.name if best is not None else None,
    )
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name=JOB_TYPE,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    validate_submit=_validate_submit,
    dispatch=_dispatch,
    wall_seconds=_relax_timeout_s,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "JOB_TYPE",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "SPEC",
    "load",
]
