"""``briefing`` job_type — generate one morning news digest, deterministically.

Wrapper so the morning briefing runs as a precis *job* (driven by a
``level:recurring`` todo, typically ``meta.schedule={'cron':'0 6 * * *'}``)
rather than an OS timer. Deterministic, in-process (the LLM call goes to
the litellm ``summarizer`` alias, not a claude subprocess), registered
with a plugin ``dispatch`` so ``claude_inproc`` runs it directly.

Runs :func:`precis.workers.briefing.run_briefing`, which summarizes recent
``news`` refs and persists a dated ``briefing-<date>`` ref.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lookback_hours": {
            "type": "integer",
            "minimum": 1,
            "description": "News window to summarize (default 26h).",
        },
        "deliver_to": {
            "type": "string",
            "description": (
                "Delivery target for the brief, e.g. 'conv:discord/<g>/<c>/<t>'. "
                "Pushed via pg_notify('precis.cron') so asa_bot delivers it. "
                "Omit to only persist the briefing ref."
            ),
        },
    },
    "additionalProperties": False,
}


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    from precis.workers.briefing import run_briefing

    params = (ctx.meta or {}).get("params") or {}
    kwargs: dict[str, Any] = {}
    if params.get("lookback_hours"):
        kwargs["lookback_hours"] = int(params["lookback_hours"])
    if params.get("deliver_to"):
        kwargs["deliver_to"] = str(params["deliver_to"])
    try:
        r = run_briefing(ctx.store, **kwargs)
    except Exception as exc:
        log.warning("briefing job: pass raised", exc_info=True)
        ctx.record_failure(f"briefing: pass raised: {exc}")
        return

    if r["articles"] == 0:
        ctx.append_chunk(
            "job_summary", "briefing: no news in window — nothing to brief"
        )
        return
    ctx.append_chunk(
        "job_summary",
        f"briefing: {r['articles']} articles → {r['brief_chars']}-char brief "
        f"(ref {r['ref_id']})",
    )
    ctx.set_meta(articles=r["articles"], brief_ref_id=r["ref_id"])


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("briefing runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="briefing",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Summarize recent news into a dated morning briefing ref.",
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
