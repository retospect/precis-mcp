"""``reading_brief`` job_type — compose the morning reading-brief cast (a draft).

Deterministic in-process producer, run under ``claude_inproc`` (the melchior agent
worker) — the same executor + host as the news ``briefing`` job, because the
compose call uses a **nice model** (``claude-opus`` via the litellm proxy, which is
melchior-loopback-only). Driven by a ``level:recurring`` todo
(``meta.schedule={'cron':'0 6 * * *'}``, ``meta.executor='claude_inproc'``,
``meta.job_type='reading_brief'``). Calls
:func:`precis.reading.briefing_cast.build_reading_briefing`, which unions the
activity/reading/recall lanes into a standalone dated ``draft``; the ``cast_audio``
pass on spark then narrates it onto the podcast feed **as a separate downstream
step** (compose and TTS never block each other). Once-a-day, so the melchior
compute is fine.

``claude_inproc`` plugin dispatchers return ``None`` and report via ``ctx`` (the
executor auto-finalizes to ``SUCCEEDED``); a raise goes through
``ctx.record_failure``.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    from precis.reading.briefing_cast import build_reading_briefing

    try:
        draft_id = build_reading_briefing(ctx.store)
    except Exception as exc:
        log.warning("reading_brief job: pass raised", exc_info=True)
        ctx.record_failure(f"reading_brief: pass raised: {exc}")
        return

    if draft_id is None:
        ctx.append_chunk(
            "job_summary", "reading_brief: no material in any lane — nothing composed"
        )
        return
    ctx.append_chunk(
        "job_summary", f"reading_brief: composed morning cast draft ref {draft_id}"
    )
    ctx.set_meta(draft_ref_id=draft_id)


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("reading_brief runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="reading_brief",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Compose the morning reading-brief cast as a dated draft.",
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
