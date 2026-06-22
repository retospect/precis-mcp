"""``news_poll`` job_type — run one RSS ingestion pass, deterministically.

Thin wrapper that lets the news feed-poller run as a precis *job* instead
of an OS cron / launchd timer. Like ``draft_export`` it is deterministic,
in-process work (no claude) registered with a plugin ``dispatch`` so
``claude_inproc`` runs it directly.

The intended driver is a ``level:recurring`` todo
(``meta.schedule={'every':'30m'}``, ``meta.executor='claude_inproc'``,
``meta.job_type='news_poll'``): the schedule pass spawns a child each
tick, the dispatch pass mints a job, and this dispatcher runs
:func:`precis.workers.news_poll.run_news_pass`. No launchd role needed —
the already-running ``com.precis.worker`` ticks schedule + dispatch.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit_sources": {
            "type": "integer",
            "minimum": 1,
            "description": "Cap on feeds polled this pass (default: all enabled).",
        }
    },
    "additionalProperties": False,
}


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    from precis.workers.news_poll import run_news_pass

    params = (ctx.meta or {}).get("params") or {}
    limit = params.get("limit_sources")
    try:
        r = run_news_pass(ctx.store, limit_sources=limit)
    except Exception as exc:  # one bad pass shouldn't wedge the recurring
        log.warning("news_poll job: pass raised", exc_info=True)
        ctx.record_failure(f"news_poll: pass raised: {exc}")
        return

    ctx.append_chunk(
        "job_summary",
        f"news_poll: {r['claimed']} feeds, {r['ok']} new articles, "
        f"{r['failed']} failed",
    )
    ctx.set_meta(feeds=r["claimed"], new_articles=r["ok"], failed=r["failed"])


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("news_poll runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="news_poll",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Poll the news_sources RSS registry and mint new news articles.",
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
