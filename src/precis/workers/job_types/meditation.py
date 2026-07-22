"""``meditation`` job_type — compose the evening nidra cast (a draft).

The evening sibling of :mod:`precis.workers.job_types.reading_brief`. Deterministic
in-process producer under ``claude_inproc`` (melchior), so the ~45-min segmented
walk is composed by a **nice model** (``claude-opus`` via the melchior-loopback
litellm proxy). Driven by a ``level:recurring`` todo
(``meta.schedule={'cron':'0 21 * * *'}``, ``meta.executor='claude_inproc'``,
``meta.job_type='meditation'``). Calls
:func:`precis.reading.meditation.build_meditation`, which walks the concept graph
into a standalone dated ``draft`` (voice ``af_nicole``); the ``cast_audio`` pass on
spark narrates it onto the feed as a separate downstream step.
"""

from __future__ import annotations

import logging
from typing import Any

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


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    from precis.reading.cast_common import voice_skill_preamble
    from precis.reading.meditation import build_meditation

    params = (ctx.meta or {}).get("params") or {}
    kwargs: dict[str, Any] = {}
    if params.get("cohort"):
        kwargs["cohort"] = str(params["cohort"])
    if params.get("target_minutes"):
        kwargs["target_minutes"] = int(params["target_minutes"])

    try:
        draft_id = build_meditation(
            ctx.store,
            skill_preamble=voice_skill_preamble(include_numbers=False),
            **kwargs,
        )
    except Exception as exc:
        log.warning("meditation job: pass raised", exc_info=True)
        ctx.record_failure(f"meditation: pass raised: {exc}")
        return

    if draft_id is None:
        ctx.append_chunk(
            "job_summary", "meditation: too few concepts to walk — nothing composed"
        )
        return
    ctx.append_chunk(
        "job_summary", f"meditation: composed evening nidra draft ref {draft_id}"
    )
    ctx.set_meta(draft_ref_id=draft_id)


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("meditation runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="meditation",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Compose the evening nidra meditation cast as a dated draft.",
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
