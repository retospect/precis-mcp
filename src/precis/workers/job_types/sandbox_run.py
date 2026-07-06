"""sandbox_run — an open-ended coding task in a throwaway container.

Slice 1 of the ``sandbox_run`` design (``docs/design/sandbox-run.md``):
the mint → claim → launch → poll → terminal spine, run by the
:mod:`precis.workers.executors.claude_docker` executor. This slice is
**``mode:build`` + ``precis_access:none`` only** and ships **dark** —
the executor pass is registered only under ``PRECIS_SANDBOX_ENABLED``,
so a merge/deploy touches nothing in prod until a human enables it on a
sandbox host.

The job_type module is a pure declaration + helpers:

* ``PARAMS_SCHEMA`` / ``COMPATIBLE_EXECUTORS`` / ``REQUIRES`` — the
  registry metadata (validated at ``put`` time by the JobHandler).
* ``validate_submit`` — the **fail-closed** submit gate: rejects
  ``mode:run``, ``precis_access:read``, a ``secrets`` list, a
  non-sandbox / melchior ``target_node``, or a missing
  ``CLAUDE_CODE_OAUTH_TOKEN`` in the daemon env. The claude_docker
  executor re-checks the same conditions at launch (defence in depth
  for jobs minted by ``dispatch`` from a todo, which don't pass through
  the JobHandler put path).
* ``resolve_sandbox_model`` — model via the ADR 0046 router
  (``Tier.CLOUD_SUPER``) with a ``PRECIS_SANDBOX_MODEL`` override; never
  a private constant.
* ``compose_prompt`` — the ``/work/PROMPT.md`` body (task + harvest
  contract) the executor stages into the run dir.

Trust model (design decisions log): the container runs Claude with a
**dedicated** long-lived ``CLAUDE_CODE_OAUTH_TOKEN`` (Max, *not*
``--bare`` / ``ANTHROPIC_API_KEY``), no DB creds, cgroup-capped, never a
GPU. melchior is excluded (it holds OAuth / gateway / creds — an escape
target); only ``agent_sandbox_host`` nodes may run it.
"""

from __future__ import annotations

import os
from typing import Any

from precis.utils.llm.router import Tier, resolve_model

# ── Declared metadata (read by the dispatcher and the executor) ────

#: Nodes that may never be a sandbox host regardless of the allowlist —
#: melchior holds the OAuth token / gateway / DB creds, so a container
#: escape there is the whole threat model. Hard-excluded even if an
#: operator mistakenly lists it in ``PRECIS_SANDBOX_HOSTS``.
_EXCLUDED_NODES: frozenset[str] = frozenset({"melchior", "melchior.local"})

