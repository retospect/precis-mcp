"""``plan_tick`` job_type — one LLM tick of the planner coroutine.

The dispatcher mints a ``plan_tick`` job under every ``LLM:*``-tagged
todo that has no live job and no live open children. The job runs
opus (or sonnet / haiku per the tag) with the planner prompts from
:mod:`precis.workers.planner_prompt` and exits.

What the planner does during the tick is its own call (mint
children, yield to user, halt, or finish). The runner doesn't
interpret the output — it just shells out, captures stdout as a
``job_summary`` chunk under the job ref, and lets the dispatcher's
next sweep notice whatever state the planner set.

Closed vocab: ``meta.params`` carries ``model`` (one of
``opus|sonnet|haiku``) plus an optional ``timeout_s``. The model is
synthesized from the parent's ``LLM:<value>`` tag at dispatch time;
callers normally don't write ``params`` directly.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


DESCRIPTION: str = (
    "one LLM planner tick on an LLM:*-tagged todo — reads body + "
    "child summaries, mints children / yields / finishes"
)


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model": {
            "type": "string",
            "enum": ["opus", "sonnet", "haiku"],
            "description": "Which Claude tier to run. Synthesized from the "
                           "parent's LLM:<value> tag at dispatch time.",
        },
        "timeout_s": {
            "type": "integer",
            "minimum": 30,
            "maximum": 3600,
            "description": "Wall-clock cap on the tick. Default 600s.",
        },
    },
    "required": ["model"],
    "additionalProperties": False,
}


COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_inproc"})


#: ``plan_tick`` needs ``claude_bin`` (the CLI) and an
#: ``mcp_config`` (so the planner can call back via MCP). Everything
#: else is read-only.
REQUIRES: frozenset[str] = frozenset({"claude_bin", "mcp_config"})


@dataclass(frozen=True)
class PlanTickOutcome:
    """Result of one planner tick.

    ``stdout`` is captured for the ``job_summary`` chunk. ``exit_code``
    decides the job's STATUS (0 → succeeded, non-zero → failed).
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Submit-time check. Today: only validates the model value.

    ``gripe_id`` is ignored — plan_tick parents are todos, not gripes.
    Kept in the signature for the registry's uniform interface.
    """
    del gripe_id
    model = params.get("model")
    if model not in {"opus", "sonnet", "haiku"}:
        return (
            f"plan_tick: params.model must be one of "
            f"[opus, sonnet, haiku], got {model!r}"
        )
    return None


def run(
    *,
    store: Any,
    job_ref_id: int,
    parent_ref_id: int,
    params: dict[str, Any],
    log_event: Any = None,
    **_kw: Any,
) -> PlanTickOutcome:
    """Run one planner tick under ``parent_ref_id`` and return the outcome.

    The runner builds the prompts via
    :func:`precis.workers.planner_prompt.build_planner_prompts`, then
    shells out to ``claude -p`` with the appropriate ``--model`` /
    ``--append-system-prompt`` flags. The MCP config is forwarded so
    the planner can call back via the precis tools (put / tag / link /
    search / get).
    """
    from precis.workers.planner_prompt import build_planner_prompts

    model = params["model"]
    timeout_s = int(params.get("timeout_s", 600))
    started = time.monotonic()

    prompts = build_planner_prompts(store, ref_id=parent_ref_id, model=model)

    # Resolve the claude binary + MCP config from env. These are part
    # of the executor's REQUIRES set; the runner can assume they
    # exist or fail loudly.
    claude_bin = os.environ.get("PRECIS_CLAUDE_BIN", "claude")
    mcp_config = os.environ.get("PRECIS_MCP_CONFIG", "")
    if not mcp_config:
        log.warning(
            "plan_tick: PRECIS_MCP_CONFIG unset; planner won't be able to "
            "call back via MCP — children/yield/done won't land"
        )

    cmd: list[str] = [
        claude_bin,
        "-p",
        prompts.user,
        "--model",
        _model_alias(model),
        "--append-system-prompt",
        prompts.system,
        "--max-turns",
        "30",
        "--permission-mode",
        "acceptEdits",
    ]
    if mcp_config:
        cmd.extend(["--mcp-config", mcp_config, "--strict-mcp-config"])

    try:
        if log_event:
            log_event(
                "plan_tick.spawn",
                {
                    "job_ref_id": job_ref_id,
                    "parent_ref_id": parent_ref_id,
                    "model": model,
                    "system_chars": len(prompts.system),
                    "user_chars": len(prompts.user),
                },
            )
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        log.warning(
            "plan_tick: parent #%d timed out after %ds",
            parent_ref_id,
            timeout_s,
        )
        return PlanTickOutcome(
            exit_code=124,
            stdout=(exc.stdout or "").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=f"plan_tick: timeout after {timeout_s}s",
            duration_s=duration,
        )
    duration = time.monotonic() - started
    return PlanTickOutcome(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_s=duration,
    )


def _model_alias(model: str) -> str:
    """Translate the short LLM:<value> name to the real Claude model ID."""
    # Pinned model IDs so a ``LLM:opus`` tag binds to a specific
    # generation even as the CLI's default shifts. Override via env
    # (``PRECIS_MODEL_OPUS=…``) for testing or model migration.
    defaults = {
        "opus": os.environ.get("PRECIS_MODEL_OPUS", "claude-opus-4-7"),
        "sonnet": os.environ.get("PRECIS_MODEL_SONNET", "claude-sonnet-4-6"),
        "haiku": os.environ.get(
            "PRECIS_MODEL_HAIKU", "claude-haiku-4-5-20251001"
        ),
    }
    return defaults.get(model, model)
