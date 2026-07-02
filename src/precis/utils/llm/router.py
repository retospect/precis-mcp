"""The LLM routing seam — one place where model selection, transport
choice, and result normalization live (ADR 0046).

Before this module, model selection was scattered across ~a-dozen
independent ``os.environ.get(...)`` reads, three different transports
(``claude_agent`` multi-turn agent, ``claude_p`` one-shot JSON judge,
the litellm ``LlmClient`` local completion) each with its own result
shape, and three rogue subprocess sites. This module is the **seam**
that a follow-up unit (4b) folds those call sites through; it does not
rewire them itself.

Three pieces:

* :func:`resolve_model` — the single tier→model table. It reads the
  *existing* env vars / defaults so a migrated caller resolves to the
  byte-for-byte model it uses today (ADR 0046 §"Resolver").
* :func:`select_transport` + :func:`dispatch` — the routing seam. Given
  a :class:`LlmRequest` (prompt/messages + tier + tools-needed + budget
  + timeout), pick the transport and *wrap* the existing helper — never
  reimplement it.
* :class:`LlmResult` + the ``result_from_*`` adapters — one normalized
  result shape unifying the JSON-block / stream-json result-event /
  OpenAI-choices outputs.

The :class:`Tier` vocabulary aligns with the prompt-assembler
:class:`~precis.utils.prompt.model.Profile`: a ``HELPER`` (tool-less,
one-shot, structured) profile rides the ``cloud-small`` / ``local-small``
tiers on the ``claude_p`` / litellm transports; an ``AGENT`` (tools,
multi-turn) profile rides ``cloud-mid`` / ``cloud-super`` (and,
eventually, ``local-big``) on the ``claude_agent`` transport.

**Local-big + MCP tools is deliberately unimplemented here** — the
:data:`Transport.LOCAL_BIG_TOOLS` branch is the documented extension
point where the abandoned in-process litellm-with-``tools=`` wire
(ADR 0024) plugs back in as the next step (ADR 0046 §"Next step").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from precis.utils._claude_subprocess import ClaudeProcessError
from precis.utils.claude_agent import AgentResult, call_claude_agent
from precis.utils.claude_p import ClaudePResult, call_claude_p

if TYPE_CHECKING:
    from precis.utils.prompt.model import Profile


class Tier(StrEnum):
    """Capability tier — *what* the task needs, not *which* model.

    A tier bundles a capability level with a tool-use expectation, and
    maps onto both a concrete model (via :func:`resolve_model`) and a
    transport (via :func:`select_transport`).

    * ``LOCAL_SMALL`` — tool-less local completion on the loopback
      litellm proxy (the ``summarizer`` alias). The cheapest rung; the
      per-chunk gloss lives here.
    * ``LOCAL_BIG`` — a local model *with* MCP tools. **Not yet wired**
      (see :data:`Transport.LOCAL_BIG_TOOLS` and ADR 0024/0046); the
      resolver names its model so the seam is ready.
    * ``CLOUD_SMALL`` — cloud haiku, tool-less one-shot JSON judgment
      (the chase verifier shape).
    * ``CLOUD_MID`` — cloud sonnet, the agentic default (dream,
      planner ticks, tex-fix).
    * ``CLOUD_SUPER`` — cloud opus, heavy reasoning + tools (the
      structural / deep reviewers, fix-gripe, ``LLM:opus`` ticks).
    """

    LOCAL_SMALL = "local-small"
    LOCAL_BIG = "local-big"
    CLOUD_SMALL = "cloud-small"
    CLOUD_MID = "cloud-mid"
    CLOUD_SUPER = "cloud-super"


class Transport(StrEnum):
    """Which wrapper carries a request.

    * ``CLAUDE_AGENT`` — :func:`precis.utils.claude_agent.call_claude_agent`
      (multi-turn, MCP tools, stream-json result event).
    * ``CLAUDE_P`` — :func:`precis.utils.claude_p.call_claude_p`
      (one-shot, no tools, last-JSON-block parse).
    * ``LITELLM`` — the loopback litellm ``LlmClient`` (OpenAI
      ``/v1/chat/completions``, tool-less local completion).
    * ``LOCAL_BIG_TOOLS`` — **not implemented**; the extension point for
      a local model driving MCP tools over the OpenAI ``tools=`` wire
      (ADR 0024 prototyped-then-reversed; ADR 0046 §"Next step").
    """

    CLAUDE_AGENT = "claude_agent"
    CLAUDE_P = "claude_p"
    LITELLM = "litellm"
    LOCAL_BIG_TOOLS = "local_big_tools"


# ── the tier → model table (the ONE consolidation point) ───────────────
#
# Each row is ``tier: (env_var, default)`` and reproduces the default a
# current call site would resolve to, so unit 4b's migration is
# behavior-preserving. The cloud triad is the *pinned* set from
# ``plan_tick._model_alias`` — ``PRECIS_MODEL_{OPUS,SONNET,HAIKU}`` — which
# is the most deliberate of the scattered reads (it pins a model *id* so a
# ``LLM:opus`` tag binds to one generation as the CLI default drifts). The
# sonnet/opus defaults are shared verbatim by every other cloud site
# (dream, tex-fix, reviewers, fix-gripe); the one reconciliation is
# ``claude_p``'s legacy suffix-less ``claude-haiku-4-5`` default, folded
# onto the dated pin here (same family — see ADR 0046 §"Resolver").
_TIER_MODEL: dict[Tier, tuple[str, str]] = {
    Tier.CLOUD_SUPER: ("PRECIS_MODEL_OPUS", "claude-opus-4-7"),
    Tier.CLOUD_MID: ("PRECIS_MODEL_SONNET", "claude-sonnet-4-6"),
    Tier.CLOUD_SMALL: ("PRECIS_MODEL_HAIKU", "claude-haiku-4-5-20251001"),
    # The litellm ``summarizer`` alias (``LlmConfig.model`` default), read
    # from ``PRECIS_SUMMARIZE_MODEL`` exactly as ``LlmConfig.from_env``.
    Tier.LOCAL_SMALL: ("PRECIS_SUMMARIZE_MODEL", "summarizer"),
    # The future local-big alias — ADR 0024's dream model. Resolvable now
    # (so the seam is complete) but not yet dispatchable (see below).
    Tier.LOCAL_BIG: ("PRECIS_LOCAL_BIG_MODEL", "qwen-heavy"),
}

# Import-time totality guard: every Tier must have a resolver row, so
# adding a tier without a model is a load-time failure, not a KeyError
# at dispatch (mirrors the TodoView totality assert in handlers/todo.py).
assert set(_TIER_MODEL) == set(Tier), "resolve_model: tier table is not total"


def resolve_model(tier: Tier) -> str:
    """The concrete model id for ``tier`` — the ONE place model
    selection lives.

    Reads the same env var (with the same default) a current call site
    reads, so a migrated caller resolves byte-for-byte to the model it
    uses today. See :data:`_TIER_MODEL` for the table.
    """
    env_var, default = _TIER_MODEL[tier]
    return os.environ.get(env_var, default)


# ── transport selection ────────────────────────────────────────────────


def select_transport(tier: Tier, *, tools_needed: bool) -> Transport:
    """Pick the transport for ``(tier, tools_needed)`` — a pure function.

    Local tiers route to their local transport regardless of tools
    (``LOCAL_SMALL`` is tool-less by construction; ``LOCAL_BIG`` is the
    tools-capable local rung). Cloud tiers split on ``tools_needed``,
    which mirrors the ``AGENT`` vs ``HELPER``
    :class:`~precis.utils.prompt.model.Profile` split: tools ⇒
    ``claude_agent`` (AGENT), no tools ⇒ ``claude_p`` (HELPER).
    """
    if tier is Tier.LOCAL_SMALL:
        return Transport.LITELLM
    if tier is Tier.LOCAL_BIG:
        return Transport.LOCAL_BIG_TOOLS
    return Transport.CLAUDE_AGENT if tools_needed else Transport.CLAUDE_P


def transport_for_profile(profile: Profile, tier: Tier) -> Transport:
    """Convenience bridge from a prompt-assembler
    :class:`~precis.utils.prompt.model.Profile` to a transport.

    An ``AGENT`` profile needs tools; a ``HELPER`` profile does not — so
    this is :func:`select_transport` with ``tools_needed`` derived from
    the profile. Kept thin so the profile→router alignment is explicit
    (ADR 0046 §"Alignment with Profile").
    """
    from precis.utils.prompt.model import Profile as _Profile

    return select_transport(tier, tools_needed=profile is _Profile.AGENT)


# ── the normalized result ──────────────────────────────────────────────


class _HasText(Protocol):
    """Duck type for the litellm ``LlmClient.complete`` result.

    Matches :class:`precis.workers.llm_summarize.LlmResult` (``.text`` +
    ``.total_tokens``) without importing it — keeps this module free of
    the worker/DB import chain and lets tests pass a plain fake.
    """

    text: str


@dataclass(frozen=True, slots=True)
class LlmResult:
    """One normalized outcome across all three transports.

    * ``text`` — the assistant's final text. For ``claude_p`` this is the
      raw stdout (the JSON block lives inside it); for ``claude_agent``
      it is the stream-json result text; for litellm it is the OpenAI
      choice content.
    * ``cost_usd`` — best-effort USD cost (``None`` when the transport
      doesn't report one, e.g. the local litellm proxy).
    * ``turns_used`` — agent turn count (``None`` for the one-shot
      transports).
    * ``model`` / ``tier`` — what actually ran, for attribution.
    * ``error`` — ``None`` on success; a message on a caught transport
      failure (see :func:`dispatch`).
    """

    text: str
    cost_usd: float | None
    turns_used: int | None
    model: str
    tier: Tier
    error: str | None = None


def result_from_agent(res: AgentResult, *, model: str, tier: Tier) -> LlmResult:
    """Normalize a :class:`~precis.utils.claude_agent.AgentResult`."""
    return LlmResult(
        text=res.final_text,
        cost_usd=res.cost_usd,
        turns_used=res.turns_used,
        model=model,
        tier=tier,
    )


def result_from_claude_p(res: ClaudePResult, *, model: str, tier: Tier) -> LlmResult:
    """Normalize a :class:`~precis.utils.claude_p.ClaudePResult`.

    ``text`` is the raw stdout (the parsed dict stays reachable on the
    original ``res.data`` for callers that still want it in 4b).
    """
    return LlmResult(
        text=res.raw_stdout,
        cost_usd=res.cost_usd,
        turns_used=None,
        model=model,
        tier=tier,
    )


def result_from_openai(res: _HasText, *, model: str, tier: Tier) -> LlmResult:
    """Normalize a litellm ``LlmClient.complete`` result (OpenAI choices).

    The loopback proxy reports token counts, not a dollar cost, so
    ``cost_usd`` is ``None``.
    """
    return LlmResult(
        text=res.text,
        cost_usd=None,
        turns_used=None,
        model=model,
        tier=tier,
    )


# ── the request + dispatch seam ────────────────────────────────────────


@dataclass
class LlmRequest:
    """One routed LLM call. ``tier`` + ``tools_needed`` pick the
    transport; the rest are pass-through knobs for the chosen wrapper.

    ``prompt`` feeds the ``claude_*`` transports (and, when ``messages``
    is unset, the local transport as a single user turn); ``messages``
    is the OpenAI-shaped alternative for the local transport. ``model``
    overrides :func:`resolve_model` when a caller pins one explicitly.
    """

    tier: Tier
    prompt: str = ""
    messages: list[dict[str, str]] | None = None
    tools_needed: bool = False
    model: str | None = None
    max_usd: float | None = None
    timeout_s: float | None = None
    # claude_agent pass-through knobs (ignored by the other transports).
    system_prompt: str | Path | None = None
    mcp_config: str | Path | None = None
    max_turns: int = 20
    output_format: str = "text"
    # Extra CLI flags forwarded to the claude_* transports.
    extra_args: tuple[str, ...] = field(default_factory=tuple)


def dispatch(req: LlmRequest) -> LlmResult:
    """Route ``req`` to the right transport and return a normalized
    :class:`LlmResult`.

    Wraps the existing helpers — it never reimplements them. A caught
    :class:`~precis.utils._claude_subprocess.ClaudeProcessError` (or a
    local-transport ``RuntimeError``) is folded into
    :attr:`LlmResult.error` rather than raised, so every dispatch path
    returns one shape. Programming errors (an unwired tier) still raise.
    """
    transport = select_transport(req.tier, tools_needed=req.tools_needed)
    model = req.model or resolve_model(req.tier)

    if transport is Transport.CLAUDE_AGENT:
        try:
            res = call_claude_agent(
                req.prompt,
                model=model,
                system_prompt=req.system_prompt,
                mcp_config=req.mcp_config,
                max_turns=req.max_turns,
                timeout_s=req.timeout_s,
                max_usd=req.max_usd,
                output_format=req.output_format,
                extra_args=req.extra_args,
            )
        except ClaudeProcessError as exc:
            return _error_result(exc, model=model, tier=req.tier)
        return result_from_agent(res, model=model, tier=req.tier)

    if transport is Transport.CLAUDE_P:
        try:
            pres = call_claude_p(
                req.prompt,
                model=model,
                max_usd=req.max_usd,
                timeout_s=req.timeout_s,
                extra_args=req.extra_args,
            )
        except ClaudeProcessError as exc:
            return _error_result(exc, model=model, tier=req.tier)
        return result_from_claude_p(pres, model=model, tier=req.tier)

    if transport is Transport.LITELLM:
        return _dispatch_local(req, model)

    # Transport.LOCAL_BIG_TOOLS — the documented extension point.
    #
    # A local model (ADR 0024's ``qwen-heavy``) driving the precis MCP
    # tools over the OpenAI ``tools=`` wire. ADR 0024 prototyped this
    # in-process and then reversed it onto the ``claude`` binary; ADR 0046
    # §"Next step" scopes wiring it back HERE as the follow-up — a local
    # OpenAI client with ``tools=`` populated from the MCP config plus a
    # tool-call loop, normalized into :class:`LlmResult` like the rest.
    # Deliberately unimplemented in this unit.
    raise NotImplementedError(
        "local-big + MCP tools is not wired yet — the ADR 0024/0046 "
        "extension point. Route tool-using work through a cloud tier for now."
    )


def _dispatch_local(req: LlmRequest, model: str) -> LlmResult:
    """Drive the loopback litellm ``LlmClient`` for a local tier.

    Imports the summarizer client lazily so this module stays out of the
    worker/DB import chain (and so DB-free callers/tests never trigger
    it). Reuses ``LlmConfig.from_env`` and overrides only the model +
    ``enabled`` flag so the resolved tier model wins.
    """
    from dataclasses import replace

    from precis.workers.llm_summarize import LlmClient, LlmConfig

    cfg = replace(LlmConfig.from_env(), model=model, enabled=True)
    messages = req.messages or [{"role": "user", "content": req.prompt}]
    client = LlmClient(cfg)
    try:
        res = client.complete(messages)
    except (RuntimeError, OSError) as exc:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=str(exc),
        )
    return result_from_openai(res, model=model, tier=req.tier)


def _error_result(exc: ClaudeProcessError, *, model: str, tier: Tier) -> LlmResult:
    """Fold a transport failure into a normalized error result.

    Surfaces any partial stdout the wrapper captured as ``text`` so a
    caller keeps a recoverable-exhaustion answer while still seeing the
    ``error``.
    """
    return LlmResult(
        text=getattr(exc, "stdout", "") or "",
        cost_usd=None,
        turns_used=None,
        model=model,
        tier=tier,
        error=str(exc),
    )


__all__ = [
    "AgentResult",
    "ClaudePResult",
    "LlmRequest",
    "LlmResult",
    "Tier",
    "Transport",
    "dispatch",
    "resolve_model",
    "result_from_agent",
    "result_from_claude_p",
    "result_from_openai",
    "select_transport",
    "transport_for_profile",
]
