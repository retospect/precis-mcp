"""``se_simp`` job_type — the enqueued half of
``realize(block=..., strategy='simp', ...)`` (docs/backlog/
structural-solution-space.md "Slice 4 bridge").

The op (:func:`precis_se.simp_bridge.prepare_simp`, run inside
``SeHandler.put``/``edit``) only validates and enqueues; this job does
the minutes-long work — voxelise, solve, store the field, mint the cad
design, bind the block — by calling :func:`precis_se.simp_bridge.
run_simp`, the same function the tests run in-process. Runs under the
``job_inproc`` executor (bounded, in-process CPU work; the ``pcb_place``
dispatch shape verbatim): never inline in an MCP tool call.

Params are the :class:`~precis_se.simp_bridge.SimpRequest` fields plus
``se_ref_id`` (and ``slug`` for the summary line). A refusal the op could
not foresee (an envelope that voxelises to nothing, a passive set that
eats the volume budget) is a ``record_failure`` with the bridge's own
wording; nothing is minted on failure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.workers.job_types import JobTypeSpec
from precis_se import simp_bridge

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
        "volfrac": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
        "build_dir": {"type": "string", "enum": list(simp_bridge.AXIS_TOKENS)},
        "build_dir_source": {"type": "string"},
        "load_at": {"type": "string", "minLength": 1},
        "fixed_at": {"type": "string", "minLength": 1},
        "round": {"type": ["number", "null"]},
        "open": {"type": ["number", "null"]},
        "close": {"type": ["number", "null"]},
        "max_iter": {
            "type": "integer",
            "minimum": 1,
            "maximum": simp_bridge.MAX_ITER_CAP,
        },
    },
    "required": [
        "se_ref_id",
        "block",
        "mode",
        "pitch",
        "volfrac",
        "build_dir",
        "load_at",
        "fixed_at",
    ],
    "additionalProperties": False,
}

#: job_inproc only — bounded in-process compute, never a blocking MCP call.
COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "SIMP topology solve over one se block's envelope, bound back as a cad "
    "field-leaf design — the enqueued half of realize(strategy='simp')."
)


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    try:
        se_ref_id = int(params["se_ref_id"])
        request = simp_bridge.SimpRequest.from_params(params)
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"se_simp: malformed params ({exc})", failure_class="infra")
        return
    slug = str(params.get("slug") or se_ref_id)
    ctx.append_chunk(
        "job_event",
        f"se_simp: {slug}.{request.block} pitch={request.pitch:g} m "
        f"volfrac={request.volfrac:g} build_dir={request.build_dir}",
    )
    try:
        outcome = simp_bridge.run_simp(ctx.store, se_ref_id, request)
    except simp_bridge.SimpBridgeError as exc:
        ctx.record_failure(f"se_simp: {exc}")
        return
    summary = outcome.summary
    ctx.append_chunk("job_summary", outcome.echo)
    ctx.set_meta(
        cad=outcome.cad_slug,
        compliance_last=summary["compliance_last"],
        volume_fraction=summary["volume_fraction"],
        iterations=summary["iterations"],
        converged=summary["converged"],
        overhang_violations=summary["overhang_violations_field"],
    )
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name=simp_bridge.JOB_TYPE,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
