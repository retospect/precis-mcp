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

* :func:`resolve_model` — the single tier→model table. Resolution is a
  web-set ``app_settings`` override (the ``/factory`` live switch,
  :mod:`precis.utils.llm.live_config`) → the *existing* env var → the
  compiled default, so a caller with no override row resolves byte-for-byte
  to the model it uses today (ADR 0046 §"Resolver"). :func:`resolve_backend`
  layers the same DB tier over ``PRECIS_LLM_BACKEND``.
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
    from precis.utils.llm.openai_tools import AgentLoopResult
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
    # ADR 0024's dream model — local big + tools. Dispatchable to a per-host
    # llama-swap endpoint when a served_by card declares one (OPENAI_TOOLS now
    # honors the slot's local_url); with no served_by it falls to the hosted
    # PRECIS_LLM_BASE_URL path (dark by default).
    Tier.LOCAL_BIG: ("PRECIS_LOCAL_BIG_MODEL", "qwen-heavy"),
}

# Import-time totality guard: every Tier must have a resolver row, so
# adding a tier without a model is a load-time failure, not a KeyError
# at dispatch (mirrors the TodoView totality assert in handlers/todo.py).
assert set(_TIER_MODEL) == set(Tier), "resolve_model: tier table is not total"


def resolve_model(tier: Tier) -> str:
    """The concrete model id for ``tier`` — the ONE place model
    selection lives.

    Resolution order: a web-set ``app_settings`` override (the ``/factory``
    live switch, :func:`precis.utils.llm.live_config.model_override`) → the
    env var → the compiled default in :data:`_TIER_MODEL`. With no override
    row (or no store bound) the DB tier is a no-op, so a caller resolves
    byte-for-byte to the model it uses today.
    """
    from precis.utils.llm import live_config

    override = live_config.model_override(tier)
    if override:
        return override
    env_var, default = _TIER_MODEL[tier]
    return os.environ.get(env_var, default)


# ── planner model aliases (the LLM:<value> dropdown vocab) ─────────────
#
# The ``LLM:<value>`` tag a todo carries names a *capability tier*, not a
# vendor model: the dispatcher synthesizes ``plan_tick``'s ``model`` param
# from it and the tick resolves the concrete model via :func:`resolve_model`.
# This is the ONE ordered source the dispatcher (plan_tick), the closed-vocab
# guards, and the web model-pickers key on, so the tier map and the dropdown
# never drift. ``local`` is the cluster's served OSS tier (``qwen-heavy`` +
# tools), reachable now that ADR 0046's ``OPENAI_TOOLS`` loop drives the verbs
# in-process — a planner tick runs on it just like the cloud tiers.
PLANNER_TIER_BY_ALIAS: dict[str, Tier] = {
    "opus": Tier.CLOUD_SUPER,
    "sonnet": Tier.CLOUD_MID,
    "haiku": Tier.CLOUD_SMALL,
    "local": Tier.LOCAL_BIG,
}

#: Ordered alias vocabulary — dropdown order AND the ``LLM:`` closed-vocab set.
PLANNER_MODEL_ALIASES: tuple[str, ...] = tuple(PLANNER_TIER_BY_ALIAS)


def planner_model_choices() -> list[tuple[str, str]]:
    """``(alias, resolved-model)`` for each planner tier — the picker source.

    The label is the model the tier *currently* resolves to (env +
    ``app_settings`` live overrides), so the web dropdown shows the model each
    tier actually runs on this cluster rather than a hardcoded vendor name.
    """
    return [
        (alias, resolve_model(tier)) for alias, tier in PLANNER_TIER_BY_ALIAS.items()
    ]


# ── transport selection ────────────────────────────────────────────────


