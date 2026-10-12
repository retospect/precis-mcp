"""``briefing`` job_type — generate one morning news digest, deterministically.

Wrapper so the morning briefing runs as a precis *job* (driven by a
recurring (``meta.schedule`` set) todo, typically ``meta.schedule={'cron':'0 6 * * *'}``)
rather than an OS timer. Deterministic in the sense that it's a plain
function call, not agentic tool use — the LLM call itself now folds through
the router onto ``claude_agent`` (a ``claude -p`` subprocess,
direct Anthropic OAuth) rather than the litellm proxy. Registered with a
plugin ``dispatch`` so ``claude_inproc`` runs it directly.

Runs :func:`precis.workers.briefing.run_briefing`, which summarizes recent
``news`` refs and persists a dated ``briefing-<date>`` ref.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
        "deadline_utc": {
            "type": "string",
            "pattern": "^[0-2][0-9]:[0-5][0-9]$",
            "description": (
                "Latest UTC time (HH:MM, default 12:00) a budget-paused brief "
                "keeps retrying hourly; past it the day is skipped with a "
                "notice to deliver_to."
            ),
        },
    },
    "additionalProperties": False,
}


def _request_retry(ctx: Any, retry_at: datetime) -> bool:
    """Stamp ``deliverable_retry_at`` on the job's parent tick todo, which
    the schedule worker re-mints at that instant. ``False`` when the job
    has no parent (a hand-minted job: notice only)."""
    from precis.workers.schedule.worker import DELIVERABLE_RETRY_AT

    with ctx.store.tx() as conn:
        row = conn.execute(
            "SELECT parent_id FROM refs WHERE ref_id = %s", (ctx.ref_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return False
        ctx.store.stamp_ref_meta(
            int(row[0]), {DELIVERABLE_RETRY_AT: retry_at.isoformat()}, conn=conn
        )
    return True


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    from precis.workers.briefing import BriefingPaused, handle_pause, run_briefing

    params = (ctx.meta or {}).get("params") or {}
    kwargs: dict[str, Any] = {}
    if params.get("lookback_hours"):
        kwargs["lookback_hours"] = int(params["lookback_hours"])
    if params.get("deliver_to"):
        kwargs["deliver_to"] = str(params["deliver_to"])
    try:
        r = run_briefing(ctx.store, **kwargs)
    except BriefingPaused as exc:
        # A deliverable never fails silently on a budget pause (gr347551):
        # notice to the channel, then retry hourly until the deadline.
        retry_at = handle_pause(
            ctx.store,
            deliver_to=kwargs.get("deliver_to"),
            now=datetime.now(UTC),
            deadline_utc=params.get("deadline_utc"),
            reason=str(exc),
        )
        if retry_at is not None and _request_retry(ctx, retry_at):
            ctx.append_chunk(
                "job_summary",
                f"briefing: budget-paused ({exc}); retry at {retry_at.isoformat()}",
            )
            ctx.set_meta(paused=True, retry_at=retry_at.isoformat())
        else:
            ctx.append_chunk("job_summary", f"briefing: budget-paused, skipped ({exc})")
            ctx.set_meta(paused=True, skipped=True)
        return
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
        f"(ref {r['ref_id']})"
        + (f", degraded to {r['degraded_to']}" if r.get("degraded_to") else ""),
    )
    ctx.set_meta(articles=r["articles"], brief_ref_id=r["ref_id"])
    if r.get("degraded_to"):
        ctx.set_meta(degraded_to=r["degraded_to"])


SPEC = JobTypeSpec(
    name="briefing",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Summarize recent news into a dated morning briefing ref.",
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
