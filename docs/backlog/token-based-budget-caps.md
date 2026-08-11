# Add token-rate caps to budget breaker

The budget breaker (`src/precis/budget/breaker.py::gate_tier`) gates on
`cost_usd` only. OAuth transports (claude_agent, claude_p) are excluded
as notional dollars, so quota-drawing transports cannot be throttled before
subscription-quota exhaustion (e.g., the 2026-08-05 seven_day alert). Once
token columns exist in `llm_call_log`, add optional token-rate caps so
quota-drawing transports can be throttled before the subscription wall.

## Motivation

- Real-money transports (openrouter, openai) are cost-gated; OAuth transports
  (claude subscription) hit a quota wall, not a dollar wall.
- Quota wall is invisible to the budget breaker — it gates on USD only.
- Subscription exhaustion stalls all paid work, not just one transport.
- The 2026-08-05 incident: seven_day alert fired after quota was nearly
  consumed; no gate caught it earlier.
- Token columns now exist (shipped 2026-08-11); the data to compute rates is
  available.

## In scope

- Add `token_rate_cap` (tokens/day) to `PrecisConfig` and budget tables.
- Gate quota-drawing transports against their token-day budget before
  dispatch.
- Backfill observed rates to suggest defaults.

## Out of scope

- Per-model token budgets (aggregate across all models on a transport).
- Retroactive token rate for past rows.
- Integration with the web `/budget` UI (separate item).