def resolve_backend() -> Backend:
    """The cloud backend family for this process — the LLM-independence switch.

    Resolution order: a web-set ``app_settings`` override (the ``/factory``
    live toggle, :func:`precis.utils.llm.live_config.backend_override`) →
    ``PRECIS_LLM_BACKEND`` (default ``anthropic``). An unknown value at either
    tier degrades to ``anthropic`` so a typo can't dark a deployment. The
    OpenAI-compatible path additionally needs ``PRECIS_LLM_BASE_URL`` set
    (checked at dispatch); with the backend on but no base url, cloud calls
    fall back to ``claude`` rather than hit a phantom endpoint. With no override
    row the DB tier is a no-op — byte-identical to the env-only read.
    """
    from precis.utils.llm import live_config

    override = live_config.backend_override()
    if override is not None:
        return Backend.OPENAI if override == Backend.OPENAI else Backend.ANTHROPIC
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
    * ``interrupted`` — ``True`` when ``error`` is a *signal-termination* of the
      subprocess (exit ≥ 128 = 128 + signum: SIGTERM→143, SIGINT→130, SIGKILL→
      137). The worker was told to stop mid-call (a launchd/deploy bounce or a
      jetsam cull), so the ``claude -p`` child died with the worker — this is
      **not** a dispatch/config failure and must not be recorded as one. Same
      skip-not-fail treatment as ``paused``: the call simply didn't run, and the
      next tick re-attempts for free. (Without it, every worker bounce mid-review
      wrote a false 5h ``review-fail`` cooldown marker.)
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
    interrupted: bool = False
    #: OpenAI ``usage.total_tokens`` for the local / openai-compat transports
    #: (``None`` for the claude transports, which report cost not tokens). Kept
    #: so a direct-``LlmClient`` pass folded through :class:`DispatchClient`
    #: still gets the token count it recorded for accounting.
    total_tokens: int | None = None
    #: Count of ``tool_use`` blocks in the ``claude_agent`` stream-json stream
    #: (``None`` for one-shot transports and any run without a stream to count).
    #: The review seam's empty-result assertion reads this as *definitive*
    #: evidence the pass acted: a ``0`` here (not ``None``) is one leg of the
    #: silent-empty conjunction.
    tool_calls: int | None = None
    #: The complete raw stdout of a ``claude_agent`` stream-json run (every turn
    #: + tool call/result), preserved so a caller that stores a debuggable
    #: transcript or parses the terminal reason itself (the planner tick) can.
    #: ``None`` for the non-agent transports, where ``text`` carries the answer.
    raw_text: str | None = None
    #: How a ``claude_agent`` run terminated *abnormally* — ``'max_turns'``, a
    #: ``'budget'``-class reason, or another ``error_*`` subtype — ``None`` on a
    #: clean run. Lets a caller map a recovered exhaustion onto a resumable
    #: outcome without re-parsing the stream. ``None`` for non-agent transports.
    terminal_reason: str | None = None
    #: The OSS ``tools=`` loop's raw ``stop_reason`` (``'stop'`` — model
    #: answered · ``'max_turns'`` — turn ceiling · ``'error'`` — transport
    #: failure), threaded through so the planner tick can tell a clean answer
    #: from a resumable exhaustion (mirroring how the claude path reads
    #: ``terminal_reason``). ``None`` for the non-OSS transports.
    stop_reason: str | None = None


