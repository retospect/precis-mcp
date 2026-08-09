"""The LLM routing layer — tiers, chains, catalog.

One seam for model selection + transport choice + result normalization:
every routed call goes through :func:`dispatch` (or :func:`dispatch_async`
for streaming) to a narrow :class:`LlmProvider` port picked from a
:class:`Transport`-keyed registry. ``claude -p`` is just two adapters among
peers — Anthropic is a swappable leaf, and every agentic + judge call site
(dream, reviewers, cad/structure propose, web ask, the ``claude_p`` judges)
is folded through the seam.

Public surface:

* :class:`Tier` — capability tiers (frontier / big / medium / small).
* :class:`Transport` — the transports + the OpenAI-tools extension point.
* :class:`Backend` / :func:`resolve_backend` — the anthropic↔openai switch
  (LLM independence); ``PRECIS_LLM_BACKEND`` selects, ships dark by default.
* :func:`resolve_model` — the ONE tier→model table.
* :func:`select_transport` / :func:`transport_for_profile` — routing.
* :class:`LlmProvider` / :func:`provider_for` — the swappable backend port
  + its registry accessor (the LLM-independence seam).
* :class:`LlmRequest` / :func:`dispatch` — the seam.
* :class:`LlmResult` + ``result_from_*`` — the normalized result.

Tiers
------------------------
:class:`Tier` is pure capability: call sites route on what a task needs,
never where it runs (a served OSS model backs ``BIG`` when the chain routes
there — there is no ``LOCAL_BIG``). The five retired location-coupled tier
strings survive only as *stored* values (quest loops, baked jobs, route-log
rows); ``router.tier_from_str`` degrades them onto capability analogues
instead of raising. ``llm_catalog.seed_default_cards`` seeds one
``tier_floor`` card per tier. The planner alias vocab
(``PLANNER_TIER_BY_ALIAS``: ``frontier/big/medium/small`` + legacy
``opus/sonnet/haiku/local``) is the single source the dispatcher, the todo
guards, asa's ``/model``, and the web model pickers key on;
``planner_model_choices`` renders it through the LIVE ``resolve_chain``.

Resolution, per dispatch (in order)
-----------------------------------
1. **Backend** — app_settings ``llm.backend`` → ``PRECIS_LLM_BACKEND``
   (default ``anthropic``); ``openai`` without ``PRECIS_LLM_BASE_URL``
   demotes to ``anthropic`` rather than POST to a phantom endpoint.
2. **Operation** — a ``req.source`` registered in
   :mod:`~precis.utils.llm.operations`' opt-in allow-list
   (``LLM_OPERATIONS``: ``reading_brief`` / ``meditation`` / ``briefing`` /
   ``plan_tick``) has its tier + model owned by the registry default + a
   live ``llm.op.<source>`` override. Excluded on purpose: functional pins
   (``classify``→``summarizer``) and router-bypassers (``fix_gripe``).
3. **Model** — :func:`resolve_model`: ``llm.model.<tier>`` DB override →
   ``PRECIS_MODEL_*`` env → compiled default. Backend-aware: under an
   effective ANTHROPIC backend, an OSS override on a claude-bound tier
   (FRONTIER/BIG/MEDIUM) is dropped, so a half-applied flip never hands an
   OSS slug to a claude transport; ``SMALL``'s override is always honored.
4. **Chain** — ``router.resolve_chain``: an operator ``llm.chain.<tier>``
   override is read *regardless of* ``PRECIS_LLM_FAILOVER``; with none, a
   single primary rung (``select_transport``), or — flag on — the built-in
   OSS→claude failover ladder (claude rung pinned to the compiled tier
   default; ``SMALL`` gets no claude rung). A tool-using call skips rungs
   whose transport can't carry tools (``Transport.carries_tools`` — only
   ``CLAUDE_AGENT``/``OPENAI_TOOLS``): unfiltered, an agentic call on a
   completion wire writes its verb calls as prose and bills in full. An
   emptied chain falls back to the correct-by-construction default, logged.
5. **Chain filters** — per-request ``LlmRequest.placement``
   (``'local'``/``'cloud'``, strict: emptying a nonempty chain is an
   *error* — the caller asked for a rung the chain lacks); the cloud
   throttle (``llm.cloud_enabled`` off prunes cloud rungs; a tier left
   rungless returns ``paused``, skip-not-fail, never a silent local
   degrade); a serving-aware skip of a loopback ``LOCAL`` rung whose model
   this host doesn't serve (else every call ECONNREFUSEs then fails over).
6. **Gates** — the budget breaker (keyed on the resolved rung's resource:
   OAuth quota vs dollars vs free local) and the catalog window admission —
   both fold refusals into the normalized result, never raise.

Local serving: a host that declares ``served_by`` for the model holds one
of its ``resource_slots`` for the call (``local_serving.acquire``); a
reserved slot's ``endpoint`` repoints the local wire at llama-swap
directly. A *saturated* slot (all capacity busy) retries rung 0 against the
hosted OSS endpoint instead of the busy hardware — unless
``placement='local'`` pins the call, which takes the paused backoff.
``SMALL``'s local-only aliases remap to a hosted small model
(``llm.model.small`` → ``PRECIS_LOCAL_SMALL_HOSTED_MODEL`` → default
``z-ai/glm-4.7-flash``) whenever a call lands on a hosted OSS transport.

Failure semantics: a transport exception is classified
(``router._is_unavailability``) — timeout / connection / 5xx / 429 →
``paused`` (skip-not-fail: the caller retries next cycle, never parks);
other 4xx stays ``error``. A claude wall-clock timeout counts as
unavailability via ``ClaudeProcessError.timed_out``. Every transport
carries a wall-clock timeout (claude 600 s; local/openai 120 s; a SMALL
local judge is capped at 30 s so a flapping loopback fails fast into the
failover ladder instead of stranding a whole batch).

Gen-params: per-tier ``(thinking, temperature)`` defaults (``SMALL`` =
thinking off + temp 0.0 — a reasoning model on that rung otherwise burns
the whole budget on its trace and returns empty); ``rung_knobs`` is the
per-transport honesty table (which knobs a wire actually forwards);
``resolve_selection`` is the never-raising preview resolver behind
``GET /api/llm/resolve`` and the shared ``llm_selector`` widget. A todo's
``meta.llm_select`` (guarded sibling of ``meta.llm_tier``) threads
placement/thinking/effort/temperature onto its ticks.

All switches are live: ``live_config`` layers app_settings rows (written by
``/status?tab=services``) over env, TTL-cached ~15 s, dark when unset —
no row ⇒ byte-identical to env-only routing. Two call sites whose
``--model`` assumes claude semantics — ``fix_gripe`` and
``sandbox_run``/``claude_docker`` — read :func:`resolve_backend` and skip
clean under ``backend=openai`` instead of folding through :func:`dispatch`.

The ``llm`` model catalog (``llm-catalog`` (git-only); all five slices
built, ships dark) layers on this seam — facts/writer in
:mod:`precis.llm_catalog`, handler in :mod:`precis.handlers.llm`:

* :mod:`~precis.utils.llm.admit` — pure window fit-check wired into
  :func:`dispatch` after ``gate_tier``; a doomed (context, model) pairing
  is refused *with the numbers* as a normalized ``LlmResult.error``, never
  raised. No card / no known window ⇒ no-op.
* :mod:`~precis.utils.llm.policy` — ``select_offering`` (deterministic
  requirement → model, decision-point only, never the hot path).
* :mod:`~precis.utils.llm.requirement` — ``infer_requirement`` (the LLM
  judges *capability needed*, never picks a model), ``choose_model`` chains
  the two.

Invariant: empty catalog / nothing-fits ⇒ ``resolve_model(tier_floor)``,
byte-identical to routing without a catalog.
"""

from __future__ import annotations

from precis.utils.llm.router import (
    Backend,
    LlmProvider,
    LlmRequest,
    LlmResult,
    Tier,
    Transport,
    dispatch,
    provider_for,
    resolve_backend,
    resolve_model,
    result_from_agent,
    result_from_claude_p,
    result_from_openai,
    select_transport,
    transport_for_profile,
)

__all__ = [
    "Backend",
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
