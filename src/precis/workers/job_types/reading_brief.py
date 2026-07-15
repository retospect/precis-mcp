"""``reading_brief`` job_type — compose the morning reading-brief cast (a draft).

Deterministic in-process producer, run under the **coordinator** executor so it
claims on any system-profile node — dodging the single melchior ``claude_inproc``
executor (the "45-min dark" SPOF). Driven by a ``level:recurring`` todo
(``meta.schedule={'cron':'0 6 * * *'}``, ``meta.executor='coordinator'``,
``meta.job_type='reading_brief'``). Calls
:func:`precis.reading.briefing_cast.build_reading_briefing`, which unions the
activity/reading/recall lanes into a standalone dated ``draft``; the ``cast_audio``
pass on spark then narrates it onto the podcast feed.

Coordinator dispatchers return :class:`Done` / :class:`Yield`; this one is a
single deterministic slice, so it always returns ``Done``.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.executors._yield import Done
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _dispatch(ctx: Any, spec: Any) -> Any:
    """Coordinator dispatcher: compose today's reading-brief draft."""
    from precis.reading.briefing_cast import build_reading_briefing

    try:
        draft_id = build_reading_briefing(ctx.store)
    except Exception as exc:
        log.warning("reading_brief job: pass raised", exc_info=True)
        return Done(summary=f"reading_brief: pass raised: {exc}", success=False)

    if draft_id is None:
        return Done(summary="reading_brief: no material in any lane — nothing composed")
    return Done(
        summary=f"reading_brief: composed morning cast draft ref {draft_id}",
        summary_meta={"draft_ref_id": draft_id},
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("reading_brief runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="reading_brief",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"coordinator"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Compose the morning reading-brief cast as a dated draft.",
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