#: This slice supports only the build lane and no precis DB access.
_SUPPORTED_MODE = "build"
_SUPPORTED_PRECIS_ACCESS = "none"


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The open-ended coding task handed to Claude in the container.
        "prompt": {"type": "string"},
        # Which sandbox host runs it — pins the claim to that node's
        # worker (params.target_node is the shared node-gate key).
        "target_node": {"type": "string"},
        # Hard wall-clock ceiling (seconds); sizes the lease + deadline.
        "wall_seconds": {"type": "integer"},
        # Container image tag (built in place per host by the ops play).
        "image": {"type": "string"},
        # Model override; unset → resolve_model(Tier.CLOUD_SUPER).
        "model": {"type": "string"},
        # Slice gates — declared so additionalProperties=False lets a
        # caller *pass* them, then validate_submit rejects the
        # not-yet-supported values fail-closed.
        "mode": {"type": "string"},
        "precis_access": {"type": "string"},
        "secrets": {"type": "array"},
    },
    "required": ["prompt", "target_node", "wall_seconds"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_docker"})

REQUIRES: frozenset[str] = frozenset({"podman", "claude_oauth"})

DESCRIPTION: str = (
    "Run an open-ended coding task inside a throwaway, cgroup-capped "
    "container (mode:build, precis_access:none) on an agent_sandbox_host "
    "and keep minimal forensics. Registered only under "
    "PRECIS_SANDBOX_ENABLED. See docs/design/sandbox-run.md."
)


# ── Config helpers ─────────────────────────────────────────────────


def _sandbox_hosts() -> frozenset[str]:
    """Allowlist of ``agent_sandbox_host`` node names.

    Read from ``PRECIS_SANDBOX_HOSTS`` (comma- or whitespace-separated).
    Empty when unset — ``validate_submit`` then fails closed (a job can't
    target a host we can't confirm is a sandbox).
    """
    raw = os.environ.get("PRECIS_SANDBOX_HOSTS", "")
    return frozenset(h.strip() for h in raw.replace(",", " ").split() if h.strip())


def resolve_sandbox_model() -> str:
    """Model for the container run.

    ``PRECIS_SANDBOX_MODEL`` override wins; otherwise the ADR 0046
    ``Tier.CLOUD_SUPER`` opus pin (``PRECIS_MODEL_OPUS`` /
    ``claude-opus-4-7`` default). Never a private constant.
    """
    return os.environ.get("PRECIS_SANDBOX_MODEL") or resolve_model(Tier.CLOUD_SUPER)


def default_image() -> str:
    """Default container image tag.

    ``PRECIS_SANDBOX_IMAGE`` override, else ``code-task:latest``. The ops
    play builds the image in place per host and tags it by git sha
    (``code-task:<sha>``); the default here is the movable ``latest`` tag
    it also stamps. The ``image`` param overrides per job.
    """
    return os.environ.get("PRECIS_SANDBOX_IMAGE") or "code-task:latest"


# ── Fail-closed submit / launch gate ───────────────────────────────


def semantic_rejection(params: dict[str, Any]) -> str | None:
    """Return a fail-closed rejection reason for ``params``, or ``None``.

    The single source of truth for the slice-1 gates, shared by
    ``validate_submit`` (put time) and the claude_docker executor
    (launch time). Rejects, in order: an unsupported ``mode``, any
    ``precis_access`` other than ``none``, a non-empty ``secrets`` list,
    a missing / melchior / non-allowlisted ``target_node``, and a
    non-positive ``wall_seconds``.
    """
    mode = params.get("mode", _SUPPORTED_MODE)
    if mode != _SUPPORTED_MODE:
        return (
            f"sandbox_run: mode:{mode!r} is not supported in slice 1 "
            f"(mode:{_SUPPORTED_MODE} only — mode:run is a later slice)"
        )
    precis_access = params.get("precis_access", _SUPPORTED_PRECIS_ACCESS)
    if precis_access != _SUPPORTED_PRECIS_ACCESS:
        return (
            f"sandbox_run: precis_access:{precis_access!r} is not supported "
            f"in slice 1 (precis_access:{_SUPPORTED_PRECIS_ACCESS} only — "
            "read access needs a read-only DB role + MCP endpoint)"
        )
    secrets = params.get("secrets") or []
    if secrets:
        return (
            "sandbox_run: params.secrets is not supported in slice 1 "
            "(task secrets are a later slice)"
        )
    target_node = params.get("target_node")
    if not target_node or not isinstance(target_node, str):
        return "sandbox_run: params.target_node is required (an agent_sandbox_host)"
    if target_node in _EXCLUDED_NODES:
        return (
            f"sandbox_run: target_node {target_node!r} is excluded — it holds "
            "the OAuth token / gateway / DB creds (an escape target), so it is "
            "never a sandbox host"
        )
    allowed = _sandbox_hosts()
    if not allowed:
        return (
            "sandbox_run: no PRECIS_SANDBOX_HOSTS configured, so no node can be "
            "confirmed an agent_sandbox_host (fail-closed)"
        )
    if target_node not in allowed:
        return (
            f"sandbox_run: target_node {target_node!r} is not an "
            f"agent_sandbox_host (allowed: {sorted(allowed)})"
        )
    wall = params.get("wall_seconds")
    if not isinstance(wall, int) or isinstance(wall, bool) or wall <= 0:
        return "sandbox_run: params.wall_seconds must be a positive integer"
    return None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Submit-time fail-closed gate. Returns an error string or ``None``.

    ``gripe_id`` is ignored — sandbox_run parents on a todo, not a gripe;
    it's kept for the registry's uniform ``validate_submit`` signature.
    The JobHandler surfaces a non-``None`` return as a ``BadInput`` at
    ``put(kind='job', ...)`` time.
    """
    del store, gripe_id
    reason = semantic_rejection(params)
    if reason is not None:
        return reason
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return (
            "sandbox_run: CLAUDE_CODE_OAUTH_TOKEN is not set in the daemon "
            "env. The container authenticates Claude via a dedicated "
            "long-lived OAuth token (Max) inherited through --env; without "
            "it the run can't authenticate. Set it on the sandbox host's "
            "worker daemon."
        )
    return None


# ── Prompt composition ─────────────────────────────────────────────


def compose_prompt(task: str) -> str:
    """Build the ``/work/PROMPT.md`` body: task + the harvest contract.

    The harvest contract describes the ``/work/out`` output lanes even
    though slice 1 discards ``out/`` (harvest is slice 2) — writing it
    now keeps the container-side convention stable across slices.
    """
    lines = [
        "# Coding task",
        "",
        "You are an autonomous engineer working inside a throwaway "
        "container with a real toolchain (uv, tests, network). You have "
        "no database access. Do the task below, then leave your work "
        "under `/work/out/`.",
        "",
        "## Task",
        "",
        task.strip(),
        "",
        "## Harvest contract (`/work/out/`)",
        "",
        "- `<code>` — the code you write (a folder tree).",
        "- `tests/` — tests that prove it (run them; green is the proof).",
        "- `pyproject.toml` + `uv.lock` — the dependency recipe.",
        "- `RUN.json` — `{cmd, inputs, outputs, image}` to re-run it.",
        "- `RESULT.md` — a short answer, if the task produced one.",
        "",
        "Env keys (your OAuth token) are passed via `--env`, never on "
        "`/work`. The `.venv` is scratch — it is never harvested "
        "(reconstructible from `uv.lock`).",
    ]
    return "\n".join(lines)


def _run_not_supported(**_kw: Any) -> None:  # pragma: no cover - guard
    """Placeholder ``run`` — sandbox_run is driven by the claude_docker
    executor's poll loop, which handles launch/poll/reap directly keyed
    on the job_type. ``spec.run`` is never invoked for it."""
    raise NotImplementedError(
        "sandbox_run is executed by the claude_docker poll loop, not spec.run"
    )


run = _run_not_supported


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "compose_prompt",
    "default_image",
    "resolve_sandbox_model",
    "run",
    "semantic_rejection",
    "validate_submit",
]
