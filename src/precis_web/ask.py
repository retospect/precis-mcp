"""Follow-up "ask a question about this thought" dispatch.

The Refs detail pages (memory / dream / any browsable kind) carry a
textbox + button: type a question, the server "thinks" about it, and
the answer is stored as a turn in a ``conv`` thread linked back to the
source ref (chunk-scoped when the question was asked on a chunk).

This module is the *thinking* half — it reuses the dreaming
infrastructure (:func:`precis.utils.claude_agent.call_claude_agent`,
the same SOUL system prompt + MCP precis config the dream pass runs
with) so a follow-up reasons over the corpus the same way a dream
does. The conv-thread writes + the link back to the source go through
the normal ``put`` / ``link`` verbs in the route, so all DB mutation
and link management stays single-sourced with MCP.

Config (env, all optional — absence degrades to plain reasoning over
the supplied source text):

* ``PRECIS_FOLLOWUP_MODEL`` — model override; falls back to
  ``PRECIS_DREAM_AGENT_MODEL`` then sonnet.
* ``PRECIS_DREAM_SOUL_PATH`` — system prompt file (shared with the
  dream pass — the agent answers in the same voice).
* ``PRECIS_MCP_CONFIG`` — MCP config JSON; when present the agent can
  call precis tools (search / get) to ground the answer in the corpus.
* ``PRECIS_FOLLOWUP_TIMEOUT_S`` — wall-clock cap (default 600).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from precis.utils.claude_agent import AgentResult, ClaudeAgentError
from precis.utils.llm.router import LlmRequest, Tier, dispatch

if TYPE_CHECKING:
    from precis.store import Store

#: Fallback author slug for the human question turn. The route stamps
#: the configured ``WebConfig.owner`` (``PRECIS_OWNER``) instead; this
#: generic default applies only if a caller has no config in hand.
ASKER = "owner"
#: Author slug stamped on the generated answer turn (asa's voice — the
#: SOUL.md the dream pass injects belongs to asa).
ANSWERER = "asa"

_DEFAULT_TIMEOUT_S = 600.0


@dataclass(frozen=True, slots=True)
class _Config:
    model: str | None
    soul_path: Path | None
    mcp_path: Path | None
    timeout_s: float


def _env_path(var: str) -> Path | None:
    """Resolve an env var to a :class:`Path` if the file exists."""
    raw = os.environ.get(var)
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


def _resolve_config() -> _Config:
    model = os.environ.get("PRECIS_FOLLOWUP_MODEL") or os.environ.get(
        "PRECIS_DREAM_AGENT_MODEL"
    )
    raw_timeout = os.environ.get("PRECIS_FOLLOWUP_TIMEOUT_S")
    timeout_s = float(raw_timeout) if raw_timeout else _DEFAULT_TIMEOUT_S
    return _Config(
        model=model,
        soul_path=_env_path("PRECIS_DREAM_SOUL_PATH"),
        mcp_path=_env_path("PRECIS_MCP_CONFIG"),
        timeout_s=timeout_s,
    )


def followup_slug(kind: str, ref_id: int, chunk_pos: int | None) -> str:
    """Stable conv slug for the discussion about one source (or chunk).

    One thread per (source[, chunk]) so repeat asks accumulate into a
    single transcript rather than spawning a conv per question.
    """
    base = f"followup/{kind}/{ref_id}"
    return f"{base}/c{chunk_pos}" if chunk_pos is not None else base


def source_handle(kind: str, ref_id: int, chunk_pos: int | None) -> str:
    """Link-target handle for the source, chunk-scoped when applicable."""
    return f"{kind}:{ref_id}" + (f"~{chunk_pos}" if chunk_pos is not None else "")


def build_prompt(
    *,
    source_kind: str,
    source_handle_str: str,
    source_title: str,
    source_body: str,
    focus_text: str | None,
    prior_turns: list[tuple[str, str]],
    question: str,
) -> str:
    """Assemble the directive prompt for one follow-up answer.

    Includes the source thought, the chunk-in-focus (if the question
    was asked on a chunk), the discussion so far (so a follow-up has
    context), and the new question.
    """
    parts: list[str] = [
        "You are continuing a reflective discussion about a stored "
        f"{source_kind} in the precis corpus (handle: {source_handle_str}).",
        "",
        f"# Source thought — {source_title}".rstrip(),
        source_body.strip() or "(no body text)",
    ]
    if focus_text:
        parts += [
            "",
            "# The reader is asking specifically about this passage",
            focus_text.strip(),
        ]
    if prior_turns:
        parts += ["", "# Discussion so far"]
        for author, text in prior_turns:
            parts.append(f"**{author}:** {text.strip()}")
    parts += [
        "",
        "# New question",
        question.strip(),
        "",
        "Answer the question directly and concisely, grounded in the "
        "source thought above. If precis tools are available, you may "
        "search the corpus for supporting context, but keep the answer "
        "self-contained — it will be stored verbatim as your reply in "
        "the discussion thread. Do not preface with 'Sure' or restate "
        "the question.",
    ]
    return "\n".join(parts)


def generate_answer(prompt: str, *, store: Store, conv_ref_id: int) -> AgentResult:
    """Run the agentic follow-up and return the result (blocking).

    Call from a worker thread (``asyncio.to_thread``) — the underlying
    ``claude -p`` subprocess can take tens of seconds. Reuses the dream
    pass's flag set (SOUL system prompt, MCP config, web tools disabled,
    stream-json for cost/turn accounting). ``log_event`` attributes the
    run on the conv ref's ``ref_events`` for per-host telemetry.
    """
    cfg = _resolve_config()
    # Routed through the LLM seam (ADR 0046 unit 4b) so PRECIS_LLM_BACKEND can
    # switch the follow-up onto an OSS model. The AgentResult-returning /
    # ClaudeAgentError-raising contract is preserved so the route is untouched:
    # dispatch folds failures into res.error, which we re-raise.
    res = dispatch(
        LlmRequest(
            tier=Tier.CLOUD_SUPER,
            source="followup",
            prompt=prompt,
            tools_needed=True,
            model=cfg.model,
            system_prompt=cfg.soul_path,
            mcp_config=cfg.mcp_path,
            timeout_s=cfg.timeout_s,
            # Same as dreams: stay on corpus state, don't fan out to the web.
            disallowed_tools=("WebFetch", "WebSearch"),
            output_format="stream-json",
            extra_args=("--verbose",),
            log_event=(store, conv_ref_id, "followup"),
        )
    )
    if res.error:
        raise ClaudeAgentError(res.error, stdout=res.text)
    return AgentResult(
        final_text=res.text,
        cost_usd=res.cost_usd,
        duration_s=res.duration_s or 0.0,
        turns_used=res.turns_used,
    )


__all__ = [
    "ANSWERER",
    "ASKER",
    "build_prompt",
    "followup_slug",
    "generate_answer",
    "source_handle",
]
