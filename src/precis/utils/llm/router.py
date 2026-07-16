"""The LLM routing seam — one place where model selection, transport
choice, and result normalization live (ADR 0046).

Before this module, model selection was scattered across ~a-dozen
independent ``os.environ.get(...)`` reads, three different transports
(``claude_agent`` multi-turn agent, ``claude_p`` one-shot JSON judge,
the litellm ``LlmClient`` local completion) each with its own result
shape, and three rogue subprocess sites. This module is the **seam**
that a follow-up unit (4b) folds those call sites through; it does not
rewire them itself.

Four pieces:

* :func:`resolve_model` — the single tier→model table. It reads the
  *existing* env vars / defaults so a migrated caller resolves to the
  byte-for-byte model it uses today (ADR 0046 §"Resolver").
* :func:`select_transport` — the pure (tier, tools) → transport choice.
* :class:`LlmProvider` + the adapter classes + :func:`dispatch` — the
  **port**. Every backend implements one narrow ``run(req, *, model)``
  method returning a normalized :class:`LlmResult`; :func:`dispatch`
  just resolves the model, picks the provider from a
  :data:`Transport`-keyed registry, and calls it. This is the seam that
  makes the router *switchable*: a new backend (an OpenAI-compatible OSS
  model, a failover ladder) is a new provider class + a registry row,
  with **zero caller changes** — the LLM-independence goal. Each adapter
  *wraps* the existing helper; it never reimplements it.
* :class:`LlmResult` + the ``result_from_*`` adapters — one normalized
  result shape unifying the JSON-block / stream-json result-event /
  OpenAI-choices outputs.

The :class:`Tier` vocabulary aligns with the prompt-assembler
:class:`~precis.utils.prompt.model.Profile`: a ``HELPER`` (tool-less,
one-shot, structured) profile rides the ``cloud-small`` / ``local-small``
tiers on the ``claude_p`` / litellm transports; an ``AGENT`` (tools,
multi-turn) profile rides ``cloud-mid`` / ``cloud-super`` (and,
eventually, ``local-big``) on the ``claude_agent`` transport.

**OSS tool-calling lands on** :data:`Transport.OPENAI_TOOLS` — an
open-source model driving the precis verbs over the OpenAI ``tools=``
wire (:class:`OpenAIToolsProvider`), the ADR 0024 loop rebuilt behind
the provider port. It serves the ``LOCAL_BIG`` tier and, when
``PRECIS_LLM_BACKEND=openai``, the tool-using cloud tiers.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from precis.utils._claude_subprocess import ClaudeProcessError
from precis.utils.claude_agent import AgentResult, call_claude_agent
from precis.utils.claude_p import ClaudePResult, call_claude_p

if TYPE_CHECKING:
    from precis.utils.prompt.model import Profile

log = logging.getLogger(__name__)


class Tier(StrEnum):
    """Capability tier — *what* the task needs, not *which* model.

    A tier bundles a capability level with a tool-use expectation, and
    maps onto both a concrete model (via :func:`resolve_model`) and a
    transport (via :func:`select_transport`).

    * ``LOCAL_SMALL`` — tool-less local completion on the loopback
      litellm proxy (the ``summarizer`` alias). The cheapest rung; the
      per-chunk gloss lives here.
    * ``LOCAL_BIG`` — a local model *with* tools, over the OpenAI
      ``tools=`` loop (:data:`Transport.OPENAI_TOOLS`); the resolver
      names its model (``qwen-heavy``).
    * ``CLOUD_SMALL`` — cloud haiku, tool-less one-shot JSON judgment
      (the chase verifier shape).
    * ``CLOUD_MID`` — cloud sonnet, the mid agentic rung (planner
      ticks, tex-fix).
    * ``CLOUD_SUPER`` — cloud opus-4.8, the consolidated cloud
      reasoning tier: heavy reasoning + tools (the structural / deep
      reviewers, fix-gripe, ``LLM:opus`` ticks, the dream pass, and
      the generic ``claude_agent`` default).
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
    * ``OPENAI_COMPAT`` — the same OpenAI ``/v1/chat/completions`` wire
      pointed at a *hosted* OSS backend (OpenRouter / DeepInfra / a
      remote vLLM), authed with a vault-resolved key. Tool-less (the
      one-shot / completion path); tool-using calls go to ``OPENAI_TOOLS``.
    * ``OPENAI_TOOLS`` — an OSS model driving the precis verbs over the
      OpenAI ``tools=`` wire, in-process (:mod:`precis.utils.llm.openai_tools`
      + :mod:`precis.utils.llm.precis_tools`). Serves both the ``LOCAL_BIG``
      tier (a local model + tools) and the ``OPENAI`` backend's tool-using
      cloud calls — same wire, different base url. Implements the ADR 0024
      loop that was prototyped-then-reversed onto ``claude`` (ADR 0046
      §"Next step").
    """

    CLAUDE_AGENT = "claude_agent"
    CLAUDE_P = "claude_p"
    LITELLM = "litellm"
    OPENAI_COMPAT = "openai_compat"
    OPENAI_TOOLS = "openai_tools"


class Backend(StrEnum):
    """Which vendor family a cloud request is routed to — the switch that
    delivers LLM independence.

    Resolved once per :func:`dispatch` from ``PRECIS_LLM_BACKEND`` (see
    :func:`resolve_backend`) and passed into :func:`select_transport`.
    Default ``ANTHROPIC`` keeps the ``claude -p`` transports, so the
    OpenAI-compatible path **ships dark** — it engages only when a
    deployment opts in *and* points ``PRECIS_LLM_BASE_URL`` at a backend.
    ``OPENAI`` routes tool-less cloud calls to :data:`Transport.OPENAI_COMPAT`
    and tool-using cloud calls to :data:`Transport.OPENAI_TOOLS` (the
    in-process ``tools=`` loop).
    """

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


# ── the tier → model table (the ONE consolidation point) ───────────────
#
# Each row is ``tier: (env_var, default)``. The cloud triad is the *pinned*
# set from ``plan_tick._model_alias`` — ``PRECIS_MODEL_{OPUS,SONNET,HAIKU}`` —
# which is the most deliberate of the scattered reads (it pins a model *id*
# so a ``LLM:opus`` tag binds to one generation as the CLI default drifts).
# The cloud-super default is ``claude-opus-4-8`` — the consolidation point
# for the whole cloud reasoning tier (dream, tex-fix, reviewers, fix-gripe,
# the generic ``claude_agent`` default all resolve through here). 4-7 and
# 4-8 are the same price, so there is no cost reason to stay on 4-7 and the
# reasoning/agentic work is exactly where the stronger model earns its keep.
# ``claude_p``'s legacy suffix-less ``claude-haiku-4-5`` default is folded
# onto the dated pin here (same family — see ADR 0046 §"Resolver").
_TIER_MODEL: dict[Tier, tuple[str, str]] = {
    Tier.CLOUD_SUPER: ("PRECIS_MODEL_OPUS", "claude-opus-4-8"),
    Tier.CLOUD_MID: ("PRECIS_MODEL_SONNET", "claude-sonnet-5"),
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


def resolve_backend() -> Backend:
    """The cloud backend family for this process — the LLM-independence switch.

    Reads ``PRECIS_LLM_BACKEND`` (default ``anthropic``); an unknown value
    degrades to ``anthropic`` so a typo can't dark a deployment. The
    OpenAI-compatible path additionally needs ``PRECIS_LLM_BASE_URL`` set
    (checked at dispatch); with the backend on but no base url, cloud
    calls fall back to ``claude`` rather than hit a phantom endpoint.
    """
    raw = os.environ.get("PRECIS_LLM_BACKEND", Backend.ANTHROPIC).strip().lower()
    return Backend.OPENAI if raw == Backend.OPENAI else Backend.ANTHROPIC


def select_transport(
    tier: Tier, *, tools_needed: bool, backend: Backend = Backend.ANTHROPIC
) -> Transport:
    """Pick the transport for ``(tier, tools_needed, backend)`` — a pure function.

    Local tiers route to their local transport regardless of tools
    (``LOCAL_SMALL`` is tool-less by construction; ``LOCAL_BIG`` is the
    tools-capable local rung). Cloud tiers split on ``tools_needed``,
    which mirrors the ``AGENT`` vs ``HELPER``
    :class:`~precis.utils.prompt.model.Profile` split: tools ⇒
    ``claude_agent`` (AGENT), no tools ⇒ ``claude_p`` (HELPER).

    ``backend`` (default ``ANTHROPIC``, so existing callers are unchanged)
    routes cloud work to the OSS path when ``OPENAI``: tool-less →
    :data:`Transport.OPENAI_COMPAT`, tool-using → :data:`Transport.OPENAI_TOOLS`
    (the in-process ``tools=`` loop). Under ``ANTHROPIC`` both stay on the
    ``claude`` transports. The ``LOCAL_BIG`` tier (a local model + tools)
    always takes the OSS tools loop.
    """
    if tier is Tier.LOCAL_SMALL:
        return Transport.LITELLM
    if tier is Tier.LOCAL_BIG:
        return Transport.OPENAI_TOOLS
    if tools_needed:
        return (
            Transport.OPENAI_TOOLS
            if backend is Backend.OPENAI
            else Transport.CLAUDE_AGENT
        )
    if backend is Backend.OPENAI:
        return Transport.OPENAI_COMPAT
    return Transport.CLAUDE_P


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
    * ``duration_s`` — agent wall-clock (``None`` for the one-shot /
      local transports); read by dream + review telemetry.
    * ``data`` — the parsed JSON dict for the ``claude_p`` judge path
      (``None`` otherwise). Preserves the ``ClaudePResult.data`` a judge
      caller reads without re-parsing ``text``.
    * ``model`` / ``tier`` — what actually ran, for attribution.
    * ``error`` — ``None`` on success; a message on a caught transport
      failure (see :func:`dispatch`).
    * ``paused`` — ``True`` when ``error`` is a *window-scoped breaker trip*
      (the daily/hourly dollar cap or the claude-OAuth quota snapshot), not a
      genuine transport failure. A pinned pass reads this to **skip** (a no-op
      that clears when the window rolls off) instead of recording a failure and
      re-attempting every cycle — the spin that flooded the FAILED-PASSES panel
      with 100k+ structural "failures" while the budget was capped.
    """

    text: str
    cost_usd: float | None
    turns_used: int | None
    model: str
    tier: Tier
    error: str | None = None
    duration_s: float | None = None
    data: dict[str, Any] | None = None
    paused: bool = False


