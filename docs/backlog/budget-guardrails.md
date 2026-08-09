# Budget guardrails — a lightweight cost/token backstop

> Design-of-record: loose guide rails + a global circuit breaker, not
> a rigid per-task accounting regime.

Shipped portion: see the `precis.budget` package docstrings
(`breaker.py`, `meter.py`, `quota.py`, `bands.py`); full design in
git history. Live: Piece B (the global breaker wired into
`router.dispatch` + paid `_fetch`, web-editable cap, trip alert),
real-cost capture + the read-only meter/tote, and the OAuth-vs-money
split (`gate_tier(transport=…)`): real money → the dollar meter
(OAuth transports excluded from the SUM); Claude subscription → the
quota snapshot gate (`budget.quota.evaluate`, pause only on
`rejected` / ≥ `PRECIS_QUOTA_CEILING_PCT`, auto-clearing), plus the
`/budget` "Resume paid work now" soft-trip override.

## Open scope

- **Piece A — cost-band affordance:** `budget/bands.py` has the
  Cost/Pace enums + `Band.label()`, but nothing surfaces the label to
  any model — wire it + a permissive "escalate freely when needful"
  line into the agent system prompts. Sense, not enforcement.
- **Piece C — quest attribution remainder:** stamp
  `precis_web/ask.py` (conv_ref_id accepted but not threaded onto
  `LlmRequest`) and `workers/_chase_llm.py` ×3 (dispatches carry no
  ref_id); pass-level passes (dream, review) legitimately stay
  unstamped. Per-quest spend *views* land with the quest layer.
- **Non-LLM compute** (spark DFT/relax/fold, container jobs) never
  touches dispatch — build a `service_calls (pass, host, day)` rollup
  only if the data says local compute capacity is the constraint.

## Open decisions

1. **Ledger union without double-count** — `llm_call_log` for router
   spend, `cache_state` for paid fetches, `ref_events` excluded from
   the money SUM (notional OAuth rows already out); confirm.
2. **Per-model price table** — source + upkeep for tokens→$ on paths
   that don't report cost (lean: small checked-in constant + env
   override; prefer OpenRouter's returned `cost` when present).
3. **Cheap-band threshold** (`~$0.02` is a guess) — tune from the
   meter's real distribution.
4. **Cap defaults** — matter only for the real-money lane now (the
   claude lane self-bounds on quota); set from a week of observed
   spend.

## Out of scope (deliberately)

- Per-task / per-quest hard budgets (the quest "tote" — views first).
- Token-level accounting (dollars are the honest unit).
- Rigid weekly proportional allocation across quests.
- Blocking interactive user work — the breaker only ever throttles
  autonomy.
