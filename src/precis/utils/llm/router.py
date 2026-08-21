"""The LLM routing seam — one place where model selection, transport
choice, and result normalization live.

Before this module, model selection was scattered across ~a-dozen
independent ``os.environ.get(...)`` reads, three different transports
(``claude_agent`` multi-turn agent, ``claude_p`` one-shot JSON judge,
the local ``LlmClient`` completion) each with its own result
shape, and three rogue subprocess sites. This module is the **seam**
that a follow-up unit (4b) folds those call sites through; it does not
rewire them itself.

Four pieces:

* :func:`resolve_model` — the single tier→model table. Resolution is a
  web-set ``app_settings`` override (the ``/factory`` live switch,
  :mod:`precis.utils.llm.live_config`) → the *existing* env var → the
  compiled default, so a caller with no override row resolves byte-for-byte
  to the model it uses today. :func:`resolve_backend`
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
one-shot, structured) profile rides the ``MEDIUM`` / ``SMALL`` tiers on
the ``claude_p`` / local transports; an ``AGENT`` (tools, multi-turn)
profile rides ``BIG`` / ``FRONTIER`` on the ``claude_agent`` transport
(and, when the backend routes there, a served OSS model on ``BIG``).

**OSS tool-calling lands on** :data:`Transport.OPENAI_TOOLS` — an
open-source model driving the precis verbs over the OpenAI ``tools=``
wire (:class:`OpenAIToolsProvider`), the in-process litellm tool loop rebuilt behind
the provider port. It serves a served local model on the ``BIG`` tier
and, when ``PRECIS_LLM_BACKEND=openai``, the tool-using cloud tiers.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlparse

from precis.utils._claude_subprocess import ClaudeProcessError
from precis.utils.claude_agent import (
    AgentResult,
    call_claude_agent,
    call_claude_agent_async,
)
from precis.utils.claude_p import ClaudePResult, call_claude_p

if TYPE_CHECKING:
    from precis.utils.llm.openai_tools import AgentLoopResult
    from precis.utils.prompt.model import Profile

log = logging.getLogger(__name__)


class Tier(StrEnum):
    """Capability tier — *what* the task needs, not *which* model.

    A tier bundles a capability level with a tool-use expectation, and
    maps onto both a concrete model (via :func:`resolve_model`) and a
    transport (via :func:`select_transport`). Capability tiers + placement chains Phase C retired the
    original five location-coupled members (``local-small``/``local-big``/
    ``cloud-small``/``cloud-mid``/``cloud-super``) in favor of these four
    pure-capability tiers — every call site now routes on *what a task
    needs*, not on where it happens to run.

    * ``FRONTIER`` — the trusted-answer tier: heavy reasoning + tools
      (opus-class). The structural / deep reviewers, fix-gripe,
      ``llm_tier='opus'`` ticks, the dream pass, and the generic
      ``claude_agent`` default.
    * ``BIG`` — the general agentic workhorse (sonnet-class) — planner,
      tex-fix, weave. Also the tools-capable rung a served OSS model runs
      on (a local ``qwen-heavy``-class model when the backend routes it
      there).
    * ``MEDIUM`` — one-shot JSON judge / cheap triage (haiku-class) — the
      chase-verifier shape.
    * ``SMALL`` — the categorizer / classifier rung (the ``summarizer``
      alias) — per-chunk gloss, inject-scan. Tool-less by construction.
    """

    FRONTIER = "frontier"
    BIG = "big"
    MEDIUM = "medium"
    SMALL = "small"


#: Capability tiers + placement chains Phase C retired these five location-coupled tier strings from the
#: enum. A *stored* value written before this ship (a quest's
#: ``meta.loop.tier``, an already-baked ``quest_tick`` job's
#: ``meta.params.tier``, a route-log ``llm_call_log.tier`` row, …) can still
#: carry one — mapped here onto its capability-tier analogue so
#: :func:`tier_from_str` degrades instead of raising. New code should never
#: write these.
_LEGACY_TIER_ALIASES: dict[str, Tier] = {
    "cloud-super": Tier.FRONTIER,
    "cloud-mid": Tier.BIG,
    "cloud-small": Tier.MEDIUM,
    "local-small": Tier.SMALL,
    "local-big": Tier.BIG,
}


def tier_from_str(value: str, *, default: Tier = Tier.MEDIUM) -> Tier:
    """Resolve ``value`` to a :class:`Tier` — a live capability-tier string,
    or one of the five retired legacy strings (see
    :data:`_LEGACY_TIER_ALIASES`) — without ever raising. Anything else
    unrecognized (a typo, a future/unknown value) logs and falls back to
    ``default`` (``MEDIUM``) rather than crash-looping the caller. Callers
    resolving a **stored** tier value (as opposed to a fresh CLI/API argument,
    where a typo raising immediately is the right, loud failure) should use
    this instead of a bare ``Tier(value)``.
    """
    try:
        return Tier(value)
    except ValueError:
        pass
    alias = _LEGACY_TIER_ALIASES.get(value)
    if alias is not None:
        return alias
    log.warning(
        "router: unrecognized tier %r — falling back to %s", value, default.value
    )
    return default


class Transport(StrEnum):
    """Which wrapper carries a request.

    * ``CLAUDE_AGENT`` — :func:`precis.utils.claude_agent.call_claude_agent`
      (multi-turn, MCP tools, stream-json result event).
    * ``CLAUDE_P`` — :func:`precis.utils.claude_p.call_claude_p`
      (one-shot, no tools, last-JSON-block parse).
    * ``LOCAL`` — the loopback ``LlmClient`` (OpenAI
      ``/v1/chat/completions``, tool-less local completion). Was named
      ``LITELLM`` for the now-retired central litellm ``:4000`` proxy this
      used to front (migration 0107 renamed the enum + backfilled the
      ``llm_call_log`` history); the wire it denotes is the
      served_by-direct/loopback OpenAI-compat path, not a proxy.
    * ``OPENAI_COMPAT`` — the same OpenAI ``/v1/chat/completions`` wire
      pointed at a *hosted* OSS backend (OpenRouter / DeepInfra / a
      remote vLLM), authed with a vault-resolved key. Tool-less (the
      one-shot / completion path); tool-using calls go to ``OPENAI_TOOLS``.
    * ``OPENAI_TOOLS`` — an OSS model driving the precis verbs over the
      OpenAI ``tools=`` wire, in-process (:mod:`precis.utils.llm.openai_tools`
      + :mod:`precis.utils.llm.precis_tools`). Serves both a served local
      model backing the ``BIG`` tier and the ``OPENAI`` backend's tool-using
      cloud calls — same wire, different base url. Implements the in-process litellm tool
      loop that was prototyped-then-reversed onto ``claude``.
    """

    CLAUDE_AGENT = "claude_agent"
    CLAUDE_P = "claude_p"
    LOCAL = "local"
    OPENAI_COMPAT = "openai_compat"
    OPENAI_TOOLS = "openai_tools"

    @property
    def carries_tools(self) -> bool:
        """Can a call on this wire actually invoke a precis verb?

        Only two transports advertise tools: ``CLAUDE_AGENT`` (MCP over
        ``claude -p --mcp-config``) and ``OPENAI_TOOLS`` (the in-process
        ``tools=`` loop). The other three are completion wires — they accept
        a ``tools_needed=True`` request without complaint and return prose.

        This is the invariant :func:`select_transport` is written around,
        named so the operator-override path (:func:`resolve_chain`) can
        enforce the same thing instead of re-deriving it.
        """
        return self in (Transport.CLAUDE_AGENT, Transport.OPENAI_TOOLS)


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
# Each row is ``tier: (env_var, default)``. The cloud triad (FRONTIER/BIG/
# MEDIUM) is the *pinned* set from ``plan_tick._model_alias`` —
# ``PRECIS_MODEL_{OPUS,SONNET,HAIKU}`` — which is the most deliberate of the
# scattered reads (it pins a model *id* so ``llm_tier='opus'`` binds to one
# generation as the CLI default drifts). The FRONTIER default is
# ``claude-opus-4-8`` — the consolidation point for the whole cloud reasoning
# tier (dream, tex-fix, reviewers, fix-gripe, the generic ``claude_agent``
# default all resolve through here). 4-7 and 4-8 are the same price, so there
# is no cost reason to stay on 4-7 and the reasoning/agentic work is exactly
# where the stronger model earns its keep. ``claude_p``'s legacy suffix-less
# ``claude-haiku-4-5`` default is folded onto the dated pin here (same
# family).
_TIER_MODEL: dict[Tier, tuple[str, str]] = {
    Tier.FRONTIER: ("PRECIS_MODEL_OPUS", "claude-opus-4-8"),
    Tier.BIG: ("PRECIS_MODEL_SONNET", "claude-sonnet-5"),
    Tier.MEDIUM: ("PRECIS_MODEL_HAIKU", "claude-haiku-4-5-20251001"),
    # The local ``summarizer`` alias (``LlmConfig.model`` default), read
    # from ``PRECIS_SUMMARIZE_MODEL`` exactly as ``LlmConfig.from_env``.
    Tier.SMALL: ("PRECIS_SUMMARIZE_MODEL", "summarizer"),
}

# Import-time totality guard: every Tier must have a resolver row, so
# adding a tier without a model is a load-time failure, not a KeyError
# at dispatch (mirrors the TodoView totality assert in handlers/todo.py).
assert set(_TIER_MODEL) == set(Tier), "resolve_model: tier table is not total"

#: The tiers that route to a *claude* transport under the ANTHROPIC backend —
#: the only ones an OSS model override is incoherent for (see resolve_model's
#: Part-3 coherence check). ``SMALL`` is never claude-bound (it always routes
#: to the local/hosted-OSS split via :func:`select_transport`), so it is not
#: here.
_CLOUD_TIERS: frozenset[Tier] = frozenset({Tier.FRONTIER, Tier.BIG, Tier.MEDIUM})


def resolve_model(tier: Tier, backend: Backend | None = None) -> str:
    """The concrete model id for ``tier`` — the ONE place model
    selection lives.

    Resolution order: a web-set ``app_settings`` override (the ``/factory``
    live switch, :func:`precis.utils.llm.live_config.model_override`) → the
    env var → the compiled default in :data:`_TIER_MODEL`. With no override
    row (or no store bound) the DB tier is a no-op, so a caller resolves
    byte-for-byte to the model it uses today.

    ``backend`` (default ``None`` — no coherence check, today's behavior for
    every caller that doesn't pass it) is the *effective*, already-demoted
    backend a dispatch call is about to run on
    (``glm-fleet-flip-safety`` (git-only) Part 3). When it is
    :data:`Backend.ANTHROPIC` and the ``app_settings`` override resolves to a
    non-claude slug (heuristic: doesn't start with ``"claude"``), the override
    is incoherent with the claude transport it would land on — drop it and
    fall through to the env var / compiled default instead. This is what
    keeps a half-applied flip (backend demoted to ``ANTHROPIC`` for a missing
    ``PRECIS_LLM_BASE_URL``, but an OSS ``app_settings`` model override still
    set) from handing an OSS model id to a claude transport — the ``dream``
    ``api_error`` class. :func:`dispatch` passes its post-demotion
    ``backend`` here; every other caller (claude-only helpers that never see
    an OSS override) leaves it ``None`` and is unaffected.
    """
    from precis.utils.llm import live_config

    override = live_config.model_override(tier)
    # The coherence drop applies ONLY to the cloud tiers: they are the ones
    # that route to a claude transport under ANTHROPIC, so an OSS override
    # there is incoherent. SMALL (→LOCAL/OPENAI_COMPAT) never touches a
    # claude transport, so its (always-non-claude) override is legitimate and
    # must be honored — dropping it would silently ignore a live
    # `llm.model.small` row (incl. the one Part 1's remap reads).
    claude_bound = backend is Backend.ANTHROPIC and tier in _CLOUD_TIERS
    if override and not (claude_bound and not override.startswith("claude")):
        return override
    env_var, default = _TIER_MODEL[tier]
    return os.environ.get(env_var, default)


#: Per-tier ``(thinking, temperature)`` default — capability tiers + placement chains gen-param
#: passthrough (:attr:`LlmRequest.thinking` / :attr:`LlmRequest.temperature`).
#: ``SMALL`` (the categorizer/classifier rung) gets thinking
#: **off** + temperature **0.0**: a per-chunk gloss/inject-scan must not spend
#: reasoning tokens on a one-line judgment, must answer deterministically, and
#: — the fix this table exists for — must not degrade to an empty completion
#: when the tier's model is a local *thinking-only* model (leaving thinking on
#: there burns the whole budget on the reasoning trace with nothing left for
#: the answer). Every other tier gets thinking **on** + temperature **None**
#: (the provider's own default, sent as no field at all) — today's implicit
#: behaviour for the bigger/agentic tiers, made explicit here rather than
#: changed.
_TIER_GEN_DEFAULTS: dict[Tier, tuple[bool, float | None]] = {
    Tier.SMALL: (False, 0.0),
    Tier.MEDIUM: (True, None),
    Tier.BIG: (True, None),
    Tier.FRONTIER: (True, None),
}

# Import-time totality guard, mirroring _TIER_MODEL's above.
assert set(_TIER_GEN_DEFAULTS) == set(Tier), (
    "_tier_gen_defaults: tier table is not total"
)


def _tier_gen_defaults(tier: Tier) -> tuple[bool, float | None]:
    """``tier``'s ``(thinking, temperature)`` default — see
    :data:`_TIER_GEN_DEFAULTS`. :func:`dispatch` uses this only when the
    caller's :class:`LlmRequest` left the corresponding field ``None``; an
    explicit request value always wins."""
    return _TIER_GEN_DEFAULTS[tier]


# ── planner model aliases (the meta.llm_tier dropdown vocab) ───────────
#
# ``meta.llm_tier`` on a todo names a *capability tier*, not a
# vendor model: the dispatcher synthesizes ``plan_tick``'s ``model`` param
# from it and the tick resolves the concrete model via :func:`resolve_model`.
# This is the ONE ordered source the dispatcher (plan_tick), the closed-vocab
# guards, and the web model-pickers key on, so the tier map and the dropdown
# never drift. ``local`` is the cluster's served OSS tier (``qwen-heavy`` +
# tools), reachable now that LLM routing seam's ``OPENAI_TOOLS`` loop drives the verbs
# in-process — a planner tick runs on it just like the cloud tiers.
#
# ``local`` now pins ``BIG`` directly (the location-coupled
# ``LOCAL_BIG`` tier is retired) — a served OSS model still backs it when the
# backend/chain routes there (:func:`select_transport` / a served_by slot);
# the legacy {opus, sonnet, haiku} aliases are otherwise unchanged.
PLANNER_TIER_BY_ALIAS: dict[str, Tier] = {
    "opus": Tier.FRONTIER,
    "sonnet": Tier.BIG,
    "haiku": Tier.MEDIUM,
    "local": Tier.BIG,
    "frontier": Tier.FRONTIER,
    "big": Tier.BIG,
    "medium": Tier.MEDIUM,
    "small": Tier.SMALL,
}

#: Ordered alias vocabulary — dropdown order AND the ``LLM:`` closed-vocab set.
PLANNER_MODEL_ALIASES: tuple[str, ...] = tuple(PLANNER_TIER_BY_ALIAS)


#: TTL (seconds) for the per-model catalog-card cache backing
#: :func:`planner_model_choices` — mirrors :data:`live_config._TTL_S` so a
#: page rendering several pickers doesn't re-query the ``llm`` catalog per
#: dropdown, while a ``params``/``capability`` edit still surfaces within one
#: cache window instead of needing a process restart.
_CARD_TTL_S = 15.0
_card_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
# Same check-then-set guard live_config._cache carries — keeps concurrent web
# renders from racing duplicate catalog queries for the same model id.
_card_lock = threading.Lock()


def _catalog_card_meta(model: str) -> dict[str, Any] | None:
    """Best-effort ``llm`` catalog-card ``meta`` for ``model`` — the ``size``/
    ``context`` decoration on the picker. Reads through
    :func:`precis.budget.meter.active_store` (the same channel
    :mod:`live_config` reads through); no store bound, no matching card, or
    any lookup failure all degrade to ``None`` rather than raising — this must
    never break :func:`planner_model_choices`. TTL-cached per model id
    (:data:`_CARD_TTL_S`).
    """
    with _card_lock:
        now = time.monotonic()
        cached = _card_cache.get(model)
        if cached is not None and cached[0] > now:
            return cached[1]
        meta: dict[str, Any] | None = None
        try:
            from precis.budget import meter

            store = meter.active_store()
            if store is not None:
                ref = store.find_ref_by_meta(kind="llm", key="model_id", value=model)
                if ref is not None:
                    meta = ref.meta
        except Exception:
            meta = None
        _card_cache[model] = (now + _CARD_TTL_S, meta)
        return meta


def planner_model_choices() -> list[dict[str, Any]]:
    """THE model-picker source — one row per ``(alias, tier)`` in
    :data:`PLANNER_TIER_BY_ALIAS`, reflecting what the tier *actually* routes
    to right now.

    ``model`` is rung 0 of the LIVE placement chain (:func:`resolve_chain`) —
    the same resolver :func:`dispatch` walks — not the bare
    :func:`resolve_model` tier default, so an operator ``llm.chain.<tier>``
    override (e.g. the BIG-routes-local-first chain) shows up here
    instead of a stale cloud label. ``tools_needed`` is ``True`` for every
    tier but ``SMALL``: FRONTIER/BIG/MEDIUM feed planner ticks (tool-using
    agentic dispatch), so the picker must mirror the chain a tool-needing call
    walks, not the tool-less one. ``placement`` (``"local"``/``"cloud"``) and
    ``fallbacks`` (the ordered, de-duped rest of the chain) describe that same
    chain; ``size``/``context`` are best-effort ``llm`` catalog-card meta
    (:func:`_catalog_card_meta`) — ``None`` when no card exists or no store is
    bound, never a hard failure.

    A per-alias resolution error degrades that row to a bare
    ``{alias, tier, model, placement: None, fallbacks: [], size: None,
    context: None}`` shape rather than raising, so one bad tier can't blank
    the whole dropdown.
    """
    try:
        backend = resolve_backend()
    except Exception:
        backend = Backend.ANTHROPIC

    rows: list[dict[str, Any]] = []
    for alias, tier in PLANNER_TIER_BY_ALIAS.items():
        try:
            tools_needed = tier is not Tier.SMALL
            chain = resolve_chain(tier, tools_needed=tools_needed, backend=backend)
            default_model = resolve_model(tier)
            if chain:
                rung0 = chain[0]
                model = rung0.model or default_model
                placement = "cloud" if _rung_is_cloud(rung0) else "local"
                seen = {model}
                fallbacks: list[str] = []
                for r in chain[1:]:
                    # A rung with ``model=None`` inherits the tier's resolved
                    # primary (same rule dispatch applies), NOT rung 0's model.
                    fb = r.model or default_model
                    if fb not in seen:
                        fallbacks.append(fb)
                        seen.add(fb)
            else:
                model = default_model
                placement = "cloud"
                fallbacks = []

            card = _catalog_card_meta(model)
            size: Any = None
            context: Any = None
            if card:
                params = card.get("params")
                if isinstance(params, dict):
                    size = params.get("size")
                capability = card.get("capability")
                if isinstance(capability, dict):
                    context = capability.get("max_input")

            rows.append(
                {
                    "alias": alias,
                    "tier": tier.value,
                    "model": model,
                    "placement": placement,
                    "fallbacks": fallbacks,
                    "size": size,
                    "context": context,
                }
            )
        except Exception:
            log.warning(
                "router: planner_model_choices failed for alias %r",
                alias,
                exc_info=True,
            )
            # ``resolve_model`` may be the very call that failed above —
            # degrade to the alias itself rather than raise out of the
            # degrade path and blank the whole dropdown.
            try:
                fallback_model = resolve_model(tier) or alias
            except Exception:
                fallback_model = alias
            rows.append(
                {
                    "alias": alias,
                    "tier": tier.value,
                    "model": fallback_model,
                    "placement": None,
                    "fallbacks": [],
                    "size": None,
                    "context": None,
                }
            )
    return rows


def rung_knobs(rung: Rung) -> dict[str, Any]:
    """Static per-transport honesty table for the structured-selection UI's
    knob greying — which gen-param controls (`temperature` / `thinking` /
    `effort`) a rung's transport actually forwards to the wire, mirroring what
    each provider adapter genuinely does with :class:`LlmRequest`'s fields
    rather than accepting-and-ignoring one silently.

    Returns ``{"temperature": bool, "temp_max": float | None, "thinking":
    bool, "effort": bool}``. Per transport:

    * ``CLAUDE_AGENT`` / ``CLAUDE_P`` — none of the three: the claude
      transports have no such knobs at all (see :attr:`LlmRequest.thinking`'s
      docstring — Anthropic's extended-thinking budget is a separate, unrelated
      knob these fields do not touch).
    * ``LOCAL`` — ``temperature`` (max ``2.0``, the OpenAI-compat wire's
      range); ``thinking`` is deliberately **not** forwarded (see the NOTE in
      :func:`_dispatch_local` — no confirmed backend-specific key for the
      deployed llama.cpp/llama-swap build); ``effort`` unsupported (no
      OpenRouter-style ``reasoning.effort`` on a bare local completion call).
    * ``OPENAI_COMPAT`` — ``temperature`` (max ``2.0``), ``thinking`` (the
      capability tiers + placement chains toggle, honored via :func:`openrouter_routing`'s
      ``reasoning.enabled``), ``effort`` (``reasoning.effort``, same
      function) — the OpenRouter-fronted completion wire honors all three.
    * ``OPENAI_TOOLS`` — ``temperature`` (max ``2.0``); ``thinking`` only when
      ``PRECIS_LLM_BASE_URL`` is set — :func:`run_oss_tool_loop` applies the
      no-thinking directive only for a genuinely *hosted* call, never a
      direct local llama-swap endpoint (no confirmed key there either, same
      caveat as ``LOCAL``); ``effort`` is never forwarded on this transport
      (:func:`run_oss_tool_loop` takes no ``effort`` parameter).
    """
    if rung.transport in (Transport.CLAUDE_AGENT, Transport.CLAUDE_P):
        return {
            "temperature": False,
            "temp_max": None,
            "thinking": False,
            "effort": False,
        }
    if rung.transport is Transport.LOCAL:
        return {
            "temperature": True,
            "temp_max": 2.0,
            "thinking": False,
            "effort": False,
        }
    if rung.transport is Transport.OPENAI_COMPAT:
        return {
            "temperature": True,
            "temp_max": 2.0,
            "thinking": True,
            "effort": True,
        }
    # Transport.OPENAI_TOOLS
    return {
        "temperature": True,
        "temp_max": 2.0,
        "thinking": bool(os.environ.get("PRECIS_LLM_BASE_URL")),
        "effort": False,
    }


#: The UI-level combined reasoning selector → the ``(thinking, effort)`` pair
#: :func:`resolve_selection` / a future dispatch caller thread onto
#: :attr:`LlmRequest.thinking` / :attr:`.effort`.
_REASONING_TO_KNOBS: dict[str, tuple[bool | None, str | None]] = {
    "off": (False, None),
    "low": (True, "low"),
    "medium": (True, "medium"),
    "high": (True, "high"),
}


def reasoning_to_knobs(reasoning: str | None) -> tuple[bool | None, str | None]:
    """Map the UI's combined reasoning selector to the
    ``(thinking, effort)`` pair :class:`LlmRequest` actually carries.

    ``None`` or ``"default"`` → ``(None, None)`` — let :func:`dispatch`
    resolve the tier's own default (:func:`_tier_gen_defaults`), the same as
    a caller that never touched the selector. ``"off"`` → ``(False, None)`` —
    thinking off, no effort level. ``"low"``/``"medium"``/``"high"`` →
    ``(True, <value>)`` — thinking on, at that effort. Any other (unknown)
    string degrades to the same ``(None, None)`` "resolve the default" pair
    as ``None``, never raising.
    """
    if reasoning is None or reasoning == "default":
        return (None, None)
    return _REASONING_TO_KNOBS.get(reasoning, (None, None))


def resolve_selection(
    alias: str,
    *,
    placement: str | None = None,
    reasoning: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Preview what :func:`dispatch` **would** pick for a structured
    ``(alias, placement, reasoning, temperature)`` selection, without making a
    call — the ``/factory`` picker's live preview row. Never raises; every
    failure degrades to an ``error``-carrying row of the same shape.

    Resolution mirrors :func:`planner_model_choices` (alias → tier →
    :func:`resolve_chain`) with :func:`_apply_placement` layered in, same as
    :func:`dispatch`. Return keys are always present:
    ``alias, tier, model, transport, placement_effective, fallbacks, knobs,
    size, context, warnings, error, temp_default`` — ``error`` is ``None`` on
    success. ``temp_default`` is the sampling temperature :func:`dispatch`
    would apply when the caller leaves ``temperature`` unset: ``None`` when
    the resolved rung ignores temperature (:func:`rung_knobs`) or the
    provider's own default otherwise applies; else the catalog card's
    ``gen_defaults.temperature`` override when present and in ``[0, 2]``;
    else the tier's :func:`_tier_gen_defaults` temperature.

    ``warnings`` are honesty notes computed against the resolved rung's
    :func:`rung_knobs` — e.g. a ``temperature`` given to a route that ignores
    it, or a ``reasoning`` level set where the route only supports on/off (or
    ignores the setting altogether). These are advisory only; resolution
    itself never fails because of them.
    """
    degraded: dict[str, Any] = {
        "alias": alias,
        "tier": None,
        "model": None,
        "transport": None,
        "placement_effective": None,
        "fallbacks": [],
        "knobs": None,
        "size": None,
        "context": None,
        "warnings": [],
        "error": None,
        "temp_default": None,
    }

    tier = PLANNER_TIER_BY_ALIAS.get(alias.lower())
    if tier is None:
        degraded["error"] = f"unknown tier/alias {alias!r}"
        return degraded

    degraded["tier"] = tier.value

    try:
        backend = resolve_backend()
    except Exception:
        backend = Backend.ANTHROPIC

    tools_needed = tier is not Tier.SMALL
    try:
        chain = resolve_chain(tier, tools_needed=tools_needed, backend=backend)
    except Exception:
        log.warning(
            "router: resolve_selection failed resolving chain for alias %r",
            alias,
            exc_info=True,
        )
        degraded["error"] = f"failed to resolve the chain for tier {tier.value}"
        return degraded

    placed = _apply_placement(chain, placement)
    if chain and not placed:
        degraded["error"] = f"no {placement} rung for tier {tier.value}"
        return degraded

    try:
        default_model = resolve_model(tier)
    except Exception:
        default_model = alias

    if not placed:
        degraded["model"] = default_model
        degraded["error"] = f"no rung resolved for tier {tier.value}"
        return degraded

    rung0 = placed[0]
    model = rung0.model or default_model
    placement_effective = "cloud" if _rung_is_cloud(rung0) else "local"

    seen = {model}
    fallbacks: list[str] = []
    for r in placed[1:]:
        # A rung with model=None inherits the tier's resolved primary (same
        # de-dup rule as planner_model_choices), NOT rung 0's model.
        fb = r.model or default_model
        if fb not in seen:
            fallbacks.append(fb)
            seen.add(fb)

    knobs = rung_knobs(rung0)

    card = _catalog_card_meta(model)
    size: Any = None
    context: Any = None
    if card:
        params = card.get("params")
        if isinstance(params, dict):
            size = params.get("size")
        capability = card.get("capability")
        if isinstance(capability, dict):
            context = capability.get("max_input")

    warnings: list[str] = []
    if temperature is not None and not knobs["temperature"]:
        warnings.append("temperature is ignored on this route")
    if reasoning in ("low", "medium", "high"):
        if not knobs["effort"] and knobs["thinking"]:
            warnings.append(
                "reasoning levels are not supported on this route — on/off only"
            )
        elif not knobs["thinking"] and not knobs["effort"]:
            warnings.append("reasoning setting is ignored on this route")
    elif reasoning is not None and reasoning != "default":
        if not knobs["thinking"] and not knobs["effort"]:
            warnings.append("reasoning setting is ignored on this route")

    temp_default: float | None = None
    if knobs["temperature"]:
        card_temp = None
        if card:
            gen_defaults = card.get("gen_defaults")
            if isinstance(gen_defaults, dict):
                candidate = gen_defaults.get("temperature")
                if isinstance(candidate, (int, float)) and not isinstance(
                    candidate, bool
                ):
                    if 0 <= candidate <= 2:
                        card_temp = float(candidate)
        temp_default = (
            card_temp if card_temp is not None else _tier_gen_defaults(tier)[1]
        )

    return {
        "alias": alias,
        "tier": tier.value,
        "model": model,
        "transport": rung0.transport.value,
        "placement_effective": placement_effective,
        "fallbacks": fallbacks,
        "knobs": knobs,
        "size": size,
        "context": context,
        "warnings": warnings,
        "error": None,
        "temp_default": temp_default,
    }


#: Web-payload keys :func:`llm_select_from_payload` accepts for ``placement``.
_SELECT_PLACEMENT_VALUES: frozenset[str] = frozenset({"local", "cloud"})


def llm_select_from_payload(
    *,
    placement: str | None = None,
    reasoning: str | None = None,
    temperature: str | float | int | None = None,
) -> dict[str, Any]:
    """Build a ``meta.llm_select`` dict from raw web-form/JSON knobs.

    The single shared mapping behind every write path that lets a caller
    thread a structured selection (placement / reasoning / temperature) onto
    a todo (the smartdraft ask, the retry form) — one source so the two
    routes can't drift on what counts as a valid knob.

    Degrades a junk value to *absent* rather than raising: an unrecognised
    ``placement``, an unmapped ``reasoning`` string, or an unparsable/
    out-of-range ``temperature`` is silently dropped from the result — the
    caller (a websocket ask, a retry form post) must not 500 on a typo'd
    knob. Returns ``{}`` when nothing valid was supplied, so callers can
    test truthiness to decide whether to set ``meta['llm_select']`` at all.
    """
    select: dict[str, Any] = {}
    # Guard against a non-str web knob (e.g. a stray list/dict in the JSON
    # body) *before* it reaches a frozenset/dict membership test below —
    # those raise TypeError on an unhashable key, which would turn a junk
    # knob into a 500 instead of the required silent degrade.
    if not isinstance(placement, str):
        placement = None
    if not isinstance(reasoning, str):
        reasoning = None
    if placement in _SELECT_PLACEMENT_VALUES:
        select["placement"] = placement
    thinking, effort = reasoning_to_knobs(reasoning)
    if thinking is not None:
        select["thinking"] = thinking
    if effort is not None:
        select["effort"] = effort
    if temperature is not None:
        try:
            t = float(temperature)
        except (TypeError, ValueError):
            t = None
        if t is not None and 0 <= t <= 2:
            select["temperature"] = t
    return select


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

    ``SMALL`` is tool-less by construction and routes to the local/hosted-OSS
    split regardless of ``tools_needed``: under ``ANTHROPIC`` (the default) it
    takes the loopback local completion wire (:data:`Transport.LOCAL`); under
    ``OPENAI`` it takes :data:`Transport.OPENAI_COMPAT` (a hosted OSS backend —
    OpenRouter — with no local hardware fallback of its own). This is the
    OpenRouter-bypass hosted-small gap-fill.

    ``MEDIUM`` / ``BIG`` / ``FRONTIER`` split on ``tools_needed``, which
    mirrors the ``AGENT`` vs ``HELPER`` :class:`~precis.utils.prompt.model.Profile`
    split: tools ⇒ ``claude_agent`` (AGENT), no tools ⇒ ``claude_p`` (HELPER).
    ``backend`` (default ``ANTHROPIC``, so existing callers are unchanged)
    routes cloud work to the OSS path when ``OPENAI``: tool-less →
    :data:`Transport.OPENAI_COMPAT`, tool-using → :data:`Transport.OPENAI_TOOLS`
    (the in-process ``tools=`` loop, also how a served local model backs
    ``BIG`` under an ``OPENAI_TOOLS``-carrying chain rung). Under ``ANTHROPIC``
    both stay on the ``claude`` transports.
    """
    if tier is Tier.SMALL:
        return Transport.OPENAI_COMPAT if backend is Backend.OPENAI else Transport.LOCAL
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
    the profile. Kept thin so the profile→router alignment is explicit.
    """
    from precis.utils.prompt.model import Profile as _Profile

    return select_transport(tier, tools_needed=profile is _Profile.AGENT)


# ── the normalized result ──────────────────────────────────────────────


class _HasText(Protocol):
    """Duck type for the local ``LlmClient.complete`` result.

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
      it is the stream-json result text; for the local transport it is
      the OpenAI choice content.
    * ``cost_usd`` — best-effort USD cost (``None`` when the transport
      doesn't report one, e.g. the loopback local transport).
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
    * ``timed_out`` — ``True`` when ``error`` is a *wall-clock timeout* (the
      streamed hard ceiling / idle timeout, or a one-shot transport's socket
      cap). A strict refinement of ``paused``: a timeout is always also a
      ``paused`` unavailability, but not the reverse (a 429, a connection
      failure, a worker drain). Callers that merely back off don't need the
      split; a caller holding a bounded give-up budget does, because the two
      retry differently — a capped window or a drain clears by itself, whereas
      re-sending an identical prompt to an identical rung that just exhausted
      its wall clock exhausts it again, deterministically, forever (2026-08-13:
      all 10 of the ``quest_tick`` BIG chain's 900s-ceiling trips in 7 days were
      the same cloud rung, none recovered on retry). Read by
      :func:`~precis.quest.tick.run_quest_tick` to stamp
      :attr:`~precis.quest.tick.QuestTickOutcome.pause_kind`.
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
    timed_out: bool = False
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
    #: Token telemetry from a ``claude_agent`` stream-json run's trailing
    #: ``result`` event (:func:`~precis.utils.claude_agent.AgentResult`'s
    #: matching fields — already a cumulative total for the whole run, not a
    #: per-turn delta). ``None`` for the non-agent transports and for any
    #: ``claude_agent`` run without a stream to read (mirrors ``tool_calls``'
    #: never-a-false-zero discipline).
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    #: ``'local'`` / ``'cloud'`` for the rung that ACTUALLY ran, classified by
    #: :func:`_rung_is_cloud`. Stamped from the *winning* rung, not rung 0 —
    #: a local primary that fell back to a cloud rung really did spend money,
    #: and recording it as local would hide that from the cost caps. ``None``
    #: only when no rung was resolved (an early error return).
    placement: str | None = None


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
        input_tokens=res.input_tokens,
        output_tokens=res.output_tokens,
        cache_read_tokens=res.cache_read_tokens,
        cache_creation_tokens=res.cache_creation_tokens,
    )


def result_from_claude_p(res: ClaudePResult, *, model: str, tier: Tier) -> LlmResult:
    """Normalize a :class:`~precis.utils.claude_p.ClaudePResult`.

    ``text`` is the assistant's reply with the metering envelope stripped;
    ``data`` carries the parsed JSON dict so a judge caller reads
    ``LlmResult.data`` exactly as it read ``ClaudePResult.data``.

    The ``or res.raw_stdout`` fallback is what keeps this identical to the
    pre-metering behaviour for a legacy 3-field ``ClaudePResult`` (test fakes)
    and for any stdout that wasn't an envelope. Without it, turning on
    ``--output-format json`` would have quietly redefined ``LlmResult.text``
    as the metadata envelope for every ``claude_p`` caller — including the
    ``res.data or _parse_json_object(res.text)`` fallbacks in
    :mod:`precis.taproot.canon` and :mod:`precis.utils.llm.requirement`, which
    would then parse the envelope's own keys as if they were the judge's answer.
    """
    return LlmResult(
        text=res.text or res.raw_stdout,
        cost_usd=res.cost_usd,
        turns_used=None,
        model=model,
        tier=tier,
        data=res.data,
        input_tokens=res.input_tokens,
        output_tokens=res.output_tokens,
        cache_read_tokens=res.cache_read_tokens,
        cache_creation_tokens=res.cache_creation_tokens,
    )


def result_from_openai(res: _HasText, *, model: str, tier: Tier) -> LlmResult:
    """Normalize a local ``LlmClient.complete`` result (OpenAI choices).

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

    Tokens: ``prompt_tokens``/``completion_tokens`` (the same OpenAI
    ``usage`` split the cost estimate above prices) map straight onto
    :attr:`LlmResult.input_tokens`/``output_tokens``. No cache split is set —
    a local/OpenRouter completion's ``usage`` block carries no cache-read/
    -write breakdown, so inventing one would be a silent fabrication rather
    than an honest ``None``.

    All fields are read leniently (``getattr``) so a bare ``.text`` fake still
    normalizes.
    """
    from precis.budget.pricing import cost_from_tokens
    from precis.utils.claude_p import _parse_last_json_block

    prompt_tokens = getattr(res, "prompt_tokens", None)
    completion_tokens = getattr(res, "completion_tokens", None)
    cost = getattr(res, "cost_usd", None)
    if cost is None:
        cost = cost_from_tokens(
            model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
    return LlmResult(
        text=res.text,
        cost_usd=cost,
        turns_used=None,
        model=model,
        tier=tier,
        data=_parse_last_json_block(res.text),
        total_tokens=getattr(res, "total_tokens", None),
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
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
    #: Stream the completion over SSE (honoured by the ``OPENAI_TOOLS``
    #: transport; ignored elsewhere). Streaming changes the timeout semantics:
    #: ``timeout_s`` becomes the *hard wall ceiling* per turn while a separate
    #: idle timeout (:func:`_stream_idle_timeout_s`) detects a dead connection
    #: — so a model actively emitting reasoning keeps its connection instead
    #: of being cut mid-thought by a blind socket cap, and on any abort the
    #: partial output is preserved (``StreamTimeout`` → ``LlmResult.text``)
    #: rather than lost. Default ``False`` = the blocking POST, byte-identical
    #: to before this knob existed.
    stream: bool = False
    #: Completion-length cap. For the local / openai-compat transports this is
    #: the ``max_tokens`` field of the underlying ``LlmConfig`` — a real,
    #: generation-time stop; ``OPENAI_TOOLS`` puts it on each turn's wire
    #: payload for the same effect (reachable since 2026-08-13 — see
    #: :func:`run_oss_tool_loop`). ``None`` keeps ``LlmConfig.from_env``'s default
    #: (220) — the summarizer's short-gloss cap. A pass with a longer
    #: structured payload pins its own (e.g. paper_glossary needs 2000, else
    #: the JSON truncates); this is the knob that lets a direct-``LlmClient``
    #: caller fold through ``dispatch`` without silently shrinking its budget.
    #: ``claude_agent`` (the ``claude`` CLI) has no completion-length flag at
    #: all — only ``--max-turns`` (turn count) and ``--max-budget-usd``
    #: (dollar cost), neither of which bounds a single response's length —
    #: so :class:`ClaudeAgentProvider` treats a set ``max_tokens`` as a
    #: **best-effort post-hoc truncation** of the final text (see
    #: :func:`_truncate_to_max_tokens`), not a real stop: the full (costly)
    #: response is still generated. A caller that needs a hard generation-time
    #: cap must use a local/openai-compat tier instead. ``None`` (the default)
    #: leaves ``claude_agent`` output untruncated, as before this knob existed.
    max_tokens: int | None = None
    #: Reasoning + sampling passthrough (gen-param knobs). ``None`` on
    #: either ⇒ :func:`dispatch` resolves the tier default (:func:`_tier_gen_defaults`):
    #: ``SMALL`` (a categorizer/classifier — per-chunk gloss,
    #: inject-scan) wants thinking **off** + temperature **0.0**, so it never
    #: burns reasoning tokens on a one-line judgment and stays deterministic —
    #: this is also what makes the tier runnable on a local *thinking-only*
    #: model (thinking left on would otherwise degrade the completion to a
    #: reasoning trace with an empty/truncated final answer). Every other tier
    #: (``MEDIUM``/``BIG``/``FRONTIER``) wants thinking **on** + temperature
    #: **None** (the provider's own default — the field is omitted from the
    #: wire rather than pinned).
    #: An explicit non-``None`` value set here always wins over the tier
    #: default. No-op on the claude transports (``claude_agent``/``claude_p``
    #: have no such knobs — Anthropic's extended-thinking budget is a separate,
    #: unrelated knob this does not touch).
    thinking: bool | None = None
    temperature: float | None = None
    #: A booked OpenRouter variant (a ``meta.endpoints`` dict — provider / quant /
    #: window) + reasoning effort, pinned onto the ``openai_compat`` wire so the
    #: call reproducibly hits *that* provider×quant instead of OpenRouter load-
    #: balancing the ~28 (gripe 162624). ``None`` ⇒ today's behaviour (slug only).
    #: A ``select_offering`` caller threads ``Selection.endpoint`` here.
    endpoint: dict[str, Any] | None = None
    effort: str | None = None
    #: Strict rung filter on the resolved chain — ``'local'`` / ``'cloud'`` keep
    #: only the matching rungs (classified by :func:`_rung_is_cloud`); any other
    #: value (unknown string or ``None``) leaves the chain unchanged, walked as
    #: configured. This is **strict**, unlike :func:`_apply_cloud_throttle`'s
    #: fallback-to-paused behavior: a placement filter that empties an
    #: otherwise-nonempty chain is an *error* result (the caller asked for a
    #: rung that doesn't exist), not a silent degrade.
    placement: str | None = None
    #: Direct local-serving base URL (llama-swap's OpenAI endpoint), threaded in
    #: by :func:`dispatch` when a reserved :class:`~precis.utils.llm.local_serving.LocalSlot`
    #: declares an ``endpoint`` — the LOCAL transport routes here instead of the
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
    #: ``stream-json``, not ``text`` — this is where ``cost_usd`` comes from.
    #: ``claude -p`` reports ``total_cost_usd`` / ``num_turns`` / ``usage`` only
    #: in the trailing stream-json ``result`` event; on the text path
    #: ``AgentResult`` falls back to a stderr regex for ``Cost: $N.NN`` that
    #: modern Claude Code no longer prints, so cost lands ``NULL``. That was a
    #: silent per-call-site opt-in: the passes that happened to pass
    #: ``output_format="stream-json"`` (dream, review, plan_tick) billed
    #: visibly, and the ones that didn't (briefing 815 calls, meditation,
    #: card_forge, reading_brief) logged >1000 Claude calls at NULL cost —
    #: invisible to ``PRECIS_DAILY_COST_CEILING``, which sums this column.
    #: Defaulting it here makes cost the default and text the opt-out.
    #: Safe for the other transports (they ignore it, per the comment above)
    #: and for callers reading ``LlmResult.text``: the stream-json path lifts
    #: the result event's ``result`` field into ``final_text``, so ``text``
    #: stays the assistant's answer and only ``raw_text`` (the audit blob)
    #: changes shape.
    output_format: str = "stream-json"
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
    #: neutral cwd so ``claude -p`` discovers no ambient project persona. Ignored by the other transports. ``None`` ⇒ the worker's cwd.
    cwd: str | Path | None = None
    #: Real-time progress callback, awaited once per parsed ``stream-json``
    #: event as a ``claude_agent`` run streams (asa_bot's Discord "thinking…"
    #: updates, Phase 3 of the router-migration plan). Only
    #: :func:`dispatch_async` honors this — the sync :func:`dispatch` ignores
    #: it entirely (there is no streaming path on the blocking transport).
    #: ``None`` ⇒ no callback, and :func:`dispatch_async` degrades to calling
    #: the sync :func:`dispatch` for every transport (today's behavior).
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None


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


#: Rough words-per-token ratio for approximating a length cap on a transport
#: that has no native ``max_tokens`` knob — mirrors
#: ``precis.reading.cast_common._TOKENS_PER_WORD`` (kept as a local constant
#: here so this module doesn't reach into the reading layer).
_APPROX_TOKENS_PER_WORD = 1.4


def _truncate_to_max_tokens(text: str, max_tokens: int) -> str:
    """Best-effort post-hoc length cap for ``claude_agent``.

    The ``claude`` CLI has no completion-length flag (only ``--max-turns`` /
    ``--max-budget-usd``, neither of which bounds one response), so a caller
    that pins :attr:`LlmRequest.max_tokens` on a ``claude_agent`` call can't
    get a real generation-time stop — the model still runs to completion (and
    is still billed for it). This restores the *output-length* guarantee a
    duration-targeted pass needs (a nidra/brief that must fit its spoken-time
    budget) by cutting the text back down after the fact, snapping to the
    last paragraph break at/before the word budget so the cut isn't a
    mid-sentence guillotine (falls back to a sentence break, then a hard word
    cut, if there's no paragraph break early enough).
    """
    budget_words = max(1, int(max_tokens / _APPROX_TOKENS_PER_WORD))
    positions = list(re.finditer(r"\S+", text))
    if len(positions) <= budget_words:
        return text
    cutoff = positions[budget_words - 1].end()
    para_break = text.rfind("\n\n", 0, cutoff)
    if para_break > 0:
        return text[:para_break].rstrip()
    sentence_break = max(
        (
            text.rfind(marker, 0, cutoff)
            for marker in (". ", "! ", "? ", ".\n", "!\n", "?\n")
        ),
        default=-1,
    )
    if sentence_break > 0:
        return text[: sentence_break + 1].rstrip()
    return text[:cutoff].rstrip()


class ClaudeAgentProvider:
    """``claude -p`` multi-turn agent (MCP tools, stream-json result).

    Wraps :func:`~precis.utils.claude_agent.call_claude_agent` via the
    module global so a test that monkeypatches ``router.call_claude_agent``
    still intercepts it.

    ``bare`` comes from the chain rung (see :class:`Rung`) and forces
    API-key auth for this rung's calls.
    """

    def __init__(self, *, bare: bool = False) -> None:
        self._bare = bare

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        try:
            res = call_claude_agent(
                req.prompt,
                model=model,
                bare=self._bare,
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
        result = result_from_agent(res, model=model, tier=req.tier)
        if req.max_tokens is not None and result.error is None:
            # No CLI flag bounds a single response's length (see
            # LlmRequest.max_tokens) — truncate post-hoc so a caller that
            # pinned a budget (a duration-targeted cast) still gets output
            # that fits it, even though the full (costly) generation ran.

            result = _replace(
                result, text=_truncate_to_max_tokens(result.text, req.max_tokens)
            )
        return result


class ClaudePProvider:
    """``claude -p`` one-shot JSON judge (no tools, last-JSON-block).

    ``bare`` comes from the chain rung (see :class:`Rung`) and forces
    API-key auth for this rung's calls.
    """

    def __init__(self, *, bare: bool = False) -> None:
        self._bare = bare

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        try:
            pres = call_claude_p(
                req.prompt,
                model=model,
                max_usd=req.max_usd,
                timeout_s=req.timeout_s,
                extra_args=req.extra_args,
                bare=self._bare,
            )
        except ClaudeProcessError as exc:
            return _error_result(exc, model=model, tier=req.tier)
        return result_from_claude_p(pres, model=model, tier=req.tier)


class LocalProvider:
    """Loopback local ``LlmClient`` — OpenAI ``/v1/chat/completions``,
    tool-less local completion."""

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        return _dispatch_local(req, model)


class OpenAICompatProvider:
    """A *hosted* OpenAI-compatible OSS backend — OpenRouter / DeepInfra /
    a remote vLLM — over the same ``/v1/chat/completions`` wire as the
    loopback proxy, but at ``PRECIS_LLM_BASE_URL`` and authed with a
    vault-resolved key (:func:`_provider_api_key`, keyed off that url's
    host — see gripe 159988).

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

    The in-process tool loop, rebuilt behind the provider port:
    :func:`~precis.utils.llm.openai_tools.run_tool_loop` drives a hosted or
    local OSS backend (``PRECIS_LLM_BASE_URL``, vault key) through a
    tool-calling conversation, executing each call in-process via
    ``runtime.dispatch`` — no MCP socket round-trip. Serves both a served
    local model backing the ``BIG`` tier and the ``OPENAI`` backend's
    tool-using cloud calls.
    """

    def run(self, req: LlmRequest, *, model: str) -> LlmResult:
        return _dispatch_openai_tools(req, model)


# The Transport → provider registry: the ONE place a transport binds to a
# concrete backend. Swap or add a row to reroute without touching callers.
_PROVIDERS: dict[Transport, LlmProvider] = {
    Transport.CLAUDE_AGENT: ClaudeAgentProvider(),
    Transport.CLAUDE_P: ClaudePProvider(),
    Transport.LOCAL: LocalProvider(),
    Transport.OPENAI_COMPAT: OpenAICompatProvider(),
    Transport.OPENAI_TOOLS: OpenAIToolsProvider(),
}

# Import-time totality guard: every Transport must have a provider, so
# adding one without wiring a backend is a load-time failure, not a
# KeyError at dispatch (mirrors the _TIER_MODEL resolver assert above).
assert set(_PROVIDERS) == set(Transport), "dispatch: provider registry is not total"


def provider_for(transport: Transport, *, bare: bool = False) -> LlmProvider:
    """The provider bound to ``transport`` — the registry accessor a
    future config layer overrides to reroute a transport.

    ``bare=True`` (a rung's API-key opt-in) can't come from the shared
    registry, because the registry holds one stateless singleton per
    transport and flipping auth on it would leak onto every other caller of
    that transport. Build a per-rung instance instead; only the two claude
    transports carry the flag, so anything else ignores it and keeps the
    singleton.
    """
    if not bare:
        return _PROVIDERS[transport]
    if transport is Transport.CLAUDE_P:
        return ClaudePProvider(bare=True)
    if transport is Transport.CLAUDE_AGENT:
        return ClaudeAgentProvider(bare=True)
    log.warning(
        "llm-chain: bare=true on a %s rung has no effect — API-key auth is a "
        "claude-transport concept (claude_p / claude_agent); the OSS wires "
        "authenticate with their own vault-resolved keys.",
        transport.value,
    )
    return _PROVIDERS[transport]


# ── failover ladder (composes the port) ────────────────────────────────


@dataclass(frozen=True, slots=True)
class Rung:
    """One failover attempt: a :class:`Transport` + an optional model override.

    ``model=None`` uses the ``model`` :meth:`FailoverProvider.run` was given
    (the primary, tier-resolved one); a fallback rung pins its own — e.g. the
    claude safety net pins the tier's compiled-in claude id so a PRECIS_MODEL_*
    override pointing at an OSS id doesn't leak onto ``claude -p``.

    ``bare`` opts this rung into **API-key** auth (``ANTHROPIC_API_KEY``,
    billed per token) instead of the Max subscription's OAuth token. Only
    the two claude transports honor it; it is inert elsewhere. Default
    ``False`` everywhere, including both default ladders — moving spend onto
    the key is always an explicit per-rung decision, never a side effect of
    a chain edit that forgot the field.
    """

    transport: Transport
    model: str | None = None
    label: str = ""
    bare: bool = False


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
            last = provider_for(rung.transport, bare=rung.bare).run(
                req, model=rung.model or model
            )
            # Stamp the rung that actually ran. Done per-iteration (not once at
            # the end) so a fall-through to a cloud rung is recorded as cloud —
            # the accounting must follow the money, not the operator's intent.
            last = _replace(last, placement=_placement_of(rung))
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
    points the primary at an OSS id. ``BIG``/``MEDIUM``/``FRONTIER`` each have
    their own claude default; ``SMALL`` never reaches this (its ladder skips
    the claude fallback entirely — see :func:`_failover_ladder`)."""
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
    transport — a claude/local primary has nothing to fall back to).

    ``SMALL`` is the one exception: per the decided OpenRouter roster
    cascade ("small" skips Anthropic
    entirely, low-stakes/high-volume dispatch traffic), its ladder stops at
    the OSS rung with no claude fallback, however this call resolves.
    """
    primary = select_transport(tier, tools_needed=tools_needed, backend=backend)
    if primary in (Transport.OPENAI_TOOLS, Transport.OPENAI_COMPAT):
        if tier is Tier.SMALL:
            return [Rung(primary, label="oss")]
        claude = Transport.CLAUDE_AGENT if tools_needed else Transport.CLAUDE_P
        return [
            Rung(primary, label="oss"),
            Rung(claude, model=_claude_default(tier), label="claude-fallback"),
        ]
    return [Rung(primary, label="primary")]


def _default_chain(tier: Tier, *, tools_needed: bool, backend: Backend) -> list[Rung]:
    """The rung list for a tier with **no operator chain override** — the
    behaviour-preserving default under capability tiers + placement chains Phase B's always-on chain.

    The built-in OSS→claude safety-net ladder (:func:`_failover_ladder`) is
    opt-in via ``PRECIS_LLM_FAILOVER``. Without it a tier resolves to a
    **single rung** — its primary transport — which is exactly the pre-Phase-B
    non-failover dispatch path (``select_transport`` → one provider,
    byte-for-byte). With the flag on it resolves to the full auto-failover
    ladder, exactly as before. So making the chain the always-on resolution
    path (Phase B) doesn't change a no-override tier's routing: only an
    operator-written ``llm.chain.<tier>`` row does, and that is now read
    regardless of the flag (:func:`resolve_chain`).
    """
    if _failover_enabled():
        return _failover_ladder(tier, tools_needed=tools_needed, backend=backend)
    primary = select_transport(tier, tools_needed=tools_needed, backend=backend)
    return [Rung(primary, label="primary")]


def resolve_chain(tier: Tier, *, tools_needed: bool, backend: Backend) -> list[Rung]:
    """The rung list :func:`dispatch` / :func:`dispatch_async` actually walk —
    an operator-owned ``app_settings`` chain override layered in
    front of the compiled default (:func:`_default_chain`).

    **capability tiers + placement chains Phase B — the chain is the always-on resolution path.** An
    ``llm.chain.<tier>`` override is honoured *regardless of*
    ``PRECIS_LLM_FAILOVER``, so the operator chain editor's rows are actually
    read (Phase A wired this call inside ``if _failover_enabled():``, which
    left a set chain inert unless the legacy flag was on). With no override
    (today's steady state, and every non-chain test), this returns
    :func:`_default_chain` — a single primary rung, or the built-in
    auto-failover ladder when the flag is on — so a no-override tier routes
    byte-for-byte as it does today.

    A configured override is a list of rung dicts (``{"placement":
    "cloud"|"local", "model": <str>, "transport": <str>, "bare": <bool>}``,
    ``bare`` optional and defaulting to ``False`` — see :class:`Rung`;
    a non-boolean value degrades the whole chain like any other malformed
    field, because silently coercing ``"true"`` would decide a billing
    question on a typo),
    :func:`~precis.utils.llm.live_config.chain_override`) mapped onto
    :class:`Rung` in order, with ``placement`` carried through as the label.
    Any malformed rung — an unrecognized ``transport`` string, a
    missing/non-string ``model``, a non-object entry — degrades the *whole*
    chain back to :func:`_default_chain` (never a partial or best-effort
    chain), logged once so a typo'd override is visible without darking the
    tier.

    **A tool-using call skips rungs whose transport can't carry tools.** A
    chain is written per *tier*, but a tier serves both agentic and
    completion traffic, so a completion rung (``openai_compat`` / ``local`` /
    ``claude_p``) is legitimate in the chain and wrong only for *this* call —
    which makes it a per-call filter, not a write-time rejection. Unfiltered
    it is silent and expensive: an agentic call on a completion wire gets no
    verbs, reasons from the prompt alone, writes what it *would* have called
    as prose, and exits clean, so the run bills in full and looks successful
    while changing nothing. Prod ran that way for days —
    ``llm.chain.medium = [openai_compat/glm-4.7]`` sent every MEDIUM-tier
    planner tick to a wire with no tools, and the ticks re-minted forever
    because nothing they were supposed to record ever got recorded. If the
    filter empties the chain we fall back to :func:`_default_chain`, whose
    :func:`select_transport` is correct by construction — louder and pricier
    than the operator asked for, but a tool-using call that cannot use tools
    is pure waste. An operator who wants OSS on agentic work says so with an
    explicit ``openai_tools`` rung.
    """
    from precis.utils.llm import live_config

    override = live_config.chain_override(tier)
    if not override:
        return _default_chain(tier, tools_needed=tools_needed, backend=backend)

    def _fallback(reason: str, i: int, detail: object) -> list[Rung]:
        log.warning(
            "llm-chain: %s rung %d %s (%r) — falling back to the default chain",
            live_config.chain_key(tier),
            i,
            reason,
            detail,
        )
        return _default_chain(tier, tools_needed=tools_needed, backend=backend)

    rungs: list[Rung] = []
    for i, raw in enumerate(override):
        if not isinstance(raw, dict):
            return _fallback("is not an object", i, raw)
        model = raw.get("model")
        if not model or not isinstance(model, str):
            return _fallback("is missing a model", i, raw)
        transport_raw = raw.get("transport")
        if not isinstance(transport_raw, str):
            return _fallback("has an unknown transport", i, transport_raw)
        try:
            transport = Transport(transport_raw)
        except ValueError:
            return _fallback("has an unknown transport", i, transport_raw)
        placement = raw.get("placement")
        bare_raw = raw.get("bare", False)
        if not isinstance(bare_raw, bool):
            return _fallback("has a non-boolean bare", i, bare_raw)
        rungs.append(
            Rung(
                transport,
                model=model,
                label=placement if placement else "chain",
                bare=bare_raw,
            )
        )

    if tools_needed:
        keep = [r for r in rungs if r.transport.carries_tools]
        if len(keep) != len(rungs):
            log.warning(
                "llm-chain: %s drops %d tool-less rung(s) (%s) for a tool-using "
                "call — a completion wire returns prose, not verb calls%s",
                live_config.chain_key(tier),
                len(rungs) - len(keep),
                ", ".join(
                    r.transport.value for r in rungs if not r.transport.carries_tools
                ),
                "" if keep else "; falling back to the default chain",
            )
        return keep or _default_chain(tier, tools_needed=True, backend=backend)

    return rungs


def planner_rung0_model(model_alias: str, job_type: str | None = None) -> str | None:
    """The concrete model id a planner tick for ``model_alias`` will drive on
    rung 0, when that rung is an OSS model-carrying transport — else ``None``.

    Mirrors ``plan_tick._select_planner_transport``'s resolution (tier alias →
    operations override → resolved placement chain → rung 0) so the affinity
    decision agrees with the harness the tick will actually run. Returns
    ``rung0.model`` only for a ``LOCAL`` / ``OPENAI_TOOLS`` rung that pins a
    concrete model (the served-OSS case, a candidate ``llm:<model>`` slot); a
    ``claude`` cloud rung (no local serving) or a model-less rung yields
    ``None``. The caller decides whether that model is actually *served* (an
    advertised slot) before gating a job on it — this function only says which
    model the tick would use, not where it lives.
    """
    from precis.utils.llm import operations

    tier = PLANNER_TIER_BY_ALIAS.get(model_alias, Tier.FRONTIER)
    op = operations.resolve_op(job_type) if job_type else None
    if op is not None:
        tier = op[0]
    backend = resolve_backend()
    ladder = resolve_chain(tier, tools_needed=True, backend=backend)
    if not ladder:
        return None
    rung0 = ladder[0]
    if rung0.model and rung0.transport in (Transport.LOCAL, Transport.OPENAI_TOOLS):
        return rung0.model
    return None


def _placement_of(rung: Rung) -> str:
    """``'cloud'`` / ``'local'`` for one rung — the string form of
    :func:`_rung_is_cloud`, recorded on :attr:`LlmResult.placement` and
    ``llm_call_log.placement`` so the cost caps can tell spend from utilisation.

    One classifier, two consumers: the capability tiers + placement chains cloud-throttle prunes on
    ``_rung_is_cloud`` and the planner's dollar caps exclude on this. A second,
    independent notion of "is this local" is exactly how the caps drifted before.
    """
    return "cloud" if _rung_is_cloud(rung) else "local"


def _rung_is_cloud(rung: Rung) -> bool:
    """Classify a chain rung as cloud (hits a cloud API) vs local, for the
    capability tiers + placement chains throttle's cloud-rung pruning.

    An operator chain rung carries an explicit ``placement`` label
    (``"cloud"`` / ``"local"``, written by the chain editor) — authoritative
    when present. A default-chain rung (``_failover_ladder`` / ``_default_chain``
    labels: ``"primary"`` / ``"oss"`` / ``"claude-fallback"``, or an operator
    rung with no ``placement`` → ``"chain"``) is classified by transport:

    * ``CLAUDE_AGENT`` / ``CLAUDE_P`` → always Anthropic **cloud**.
    * ``LOCAL`` → the loopback local completion wire → **local**.
    * ``OPENAI_COMPAT`` / ``OPENAI_TOOLS`` → hosted-OSS **cloud** when a hosted
      endpoint is configured (``PRECIS_LLM_BASE_URL`` set), else **local** (a
      ``served_by`` llama-swap slot is the only way these run without a base
      url). Conservative for a throttle whose job is to keep work off cloud: a
      rung that *might* have gone to a local ``served_by`` slot but has a hosted
      base url set is treated as cloud and pruned — an operator that wants it
      kept labels it ``placement: "local"`` in the chain, which wins above.
    """
    if rung.label == "local":
        return False
    if rung.label == "cloud":
        return True
    if rung.transport in (Transport.CLAUDE_AGENT, Transport.CLAUDE_P):
        return True
    if rung.transport is Transport.LOCAL:
        return False
    return bool(os.environ.get("PRECIS_LLM_BASE_URL"))


def _fallback_placement(transport: Transport) -> str:
    """Transport-only local/cloud guess — :func:`_record_dispatch`'s
    belt-and-suspenders fallback for a call whose :attr:`LlmResult.placement`
    somehow arrived unset.

    Every current call site already stamps ``placement`` before reaching
    ``_record_dispatch`` (``FailoverProvider.run`` per rung, or the
    ``if result.placement is None`` fill in ``dispatch``/``dispatch_async``
    below), so this branch is not known to fire today. It mirrors
    :func:`_rung_is_cloud`'s label-less, transport-only classification (no
    ``Rung`` — with its possible operator ``placement`` label — is available
    at the recording chokepoint, only the transport that ran) so a *future*
    call site that forgets to stamp placement still logs a best-effort value
    instead of quietly reopening the migration-0112 NULL-placement gap.
    """
    if transport in (Transport.CLAUDE_AGENT, Transport.CLAUDE_P):
        return "cloud"
    if transport is Transport.LOCAL:
        return "local"
    return "cloud" if os.environ.get("PRECIS_LLM_BASE_URL") else "local"


def _apply_cloud_throttle(chain: list[Rung]) -> list[Rung]:
    """Prune cloud rungs from ``chain`` when the operator has disabled cloud
    (``llm.cloud_enabled = false``) — a no-op returning ``chain``
    unchanged while cloud is on (the default), so dispatch stays byte-identical
    until an operator flips the dial.

    When cloud is off, only the local rungs survive. A tier whose chain has a
    local rung keeps flowing on it; a tier left with **no** rung prunes to an
    **empty** list, which :func:`dispatch` turns into a ``paused`` result
    (skip-not-fail: the call queues and resumes when cloud is re-enabled, never
    silently degraded to a weaker local model — the §5 contract).

    **Which tiers survive a throttle depends on their chain, not just their
    name.** ``FRONTIER`` is cloud-only by construction (no local mirror), so it
    always pauses. Today (default ``ANTHROPIC`` backend, no operator chain
    written) only ``SMALL`` has a standing local rung (``LOCAL``, via
    :func:`select_transport`); ``BIG`` / ``MEDIUM`` resolve to a single cloud
    rung and so *also* pause under throttle **until an operator chain gives
    them a `placement: "local"` rung** (the Phase-3 roster / chain editor). The
    §5 "``BIG`` / ``MEDIUM`` / ``SMALL`` drop to local" story is the target
    state once those chains exist; the pruning mechanism here is correct for
    it, but doesn't manufacture a local rung a chain doesn't have.
    """
    from precis.utils.llm import live_config

    if live_config.cloud_enabled():
        return chain
    return [rung for rung in chain if not _rung_is_cloud(rung)]


def _skip_unserved_local_rung(chain: list[Rung], model: str) -> list[Rung]:
    """Drop a leading loopback ``LOCAL`` rung that has no live endpoint on this
    host, returning the tail so :func:`dispatch` advances straight to the
    fallback instead of dispatching a guaranteed ``ECONNREFUSED``.

    A ``Transport.LOCAL`` rung 0 routes through ``_dispatch_local`` → the
    ``PRECIS_SUMMARIZE_LLM_URL`` default ``http://127.0.0.1:4000/v1`` (the
    retired litellm proxy) *unless* a reserved ``served_by`` slot pins a real
    llama-swap endpoint. On a host that doesn't serve the model, no slot is
    reserved, so the call hits the dead ``:4000`` and fails over — logging a WARN
    every time (the non-serving-host failover storm). Detect that up front with a
    read-only served-set test and prune the rung.

    Left **unchanged** (returns ``chain``) when any guard fails, so the pruning
    can only ever skip a rung that was certain to fail:
      * fewer than 2 rungs — a rung-0-only chain must surface its own error (and
        a strict ``placement='local'`` pin that filtered down to one local rung
        must not silently escape to cloud);
      * rung 0 isn't ``Transport.LOCAL`` (a cloud/tools rung has its own wire);
      * ``PRECIS_SUMMARIZE_LLM_URL`` is set — an operator pointed the loopback at
        a real endpoint, so it isn't the dead default;
      * the model *is* served on this host — its reserved slot will pin a live
        endpoint (the melchior path), so the rung works.
    """
    if len(chain) < 2:
        return chain
    rung0 = chain[0]
    if rung0.transport is not Transport.LOCAL:
        return chain
    if os.environ.get("PRECIS_SUMMARIZE_LLM_URL"):
        return chain
    from precis.utils.llm import local_serving as _local

    served_model = rung0.model or model
    if _local.served_locally(served_model):
        return chain
    log.debug(
        "llm-failover: skipping unserved local rung 0 (model=%s) — not served on "
        "this host and no PRECIS_SUMMARIZE_LLM_URL override, advancing to the "
        "fallback rung instead of dispatching into the dead loopback wire",
        served_model,
    )
    return chain[1:]


def _apply_placement(chain: list[Rung], placement: str | None) -> list[Rung]:
    """Strict local/cloud rung filter for :attr:`LlmRequest.placement` (the
    structured-selection UI's "run this here" pin) — classified by
    :func:`_rung_is_cloud`, the same local/cloud call the cloud throttle above
    uses.

    ``None`` or any value other than ``"local"``/``"cloud"`` is a no-op,
    returning ``chain`` unchanged (walked as configured — today's behavior for
    every caller that doesn't set ``placement``). Unlike
    :func:`_apply_cloud_throttle`, an emptied-by-this-filter chain is **not**
    handled here — it is the caller's job (:func:`dispatch` /
    :func:`dispatch_async`) to turn that into an explicit error result rather
    than a silent paused/degrade, since the caller asked for a specific rung
    that the chain doesn't have.
    """
    if placement == "local":
        return [rung for rung in chain if not _rung_is_cloud(rung)]
    if placement == "cloud":
        return [rung for rung in chain if _rung_is_cloud(rung)]
    return chain


#: ``SMALL`` (categorizer) model ids that only mean something on the
#: local loopback (the ``summarizer`` alias and the ``rake-lemma`` name it
#: resolves to) — POSTing either to a hosted OSS backend (OpenRouter et al.)
#: 400s, since the hosted side has never heard of them. See
#: :func:`_hosted_small_remap`. Deliberately scoped to the ``SMALL``-only
#: aliases: a served local model backing ``BIG`` is NOT here — it always
#: routes ``OPENAI_TOOLS`` regardless of the backend flag, so remapping it
#: would fire even under the default ``ANTHROPIC`` (breaking byte-identity)
#: and silently downgrade a big call to a *small* hosted model. ``BIG``'s
#: hosted fallback is the Phase-2 per-tier failover chain's job, not this
#: small-model remap.
_LOCAL_ONLY_MODEL_ALIASES = frozenset({"summarizer", "rake-lemma"})


def _hosted_small_model() -> str:
    """The hosted small model a local-only alias remaps onto — resolved
    ``llm.model.small`` ``app_settings`` override →
    ``PRECIS_LOCAL_SMALL_HOSTED_MODEL`` → a compiled default. Mirrors
    :func:`resolve_model`'s override→env→default order but against a
    dedicated env var/default pair, since ``PRECIS_SUMMARIZE_MODEL``'s
    default (``"summarizer"``) is itself one of the aliases being remapped
    *away from*.
    """
    from precis.utils.llm import live_config

    override = live_config.model_override(Tier.SMALL)
    if override:
        return override
    return os.environ.get("PRECIS_LOCAL_SMALL_HOSTED_MODEL", "z-ai/glm-4.7-flash")


def _hosted_small_remap(
    model: str, transport: Transport, *, has_local_slot: bool
) -> str:
    """Transparently remap a local-only model alias onto a real hosted small
    model when the call is actually headed to a hosted OSS backend
    (``glm-fleet-flip-safety`` (git-only) Part 1 — the 395-error class:
    ``classify``/``summarize`` pin ``model="summarizer"``, which means nothing
    to OpenRouter).

    A no-op unless *all* of: the transport is a hosted-OSS one
    (:data:`Transport.OPENAI_COMPAT` / :data:`Transport.OPENAI_TOOLS`),
    ``PRECIS_LLM_BASE_URL`` is set (so there is a hosted endpoint to remap
    onto at all), the call is not already pinned to a local ``served_by``
    slot (``has_local_slot`` — that slot's own model name is already correct
    for its endpoint), and ``model`` is one of :data:`_LOCAL_ONLY_MODEL_ALIASES`.
    Under the default ``ANTHROPIC`` backend ``SMALL`` resolves to
    :data:`Transport.LOCAL`, not a hosted-OSS transport, so this is
    byte-identical to today whenever the flip is off.
    """
    if has_local_slot:
        return model
    if transport not in (Transport.OPENAI_COMPAT, Transport.OPENAI_TOOLS):
        return model
    if not os.environ.get("PRECIS_LLM_BASE_URL"):
        return model
    if model not in _LOCAL_ONLY_MODEL_ALIASES:
        return model
    return _hosted_small_model()


def dispatch(req: LlmRequest) -> LlmResult:
    """Route ``req`` to its provider and return a normalized
    :class:`LlmResult`.

    Resolve the backend + model, pick the transport (pure), look up the
    provider, and delegate. Each provider *wraps* the existing helper —
    never reimplements it — and folds a caught
    :class:`~precis.utils._claude_subprocess.ClaudeProcessError` (or a
    local-transport ``RuntimeError``) into :attr:`LlmResult.error` rather
    than raising, so every dispatch path returns one shape. A programming
    error (an unwired local-tools path) still raises.

    The ``OPENAI`` backend needs ``PRECIS_LLM_BASE_URL``; with the backend
    on but no base url set, cloud calls fall back to ``claude`` rather than
    POST to a phantom endpoint — the ships-dark safety net.

    Routing walks a per-tier **chain** (:func:`resolve_chain`, capability tiers + placement chains
    Phase B — always-on): an operator ``llm.chain.<tier>`` override, else the
    default (a single primary rung, or — with ``PRECIS_LLM_FAILOVER`` on — the
    built-in OSS→claude auto-failover ladder). A multi-rung chain is wrapped in
    a :class:`FailoverProvider` that falls through on error, so a flipped
    backend degrades to its next rung instead of failing. The ladder also
    covers a *saturated local slot*: a ``paused`` local-serving result retries
    rung 0 against the hosted OSS endpoint (skipping the busy local hardware)
    before falling to the next rung.
    """

    backend = resolve_backend()
    if backend is Backend.OPENAI and not os.environ.get("PRECIS_LLM_BASE_URL"):
        backend = Backend.ANTHROPIC
    # Per-operation routing (the operation rung, `utils/llm/operations.py`):
    # a *registered* (allow-listed) source has its effective tier + model owned
    # by the registry default + any live `llm.op.<source>` override — the
    # operation rung between the tier default and a call-site `req.model` pin.
    # A non-registered source (functional pins like classify→"summarizer",
    # router-bypassers) returns None → today's path, so this ships dark.
    from precis.utils.llm import operations as _operations

    _op = _operations.resolve_op(req.source) if req.source else None
    if _op is not None:
        _op_tier, _op_model = _op
        if _op_tier != req.tier:
            req = _replace(req, tier=_op_tier)
        model = _op_model or resolve_model(req.tier, backend=backend)
    else:
        model = req.model or resolve_model(req.tier, backend=backend)
    # Capability tiers + placement chains gen-param passthrough: resolve the tier's (thinking, temperature)
    # default (_tier_gen_defaults) unless the caller already pinned one
    # explicitly. Reassigning `req` itself here (rather than threading two
    # extra locals through the rest of this function) means every downstream
    # copy — the FailoverProvider ladder's rungs, the slot/endpoint `replace`s
    # below, the route-log record — already carries the resolved values with
    # no per-transport re-derivation of tier logic.
    _default_thinking, _default_temperature = _tier_gen_defaults(req.tier)
    req = _replace(
        req,
        thinking=req.thinking if req.thinking is not None else _default_thinking,
        temperature=(
            req.temperature if req.temperature is not None else _default_temperature
        ),
    )
    # Resolve the transport *before* the breaker, so the gate can key on the
    # resource actually spent: the claude-OAuth transports draw subscription
    # quota (gated on the snapshot), everything else paid spends real dollars.
    # the chain is the always-on resolution path, so an
    # operator ``llm.chain.<tier>`` override is honoured regardless of
    # PRECIS_LLM_FAILOVER (the editor's rows are actually read). With no
    # override this collapses to today's path (single primary rung, or the
    # built-in auto-failover ladder when the flag is on — see _default_chain).
    ladder = resolve_chain(req.tier, tools_needed=req.tools_needed, backend=backend)
    # Structured placement filter (strict — unlike the cloud throttle below,
    # an emptied nonempty chain here is an *error* result, not a silent
    # paused/degrade: the caller asked for a rung the chain doesn't have).
    # No-op when req.placement is None or not a recognized value.
    _placed_ladder = _apply_placement(ladder, req.placement)
    if ladder and not _placed_ladder:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=(
                f"placement={req.placement!r} requested but the {req.tier.value} "
                f"chain has no {req.placement} rung"
            ),
            paused=False,
        )
    ladder = _placed_ladder
    # Cloud-throttle: with cloud disabled, prune the chain's
    # cloud rungs → local. A cloud-only tier (FRONTIER) prunes to empty and
    # pauses (skip-not-fail), never silently degrading to a local model. No-op
    # while cloud is on (the default) — byte-identical to above.
    ladder = _apply_cloud_throttle(ladder)
    if not ladder:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=(
                "cloud is disabled and this tier has no local rung — "
                "waiting for cloud to be re-enabled"
            ),
            paused=True,
        )
    # Serving-aware prune: a loopback ``LOCAL`` rung 0 on a host that does NOT
    # serve the model would fall to the retired ``:4000`` default and ECONNREFUSE
    # on every call (the ~8000 WARN/24h "llm-failover: rung 0 … failed" storm on
    # non-serving hosts), then fail over to the next rung anyway. Drop it up front
    # — before the breaker/transport gates below, so they see the rung that will
    # actually run — and advance straight to the fallback. A served host keeps its
    # rung 0 (``acquire`` pins a live llama-swap endpoint later); a real
    # ``PRECIS_SUMMARIZE_LLM_URL`` override or a rung-0-only chain is left alone.
    ladder = _skip_unserved_local_rung(ladder, model)
    transport = ladder[0].transport
    # Wrap in the FailoverProvider only when there's genuinely a ladder to walk:
    # the failover flag is on (preserves the pre-Phase-B flag-on wrapping
    # exactly), the chain has >1 rung, or a single rung pins its own model (an
    # operator single-rung chain — resolve_chain's parser always sets a rung
    # model, so this catches every operator override). A bare single primary
    # rung (model=None, flag off) routes straight to its provider — byte-for-
    # byte the old non-failover path, and it keeps the saturated-slot escape
    # below gated on a real ladder.
    if _failover_enabled() or len(ladder) > 1 or ladder[0].model is not None:
        provider: LlmProvider = FailoverProvider(ladder)
    else:
        # Carry the rung's auth opt-in here too. Unreachable for an operator
        # override (resolve_chain always pins a rung model, so those take the
        # FailoverProvider branch above) and no default chain sets it — but a
        # path that silently drops `bare` would decide a billing question by
        # omission the first time that changes.
        provider = provider_for(transport, bare=ladder[0].bare)
    # Global circuit breaker: refuse a *new paid* call once its resource is
    # exhausted (only free local tiers pass; dark when no store is bound).
    # Folds into the normalized error result so callers degrade gracefully.
    from precis.budget import breaker as _breaker

    # Gate on the *resolved* rung, not the tier band: a paid-band tier (BIG /
    # MEDIUM) that failed over to a free local rung (served_by slot / local /
    # a placement:"local" chain rung) spends nothing, so a tripped $ cap must
    # not starve it. ``_rung_is_cloud`` is the authoritative local/cloud
    # classifier (same one the cloud-throttle uses).
    # ``bare`` moves the resource from OAuth quota to dollars — see gate_tier.
    trip = _breaker.gate_tier(
        req.tier,
        transport=transport.value,
        local=not _rung_is_cloud(ladder[0]),
        bare=ladder[0].bare,
    )
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

    # Acquire the slot under the model rung 0 will *actually* dispatch — the
    # chain rung's pinned served id (the capacity-valve `qwen3.5-9b-q4_k_m`),
    # NOT the pre-chain tier/source model (`summarizer`). ``FailoverProvider``
    # runs rung 0 as ``rung.model or model`` (see its ``run``), so the served
    # model, not the alias, is what hits the wire — but ``resource_slots`` /
    # ``served_by`` are keyed on that served id, so acquiring under the tier
    # alias always missed the slot (capacity-valve blocker 2) → no endpoint →
    # ``_dispatch_local`` fell to the default loopback wire and failed over to
    # cloud, so SMALL never served local despite a resident 9B. ``ladder[0]``
    # is the effective rung 0 after the placement/throttle filters above. Falls
    # back to ``model`` when the rung pins nothing (no operator chain, a bare
    # auto-failover ladder rung), so the dark path stays byte-identical.
    serve_model = ladder[0].model or model
    slot = _local.acquire(serve_model)
    if slot is not None and slot.paused:
        # Local capacity is saturated, not down — a different case from a
        # transport error, and the FailoverProvider ladder built above already
        # has a rung for exactly this: retrying `req` unmodified (no
        # `local_url` override) sends rung 0 to the *hosted* OSS endpoint
        # (`PRECIS_LLM_BASE_URL`, e.g. OpenRouter) instead of the busy local
        # slot, falling through to the claude rung only if that also fails.
        # The saturated-local-slot hosted escape — gated on the
        # PRECIS_LLM_FAILOVER flag OR an operator chain (both produce a
        # FailoverProvider here) rather than a new one. But this
        # escape only exists when rung 0's transport is one of the two that
        # read `PRECIS_LLM_BASE_URL` when `local_url` is unset (OPENAI_TOOLS /
        # OPENAI_COMPAT) — ``Transport.LOCAL`` (e.g. SMALL under the
        # default ANTHROPIC backend) has no hosted mode at all and would just
        # re-hit the same saturated loopback wire, so that case (like
        # failover-off, or a primary with no fallback rung) stays
        # byte-identical to the old behavior: return the paused result
        # immediately so a pinned pass backs off instead of spinning.
        # A strict `placement='local'` pin forbids this escape entirely: the
        # hosted retry IS a cloud endpoint, so a placement-pinned call must
        # take the paused backoff below instead of silently leaving local
        # hardware.
        if (
            req.placement != "local"
            and isinstance(provider, FailoverProvider)
            and transport
            in (
                Transport.OPENAI_TOOLS,
                Transport.OPENAI_COMPAT,
            )
        ):
            log.debug(
                "llm-failover: local slot for %s is saturated — trying the "
                "hosted fallback rung instead of failing the call "
                "(capacity backoff, not a transport error)",
                model,
            )
            # No local slot is in play here by construction (the local host
            # is saturated, that's why we're escaping to the hosted rung) —
            # so a local-only alias (`summarizer` et al.) still needs the
            # Part 1 remap before it hits the hosted endpoint.
            saturated_model = _hosted_small_remap(
                model, transport, has_local_slot=False
            )
            started = time.monotonic()
            result = provider.run(req, model=saturated_model)
            _record_dispatch(
                req,
                result,
                transport=transport,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return result
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
    # LOCAL (tool-less, ``_dispatch_local``) and OPENAI_TOOLS (a served local
    # model backing ``BIG``, tools, ``run_oss_tool_loop``).
    call_req = req
    call_model = model
    if slot is not None and slot.reserved and slot.endpoint:
        call_req = _replace(req, local_url=slot.endpoint)
        call_model = slot.served_model or model
    # Part 1 hosted-small remap: only when this call is NOT pinned to a local
    # `served_by` slot (`call_req.local_url` unset above means no slot — or a
    # slot with no direct endpoint — is in play).
    call_model = _hosted_small_remap(
        call_model, transport, has_local_slot=call_req.local_url is not None
    )
    started = time.monotonic()
    try:
        result = provider.run(call_req, model=call_model)
    finally:
        _local.release(slot)
    # The direct (non-FailoverProvider) path has exactly one rung, so nothing
    # stamped placement above. A reserved `served_by` slot is local by
    # definition regardless of how the rung classifies — that IS the local
    # hardware — so it wins over the rung's own label.
    if result.placement is None:
        result = _replace(
            result,
            placement=(
                "local"
                if (slot is not None and slot.reserved and slot.endpoint)
                else _placement_of(ladder[0])
            ),
        )
    _record_dispatch(
        req,
        result,
        transport=transport,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return result


async def _dispatch_claude_agent_async(req: LlmRequest, model: str) -> LlmResult:
    """Async analog of :class:`ClaudeAgentProvider`'s ``run`` — the streaming
    leg :func:`dispatch_async` calls when a caller sets ``on_event``. Same
    post-processing (result normalization + the post-hoc ``max_tokens``
    truncation) as the sync provider; only the underlying call
    (:func:`~precis.utils.claude_agent.call_claude_agent_async` vs. the sync
    ``call_claude_agent``) differs, so the two providers can't drift apart on
    anything but streaming.
    """
    try:
        res = await call_claude_agent_async(
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
            on_event=req.on_event,
        )
    except ClaudeProcessError as exc:
        return _error_result(exc, model=model, tier=req.tier)
    result = result_from_agent(res, model=model, tier=req.tier)
    if req.max_tokens is not None and result.error is None:
        result = _replace(
            result, text=_truncate_to_max_tokens(result.text, req.max_tokens)
        )
    return result


async def dispatch_async(req: LlmRequest) -> LlmResult:
    """Async twin of :func:`dispatch`, for a caller that needs the real-time
    ``on_event`` stream (asa_bot's Discord bridge, Phase 3 of the
    router-migration plan — the LLM routing seam follow-up).

    Mirrors :func:`dispatch`'s gate sequence — backend fallback, transport
    resolution, the budget breaker, window admission, the local-serving slot —
    inline and synchronously (all fast in-memory/DB checks, not blocking I/O
    of consequence, exactly as the sync function treats them). When the
    resolved transport is :data:`Transport.CLAUDE_AGENT` **and**
    ``req.on_event`` is set, this awaits :func:`_dispatch_claude_agent_async`
    (which calls :func:`~precis.utils.claude_agent.call_claude_agent_async`)
    instead of the sync :class:`ClaudeAgentProvider`. For any other transport,
    or when ``on_event`` is unset, it delegates straight to the existing sync
    :func:`dispatch` — safe to call a sync function from an async one here,
    since ``dispatch``'s own I/O is either fast (the gate checks) or itself
    already calls the sync ``call_claude_agent`` (no event loop to block).

    Logs via the same :func:`_record_dispatch` call the sync path uses, so
    ``llm_call_log`` gets an entry on both branches. The breaker-pause /
    admission-refusal / all-slots-busy early-outs are **not** logged here
    either — same as :func:`dispatch`, which only records a call once a
    provider actually ran.

    Note: this checks only the primary rung's transport to decide whether to
    stream — it does not wrap the streaming call in the sync path's
    :class:`FailoverProvider` ladder, so a streaming caller trades the
    multi-rung safety net for real-time progress on that one call. Ships dark:
    a streaming ``on_event`` caller (asa_bot) sits on the cloud claude tiers,
    where :func:`resolve_chain` returns a single ``[Rung(primary)]`` anyway (no
    chain override configured, nothing to fail over to), so this is a no-op
    distinction until an OSS tier ever streams.
    """
    backend = resolve_backend()
    if backend is Backend.OPENAI and not os.environ.get("PRECIS_LLM_BASE_URL"):
        backend = Backend.ANTHROPIC
    model = req.model or resolve_model(req.tier, backend=backend)
    # Resolve the primary transport through the always-on chain, same as sync dispatch — the streaming decision keys on rung 0.
    ladder = resolve_chain(req.tier, tools_needed=req.tools_needed, backend=backend)
    # Structured placement filter — strict parity with sync dispatch: an
    # emptied nonempty chain is an explicit error result, not a silent
    # paused/degrade. No-op when req.placement is None/unrecognized.
    _placed_ladder = _apply_placement(ladder, req.placement)
    if ladder and not _placed_ladder:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=(
                f"placement={req.placement!r} requested but the {req.tier.value} "
                f"chain has no {req.placement} rung"
            ),
            paused=False,
        )
    ladder = _placed_ladder
    # Cloud-throttle parity with sync dispatch: prune cloud rungs when disabled.
    # An empty result (a cloud-only tier under throttle) is delegated to the
    # sync dispatch below, which returns the paused result — so the streaming
    # path never needs its own copy of that early-out.
    ladder = _apply_cloud_throttle(ladder)
    transport = ladder[0].transport if ladder else None

    if transport is not Transport.CLAUDE_AGENT or req.on_event is None:
        return dispatch(req)

    # Same budget-breaker / window-admission / local-serving-slot gate
    # dispatch() runs, ahead of the (here: async) provider call.
    from precis.budget import breaker as _breaker

    # Reached only for CLAUDE_AGENT (always cloud/OAuth — the non-agent path
    # delegated to sync dispatch above), so ``local`` is False here; passed for
    # symmetry with the sync gate and to stay correct if the guard ever widens.
    trip = _breaker.gate_tier(
        req.tier,
        transport=transport.value,
        local=not _rung_is_cloud(ladder[0]),
        bare=ladder[0].bare,
    )
    if trip is not None:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=trip,
            paused=True,
        )

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

    from precis.utils.llm import local_serving as _local

    # Parity with sync dispatch: acquire under the rung-0 served model, not the
    # pre-chain alias (see the comment there). Reached only for CLAUDE_AGENT
    # (always cloud), so this is a de-facto no-op today — kept identical so the
    # two paths can't drift if a local-served model ever streams.
    serve_model = ladder[0].model or model
    slot = _local.acquire(serve_model)
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
    call_req = req
    call_model = model
    if slot is not None and slot.reserved and slot.endpoint:
        call_req = _replace(req, local_url=slot.endpoint)
        call_model = slot.served_model or model

    started = time.monotonic()
    try:
        result = await _dispatch_claude_agent_async(call_req, model=call_model)
    finally:
        _local.release(slot)
    # The direct (non-FailoverProvider) path has exactly one rung, so nothing
    # stamped placement above. A reserved `served_by` slot is local by
    # definition regardless of how the rung classifies — that IS the local
    # hardware — so it wins over the rung's own label.
    if result.placement is None:
        result = _replace(
            result,
            placement=(
                "local"
                if (slot is not None and slot.reserved and slot.endpoint)
                else _placement_of(ladder[0])
            ),
        )
    _record_dispatch(
        req,
        result,
        transport=transport,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return result


class DispatchError(RuntimeError):
    """Raised by :meth:`DispatchClient.complete` on a dispatch error or a
    breaker/admission pause — a distinct subclass (not a bare ``RuntimeError``)
    so a caller's retry policy can tell "the router refused/failed this call"
    apart from an unrelated ``RuntimeError`` it might raise for its own reasons
    (e.g. a malformed-response parse failure), without the two collapsing into
    one undifferentiable type.

    ``paused`` mirrors :attr:`LlmResult.paused` — ``True`` for a breaker trip
    or an all-local-slots-busy backoff (an expected, self-clearing condition
    under contention), ``False`` for a genuine transport/provider failure. A
    caller can use it to skip the per-item ERROR traceback for the former
    (see ``llm_summarize._complete``'s empty-summary handling for the same
    pattern) without resorting to message-string sniffing."""

    def __init__(self, message: str, *, paused: bool = False) -> None:
        super().__init__(message)
        self.paused = paused


@dataclass
class DispatchClient:
    """A ``.complete(messages)``-shaped adapter that routes a completion
    through :func:`dispatch` instead of holding its own local ``LlmClient``.

    Drop-in for the summarize / classify / glossary passes' ``client=`` seam:
    the same ``complete(messages, *, extra_body=None) -> LlmResult`` contract
    (``.text`` + ``.total_tokens``), but every call folds through the router — so
    it gains the budget breaker gate, the ``served_by`` reroute (local tiers) /
    ``claude_agent`` transport (cloud tiers), and the route-log.

    **The local tier** (the default, ``SMALL``): behaviour-preserving until
    ``served_by`` is seeded — with no slot the model resolves to today's
    ``summarizer`` alias on the ``LlmConfig.from_env`` proxy URL, byte-identical
    to the raw client.

    **Cloud tiers** (``FRONTIER`` / ``BIG``): ``messages`` is split into
    a ``system_prompt`` (the ``system``-role turn(s), joined) and a ``prompt``
    (everything else, joined) — the shape ``claude_agent`` / ``claude_p`` need,
    since those transports read ``LlmRequest.prompt`` / ``.system_prompt``, not
    ``.messages`` (that field only feeds the local/openai-compat transports).
    Set ``tools_needed=True`` to land on ``claude_agent`` (free-text final
    answer + system prompt honored, no tools advertised when ``mcp_config`` is
    left ``None`` — the established "text-only agent wrapper" idiom used by
    ``precis_web.ask.generate_answer`` / ``figure.turn`` / ``mermaid.turn``);
    the tool-less default (``False``) lands on ``claude_p``, which demands a
    parseable trailing JSON block and drops the system prompt entirely — wrong
    for a free-text compose call.

    Raises :class:`DispatchError` (a ``RuntimeError`` subclass) on a dispatch
    error / breaker-pause so the pass marks the item failed and retries —
    exactly as the raw ``LlmClient.complete`` raised on a transport error (the
    passes' ``except`` blocks count it failed). Local tiers are free, so the
    breaker never trips them; the only pause is all-slots-busy, which correctly
    backs a batch off.
    """

    tier: Tier = Tier.SMALL
    model: str | None = None
    max_tokens: int | None = None
    #: Capability tiers + placement chains gen-param passthrough (see :attr:`LlmRequest.thinking` /
    #: ``.temperature``) — ``None`` (the default) leaves the tier's own
    #: default in force, so a bare ``DispatchClient`` (``tier=SMALL``) gets
    #: thinking-off/temperature-0 with zero caller change.
    thinking: bool | None = None
    temperature: float | None = None
    source: str = ""
    #: Route to ``claude_agent`` (cloud tiers only — local tiers ignore this)
    #: instead of the tool-less ``claude_p`` judge shape. See the class
    #: docstring's "Cloud tiers" section for why a free-text compose call needs
    #: this set.
    tools_needed: bool = False
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
        # ``system_prompt``/``prompt`` are derived so a cloud-tier caller (which
        # reads these, not ``messages``) works too — harmless for the local/
        # openai-compat transports, which prefer ``messages`` when set.
        system_prompt = "\n\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "system"
        )
        prompt = "\n\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") != "system"
        )
        res = dispatch(
            LlmRequest(
                tier=self.tier,
                messages=messages,
                prompt=prompt,
                system_prompt=system_prompt or None,
                tools_needed=self.tools_needed,
                model=self.model,
                max_tokens=self.max_tokens,
                thinking=self.thinking,
                temperature=self.temperature,
                source=self.source,
                log_call=self.log_call,
                log_blobs=self.log_blobs,
            )
        )
        if res.error is not None:
            raise DispatchError(res.error, paused=res.paused)
        return res


def _route_features(req: LlmRequest, result: LlmResult | None = None) -> dict[str, Any]:
    """Cheap, deterministic code features for the route-log (the categorizer's
    first layer). No model call — just what's readable off the request, plus how
    the call *terminated* when a result is available.

    **Why termination belongs here.** A tool loop that answers and a tool loop
    that hits its turn ceiling both land in ``llm_call_log`` with
    ``errored = false`` — ``max_turns`` is a *resumable* exhaustion, not a
    failure, which is right for the executor but leaves the ledger unable to
    tell "did 60 turns of work" from "spun 60 times and produced nothing".
    Prod ran 5 such ticks in 3h on 2026-08-07 (``turns_used = 60``,
    ``response_chars`` 0–3, 256–618s each), one todo exhausting twice, and
    nothing anywhere objected. The only tell was ``turns_used >= 60 AND
    response_chars < 10``, which is a heuristic nobody alerts on.

    ``exhausted`` is the derived flag worth querying: it folds the OSS loop's
    ``stop_reason`` and the claude lane's ``terminal_reason`` into one boolean
    so a watchdog doesn't need to know which transport ran.
    """
    prompt_chars = len(req.prompt or "")
    if req.messages:
        prompt_chars += sum(len(str(m.get("content", ""))) for m in req.messages)
    feats: dict[str, Any] = {
        "prompt_chars": prompt_chars,
        "tier": req.tier.value,
        "tools_needed": req.tools_needed,
        "source": req.source or None,
        "has_system": bool(req.system_prompt),
        "has_mcp": bool(req.mcp_config),
    }
    if result is not None:
        stop = result.stop_reason
        terminal = result.terminal_reason
        feats["stop_reason"] = stop
        feats["terminal_reason"] = terminal
        # A turn-ceiling stop on either lane. Deliberately a stored boolean, not
        # a query-time expression: the two lanes spell it differently and a
        # consumer that has to remember both spellings gets it wrong once.
        feats["exhausted"] = stop == "max_turns" or terminal == "max_turns"
        # Empty output on an exhausted run is the pathological shape — the run
        # burned the ceiling and recorded nothing. Cheap to stamp, and it is
        # what an alert should actually fire on.
        feats["empty_output"] = len((result.text or "").strip()) < 10
    return feats


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
            "max_tokens": req.max_tokens,
            "thinking": req.thinking,
            "temperature": req.temperature,
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
                # Every caller already stamps this before calling us; the
                # fallback is a chokepoint safety net, not the primary path —
                # see _fallback_placement.
                placement=(
                    result.placement
                    if result.placement is not None
                    else _fallback_placement(transport)
                ),
                turns_used=result.turns_used,
                duration_ms=duration_ms,
                errored=result.error is not None,
                error=result.error,
                data_parsed=result.data is not None,
                ref_id=req.ref_id,
                store_blobs=req.log_blobs,
                features=_route_features(req, result),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_creation_tokens=result.cache_creation_tokens,
            )
        )
    except Exception:
        log.debug("route_log: dispatch record failed", exc_info=True)


def _is_unavailability(exc: BaseException) -> bool:
    """Classify a caught transport exception: unavailability (skip-and-retry,
    :attr:`LlmResult.paused`) vs. a genuine semantic failure (:attr:`LlmResult.error`
    only) that will never succeed on retry — capability tiers + placement chains "Failure & congestion
    semantics" ("a todo that can't run right now waits and retries; it does not
    park").

    * **Unavailability → True**: a request timeout (``socket.timeout`` /
      ``TimeoutError``), a connection failure (``urllib.error.URLError`` that
      is *not* an ``HTTPError``, ``ConnectionError``, any other ``OSError``),
      or an HTTP 5xx / 429 rate-limit (``urllib.error.HTTPError``).
    * **Semantic → False**: an HTTP 4xx other than 429 — a malformed/
      unauthorized request that will fail identically on every retry.

    ``HTTPError`` is checked first since it subclasses ``URLError`` subclasses
    ``OSError`` — a bare ``isinstance(exc, OSError)`` would otherwise catch it
    before its status code is inspected. Anything not covered above (e.g. a
    plain ``RuntimeError`` from a malformed response body) is *not* classified
    as unavailability — today's behavior (stays ``error``), since it isn't a
    known transient signal.
    """
    if isinstance(exc, HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, OSError)


#: Local-transport timeout cap for a SMALL-tier judge call. A classify /
#: summarize / triage judge returns in ~1s against a healthy loopback wire;
#: capping far below the 120s ``LlmConfig`` default means a stuck / flapping
#: wire fails FAST so the ``FailoverProvider`` falls over to the hosted rung,
#: instead of a batch (N chunks × 2 calls × 120s) blocking past the worker's
#: watchdog and stranding the pass with zero progress (the 2026-07-26 classify
#: stall: a transient local-wire flap hung SMALL work → no failover, no tags).
_SMALL_LOCAL_TIMEOUT_S = 30.0


def _dispatch_local(req: LlmRequest, model: str) -> LlmResult:
    """Drive the loopback local ``LlmClient`` for a local tier.

    Imports the summarizer client lazily so this module stays out of the
    worker/DB import chain (and so DB-free callers/tests never trigger
    it). Reuses ``LlmConfig.from_env`` and overrides only the model +
    ``enabled`` flag so the resolved tier model wins.
    """
    from dataclasses import replace

    from precis.workers.llm_summarize import LlmClient, LlmConfig

    cfg = replace(LlmConfig.from_env(), model=model, enabled=True)
    # A local-serving slot may pin a direct endpoint (llama-swap) — route there
    # instead of the default loopback URL (the Phase-2 litellm-retirement flip;
    # dark until a card declares served_by.endpoint). Mirrors the per-call url
    # override the openai_compat path already uses.
    if req.local_url:
        cfg = replace(cfg, url=req.local_url)
    # A caller-pinned completion cap (paper_glossary=2000, …) wins over the
    # env default so a migrated direct-``LlmClient`` pass keeps its budget.
    if req.max_tokens is not None:
        cfg = replace(cfg, max_tokens=req.max_tokens)
    # Capability tiers + placement chains gen-param passthrough: unlike `max_tokens` above, `None` here
    # is a meaningful *resolved* value (the MEDIUM/BIG/FRONTIER-tier default —
    # "omit temperature, let the provider pick"), not "caller didn't ask to
    # override" — `dispatch` always resolves `req.temperature` to a concrete
    # tier default before calling this, so the override is unconditional.
    # (Only a test that calls this function directly, bypassing `dispatch`,
    # sees the unresolved field default of `None` here — no production caller
    # does; see the docstring above.)
    cfg = replace(cfg, temperature=req.temperature)
    # NOTE — no-thinking directive intentionally NOT applied here. Disabling
    # a Qwen/GLM model's chain-of-thought over the OpenAI wire needs a
    # backend-specific key (candidates: `chat_template_kwargs:
    # {"enable_thinking": false}`, `reasoning: {"enabled": false}`, or an
    # inline `/nothink` directive) and this repo has no confirmed answer for
    # which one the deployed llama.cpp/llama-swap build (built off a rolling
    # `master` pin, deploy/roles/llamacpp/defaults/main.yml) honors — see
    # the router param-passthrough task write-up. `req.thinking=False` is
    # resolved (SMALL tier default) but deliberately unused here rather than
    # guessed; temperature=0.0 alone is applied. Revisit once the deployed
    # llama-server version's behavior is verified live.
    # Fail fast on a stuck/flapping loopback wire so the failover ladder can
    # fall over to the hosted rung. An explicit ``req.timeout_s`` wins; else a
    # SMALL-tier judge gets the tight cap (see ``_SMALL_LOCAL_TIMEOUT_S``) — a
    # 120s-per-call default let a transient local-wire flap hang whole SMALL
    # batches with no failover (the 2026-07-26 classify stall).
    if req.timeout_s is not None:
        cfg = replace(cfg, timeout=req.timeout_s)
    elif req.tier is Tier.SMALL:
        cfg = replace(cfg, timeout=min(cfg.timeout, _SMALL_LOCAL_TIMEOUT_S))
    messages = req.messages or [{"role": "user", "content": req.prompt}]
    client = LlmClient(cfg)
    try:
        res = client.complete(messages)
    except (RuntimeError, OSError) as exc:
        # A transport-level timeout / connection failure / 5xx-or-429 is
        # unavailability, not a genuine failure — flag it `paused` so a pinned
        # pass backs off and retries instead of recording a dispatch failure
        # that can park the todo.
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=str(exc),
            paused=_is_unavailability(exc),
        )
    return result_from_openai(res, model=model, tier=req.tier)


def openrouter_routing(
    endpoint: dict[str, Any] | None,
    *,
    effort: str | None = None,
    thinking: bool | None = None,
) -> dict[str, Any]:
    """Translate a booked ``meta.endpoints`` variant → the OpenRouter request-body
    block that pins it (gripe 162624), composed with the capability tiers + placement chains gen-param
    ``reasoning`` toggle.

    Emits ``provider:{order:[<slug>], quantizations:[<quant>],
    allow_fallbacks:false, require_parameters:true}`` so OpenRouter routes to
    *exactly* that provider×quant (no load-balancing across the ~28 endpoints).
    The provider slug comes from the endpoint's OpenRouter ``tag``
    (``deepinfra/fp4`` → ``deepinfra``, the routing key), falling back to a
    lower-cased ``provider`` name. A ``quant`` of ``unknown`` is omitted
    (nothing to pin).

    ``reasoning`` is built from both ``effort`` and ``thinking``: an ``effort``
    sets ``reasoning.effort``; ``thinking=False`` additionally (or solely) sets
    ``reasoning.enabled: false`` — OpenRouter's documented switch to turn
    reasoning off entirely — so a ``SMALL``-tier dispatch (thinking off by
    tier default) disables it on the hosted backend without touching the
    ``effort`` a caller may have booked. ``thinking`` of ``True``/``None``
    never clobbers a set ``effort`` (the "on" case is today's behaviour: pass
    ``effort`` through unchanged, or nothing at all).

    Returns ``{}`` when there is nothing to pin/toggle — the caller then posts
    the bare slug with no ``reasoning`` block, today's behaviour.
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
    reasoning: dict[str, Any] = {}
    if effort:
        reasoning["effort"] = effort
    # Auto-off only guards the default (no-effort) shape: an explicit effort
    # means the caller wants reasoning, so it wins over a thinking=False.
    if thinking is False and not effort:
        reasoning["enabled"] = False
    if reasoning:
        body["reasoning"] = reasoning
    return body


#: Vault secret name per OSS provider host, keyed by ``PRECIS_LLM_BASE_URL``'s
#: hostname — so switching provider is a *single* env edit
#: (``PRECIS_LLM_BASE_URL``) instead of also re-copying the matching key into
#: ``PRECIS_LLM_API_KEY`` (gripe 159988). An unlisted host (self-hosted vLLM,
#: a proxy) falls back to the generic ``PRECIS_LLM_API_KEY``, as does a listed
#: host whose provider-specific secret isn't set — so an existing deployment
#: that only sets ``PRECIS_LLM_API_KEY`` keeps working unchanged.
_PROVIDER_KEY_BY_HOST: dict[str, str] = {
    "openrouter.ai": "OPENROUTER_API_KEY",
    "deepinfra.com": "DEEPINFRA_API_KEY",
}


def _provider_api_key(base_url: str) -> str:
    """Resolve the vault-backed API key for ``base_url``'s host.

    Looks up :data:`_PROVIDER_KEY_BY_HOST` by hostname (exact match or a
    subdomain of it) and resolves that secret; falls back to
    ``PRECIS_LLM_API_KEY`` when the host is unlisted, has no matching secret
    set, or ``base_url`` doesn't parse to a host at all.
    """
    from precis.secrets import get_secret

    host = urlparse(base_url).hostname or ""
    for provider_host, secret_name in _PROVIDER_KEY_BY_HOST.items():
        if host == provider_host or host.endswith(f".{provider_host}"):
            key = get_secret(secret_name)
            if key:
                return key
            break
    return get_secret("PRECIS_LLM_API_KEY") or ""


def _dispatch_openai_compat(req: LlmRequest, model: str) -> LlmResult:
    """Drive a hosted OpenAI-compatible OSS backend (the ``OPENAI`` backend).

    Same OpenAI ``/v1/chat/completions`` client as :func:`_dispatch_local`,
    but pointed at ``PRECIS_LLM_BASE_URL`` and authed with a vault-resolved
    key chosen by :func:`_provider_api_key` from that url's host (falling
    back to ``PRECIS_LLM_API_KEY`` — env-override-wins, so a key in the
    environment still works during transition). When the request carries
    a booked ``endpoint`` (gripe 162624), the OpenRouter ``provider:{}`` /
    ``reasoning:{}`` pin is merged into the body so the call hits that exact
    provider×quant. Imports the summarizer client lazily to keep this module
    out of the worker/DB import chain.
    """
    from dataclasses import replace

    from precis.workers.llm_summarize import LlmClient, LlmConfig

    base_url = os.environ.get("PRECIS_LLM_BASE_URL", "")
    api_key = _provider_api_key(base_url)
    cfg = replace(
        LlmConfig.from_env(),
        url=base_url,
        api_key=api_key,
        model=model,
        enabled=True,
    )
    if req.max_tokens is not None:
        cfg = replace(cfg, max_tokens=req.max_tokens)
    # Capability tiers + placement chains gen-param passthrough — see the matching comment in
    # _dispatch_local (`None` is a meaningful resolved value here, not
    # "unset", so the override is unconditional). Unlike the local path, the
    # hosted OpenRouter wire's no-thinking directive IS confirmed (its
    # documented `reasoning.enabled` switch — see openrouter_routing), so
    # it's applied below via extra_body.
    cfg = replace(cfg, temperature=req.temperature)
    messages = req.messages or [{"role": "user", "content": req.prompt}]
    # SMALL-tier judges (classify / summarize / triage) never want a reasoning
    # trace — a reasoning-capable model (glm-4.7-flash) left thinking spends its
    # whole max_tokens on reasoning and returns empty content (the silent None
    # that burned ~10k classify chunks). `thinking` (False by the SMALL tier
    # default) drives `reasoning.enabled: false` without clobbering a booked
    # `effort`. Resolve the tier default here too when unresolved — so a SMALL
    # judge pins reasoning off self-contained, even on a path that reaches this
    # transport without dispatch's top-level resolution.
    thinking = (
        req.thinking if req.thinking is not None else _tier_gen_defaults(req.tier)[0]
    )
    extra_body = openrouter_routing(req.endpoint, effort=req.effort, thinking=thinking)
    client = LlmClient(cfg)
    try:
        # Only pass extra_body when there is something to send, so the bare
        # path is the byte-identical call it was before (gripe 162624 ships
        # dark); the reasoning-off pin above makes it non-empty for SMALL.
        res = (
            client.complete(messages, extra_body=extra_body)
            if extra_body
            else client.complete(messages)
        )
    except (RuntimeError, OSError) as exc:
        # Same unavailability-vs-semantic split as `_dispatch_local` above.
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=str(exc),
            paused=_is_unavailability(exc),
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


#: Default inter-chunk silence cap for a streamed (SSE) completion — the idle
#: timeout that detects a dead connection. Generous relative to real token
#: cadence (even a loaded backend emits *something* every few seconds once
#: generation starts; OpenRouter keeps the stream warm with comment pings) but
#: far below the old blind 600s blocking cap, so a hung wire fails in ~2min
#: instead of stalling the whole rung. Override via PRECIS_LLM_STREAM_IDLE_S.
_STREAM_IDLE_TIMEOUT_S = 120.0


def _stream_idle_timeout_s() -> float:
    raw = os.environ.get("PRECIS_LLM_STREAM_IDLE_S")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _STREAM_IDLE_TIMEOUT_S


def run_oss_tool_loop(
    *,
    prompt: str,
    model: str,
    system_prompt: str | Path | None = None,
    max_turns: int = 20,
    timeout_s: float | None = None,
    tool_less: bool = False,
    local_url: str | None = None,
    temperature: float | None = None,
    thinking: bool | None = None,
    stream: bool = False,
    max_tokens: int | None = None,
) -> AgentLoopResult:
    """Drive the in-process OSS ``tools=`` loop and return the RAW
    :class:`~precis.utils.llm.openai_tools.AgentLoopResult`.

    Extracted from :func:`_dispatch_openai_tools` so a caller that needs the
    loop's ``stop_reason`` verbatim — the planner tick, which must tell a clean
    answer (``stop``) from a ``max_turns`` cutoff (resumable, not failed) —
    reuses the exact client-build + verb-wiring instead of the collapsed
    :class:`LlmResult`. Builds the client from ``PRECIS_LLM_BASE_URL`` + the
    vault key chosen by :func:`_provider_api_key` from that url's host, UNLESS
    ``local_url`` is given — a local-serving slot's pinned llama-swap endpoint
    — in which case it routes there directly with an authless dummy key (a
    loopback model has no auth; the vault key is for the hosted OSS backend).
    This is what makes a served local model backing the ``BIG`` tier dispatch
    to a per-host local endpoint, mirroring :func:`_dispatch_local`'s
    ``local_url`` override.
    Runs the precis verbs in-process via ``runtime.dispatch`` unless
    ``tool_less``. May raise ``RuntimeError`` / ``OSError`` if the executor /
    tools can't be built (an unavailable runtime); the loop itself folds a
    transport failure into ``AgentLoopResult.error`` (``stop_reason='error'``)
    rather than raising. Imports the loop + bridge lazily so the router stays
    DB-free.

    ``temperature``/``thinking`` are the capability tiers + placement chains gen-param passthrough
    (:attr:`LlmRequest.temperature` / :attr:`.thinking`, already tier-resolved
    by :func:`dispatch`). ``temperature`` threads straight onto the client —
    ``None`` (the ``MEDIUM``/``BIG``/``FRONTIER`` tier default)
    means the field is omitted from the wire entirely (the provider's own
    default), a deliberate change from this loop's previous unconditional
    ``temperature: 0`` for every call. The no-thinking directive is only
    applied when this is genuinely a *hosted* OSS call (``local_url`` unset)
    — :func:`openrouter_routing`'s confirmed ``reasoning.enabled`` toggle; a
    direct local llama-swap endpoint gets no such directive (see the matching
    NOTE in :func:`_dispatch_local` — the key llama.cpp/llama-swap itself
    honors for this is unconfirmed).

    ``max_tokens`` is :attr:`LlmRequest.max_tokens` threaded onto the client's
    per-turn wire payload — a real generation-time stop on this transport,
    unlike the ``claude_agent`` path's post-hoc truncation. It was structurally
    unreachable here until 2026-08-13 (the parameter simply didn't exist, so
    ``ToolChatClient._max_tokens`` was always ``None``), which is why a verbose
    rung could generate 33k chars unbounded until the streamed wall ceiling
    killed the whole turn. ``None`` (the default) omits the field from the wire
    entirely — byte-identical to the payload sent before this knob existed; no
    caller gets a cap it didn't ask for.
    """
    from precis.liveness import drain_requested
    from precis.utils.llm.openai_tools import ToolChatClient, run_tool_loop
    from precis.utils.llm.precis_tools import precis_tool_specs, runtime_executor

    if local_url:
        base_url = local_url
        api_key = "dummy"
        # llama-server's prompt cache served CORRUPTED KV state for the
        # ~20k-char quest-tick prompts (2026-08-12: deepseek answered as if
        # the user "just typed 'thinking'", hallucinating a different
        # conversation; the same prompt replayed with cache_prompt=false
        # read correctly). Bypass the cache per-request — a llama.cpp
        # extension, only ever sent to the pinned local endpoint.
        extra_body: dict[str, Any] | None = {"cache_prompt": False}
    else:
        base_url = os.environ.get("PRECIS_LLM_BASE_URL", "")
        api_key = _provider_api_key(base_url)
        extra_body = openrouter_routing(None, thinking=thinking) or None
    timeout = timeout_s if timeout_s is not None else 600.0
    client = ToolChatClient(
        url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body=extra_body,
        stream=stream,
        idle_timeout=_stream_idle_timeout_s(),
        # Graceful drain (spine Layer 1, slice 2): a SIGTERM'd worker aborts
        # the in-flight stream between SSE chunks — partial salvaged via the
        # StreamTimeout path, job retries under the next generation.
        abort_check=drain_requested,
    )
    return run_tool_loop(
        client,
        prompt=prompt,
        tools=[] if tool_less else precis_tool_specs(),
        execute=runtime_executor(),
        system_prompt=_read_system_prompt(system_prompt),
        max_turns=max_turns,
        abort_check=drain_requested,
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
            temperature=req.temperature,
            thinking=req.thinking,
            stream=req.stream,
            # A caller-pinned completion cap reaches the wire here (it already
            # did on the LOCAL / OPENAI_COMPAT transports) — ``None`` leaves the
            # payload exactly as before, so nothing gains a default cap.
            max_tokens=req.max_tokens,
        )
    except (RuntimeError, OSError) as exc:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error=str(exc),
            # Same unavailability-vs-semantic split as `_dispatch_local` above
            # — a failure to *build* the executor/tools rarely trips this, but
            # it shares the exception classes, so classify it the same way.
            paused=_is_unavailability(exc),
            # A failure to *build* the executor/tools is a transport error to a
            # stop_reason reader (the planner tick), same as an in-loop one.
            stop_reason="error",
        )
    # Cost: prefer the loop's own summed ``usage.cost`` (OpenRouter); otherwise
    # price the accumulated token split via the catalog, exactly as
    # ``result_from_openai`` does for the tool-less openai_compat transport —
    # so the budget breaker isn't blind to openai_tools spend either
    # (``glm-fleet-flip-safety`` (git-only) Part 2).
    cost = result.cost_usd
    if cost is None and result.total_tokens is not None:
        from precis.budget.pricing import cost_from_tokens

        # No prompt/completion split survives the loop's accumulation (only a
        # running ``total_tokens``) — price the whole total at the pricier
        # output rate, a deliberately conservative (never-under) estimate for
        # a breaker whose job is to not miss real spend.
        cost = cost_from_tokens(
            model, prompt_tokens=None, completion_tokens=result.total_tokens
        )
    return LlmResult(
        text=result.final_text,
        cost_usd=cost,
        turns_used=result.turns_used,
        model=model,
        tier=req.tier,
        error=result.error,
        # The in-loop transport-error classification (`_is_unavailability`,
        # applied where the exception was actually caught — inside
        # `run_tool_loop`) rides through so an unavailability rides the same
        # skip-and-retry path as the breaker / local-slot pauses.
        paused=result.paused,
        # …and its wall-clock-timeout refinement, so a caller with a bounded
        # give-up budget can tell "wait for the window" from "this rung ran out
        # of clock and will again" without reading the error string.
        timed_out=result.timed_out,
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
        # A wall-clock timeout is a transient unavailability → paused (retry),
        # so a claude-only rung (e.g. FRONTIER) waits rather than parking the
        # todo. A non-timeout ClaudeProcessError (non-zero exit /
        # missing binary) stays a semantic error, as before.
        paused=getattr(exc, "timed_out", False),
        # Same signal, kept unmerged with ``paused`` so a bounded-budget caller
        # sees *why* it paused — this transport's pause is only ever a timeout
        # today, but that is a property of the wrapper, not a guarantee.
        timed_out=getattr(exc, "timed_out", False),
    )


__all__ = [
    "AgentResult",
    "Backend",
    "ClaudePResult",
    "DispatchClient",
    "DispatchError",
    "LlmProvider",
    "LlmRequest",
    "LlmResult",
    "Tier",
    "Transport",
    "dispatch",
    "dispatch_async",
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