def result_from_agent(res: AgentResult, *, model: str, tier: Tier) -> LlmResult:
    """Normalize a :class:`~precis.utils.claude_agent.AgentResult`."""
    return LlmResult(
        text=res.final_text,
        cost_usd=res.cost_usd,
        turns_used=res.turns_used,
        model=model,
        tier=tier,
        duration_s=res.duration_s,
    )


def result_from_claude_p(res: ClaudePResult, *, model: str, tier: Tier) -> LlmResult:
    """Normalize a :class:`~precis.utils.claude_p.ClaudePResult`.

    ``text`` is the raw stdout; ``data`` carries the parsed JSON dict so a
    judge caller reads ``LlmResult.data`` exactly as it read ``ClaudePResult.data``.
    """
    return LlmResult(
        text=res.raw_stdout,
        cost_usd=res.cost_usd,
        turns_used=None,
        model=model,
        tier=tier,
        data=res.data,
    )


def result_from_openai(res: _HasText, *, model: str, tier: Tier) -> LlmResult:
    """Normalize a litellm ``LlmClient.complete`` result (OpenAI choices).

    Cost: prefer a provider-returned dollar figure (``res.cost_usd`` — set from
    OpenRouter's ``usage.cost``); otherwise price the token split via the
    per-model table in :mod:`precis.budget.pricing` (``None`` for a local /
    unknown model, which the cost bands read as free). Either way the OSS /
    OpenRouter spend shows up in the tote instead of vanishing.

    Data: the ``claude_p`` judges (chase verify, good_search triage, figure)
    route through ``dispatch`` and read ``LlmResult.data``. So parse the
    trailing JSON block out of the text here too — the same
    :func:`~precis.utils.claude_p._parse_last_json_block` the claude path uses —
    so an OSS judge reaches parity instead of silently degrading to its
    fallback (gripe 159758).

    All fields are read leniently (``getattr``) so a bare ``.text`` fake still
    normalizes.
    """
    from precis.budget.pricing import cost_from_tokens
    from precis.utils.claude_p import _parse_last_json_block

    cost = getattr(res, "cost_usd", None)
    if cost is None:
        cost = cost_from_tokens(
            model,
            prompt_tokens=getattr(res, "prompt_tokens", None),
            completion_tokens=getattr(res, "completion_tokens", None),
        )
    return LlmResult(
        text=res.text,
        cost_usd=cost,
        turns_used=None,
        model=model,
        tier=tier,
        data=_parse_last_json_block(res.text),
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
    #: Caller label ("dream", "review:structural", "chase:verify", ...) — the
    #: categorical feature the route-log keys on and the future per-source
    #: switchover knob. Free-form; empty when a caller hasn't set one yet.
    source: str = ""
    # claude_agent pass-through knobs (ignored by the other transports).
    system_prompt: str | Path | None = None
    mcp_config: str | Path | None = None
    max_turns: int = 20
    output_format: str = "text"
    disallowed_tools: tuple[str, ...] = field(default_factory=tuple)
    #: ``(store, ref_id, source)`` for a ``ref_events`` audit row on success
    #: (the CAD / structure / follow-up paths use it). ``store`` is typed
    #: loosely to keep this module free of the DB import chain.
    log_event: tuple[Any, int, str] | None = None
    # Extra CLI flags forwarded to the claude_* transports.
    extra_args: tuple[str, ...] = field(default_factory=tuple)


class LlmProvider(Protocol):
    """One narrow port every backend implements.

    A provider takes a resolved ``model`` id and an :class:`LlmRequest`
    and returns a normalized :class:`LlmResult`, folding transport
    failures into :attr:`LlmResult.error` rather than raising (a
    programming error — an unwired path — still raises). The registry in
    :data:`_PROVIDERS` maps each :class:`Transport` to one implementation;
    :func:`dispatch` is the only caller. Adding a backend (OpenAI-
    compatible OSS, a :class:`Transport`-composing failover ladder) is a
    new class implementing this method plus a registry row — no caller,
    :func:`dispatch`, or :class:`Tier` change. That is the switchability
    the LLM-independence goal wants.
    """

    def run(self, req: LlmRequest, *, model: str) -> LlmResult: ...


class ClaudeAgentProvider:
    """``claude -p`` multi-turn agent (MCP tools, stream-json result).

    Wraps :func:`~precis.utils.claude_agent.call_claude_agent` via the
    module global so a test that monkeypatches ``router.call_claude_agent``
    still intercepts it.
    """

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
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
                disallowed_tools=req.disallowed_tools,
                extra_args=req.extra_args,
                log_event=req.log_event,
            )
        except ClaudeProcessError as exc:
            return _error_result(exc, model=model, tier=req.tier)
        return result_from_agent(res, model=model, tier=req.tier)