def result_from_agent(res: AgentResult, *, model: str, tier: Tier) -> LlmResult:
    """Normalize a :class:`~precis.utils.claude_agent.AgentResult`."""
    return LlmResult(
        text=res.final_text,
        cost_usd=res.cost_usd,
        turns_used=res.turns_used,
        model=model,
        tier=tier,
        duration_s=res.duration_s,
        tool_calls=res.tool_calls,
        raw_text=res.raw_stdout,
        terminal_reason=res.terminal_reason,
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
        total_tokens=getattr(res, "total_tokens", None),
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
    #: Completion-length cap for the local / openai-compat transports (the
    #: ``max_tokens`` field of the underlying ``LlmConfig``). ``None`` keeps
    #: ``LlmConfig.from_env``'s default (220) — the summarizer's short-gloss
    #: cap. A pass with a longer structured payload pins its own (e.g.
    #: paper_glossary needs 2000, else the JSON truncates); this is the knob
    #: that lets a direct-``LlmClient`` caller fold through ``dispatch`` without
    #: silently shrinking its budget. Ignored by the ``claude_*`` transports,
    #: which have their own turn/cost ceilings.
    max_tokens: int | None = None
    #: A booked OpenRouter variant (a ``meta.endpoints`` dict — provider / quant /
    #: window) + reasoning effort, pinned onto the ``openai_compat`` wire so the
    #: call reproducibly hits *that* provider×quant instead of OpenRouter load-
    #: balancing the ~28 (gripe 162624). ``None`` ⇒ today's behaviour (slug only).
    #: A ``select_offering`` caller threads ``Selection.endpoint`` here.
    endpoint: dict[str, Any] | None = None
    effort: str | None = None
    #: Direct local-serving base URL (llama-swap's OpenAI endpoint), threaded in
    #: by :func:`dispatch` when a reserved :class:`~precis.utils.llm.local_serving.LocalSlot`
    #: declares an ``endpoint`` — the LITELLM transport routes here instead of the
    #: litellm proxy (the Phase-2 flip). ``None`` ⇒ the ``LlmConfig.from_env`` URL.
    local_url: str | None = None
    #: Caller label ("dream", "review:structural", "chase:verify", ...) — the
    #: categorical feature the route-log keys on and the future per-source
    #: switchover knob. Free-form; empty when a caller hasn't set one yet.
    source: str = ""
    #: The ref this call is *for* (a quest / paper / todo id), stamped onto
    #: ``llm_call_log.ref_id`` so spend/wall-clock is attributable to an entity,
    #: not just a ``source`` pass. ``None`` ⇒ pass-level attribution only. Cannot
    #: be back-filled — a row logged without it is permanently un-attributable, so
    #: an inproc pass that has a natural ref should set it (gr162130).
    ref_id: int | None = None
    #: Whether to write this call to the route-log at all. Default ``True``.
    #: ``False`` = no row (a caller that wants zero footprint).
    log_call: bool = True
    #: Whether to store the full request/response *text* (the ``llm_blob`` replay
    #: material) alongside the metadata row. Default ``True``. A high-volume
    #: *mechanical* batch pass (per-chunk summarize / classify) sets this ``False``
    #: for a **lite** row — metadata (chars / cost / duration / ref_id) is kept
    #: (~660 B/row, cheap + mineable), but the ~18 KB unique-per-call blob it would
    #: never replay is skipped. Ignored when ``log_call`` is ``False``.
    log_blobs: bool = True
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
    #: Extra env vars overlaid onto the ``claude_agent`` subprocess env (the
    #: planner tick's runtime back-doors: ``PRECIS_CURRENT_TODO`` / ``_MODEL`` /
    #: ``PRECIS_WORKSPACE`` / the agentlog id / ``PRECIS_KINDS_DISABLED``). The
    #: spawned MCP server inherits them. Ignored by the other transports (the
    #: in-process loop carries context in a ContextVar, not env). ``None`` ⇒
    #: inherit the worker env unchanged.
    env_overlay: dict[str, str] | None = None
    #: Working directory for the ``claude_agent`` subprocess — a CLAUDE.md-free
    #: neutral cwd so ``claude -p`` discovers no ambient project persona (ADR
    #: 0051 §12). Ignored by the other transports. ``None`` ⇒ the worker's cwd.
    cwd: str | Path | None = None


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
                env_overlay=req.env_overlay,
                cwd=req.cwd,
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
    # Local serving slot (slice 7 / §6): if this host declares ``served_by`` for
    # the model, hold one of its local slots for the call's duration so
    # concurrent local calls can't exceed the declared ``max_parallel``. Ships
    # dark — a model not served on this host (every model until ``served_by`` is
    # populated) returns ``None`` and dispatch is byte-identical to today. A
    # ``paused`` outcome (served here but all slots busy) folds into the same
    # paused-result shape as the breaker, so a pinned pass backs off, not spins.
    from precis.utils.llm import local_serving as _local

    slot = _local.acquire(model)
    if slot is not None and slot.paused:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=f"all local serving slots for {model} are busy — backing off",
            paused=True,
        )
    # A reserved slot that declares a direct ``endpoint`` (llama-swap) routes the
    # local transport there instead of the litellm proxy, using the server-side
    # model name — the Phase-2 litellm-retirement flip. No endpoint ⇒ req + model
    # unchanged (today's behavior). Both local transports read ``local_url``:
    # LITELLM (tool-less, ``_dispatch_local``) and OPENAI_TOOLS (LOCAL_BIG, tools,
    # ``run_oss_tool_loop``).
    call_req = req
    call_model = model
    if slot is not None and slot.reserved and slot.endpoint:
        from dataclasses import replace as _replace

        call_req = _replace(req, local_url=slot.endpoint)
        call_model = slot.served_model or model
    started = time.monotonic()
    try:
        result = provider.run(call_req, model=call_model)
    finally:
        _local.release(slot)
    _record_dispatch(
        req,
        result,
        transport=transport,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return result


@dataclass
class DispatchClient:
    """A ``.complete(messages)``-shaped adapter that routes a local completion
    through :func:`dispatch` instead of holding its own litellm ``LlmClient``.

    Drop-in for the summarize / classify / glossary passes' ``client=`` seam:
    the same ``complete(messages, *, extra_body=None) -> LlmResult`` contract
    (``.text`` + ``.total_tokens``), but every call folds through the router — so
    it gains the breaker gate, the local-serving slot reservation + ``served_by``
    endpoint routing (the Phase-2 litellm-retire flip: once a card declares
    ``served_by.endpoint`` the call reroutes to llama-swap instead of the litellm
    proxy), and the route-log. **Behaviour-preserving until ``served_by`` is
    seeded** — with no slot the model resolves to today's ``summarizer`` alias on
    the ``LlmConfig.from_env`` proxy URL, byte-identical to the raw client.

    Raises ``RuntimeError`` on a dispatch error / breaker-pause so the pass marks
    the item failed and retries — exactly as the raw ``LlmClient.complete`` raised
    on a transport error (the passes' ``except`` blocks count it failed). Local
    tiers are free, so the breaker never trips them; the only pause is
    all-slots-busy, which correctly backs a batch off.
    """

    tier: Tier = Tier.LOCAL_SMALL
    model: str | None = None
    max_tokens: int | None = None
    source: str = ""
    #: Whether to write a route-log row (see :attr:`LlmRequest.log_call`).
    #: Default ``False`` — a bare ``DispatchClient`` stays silent (unchanged
    #: blast radius); a corpus batch pass opts *in* to a lite row below.
    log_call: bool = False
    #: Store the replay blobs too, or write a **lite** metadata-only row (see
    #: :attr:`LlmRequest.log_blobs`). A corpus-scale batch pass sets ``log_call=
    #: True, log_blobs=False`` so the mineable metadata (chars / cost / duration /
    #: ref_id) is kept without a per-call blob explosion.
    log_blobs: bool = True

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        extra_body: dict[str, Any] | None = None,
    ) -> LlmResult:
        # ``extra_body`` (OpenRouter booking) is a hosted-backend concern; the
        # local completion path these passes use never sets it, so it is ignored.
        res = dispatch(
            LlmRequest(
                tier=self.tier,
                messages=messages,
                model=self.model,
                max_tokens=self.max_tokens,
                source=self.source,
                log_call=self.log_call,
                log_blobs=self.log_blobs,
            )
        )
        if res.error is not None:
            raise RuntimeError(res.error)
        return res


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

    if not req.log_call or not route_log.enabled():
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
                ref_id=req.ref_id,
                store_blobs=req.log_blobs,
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
    # A local-serving slot may pin a direct endpoint (llama-swap) — route there
    # instead of the litellm proxy URL (the Phase-2 flip; dark until a card
    # declares served_by.endpoint). Mirrors the per-call url override the
    # openai_compat path already uses.
    if req.local_url:
        cfg = replace(cfg, url=req.local_url)
    # A caller-pinned completion cap (paper_glossary=2000, …) wins over the
    # env default so a migrated direct-``LlmClient`` pass keeps its budget.
    if req.max_tokens is not None:
        cfg = replace(cfg, max_tokens=req.max_tokens)
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


