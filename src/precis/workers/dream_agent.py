"""Dream-pass worker — `claude_agent` shape.

Replaces the bash `dream-pass.sh` script that lives in
`cluster/roles/precis_dream/files/`. Same dispatch payload (claude -p
with SOUL.md as system prompt + MCP precis config + bypass
permissions + WebFetch/WebSearch disabled), but lifted into the
unified :func:`precis.utils.claude_agent.call_claude_agent` so:

* cost / timeout / turn caps are uniform with the structural and
  deep reviewers,
* the helper's `log_event` hook attributes the run on
  ``ref_events`` (per-host telemetry),
* the cluster-side bash script collapses to a one-liner that just
  shells out to `precis worker --only dream_agent --once`.

Inputs (env, all required for a meaningful run):

* ``PRECIS_DREAM_PROMPT_PATH`` — file containing the directive
  prompt. The Ansible role installs it as
  ``/opt/asa/files/dream-prompt.md`` (or similar — operator's
  choice) alongside the worker.
* ``PRECIS_DREAM_SOUL_PATH`` — file containing the agent's system
  prompt (`--append-system-prompt`). For asa this is her SOUL.md.
* ``PRECIS_MCP_CONFIG`` — MCP config JSON the agent uses to call
  precis tools.

Gating: ``PRECIS_DREAM_AGENT=1`` (env). The pass is explicit-only
on the CLI (``--only dream_agent``) AND env-gated, mirroring the
existing dream worker's discipline.

Output disposition: **the dream agent writes its own memories**
via the precis MCP `put` tool during the session. The worker
itself does not write a digest — that would duplicate the agentic
side effects. Successful dispatch is logged; the audit text is
not stored as a separate memory.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from precis.store import Store
from precis.utils.claude_agent import (
    ClaudeAgentError,
    call_claude_agent,
)
from precis.utils.env import env_flag
from precis.utils.load_gate import skip_if_high_load
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


# Default model: sonnet matches the live dream-pass.sh setting
# (Max-plan Opus-4-7 was the original target but the cluster's
# bash script was running sonnet by the time of cutover; see
# cluster/roles/precis_dream/files/dream-pass.sh comments). Override
# with PRECIS_DREAM_AGENT_MODEL.
_DEFAULT_MODEL = "claude-sonnet-4-6"

# Same turn cap as the bash script's --max-turns 20.
_DEFAULT_MAX_TURNS = 20

# Same wall-clock window as structural/deep — agents that need
# longer can bump per call. The bash had no timeout; the helper's
# 10-min default is the conservative upgrade.
_DEFAULT_TIMEOUT_S = 600


def run_dream_pass(store: Store) -> BatchResult:
    """One dream cycle. Counters:

    * ``claimed`` = 1 if we ran the LLM, 0 if gated / mis-configured
    * ``ok`` = 1 on a clean dispatch (the agent's memory writes
      happen via MCP and aren't double-counted here)
    * ``failed`` = 1 if the helper raised :class:`ClaudeAgentError`
    """
    if not _gate_enabled():
        log.info("dream_agent: PRECIS_DREAM_AGENT not set; skipping")
        return BatchResult(handler="dream_agent", claimed=0, ok=0, failed=0)
    if skip_if_high_load("dream_agent"):
        return BatchResult(handler="dream_agent", claimed=0, ok=0, failed=0)
    prompt_path = _env_path("PRECIS_DREAM_PROMPT_PATH")
    soul_path = _env_path("PRECIS_DREAM_SOUL_PATH")
    mcp_path = _env_path("PRECIS_MCP_CONFIG")
    if prompt_path is None:
        log.error("dream_agent: PRECIS_DREAM_PROMPT_PATH unset / unreadable; skipping")
        return BatchResult(handler="dream_agent", claimed=0, ok=0, failed=0)
    prompt = prompt_path.read_text()
    try:
        result = call_claude_agent(
            prompt,
            model=os.environ.get("PRECIS_DREAM_AGENT_MODEL", _DEFAULT_MODEL),
            system_prompt=soul_path,
            mcp_config=mcp_path,
            max_turns=_DEFAULT_MAX_TURNS,
            timeout_s=_DEFAULT_TIMEOUT_S,
            # Dreams don't fan out to the open web — keep them on
            # corpus state. Same as the bash script's flag set.
            disallowed_tools=("WebFetch", "WebSearch"),
            # Stream-json gets us cost/turns from the result event
            # (call_claude_agent unwraps the assistant's text from
            # the ``result`` field so final_text is unchanged).
            output_format="stream-json",
            extra_args=("--verbose",),
        )
    except ClaudeAgentError as exc:
        log.exception("dream_agent: claude agent failed: %s", exc)
        return BatchResult(handler="dream_agent", claimed=1, ok=0, failed=1)
    log.info(
        "dream_agent: dispatch ok cost=$%.4f duration=%.1fs turns=%s final_text_len=%d",
        result.cost_usd or 0.0,
        result.duration_s,
        result.turns_used,
        len(result.final_text or ""),
    )
    _ = store  # reserved for future event-log writes
    return BatchResult(handler="dream_agent", claimed=1, ok=1, failed=0)


# ── helpers ────────────────────────────────────────────────────────


def _gate_enabled() -> bool:
    return env_flag("PRECIS_DREAM_AGENT")


def _env_path(var: str) -> Path | None:
    """Resolve env var → :class:`Path` if the file exists; else ``None``."""
    raw = os.environ.get(var)
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


__all__ = ["run_dream_pass"]