class ClaudePProvider:
    """``claude -p`` one-shot JSON judge (no tools, last-JSON-block)."""

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
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


class LitellmProvider:
    """Loopback litellm ``LlmClient`` — OpenAI ``/v1/chat/completions``,
    tool-less local completion."""

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        return _dispatch_local(req, model)


class OpenAICompatProvider:
    """A *hosted* OpenAI-compatible OSS backend — OpenRouter / DeepInfra /
    a remote vLLM — over the same ``/v1/chat/completions`` wire as the
    loopback proxy, but at ``PRECIS_LLM_BASE_URL`` and authed with a
    vault-resolved key (``get_secret('PRECIS_LLM_API_KEY')``).

    Tool-less (the one-shot / completion / JSON-judge path) — the
    summarize/classify/judge calls. Tool-using calls take
    :class:`OpenAIToolsProvider`. Model ids come from the same
    ``resolve_model`` table, so a deployment points ``PRECIS_MODEL_*`` at
    OSS ids (e.g. ``PRECIS_MODEL_OPUS=deepseek-ai/DeepSeek-V3``).
    """

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        return _dispatch_openai_compat(req, model)


class OpenAIToolsProvider:
    """An OSS model driving the precis verbs over the OpenAI ``tools=`` wire.

    The ADR 0024 in-process tool loop, rebuilt behind the provider port:
    :func:`~precis.utils.llm.openai_tools.run_tool_loop` drives a hosted or
    local OSS backend (``PRECIS_LLM_BASE_URL``, vault key) through a
    tool-calling conversation, executing each call in-process via
    ``runtime.dispatch`` — no MCP socket round-trip. Serves both the
    ``LOCAL_BIG`` tier and the ``OPENAI`` backend's tool-using cloud calls.
    """

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        return _dispatch_openai_tools(req, model)


