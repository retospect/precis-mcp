"""``meditation`` job_type — compose the evening nidra cast (a draft).

The evening sibling of :mod:`precis.workers.job_types.reading_brief`. Deterministic
in-process producer under the **coordinator** executor (any system-profile node).
Driven by a ``level:recurring`` todo (``meta.schedule={'cron':'0 21 * * *'}``,
``meta.executor='coordinator'``, ``meta.job_type='meditation'``). Calls
:func:`precis.reading.meditation.build_meditation`, which walks the concept graph
into a standalone dated ``draft`` (voice ``af_nicole``, ~45-min segmented walk);
the ``cast_audio`` pass on spark narrates it onto the feed. Returns ``Done``.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.executors._yield import Done
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cohort": {
            "type": ["string", "null"],
            "description": "Concept cohort to walk (else the most-recent concepts).",
        },
        "target_minutes": {
            "type": "integer",
            "minimum": 1,
            "description": "Target spoken length (default 45).",
        },
    },
    "additionalProperties": False,
}


def _dispatch(ctx: Any, spec: Any) -> Any:
    """Coordinator dispatcher: compose today's nidra draft."""
    from precis.reading.meditation import build_meditation

    params = (ctx.meta or {}).get("params") or {}
    kwargs: dict[str, Any] = {}
    if params.get("cohort"):
        kwargs["cohort"] = str(params["cohort"])
    if params.get("target_minutes"):
        kwargs["target_minutes"] = int(params["target_minutes"])

    try:
        draft_id = build_meditation(ctx.store, **kwargs)
    except Exception as exc:
        log.warning("meditation job: pass raised", exc_info=True)
        return Done(summary=f"meditation: pass raised: {exc}", success=False)

    if draft_id is None:
        return Done(summary="meditation: too few concepts to walk — nothing composed")
    return Done(
        summary=f"meditation: composed evening nidra draft ref {draft_id}",
        summary_meta={"draft_ref_id": draft_id},
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("meditation runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="meditation",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"coordinator"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Compose the evening nidra meditation cast as a dated draft.",
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