def openrouter_routing(
    endpoint: dict[str, Any] | None, *, effort: str | None = None
) -> dict[str, Any]:
    """Translate a booked ``meta.endpoints`` variant → the OpenRouter request-body
    block that pins it (gripe 162624).

    Emits ``provider:{order:[<slug>], quantizations:[<quant>],
    allow_fallbacks:false, require_parameters:true}`` so OpenRouter routes to
    *exactly* that provider×quant (no load-balancing across the ~28 endpoints),
    plus ``reasoning:{effort}`` when an effort is set. The provider slug comes
    from the endpoint's OpenRouter ``tag`` (``deepinfra/fp4`` → ``deepinfra``,
    the routing key), falling back to a lower-cased ``provider`` name. A
    ``quant`` of ``unknown`` is omitted (nothing to pin). Returns ``{}`` when
    there is nothing to pin — the caller then posts the bare slug, today's
    behaviour.
    """
    body: dict[str, Any] = {}
    provider: dict[str, Any] = {}
    if endpoint:
        tag = str(endpoint.get("tag") or "")
        slug = (
            tag.split("/")[0]
            if "/" in tag
            else str(endpoint.get("provider") or "").lower()
        )
        if slug:
            provider["order"] = [slug]
            provider["allow_fallbacks"] = False
        quant = endpoint.get("quant")
        if quant and quant != "unknown":
            provider["quantizations"] = [quant]
    if provider:
        provider["require_parameters"] = True
        body["provider"] = provider
    if effort:
        body["reasoning"] = {"effort": effort}
    return body