# The Transport → provider registry: the ONE place a transport binds to a
# concrete backend. Swap or add a row to reroute without touching callers.
_PROVIDERS: dict[Transport, LlmProvider] = {
    Transport.CLAUDE_AGENT: ClaudeAgentProvider(),
    Transport.CLAUDE_P: ClaudePProvider(),
    Transport.LITELLM: LitellmProvider(),
    Transport.OPENAI_COMPAT: OpenAICompatProvider(),
    Transport.OPENAI_TOOLS: OpenAIToolsProvider(),
}

# Import-time totality guard: every Transport must have a provider, so
# adding one without wiring a backend is a load-time failure, not a
# KeyError at dispatch (mirrors the _TIER_MODEL resolver assert above).
assert set(_PROVIDERS) == set(Transport), "dispatch: provider registry is not total"


def provider_for(transport: Transport) -> LlmProvider:
    """The provider bound to ``transport`` — the registry accessor a
    future config layer overrides to reroute a transport."""
    return _PROVIDERS[transport]


# ── failover ladder (composes the port) ────────────────────────────────


@dataclass(frozen=True, slots=True)
class Rung:
    """One failover attempt: a :class:`Transport` + an optional model override.

    ``model=None`` uses the ``model`` :meth:`FailoverProvider.run` was given
    (the primary, tier-resolved one); a fallback rung pins its own — e.g. the
    claude safety net pins the tier's compiled-in claude id so a PRECIS_MODEL_*
    override pointing at an OSS id doesn't leak onto ``claude -p``.
    """

    transport: Transport
    model: str | None = None
    label: str = ""


