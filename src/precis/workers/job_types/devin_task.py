"""devin_task — hand one task to a cloud Devin session (gr543831).

A job_type is the *what*; the :mod:`precis.workers.executors.devin_api`
executor is the *how* (mint a session via the official Devin API v3,
poll it, collect the result). This module is the declaration the
``put(kind='job')`` path validates against plus the shared fail-closed
param gate — the same shape as :mod:`sandbox_run`, whose sole executor
(``claude_docker``) also bypasses the dispatch protocol, so this spec
carries no ``run``/``dispatch``/``submit``/``poll``.

This is a **job executor, not a router rung**: Devin's API runs task
sessions, not completions, so it can never back an ``llm.op`` tier chain.
The job carries **no prod-write credential** — the executor sends only
the prompt text and the optional scoping fields below; it never attaches
``secret_ids``/``session_secrets`` and never injects a precis credential
into the payload. Builder-lane rules carry over (me538174/me538185): a
claimed result is *evidence* (provider-reported status, ``pull_requests``,
``structured_output``), not truth — a consumer verifies every claimed
commit by sha before trusting it.
``render_prompt`` appends the finishing contract to every launch, including
old queued prompts: publish only the job's named branch, never a PR. Cloud
branch handoff is pending: no fetch/landing consumer is implemented. This
executor collects evidence and does not own a local checkout; an operator
must fetch and verify the branch before arranging a land.

Ships **dark**: ``devin_api``'s pass registers structurally
(``enable_env`` on the ``job_devin_api`` ServiceSpec) but is gated off
every cycle until an operator lights a ``service_config`` row —
``precis service prio '*' job_devin_api 1``.
"""

from __future__ import annotations

from typing import Any

# ── Declared metadata (read by the dispatcher and the executor) ────

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The open-ended task handed to Devin. Required.
        "prompt": {"type": "string"},
        # Human title for the session (provider-side label only).
        "title": {"type": "string"},
        # Repositories the session may touch ('owner/repo' strings).
        "repos": {"type": "array", "items": {"type": "string"}},
        # Extra provider-side tags (the executor adds precis markers).
        "tags": {"type": "array", "items": {"type": "string"}},
        # Per-session ACU cap (provider-side spend bound).
        "max_acu_limit": {"type": "integer"},
        # Wall-clock budget — same nested shape as
        # ssh_node/coordinator/sandbox_run. Optional: the executor
        # defaults it when absent.
        "resources": {
            "type": "object",
            "properties": {"wall_seconds": {"type": "integer"}},
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"devin_api"})

#: The executor host needs no local capability — the work is remote
#: (one REST client, a vault-resolved token). Quota and concurrency are
#: provider-side (``max_acu_limit`` / Devin's own session limits), not
#: ``resource_slots``.
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Hand one task to a cloud Devin session via the official Devin API "
    "v3 (POST /v3/organizations/<org>/sessions): the devin_api executor "
    "mints the session, polls its status, collects the result and any "
    "pull-request links, and ends the job succeeded / failed / parked on "
    "quota. Ships dark — gated by a job_devin_api service_config row; "
    "the job carries no prod-write credential."
)


def render_prompt(prompt: str, *, job_id: int) -> str:
    """Append the cloud handoff contract even to previously queued prompts.

    The API executor has no checkout or fetch/landing consumer. The named
    branch awaits operator verification; provider success is no land receipt.
    """
    return (
        prompt.strip()
        + "\n\n## Finishing contract (overrides conflicting task instructions)\n\n"
        "Tests for the ask must be written before the build and supplied with "
        "the task. If they are missing, stop and report the missing tests. "
        "Use those tests as the acceptance criteria; do not weaken them. "
        "Make changes only inside this task's repository workspace.\n\n"
        "Do not commit in a local run_slice worktree: the local harness tests "
        "and commits those edits. This is the cloud lane, without a local "
        f"slice worktree: commit and push changes only to `devin/job-{job_id}`. "
        "Never open a PR. Do not land, merge or push to main. Cloud branch "
        "handoff is PENDING: no fetch/landing consumer is implemented. An "
        "operator must fetch your named branch, verify the supplied tests, "
        "and arrange the land; publishing the branch is not a landed fix. "
        "Report the repository, branch, commit SHA and test paths in your "
        "final output.\n\n"
        "If no change is needed, make the first line of your final output "
        "`moot: gr<N> already fixed by <sha>` when the gripe is already "
        "fixed on origin/main, `validated: <reason>` when the requested "
        "check passed, or `no-op: <reason>` when nothing needs changing. "
        "A bare moot is refused for code builds; name the fixing SHA and "
        "explain the evidence. "
        "Do not create an empty commit or push an unchanged branch.\n"
    )


def params_rejection(params: dict[str, Any]) -> str | None:
    """Fail-closed rejection reason for ``params``, or ``None``.

    Single source of truth, shared by ``validate_submit`` (put time) and
    the ``devin_api`` executor (launch time — a job minted by dispatch
    never passed through the JobHandler put path). Rejects: a missing or
    blank ``prompt``, a non-positive ``resources.wall_seconds`` or
    ``max_acu_limit``, and non-string ``title``/``repos``/``tags``.
    """
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return "devin_task: params.prompt is required (the task text for the session)"
    title = params.get("title")
    if title is not None and not isinstance(title, str):
        return "devin_task: params.title must be a string"
    wall = params.get("resources")
    if wall is not None:
        if not isinstance(wall, dict):
            return "devin_task: params.resources must be an object"
        seconds = wall.get("wall_seconds")
        if seconds is not None and (
            not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0
        ):
            return (
                "devin_task: params.resources.wall_seconds must be a positive integer"
            )
    acu = params.get("max_acu_limit")
    if acu is not None and (
        not isinstance(acu, int) or isinstance(acu, bool) or acu <= 0
    ):
        return "devin_task: params.max_acu_limit must be a positive integer"
    for key in ("repos", "tags"):
        value = params.get(key)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(v, str) for v in value)
        ):
            return f"devin_task: params.{key} must be a list of strings"
    return None


def validate_submit(
    store: Any, *, gripe_id: int | None, params: dict[str, Any]
) -> str | None:
    """Submit gate for ``JobHandler.put`` — :func:`params_rejection`."""
    return params_rejection(params)


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "params_rejection",
    "validate_submit",
]
