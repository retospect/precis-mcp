---
status: ready
title: Router-owned LLM failure classification, reactive quota snapshot, deferred retry for sync surfaces
prio: normal
model: sonnet
---

# Router-owned LLM failure classification, reactive quota snapshot, deferred retry for sync surfaces

Design session 2026-09-17 (Reto + agent), rippling-zooming-taco worktree.
Trigger: a web follow-up on fi191121 (conv co345510) stored a Claude
session-limit 429 verbatim as the answer turn (gr345578). Reto asked what
else dies under token shortage or budget stops, and whether a retry +
equivalent-tier fallback + free-tier scouting framework is worth building.

## Motivation / why

Every production LLM call already goes through `route()` in
`src/precis/utils/llm/router.py` (106 callers, zero bypasses outside
tests), so one fix there covers every surface. Today the router folds a
quota 429 into a bare `LlmResult.error` string with no class attached.
Consequences, each observed on prod:

- The todo lane re-parses the CLI's "resets 9pm" text itself
  (`executors/_common.py::classify_transient_backoff_hours`, gr344988);
  no other lane benefits. Web follow-ups store the failure as content;
  the taproot backfill degrades to no-claim silently
  (`taproot-backfill-llm-outage-silent-noclaim.md`).
- The claude-OAuth gate (`budget/quota.py`) pauses on the
  `claude_quota_snapshot`, but the snapshot only refreshes on the
  quota_check cadence. A live 429 never writes back, so every surface
  rediscovers exhaustion on its own until the next probe. Prod call log,
  last 30 days: 28 quota-class failures slipped through the stale
  snapshot, all on 2026-09-17, across 5 distinct hours.
- The larger failure class by count is not quota: 182 bare 429s in the
  same window, concentrated the week of 2026-09-07, top sources
  `classify` and `llm_summarize` on the SMALL lane. Nothing retries
  those in-process; they surface as errored rows.

Decision (Reto, 2026-09-17): quota exhaustion is a roughly monthly event
on the current numbers, so the cheap router-side pieces ship; the
free-tier zoo, new backends, and a FRONTIER fallback do NOT (see NOT in
scope).

## In scope

1. **Failure class on the result.** `LlmResult` gains `reason_class`
   (`quota | rate | budget | transport | content | None`) and
   `retry_at: datetime | None`. The quota-text parser and the transient
   pattern table move from `executors/_common.py` into the router (one
   owner); the executor reads `reason_class`/`retry_at` instead of
   re-parsing. `paused=True` implies a class of `quota` or `budget`.
2. **Reactive snapshot.** A `quota`-class failure stamps
   `claude_quota_snapshot` immediately (window status exceeded,
   `resets_at` from the 429 text or the CLI event), so the gate pauses
   all subsequent claude calls for the rest of the window. Then schedule
   one quota probe (`claude_quota.refresh`) at `resets_at` + jitter, not
   one per parked job; if still exceeded, re-stamp with the new
   `resets_at`. The gate's existing auto-clear does the rest.
3. **SMALL-lane in-process retry.** A `rate`-class failure on a
   tool-less transport (LOCAL / OPENAI_COMPAT / CLAUDE_P) retries inside
   `route()` with bounded exponential backoff (default 3 tries, 2s·2ⁿ,
   capped 30s) before surfacing. Env-tunable, default on.
4. **Deferred-call job for synchronous surfaces.** A `deferred_llm_call`
   job type carrying (surface, args dict, `retry_at`). First consumer:
   the web follow-up (`precis_web/routes/refs.py::_run_followup`) — on a
   `quota`/`budget`-class failure it enqueues instead of appending a
   system turn; the human turn is the resume point; the job appends the
   real answer on success. Closes gr345578.
5. **Ladder proof on MEDIUM only.** Set a `llm.chain.medium` override on
   prod (config, no code) with a paid OpenRouter no-training-provider
   rung below claude, so `FailoverProvider` runs on prod for the first
   time on a low-stakes tier. Chain fall-through must be gated on
   `reason_class in {quota, rate, budget, transport}` — a `content`
   error never falls through (small router change, part of item 1).

## Explicitly NOT in scope

- No free-tier scouting, no new backends (Google AI Studio, Groq,
  Mistral free, direct EU providers). Deferred until a second month shows
  quota exhaustion recurring; EU candidates already listed in
  `eu-llm-providers.md`.
- No FRONTIER or BIG fallback to OSS. Reviewer-grade agentic work parks
  with `retry_at`; the OSS tools loop and the golden-task harness
  (`llm-catalog-wire-policy.md`) are not proven on our jobs.
- No privacy-class filter on endpoints (data policy on catalog cards,
  proprietary → local-only). That is the precondition for any free
  tier and belongs with the catalog wiring item, not here.
- No change to the dollar breaker or token-rate caps
  (`token-based-budget-caps.md`).
- Not diagnosing the 2026-09-07 SMALL-lane 429 storm's cause (item 3
  only makes it retry); file separately if it recurs.

## Acceptance criteria

- A synthetic "You've hit your session limit · resets 9pm (UTC)" from a
  claude transport yields `reason_class='quota'`, `paused=True`,
  `retry_at` = that instant; the executor's park horizon equals it
  (existing gr344988 tests pass unchanged against the router-owned
  parser).
- After one such failure, the next claude-tier `route()` call on the
  same store returns `paused=True` without spawning a subprocess (gate
  reads the reactively stamped snapshot).
- Exactly one quota probe is scheduled per exhausted window regardless
  of how many jobs parked.
- A bare 429 on a LOCAL transport retries up to the bound and succeeds
  when the fake backend recovers on the 2nd try; a `content` error does
  not retry and does not fall through a chain.
- Web follow-up under a synthetic quota 429: no `system` turn appended,
  one `deferred_llm_call` job exists with the conv slug + question and
  `retry_at`; running that job appends an `asa` turn.
- `llm_call_log` rows carry `reason_class` (new nullable column, forward
  migration) so the 30-day query in the motivation can be re-run by
  class without regex.
- MEDIUM chain override on prod: one `llm-failover: fell back to rung 1`
  warning observed in worker logs during the next quota window, with
  the result placement stamped `cloud`.

## Target + blast radius

- `src/precis/utils/llm/router.py` — `LlmResult` fields, classifier,
  in-process retry, chain fall-through gate.
- `src/precis/workers/executors/_common.py` — delete the local parser,
  read the router's class.
- `src/precis/budget/quota.py`, `src/precis/utils/claude_quota.py` —
  reactive stamp + scheduled probe.
- `src/precis/workers/` — new `deferred_llm_call` job type + executor.
- `src/precis_web/routes/refs.py::_run_followup` — enqueue path.
- Migration: `llm_call_log.reason_class`.
- Docs: router module docstring, `budget` package docstring; skill
  `precis-status` if it renders the quota panel.

## Open questions / decisions log

- DECIDED: OpenRouter stays the single aggregator rung; at most one
  direct EU provider later, for the proprietary class and outage
  independence. Not in this item.
- DECIDED: a silent tier downgrade on reviewer-grade output is the one
  way this can do harm; FRONTIER parks, never falls through.
- OPEN (non-blocking): should `deferred_llm_call` reuse the todo lane's
  park machinery instead of a new job type? Coder's call after reading
  `executors/_common.py`; either satisfies the acceptance criteria.