def _dispatch_openai_compat(req: LlmRequest, model: str) -> LlmResult:
    """Drive a hosted OpenAI-compatible OSS backend (the ``OPENAI`` backend).

    Same OpenAI ``/v1/chat/completions`` client as :func:`_dispatch_local`,
    but pointed at ``PRECIS_LLM_BASE_URL`` and authed with a vault-resolved
    key (``get_secret('PRECIS_LLM_API_KEY')`` — env-override-wins, so a key
    in the environment still works during transition). When the request carries
    a booked ``endpoint`` (gripe 162624), the OpenRouter ``provider:{}`` /
    ``reasoning:{}`` pin is merged into the body so the call hits that exact
    provider×quant. Imports the summarizer client + the secrets resolver lazily
    to keep this module out of the worker/DB import chain.
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
    if req.max_tokens is not None:
        cfg = replace(cfg, max_tokens=req.max_tokens)
    messages = req.messages or [{"role": "user", "content": req.prompt}]
    extra_body = openrouter_routing(req.endpoint, effort=req.effort)
    client = LlmClient(cfg)
    try:
        # Only pass extra_body when there is a booking to pin, so the un-booked
        # path is the byte-identical call it was before (gripe 162624 ships dark).
        res = (
            client.complete(messages, extra_body=extra_body)
            if extra_body
            else client.complete(messages)
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


def run_oss_tool_loop(
    *,
    prompt: str,
    model: str,
    system_prompt: str | Path | None = None,
    max_turns: int = 20,
    timeout_s: float | None = None,
    tool_less: bool = False,
    local_url: str | None = None,
) -> AgentLoopResult:
    """Drive the in-process OSS ``tools=`` loop and return the RAW
    :class:`~precis.utils.llm.openai_tools.AgentLoopResult`.

    Extracted from :func:`_dispatch_openai_tools` so a caller that needs the
    loop's ``stop_reason`` verbatim — the planner tick, which must tell a clean
    answer (``stop``) from a ``max_turns`` cutoff (resumable, not failed) —
    reuses the exact client-build + verb-wiring instead of the collapsed
    :class:`LlmResult`. Builds the client from ``PRECIS_LLM_BASE_URL`` + the
    vault key, UNLESS ``local_url`` is given — a local-serving slot's pinned
    llama-swap endpoint — in which case it routes there directly with an authless
    dummy key (a loopback model has no auth; the vault key is for the hosted OSS
    backend). This is what makes the ``LOCAL_BIG`` tier dispatch to a per-host
    local endpoint, mirroring :func:`_dispatch_local`'s ``local_url`` override.
    Runs the precis verbs in-process via ``runtime.dispatch`` unless
    ``tool_less``. May raise ``RuntimeError`` / ``OSError`` if the executor /
    tools can't be built (an unavailable runtime); the loop itself folds a
    transport failure into ``AgentLoopResult.error`` (``stop_reason='error'``)
    rather than raising. Imports the loop + bridge lazily so the router stays
    DB-free.
    """
    from precis.secrets import get_secret
    from precis.utils.llm.openai_tools import ToolChatClient, run_tool_loop
    from precis.utils.llm.precis_tools import precis_tool_specs, runtime_executor

    if local_url:
        base_url = local_url
        api_key = "dummy"
    else:
        base_url = os.environ.get("PRECIS_LLM_BASE_URL", "")
        api_key = get_secret("PRECIS_LLM_API_KEY") or ""
    timeout = timeout_s if timeout_s is not None else 600.0
    client = ToolChatClient(url=base_url, api_key=api_key, model=model, timeout=timeout)
    return run_tool_loop(
        client,
        prompt=prompt,
        tools=[] if tool_less else precis_tool_specs(),
        execute=runtime_executor(),
        system_prompt=_read_system_prompt(system_prompt),
        max_turns=max_turns,
    )


def _dispatch_openai_tools(req: LlmRequest, model: str) -> LlmResult:
    """Drive the OSS ``tools=`` agent loop (the ``OPENAI_TOOLS`` transport).

    Thin wrapper over :func:`run_oss_tool_loop` that collapses the raw
    :class:`~precis.utils.llm.openai_tools.AgentLoopResult` into the normalized
    :class:`LlmResult`. The loop already folds transport errors into its result;
    the outer guard catches a failure to *build* the executor / tools (e.g. an
    unavailable runtime).

    A *tool-less* agent call (``req.mcp_config is None`` — cad_propose /
    cad_discuss / structure_propose route here with ``tools_needed=True`` only
    to get the agent wrapper's output shape, not tools) runs with an empty
    tools list, so it stays a plain completion loop and can't call precis verbs
    on the OSS backend — matching the claude path, where ``mcp_config=None``
    means no tools advertised (gripe 159759).
    """
    try:
        result = run_oss_tool_loop(
            prompt=req.prompt,
            model=model,
            system_prompt=req.system_prompt,
            max_turns=req.max_turns,
            timeout_s=req.timeout_s,
            tool_less=req.mcp_config is None,
            local_url=req.local_url,
        )
    except (RuntimeError, OSError) as exc:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=str(exc),
            # A failure to *build* the executor/tools is a transport error to a
            # stop_reason reader (the planner tick), same as an in-loop one.
            stop_reason="error",
        )
    return LlmResult(
        text=result.final_text,
        cost_usd=None,
        turns_used=result.turns_used,
        model=model,
        tier=req.tier,
        error=result.error,
        # Thread the definitive tool-call count so the review seam's
        # empty-result assertion works on this (local/OSS) backend too —
        # otherwise a silent-empty pass routed through OPENAI_TOOLS keeps
        # tool_calls=None and the guard can never trip (the anchor demands
        # a definitive 0). `tool_calls_made` is the loop's own count.
        tool_calls=result.tool_calls_made,
        # The loop's stop_reason rides through so the planner tick can tell a
        # clean answer ('stop') from a resumable exhaustion ('max_turns').
        stop_reason=result.stop_reason,
    )


def _error_result(exc: ClaudeProcessError, *, model: str, tier: Tier) -> LlmResult:
    """Fold a transport failure into a normalized error result.

    Surfaces any partial stdout the wrapper captured as ``text`` so a
    caller keeps a recoverable-exhaustion answer while still seeing the
    ``error``.

    A signal-terminated child (returncode ≥ 128 = 128 + signum) is flagged
    ``interrupted``: the process was killed by a signal (worker bounce / jetsam),
    not by a genuine program failure, so callers skip it rather than recording a
    dispatch failure (see :attr:`LlmResult.interrupted`).
    """
    rc = getattr(exc, "returncode", None)
    return LlmResult(
        text=getattr(exc, "stdout", "") or "",
        cost_usd=None,
        turns_used=None,
        model=model,
        tier=tier,
        error=str(exc),
        interrupted=rc is not None and rc >= 128,
    )


__all__ = [
    "AgentResult",
    "Backend",
    "ClaudePResult",
    "DispatchClient",
    "LlmProvider",
    "LlmRequest",
    "LlmResult",
    "Tier",
    "Transport",
    "dispatch",
    "openrouter_routing",
    "provider_for",
    "resolve_backend",
    "resolve_model",
    "result_from_agent",
    "result_from_claude_p",
    "result_from_openai",
    "run_oss_tool_loop",
    "select_transport",
    "transport_for_profile",
]
