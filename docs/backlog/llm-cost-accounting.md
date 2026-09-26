# Llm cost accounting

Grouped 2026-09-26 from 4 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Add token-rate caps to budget breaker

_Grouped 2026-09-26; was `token-based-budget-caps`._

The budget breaker (`src/precis/budget/breaker.py::gate_tier`) gates on
`cost_usd` only. OAuth transports (claude_agent, claude_p) are excluded
as notional dollars, so quota-drawing transports cannot be throttled before
subscription-quota exhaustion (e.g., the 2026-08-05 seven_day alert). Once
token columns exist in `llm_call_log`, add optional token-rate caps so
quota-drawing transports can be throttled before the subscription wall.

### Motivation

- Real-money transports (openrouter, openai) are cost-gated; OAuth transports
  (claude subscription) hit a quota wall, not a dollar wall.
- Quota wall is invisible to the budget breaker — it gates on USD only.
- Subscription exhaustion stalls all paid work, not just one transport.
- The 2026-08-05 incident: seven_day alert fired after quota was nearly
  consumed; no gate caught it earlier.
- Token columns now exist (shipped 2026-08-11); the data to compute rates is
  available.

### In scope

- Add `token_rate_cap` (tokens/day) to `PrecisConfig` and budget tables.
- Gate quota-drawing transports against their token-day budget before
  dispatch.
- Backfill observed rates to suggest defaults.

### Out of scope

- Per-model token budgets (aggregate across all models on a transport).
- Retroactive token rate for past rows.
- Integration with the web `/budget` UI (separate item).

## Broaden llm_call_log ref linkage for attribution

_Grouped 2026-09-26; was `broaden-llm-call-log-ref-linkage`._

Currently ~0.04% of LLM calls carry `ref_id` (plan_tick's todo/quest parents).
Ambient worker passes (summarize, classify, glossary) record no parent
attribution. Decide which call sites should thread `ref_id`/job attribution
through `LlmRequest`, so per-question vs amortized-infrastructure cost can
be split by query source instead of by convention.

### Motivation

- Per-call token accounting now exists (shipped 2026-08-11); reporting needs
  `ref_id` to attribute cost to the right quest/todo parent.
- Current practice: high-level dispatch (plan_tick) tags rows; pass-level
  batches (dream, review, classify, summarize) don't.
- Aggregate-by-`features->>'source'` covers most reporting today, but finer
  grain attribution requires knowing the originating question.
- Not urgent: existing rollups work for high-level billing; this unlocks
  finer per-quest spend views.

### Open questions

1. Which passes should be threaded?
   - plan_tick children (obvious — already happens).
   - Ambient classify/summarize (per-doc, amortized cost?).
   - chase/verify judgments (child job type — inherit parent?).
   - figure/mermaid render (attributed to the turn, not a quest).
2. Should ambient passes share cost across their batch, or charge each call?
3. What is the ground truth for "this call serves this quest"?

### Out of scope

- Retroactive attribution for past NULL rows.
- Changes to the dispatch signature.
- Per-task hard budgets (a separate backlog item).

## llm_call_log rollup and retention policy

_Grouped 2026-09-26; was `llm-call-log-rollup-retention`._

The `llm_call_log` table ingests ~300k rows/day (measured 2026-08-11; 4.2M
rows/14d aggregate). No retention policy exists. Design a rollup (e.g.,
hourly aggregates by model/placement/source) and raw-row retention window
before the table becomes a vacuum/index problem; coordinate with the
`scripts/db-thrash-review` practice.

### Motivation

- Table is append-only and growing at ~300k rows/day.
- At current rate, retention becomes an index bloat and vacuum pain point
  within months.
- Token columns (shipped 2026-08-11) enable more fine-grained queries; the
  same metric is queryable at multiple granularities.
- Hourly rollups preserve daily/weekly aggregates for billing/reporting
  without keeping every call row.
- Ops practice: `scripts/db-thrash-review` monitors bloat and repack; a
  retention policy feeds that cadence.

### In scope

- Design raw-row retention window (e.g., 30 days, tunable).
- Design rollup granularity and dimensions (model, placement, source, cost
  tier, transport).
- Rollup table schema and indexing.
- Operator runbook for tuning retention post-deploy.
- Integration with `scripts/db-thrash-review` cadence.

### Out of scope

- Retroactive rollup of existing data (backfill if desired, separate task).
- Real-time or sub-hourly rollups.
- Archive/export pipeline.

### Open decisions

1. Retention window duration: 30/60/90 days?
2. Rollup dimensions: should we preserve some raw rows for outlier
   investigation, or aggregate everything?
3. Write the rollup from a cron job, or as a deferred-lane pass?

## Parse token usage from claude_p output

_Grouped 2026-09-26; was `claude-p-token-usage-parsing`._

The CLAUDE_P transport (`claude -p`, one-shot judges: chase/verify, figure,
mermaid) leaves `LlmResult` token fields `None`, so its `llm_call_log` rows
carry NULL `input_tokens`, `output_tokens`, `cache_read_tokens`, and
`cache_creation_tokens`. Cost USD is
still recorded. Parse usage metadata from `claude -p --output-format json` in
`result_from_claude_p()` so those rows carry token counts too, enabling
proper token accounting across all transports.

### Motivation

- Per-call token columns now exist in `llm_call_log` (shipped 2026-08-11).
- The claude_p transport reports cost but not token counts (claude_agent
  already populates them from stream-json usage).
- Ambient accounting needs completeness: partial data obscures true cost
  distribution and per-model benchmarking.
- Upstream: the `claude -p --output-format json` response includes usage
  metadata; the data is available, just not extracted.

### In scope

- Extract `usage` fields from claude_p JSON output.
- Map them to `LlmResult.input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_creation_tokens`.
- Backfill live rows if feasible; new rows post-fix will be complete.

### Status (2026-08-15, 5da355c0)

Main scope DONE: `claude_p.py`'s `_extract_usage`/`_unwrap_envelope` now parse
the envelope's `usage` block into `ClaudePResult`, and
`result_from_claude_p()` threads it into `LlmResult` — the claude_p rung no
longer drops tokens. REMAINING: the `openai_tools` split below is untouched
by this ship (no changes to `openai_tools.py`), so this item stays open for
that half.

### Related gap: openai_tools split

The `openai_tools` multi-turn loop has the same symptom for a different
reason: `ToolChatClient.chat()` (`src/precis/utils/llm/openai_tools.py`)
reads only `usage.total_tokens` per turn, and `AgentLoopResult`/`ChatTurn`
carry only the summed total — so `_dispatch_openai_tools` rows get
`total_tokens`/cost but NULL `input_tokens`/`output_tokens`. Fix alongside
this item: read `usage.prompt_tokens`/`completion_tokens` per turn, sum
into the loop result, and thread the split into `LlmResult`.

### Out of scope

- Retroactive token estimation for past null rows.
- Changes to the claude_p transport signature.
