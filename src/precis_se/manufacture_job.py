"""``se_manufacture`` job_type — the enqueued half of
``realize(block=<group root>, strategy='manufacture', ...)``
(docs/backlog/structural-solution-space.md "Slice 4 bridge", round B2).

The op (:func:`precis_se.manufacture.prepare_manufacture`, run inside
``SeHandler.put``/``edit``) validates, and fuses INLINE when the group
is small and has no SIMP member; otherwise the handler enqueues this job,
which does the same work — sample, erode, fuse, cut the cavities, label
the objects, store the field, mint the cad design, bind the root — by
calling :func:`precis_se.manufacture.run_manufacture`, the same function
the synchronous path and the tests run. ``job_inproc`` only, the
``se_simp`` shape verbatim. A job type of its own rather than a mode on
``se_simp``: that job's params schema is closed and SIMP-shaped
(volfrac/load_at/fixed_at required), and a fuse has none of them.

Params are the :class:`~precis_se.manufacture.ManufactureRequest` fields
plus ``se_ref_id`` (and ``slug`` for the summary line).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.workers.job_types import JobTypeSpec
from precis_se import manufacture

if TYPE_CHECKING:
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "se_ref_id": {"type": "integer"},
        "slug": {"type": ["string", "null"]},
        "block": {"type": "string", "minLength": 1},
        "mode": {"type": "string", "minLength": 1},
        "pitch": {"type": "number", "exclusiveMinimum": 0},
        "pitch_source": {"type": "string"},
        "gap": {"type": ["number", "null"]},
        "gap_source": {"type": "string"},
        "gap_floor_mm": {"type": ["number", "null"]},
        "fit": {"type": ["number", "null"]},
        "blend": {"type": "number", "minimum": 0},
    },
    "required": ["se_ref_id", "block", "mode", "pitch"],
    "additionalProperties": False,
}

#: job_inproc only — bounded in-process compute, never a blocking MCP call.
COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Print-in-place fuse of one se print group (intent manufacture): the "
    "members' solids min-unioned in a sampled field with in-place gaps, "
    "cavities and fastener elision, bound back as a cad field-leaf design "
    "on the group root — the enqueued half of realize(strategy='manufacture')."
)


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    try:
        se_ref_id = int(params["se_ref_id"])
        request = manufacture.ManufactureRequest.from_params(params)
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(
            f"se_manufacture: malformed params ({exc})", failure_class="infra"
        )
        return
    slug = str(params.get("slug") or se_ref_id)
    ctx.append_chunk(
        "job_event",
        f"se_manufacture: {slug}.{request.block} pitch={request.pitch:g} m "
        f"gap={request.gap} fit={request.fit} blend={request.blend:g}",
    )
    try:
        outcome = manufacture.run_manufacture(ctx.store, se_ref_id, request)
    except manufacture.ManufactureError as exc:
        ctx.record_failure(f"se_manufacture: {exc}")
        return
    summary = outcome.summary
    ctx.append_chunk("job_summary", outcome.echo)
    ctx.set_meta(
        cad=outcome.cad_slug,
        objects=len(summary.get("objects") or []),
        cavities=len(summary.get("cavities") or []),
        elided=len(summary.get("elided") or []),
        cells=summary.get("cells"),
    )
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name=manufacture.JOB_TYPE,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