#: A quality gate on an error-free result: return ``True`` to accept, ``False``
#: to fall through to the next rung. ``None`` (the default) accepts any
#: error-free result — i.e. failover is transport-error-only.
AcceptFn = Callable[[LlmResult], bool]


class FailoverProvider:
    """Compose the port over an ordered ladder — the LLM-independence safety net.

    Walk the rungs; return the first result with no :attr:`LlmResult.error`
    that the ``accept`` gate approves, else the last attempt (carrying its
    error). Because it *is* a provider, a caller can't tell a ladder from a
    single model. Failure triggers:

    * **transport down / hard error** — a rung sets ``res.error`` → fall through.
    * **quality / verdict** — ``accept(res)`` returns ``False`` → fall through
      (the seam for a judge-gated escalate; unused by the default ladder).

    Cost / turn ceilings live *inside* the underlying providers (``max_usd`` /
    ``max_turns``), so they bound each rung rather than the ladder.
    """

    def __init__(self, rungs: list[Rung], *, accept: AcceptFn | None = None) -> None:
        if not rungs:
            raise ValueError("FailoverProvider needs at least one rung")
        self._rungs = tuple(rungs)
        self._accept = accept

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        last: LlmResult | None = None
        for i, rung in enumerate(self._rungs):
            last = provider_for(rung.transport).run(req, model=rung.model or model)
            accepted = last.error is None and (
                self._accept is None or self._accept(last)
            )
            if accepted:
                if i > 0:
                    # A fallback rung ran — warn: the primary failed and this
                    # rung costs (e.g. the claude safety net). Visible in
                    # worker_logs / the /status panel so a failover storm during
                    # an OSS eval is noticed rather than silently billed.
                    log.warning(
                        "llm-failover: fell back to rung %d (%s, model=%s) after "
                        "%d failed rung(s) — the fallback runs and costs; check "
                        "the primary backend.",
                        i,
                        rung.label or rung.transport.value,
                        rung.model or model,
                        i,
                    )
                return last
            if last.error is not None:
                log.warning(
                    "llm-failover: rung %d (%s, model=%s) failed: %s",
                    i,
                    rung.label or rung.transport.value,
                    rung.model or model,
                    last.error,
                )
        assert last is not None  # rungs is non-empty
        return last


