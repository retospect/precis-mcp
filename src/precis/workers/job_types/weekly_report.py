"""Deterministic weekly report job; no model invocation or prose rewrite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from precis.reports.weekly import collect, render_markdown, write_tables
from precis.workers.job_types import JobTypeSpec


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    del spec
    params = ctx.meta.get("params") or {}
    until = params.get("until") or datetime.now(UTC).isoformat()
    from precis.reports.weekly import bound

    try:
        since = params.get("since") or (bound(until) - timedelta(days=7)).isoformat()
        if bool(params.get("draft")) != bool(params.get("section")):
            raise ValueError("draft and section must be supplied together")
        data = collect(ctx.store, since, until)
        if params.get("draft"):
            write_tables(ctx.store, params["draft"], params["section"], data)
        ctx.append_chunk("job_summary", render_markdown(data))
        ctx.set_meta(
            weekly_report={"since": since, "until": until, "tables": len(data)}
        )
    except Exception as exc:
        ctx.record_failure(f"weekly_report: {exc}; next: check params and retry")


SPEC = JobTypeSpec(
    name="weekly_report",
    params_schema={
        "type": "object",
        "properties": {
            key: {"type": "string"} for key in ("since", "until", "draft", "section")
        },
        "additionalProperties": False,
        "dependentRequired": {"draft": ["section"], "section": ["draft"]},
    },
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description="Collect weekly factory tables, optionally replacing tables in a draft section.",
    dispatch=_dispatch,
)
