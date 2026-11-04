"""Workflow-run diagnostics shared by release polling and ci-failed.

A terminal workflow can strand queued jobs without failed-job logs. The run
verdict and its job statuses must both be read before suggesting --failed.
"""

from __future__ import annotations

from typing import Any

OK_CONCLUSIONS = ("success", "skipped", "neutral")


def unfinished_jobs(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Jobs stranded by a completed workflow, rather than currently running."""
    if run.get("status") != "completed":
        return []
    return [
        job
        for job in run.get("jobs", [])
        if job.get("status") in ("queued", "in_progress")
    ]


def rerun_hint(run: dict[str, Any], run_id: str) -> str:
    if not unfinished_jobs(run):
        return ""
    return (
        f"full rerun needed: gh run rerun {run_id}; "
        "gh run rerun --failed cannot rerun jobs that never concluded"
    )


def run_diagnostics(run: dict[str, Any], run_id: str) -> list[str]:
    jobs = unfinished_jobs(run)
    if not jobs:
        return []
    return [
        *(f"never concluded: {job['name']} ({job['status']})" for job in jobs),
        rerun_hint(run, run_id),
    ]
