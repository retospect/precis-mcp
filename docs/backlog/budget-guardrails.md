---
status: draft
pillar: platform
---

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
- **Non-LLM compute** (the twins' GPU lanes — DFT/relax/fold on pollux,
  retrosynth on castor — and container jobs) never
  touches dispatch — build a `service_calls (pass, host, day)` rollup
  only if the data says local compute capacity is the constraint.

## Open scope (folded 2026-10-01 from the closed tier-ladder cutover)

- **The breaker never gates SMALL** — `bands._TIER_BANDS[SMALL]=FREE`, so
  `breaker.gate_tier` lets SMALL through even when a cap has tripped. SMALL
  is the highest-volume tier and has been all-cloud since 2026-08-15, so a
  tripped cap pauses MEDIUM/BIG/FRONTIER while SMALL keeps spending.
- **The hourly runaway check** — an alert-board / scheduled inspection over
  the token-first rollups (`spend_rollup`, `llm_tote`, `precis llm cost`).
  The rollups exist; nothing schedules the check.

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

## Credit-aware admission: the work-class taper (gr347551)

Shipped (`quota.TAPER_FLOORS`, `breaker._gate_quota`). Every LLM request
carries a work class (`LlmRequest.work_class`; unset → the ambient
`work_class_scope` → `background`). The quota taper decides per
(class, tier) from one table; percentages are of a window *used*; "warning"
is the provider's `allowed_warning` / `warning` status with no percentage:

| class | who sets it | BIG/FRONTIER stop | MEDIUM stop | on a pause |
|---|---|---|---|---|
| background | default: every worker pass | ≥75% or warning | ≥90% | skip; re-run when it clears |
| deliverable | briefing job (`work_class='deliverable'`) | ≥85% or warning | ≥95% | degrade FRONTIER→MEDIUM + notice; all paused → "delayed" notice, hourly retry to the recurring's `deadline_utc` (default 12:00Z), then "skipped today" |
| interactive | asa turns, web requests (middleware), session MCP tool calls (tool wrapper) | never | never | — |

All classes park at 100% / a blocking status until the window resets;
SMALL and local tiers are never gated; the resume override bypasses both.
The deliverable MEDIUM floor (95%) is this change's pick — Reto's ruling set
only the BIG floor. Each paid gate decision is tallied per class as
served / paused (+ degraded from the caller) in `llm_outcome_hourly`;
/budget shows 24h counts and what each class is tapered at now.

Open:
- The prod `claude_quota_snapshot` carries no `used_percentage`, so only the
  warning stage and the park can fire; have `quota_check` capture a usable
  remaining figure from the provider.
- The deliverable ladder stops at MEDIUM; add the local rung once the
  gr479851 asa degrade ladder lands its served-local-model pin.
- Other recurring posts besides the briefing still run as background;
  mark them deliverable as they gain a notice path. `briefing_audio` makes
  no LLM call (local TTS of the persisted brief), so it needs none.
- Session MCP tool calls from cluster agents (quest ticks driving the MCP)
  also read as interactive; split by caller if that shows in the tally.