def _claude_default(tier: Tier) -> str:
    """The tier's compiled-in claude model id, ignoring any PRECIS_MODEL_*
    override — so a claude fallback rung stays on claude even when the override
    points the primary at an OSS id."""
    return _TIER_MODEL[tier][1]


def _failover_enabled() -> bool:
    return os.environ.get("PRECIS_LLM_FAILOVER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _failover_ladder(tier: Tier, *, tools_needed: bool, backend: Backend) -> list[Rung]:
    """The default OSS→claude ladder: the backend's primary transport, then the
    claude equivalent as a safety net (only when the primary is an OSS
    transport — a claude/local primary has nothing to fall back to)."""
    primary = select_transport(tier, tools_needed=tools_needed, backend=backend)
    if primary in (Transport.OPENAI_TOOLS, Transport.OPENAI_COMPAT):
        claude = Transport.CLAUDE_AGENT if tools_needed else Transport.CLAUDE_P
        return [
            Rung(primary, label="oss"),
            Rung(claude, model=_claude_default(tier), label="claude-fallback"),
        ]
    return [Rung(primary, label="primary")]


def dispatch(req: LlmRequest) -> LlmResult:
    """Route ``req`` to its provider and return a normalized
    :class:`LlmResult`.

    Resolve the backend + model, pick the transport (pure), look up the
    provider, and delegate. Each provider *wraps* the existing helper —
    never reimplements it — and folds a caught
    :class:`~precis.utils._claude_subprocess.ClaudeProcessError` (or a
    local-transport ``RuntimeError``) into :attr:`LlmResult.error` rather
    than raising, so every dispatch path returns one shape. A programming
    error (the unwired local-big path) still raises.

    The ``OPENAI`` backend needs ``PRECIS_LLM_BASE_URL``; with the backend
    on but no base url set, cloud calls fall back to ``claude`` rather than
    POST to a phantom endpoint — the ships-dark safety net.

    With ``PRECIS_LLM_FAILOVER`` on, an OSS primary is wrapped in a
    :class:`FailoverProvider` that falls back to ``claude`` on error — so a
    flipped backend degrades to claude instead of failing. Off by default.
    """
    backend = resolve_backend()
    if backend is Backend.OPENAI and not os.environ.get("PRECIS_LLM_BASE_URL"):
        backend = Backend.ANTHROPIC
    model = req.model or resolve_model(req.tier)
    # Resolve the transport *before* the breaker, so the gate can key on the
    # resource actually spent: the claude-OAuth transports draw subscription
    # quota (gated on the snapshot), everything else paid spends real dollars.
    if _failover_enabled():
        ladder = _failover_ladder(
            req.tier, tools_needed=req.tools_needed, backend=backend
        )
        transport = ladder[0].transport
        provider: LlmProvider = FailoverProvider(ladder)
    else:
        transport = select_transport(
            req.tier, tools_needed=req.tools_needed, backend=backend
        )
        provider = provider_for(transport)
    # Global circuit breaker: refuse a *new paid* call once its resource is
    # exhausted (only free local tiers pass; dark when no store is bound).
    # Folds into the normalized error result so callers degrade gracefully.
    from precis.budget import breaker as _breaker

    trip = _breaker.gate_tier(req.tier, transport=transport.value)
    if trip is not None:
        # A breaker trip is a window-scoped *pause*, not a failure — flag it so a
        # pinned pass skips (and re-runs when the window clears) rather than
        # spinning: record-failed → re-claim → re-trip every worker cycle.
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=trip,
            paused=True,
        )
    # Window admission (llm-catalog slice 2): refuse a doomed (context, model)
    # pairing loudly — with the numbers — after the budget gate, before spending
    # the call, folded into the same normalized-error shape (never raised, so a
    # pinned-model pass backs off instead of spinning). Ships dark: no store /
    # no card / no known window ⇒ None, i.e. byte-identical to today.
    from precis.utils.llm import admit as _admit

    refusal = _admit.check_dispatch(req, model=model, transport=transport)
    if refusal is not None:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=refusal,
        )
    started = time.monotonic()
    result = provider.run(req, model=model)
    _record_dispatch(
        req,
        result,
        transport=transport,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return result


def _route_features(req: LlmRequest) -> dict[str, Any]:
    """Cheap, deterministic code features for the route-log (the categorizer's
    first layer). No model call — just what's readable off the request."""
    prompt_chars = len(req.prompt or "")
    if req.messages:
        prompt_chars += sum(len(str(m.get("content", ""))) for m in req.messages)
    return {
        "prompt_chars": prompt_chars,
        "tier": req.tier.value,
        "tools_needed": req.tools_needed,
        "source": req.source or None,
        "has_system": bool(req.system_prompt),
        "has_mcp": bool(req.mcp_config),
    }


def _serialize_request(req: LlmRequest) -> str:
    """The full logical request, JSON-serialized — everything we send, so a
    later slice can replay it on another model. ``system_prompt`` is resolved
    to its text (a ``Path`` is read)."""
    import json

    return json.dumps(
        {
            "source": req.source,
            "tier": req.tier.value,
            "model": req.model,
            "tools_needed": req.tools_needed,
            "system_prompt": _read_system_prompt(req.system_prompt),
            "prompt": req.prompt,
            "messages": req.messages,
            "mcp_config": str(req.mcp_config) if req.mcp_config else None,
            "max_turns": req.max_turns,
            "max_usd": req.max_usd,
            "output_format": req.output_format,
            "disallowed_tools": list(req.disallowed_tools),
        },
        ensure_ascii=False,
    )


def _record_dispatch(
    req: LlmRequest, result: LlmResult, *, transport: Transport, duration_ms: int
) -> None:
    """Best-effort: record the full call to the route-log. Dark (no-op) unless a
    store is bound at boot; any failure is swallowed so it can't break dispatch."""
    from precis import route_log

    if not route_log.enabled():
        return
    try:
        route_log.record_call(
            route_log.LlmCallRecord(
                source=req.source or None,
                tier=req.tier.value,
                transport=transport.value,
                model=result.model,
                tools_needed=req.tools_needed,
                request_text=_serialize_request(req),
                response_text=result.text or "",
                cost_usd=result.cost_usd,
                turns_used=result.turns_used,
                duration_ms=duration_ms,
                errored=result.error is not None,
                error=result.error,
                data_parsed=result.data is not None,
                features=_route_features(req),
            )
        )
    except Exception:
        log.debug("route_log: dispatch record failed", exc_info=True)


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


def _dispatch_openai_compat(req: LlmRequest, model: str) -> LlmResult:
    """Drive a hosted OpenAI-compatible OSS backend (the ``OPENAI`` backend).

    Same OpenAI ``/v1/chat/completions`` client as :func:`_dispatch_local`,
    but pointed at ``PRECIS_LLM_BASE_URL`` and authed with a vault-resolved
    key (``get_secret('PRECIS_LLM_API_KEY')`` — env-override-wins, so a key
    in the environment still works during transition). Imports the
    summarizer client + the secrets resolver lazily to keep this module out
    of the worker/DB import chain.
    """
    from dataclasses import replace

    from precis.secrets import get_secret
    from precis.workers.llm_summarize import LlmClient, LlmConfig

    base_url = os.environ.get("PRECIS_LLM_BASE_URL", "")
    api_key = get_secret("PRECIS_LLM_API_KEY") or ""
    cfg = replace(
        LlmConfig.from_env(),
        url=base_url,
        api_key=api_key,
        model=model,
        enabled=True,
    )
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


def _read_system_prompt(sp: str | Path | None) -> str | None:
    """Resolve an ``LlmRequest.system_prompt`` to inline text for the OSS loop.

    ``claude_agent`` accepts both a file path (dream's soul file) and inline
    text (plan_tick's assembled prompt). A :class:`~pathlib.Path` is read;
    a ``str`` is treated as inline text (an unreadable path degrades to
    ``None`` rather than raising).
    """
    if sp is None:
        return None
    if isinstance(sp, Path):
        try:
            return sp.read_text()
        except OSError:
            return None
    return sp


def _dispatch_openai_tools(req: LlmRequest, model: str) -> LlmResult:
    """Drive the OSS ``tools=`` agent loop (the ``OPENAI_TOOLS`` transport).

    Assembles a :class:`~precis.utils.llm.openai_tools.ToolChatClient` (hosted
    or local OSS backend at ``PRECIS_LLM_BASE_URL``, vault key), advertises the
    precis verbs, and runs :func:`~precis.utils.llm.openai_tools.run_tool_loop`
    with each tool call executed in-process via ``runtime.dispatch``. The loop
    already folds transport errors into its result; the outer guard catches a
    failure to *build* the executor/tools (e.g. an unavailable runtime).
    Imports the loop + bridge lazily so the router stays DB-free.

    A *tool-less* agent call (``req.mcp_config is None`` — cad_propose /
    cad_discuss / structure_propose route here with ``tools_needed=True`` only
    to get the agent wrapper's output shape, not tools) runs with an empty
    tools list, so it stays a plain completion loop and can't call precis verbs
    on the OSS backend — matching the claude path, where ``mcp_config=None``
    means no tools advertised (gripe 159759).
    """
    from precis.secrets import get_secret
    from precis.utils.llm.openai_tools import ToolChatClient, run_tool_loop
    from precis.utils.llm.precis_tools import precis_tool_specs, runtime_executor

    base_url = os.environ.get("PRECIS_LLM_BASE_URL", "")
    api_key = get_secret("PRECIS_LLM_API_KEY") or ""
    timeout = req.timeout_s if req.timeout_s is not None else 600.0
    client = ToolChatClient(url=base_url, api_key=api_key, model=model, timeout=timeout)
    tool_less = req.mcp_config is None
    try:
        result = run_tool_loop(
            client,
            prompt=req.prompt,
            tools=[] if tool_less else precis_tool_specs(),
            execute=runtime_executor(),
            system_prompt=_read_system_prompt(req.system_prompt),
            max_turns=req.max_turns,
        )
    except (RuntimeError, OSError) as exc:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=str(exc),
        )
    return LlmResult(
        text=result.final_text,
        cost_usd=None,
        turns_used=result.turns_used,
        model=model,
        tier=req.tier,
        error=result.error,
    )


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
    "Backend",
    "ClaudePResult",
    "LlmProvider",
    "LlmRequest",
    "LlmResult",
    "Tier",
    "Transport",
    "dispatch",
    "provider_for",
    "resolve_backend",
    "resolve_model",
    "result_from_agent",
    "result_from_claude_p",
    "result_from_openai",
    "select_transport",
    "transport_for_profile",
]
