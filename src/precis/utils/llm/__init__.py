"""The LLM routing layer — tiers, chains, catalog.

One seam for model selection + transport choice + result normalization:
every routed call goes through :func:`route` (or :func:`dispatch_async`
for streaming) to a narrow :class:`LlmProvider` port picked from a
:class:`Transport`-keyed registry. ``claude -p`` is just two adapters among
peers — Anthropic is a swappable leaf, and every agentic + judge call site
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
* :class:`LlmRequest` / :func:`route` — the seam.
* :class:`LlmResult` + ``result_from_*`` — the normalized result.
* :mod:`~precis.utils.llm.suitability` — outcome posteriors on model cards
  (dark to routing).

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
   ``plan_tick`` / …) has its tier + model owned by the registry default + a
   live ``llm.op.<source>`` override.
   ``chase:verify`` uses the BIG capability tier; its per-operation chain
   selects the local-serving node's gpt-oss rung, with the endpoint resolved from the
   model card rather than embedded in code.
   An op override may carry a ``chain`` using ``llm.chain.<tier>`` grammar.
   Malformed chains are logged and ignored. ``resolve_chain`` consumers
   (the ``/status`` models panel, ``planner_rung0_model``) show the tier chain.
   A saturated local op rung 0 advances to the next rung. Exclusions: functional pins
   (``classify_topics``→``summarizer``, ``classify:escalate``→its Tier-2
   re-judge model) and router-bypassers (``fix_gripe``).
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
   without tools. ``CLAUDE_AGENT`` and ``OPENAI_TOOLS`` carry tools;
   the latter enforces ``LlmRequest.openai_tool_allowlist`` at execution.
   An emptied chain falls back to the default and logs it.
   A rung may also carry ``"bare": true`` — the auth opt-in: that rung runs
   ``claude -p --bare``, dropping the OAuth token and authenticating with a
   vault-resolved ``ANTHROPIC_API_KEY`` (per-token billing, not subscription
   quota; the breaker gates it on dollars accordingly). False everywhere by
   default; a non-boolean degrades the *whole* chain to the default (a
   billing switch must not flip on a typo); inert + warned on a non-claude
   transport. ⚠ ``meter.spent_usd`` does not yet count bare spend —
   `me690531`.
5. **Chain filters** — per-request ``LlmRequest.placement``
   (``'local'``/``'cloud'``, strict: emptying a nonempty chain is an
   *error* — the caller asked for a rung the chain lacks); the cloud
   throttle (``llm.cloud_enabled`` off prunes cloud rungs; a tier left
   rungless returns ``paused``, skip-not-fail, never a silent local
   degrade); a serving-aware skip of a loopback ``LOCAL`` rung whose model
   this host doesn't serve, and a cooldown for refused local endpoints.
6. **Gates** — the budget breaker (keyed on the resolved rung's resource:
   OAuth quota vs dollars vs free local) and the catalog window admission —
   both fold refusals into the normalized result, never raise. An
   ``OpDefault.quota_fallback`` op escapes a ``quota`` pause onto the
   chain's next non-subscription rung (``llm.quota_fallback_daily_usd``
   caps it daily).

Local serving reserves a ``resource_slots`` row on the ``served_by`` host
and routes to its declared endpoint. LAN-routable endpoints share that
host's capacity across the fleet. A live but full slot can wait for
capacity. Connection failures, HTTP 5xx and timeouts that produced no
output open a process-local cooldown, so acquisition skips the endpoint
while its advertisement survives a restart — a timeout that already
streamed partial output is slowness, not an outage. One call probes
after expiry and a dead-wire failure renews the cooldown. Saturation or outage can trigger hosted fallback,
logged as ``cloud``. Local-pinned calls pause, using the endpoint cooldown
as the transport retry horizon during an outage.

Before an OSS tool loop with an agentlog can write, the router records the
resolved dispatch model as ``meta.model_actual``. Revision stamps are
captured during tool execution, so run-end metadata arrives too late.

Route-log placement: ``llm_call_log.placement`` is the rung that ran
(landed; the dollar caps exclude ``local``); ``placement_routed`` (0179) is
rung 0 after the step-5 intent filters, before the serving skip, saturated
retry and failover walk — ``local`` whenever a ``served_by`` slot exists.
``routed='local' AND placement='cloud'`` = fell back to cloud.
``SMALL``'s local-only aliases remap to a hosted small model
(``llm.model.small`` → ``PRECIS_LOCAL_SMALL_HOSTED_MODEL`` → default
``z-ai/glm-4.7-flash``) whenever a call lands on a hosted OSS transport.

``features.account`` names the route.

Failure semantics: every classifiable ``LlmResult.error`` carries a
``reason_class`` and a UTC ``retry_at`` — ``quota`` (Claude window
exhausted, horizon = the parsed reset; wording in ``quota``), ``budget``
(dollar breaker, provider credits, 402), ``rate`` (bare 429/overloaded),
``transport`` (timeout, connection, 5xx) or ``content`` (refused /
filtered). One table owns it (``failure``): the router stamps live
results (structurally from the caught exception first, then by wording),
the executor's parked-job backoff reads the same table off the captured
reason string, and the route-log stores both under ``features`` (with
``reason_class`` also promoted to a real ``llm_call_log`` column, migration
0204, so the by-class query is indexable). A
``content`` error never falls through a failover chain — the next rung
would answer the same; every other class, and an unclassified error,
does. On the ``SMALL`` lane over a tool-less transport (local /
openai_compat / claude_p) a ``rate`` or ``transport`` failure is retried
inside ``route()`` with bounded exponential backoff (default 3 attempts,
2s·2ⁿ with jitter, total under 30 s; ``PRECIS_LLM_RETRY_ATTEMPTS``, ``1``
turns it off) — a timed-out result is never retried. A ``quota`` failure
on a claude-OAuth rung stamps ``claude_quota_snapshot`` at once
(``budget.quota.stamp_exhausted``) so the gate pauses the next claude call
without a subprocess.

Timeouts (including ``ClaudeProcessError.timed_out``), connection errors,
5xx and 429 pause for retry. Other 4xx stay errors
(``router._is_unavailability``). Defaults are 600 s for ``claude_agent``
and 120 s for local/openai, with SMALL local capped at 30 s for fast
failover. The tiered ``claude_p`` caps mitigate MEDIUM timeouts pending
live verification (gr696218, ``router._claude_p_timeout_s``).

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
clean under ``backend=openai`` instead of folding through :func:`route`.

``local_first.run`` is an opt-in acceptance seam
(``operations.LOCAL_FIRST_OPS``), unused by product call sites; its knobs are
in :mod:`~precis.utils.llm.local_first`.

The ``llm`` model catalog (ships dark) layers on this seam — facts/writer in
:mod:`precis.llm_catalog`, handler in :mod:`precis.handlers.llm`:

* :mod:`~precis.utils.llm.admit` — pure window fit-check wired into
  :func:`route` after ``gate_tier``; a doomed (context, model) pairing
  is refused *with the numbers* as a normalized ``LlmResult.error``, never
  raised. No card / no known window ⇒ no-op.
* :mod:`~precis.utils.llm.policy` — ``select_offering`` (deterministic
  requirement → model, decision-point only, never the hot path).
* :mod:`~precis.utils.llm.requirement` — ``infer_requirement`` (the LLM
  judges *capability needed*, never picks a model), ``choose_model`` chains
  the two. Strict classifier output, the 24 h cache and the no-subscription
  guard are documented in :mod:`requirement`.

Demand metadata — task kind, consequence, privacy; never a routing input — is
:mod:`~precis.utils.llm.recipes`.
``decide.py`` logs shadow choices without changing execution.

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
    provider_for,
    resolve_backend,
    resolve_model,
    result_from_agent,
    result_from_claude_p,
    result_from_codex_exec,
    result_from_openai,
    route,
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
    "provider_for",
    "resolve_backend",
    "resolve_model",
    "result_from_agent",
    "result_from_claude_p",
    "result_from_codex_exec",
    "result_from_openai",
    "route",
    "select_transport",
    "transport_for_profile",
]
