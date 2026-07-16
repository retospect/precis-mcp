# Budget guardrails — a lightweight cost/token backstop

> **Status: design / proposal. Not built.** Design-of-record for a
> lightweight spend guardrail: loose guide rails + a global circuit breaker,
> not a rigid per-task accounting regime. Captures the design conversation of
> 2026-07-15 (Reto + session). Ships dark when built — every slice is gated
> and additive. The **open decisions** at the end are for Reto to mark up
> before slice 1 starts. Related: `docs/proposals/quest-layer.md` (the "tote"
> / weekly-budget idea this generalizes to a global backstop first).

## Goal

precis has no aggregate cost ceiling. Per-*call* caps exist and are solid, but
nothing bounds the *sum* over time — so a tight loop of cheap calls, or many
workers firing at once, is an unguarded "AI eats the annual budget of a
mid-sized country in 5 minutes" risk.

The aim is **loose guide rails**, not obsession:

> **Prefer the cheapest tier that can do the job; escalate freely when the
> cheap tier stalls or the question is high-value — a day at the library beats
> a year of DFT.** The rails sit far away, at the catastrophe ceiling; within
> normal operation nothing is ever refused.

Two deliverables:

1. **A "sense" of cost** — a uniform `free · cheap · expensive` (+ `fast ·
   slow`) affordance surfaced to the model, so even a lesser model *feels*
   which lane it's in without doing dollar arithmetic.
2. **A global circuit breaker** — one number, "no more than $X per day",
   editable in the web UI, that refuses *new expensive* work once tripped
   (interactive + cheap/free still flow). The hard backstop.

## What already exists (reuse, don't rebuild)

**Per-call caps — solid, keep as the floor:**

- `claude_p` one-shot judge: `$0.10/call` (`PRECIS_CLAUDE_MAX_USD`).
- `claude_agent` session: `$2.00/call` + `--max-turns` + wall-clock timeout
  (`PRECIS_CLAUDE_AGENT_MAX_USD`).
- `plan_tick`: `$5.00/tick` (`PRECIS_PLAN_TICK_MAX_USD`).
- openalex backfill: `$25/sweep`.

> **These are *ceilings*, not typical costs.** `--max-budget-usd` is the
> backstop the CLI refuses to exceed; a real haiku judge or opus tick costs a
> small fraction of it. The tote must use the **actual returned cost**, never
> these caps — the caps look "massively inflated" precisely because they're
> worst-case guards, not observations.

**Cost ledger — already recorded, the backstop is a query over it:**

- `llm_call_log` (`src/precis/route_log.py`, migration 0061) — every
  `router.dispatch` call: `cost_usd`, `tier`, `source`, `model`, ts. This is
  the router path (claude, and the OpenAI-compatible / OpenRouter backends).
- `ref_events.cost_usd` — agentic `agent:done` events.
- `cache_state.cost_usd` — paid-API kinds (perplexity websearch /
  reasoning / research; any future paid fetch). Cache hits are `$0`.
- `src/precis_web/routes/status.py:_claude_usage` already rolls up 24h/7d
  spend by model — proof the aggregate query is cheap and the data is there.

**Partial affordance — scattered, to be unified:**

- Cache kinds render `[cost: free]` / `[cost: ~$0.005 — cached]`
  (`_cache_base.py:_cost_str`).
- Perplexity `Next:` trailers nudge with `~$0.005/call` / `~$0.50/call`.
- The `Tier` enum (`utils/llm/router.py`) already encodes the ladder:
  `LOCAL_SMALL` (free/fast) → `CLOUD_SMALL` (cheap) → `CLOUD_SUPER`
  (expensive/slow).

## Cost sources & authority — use the real returned number

The tote is only honest if it sums *actual* spend, not estimates. The three
transports differ in what they report:

- **Claude (`claude_agent`, `claude_p`) — true dollars, authoritative today.**
  The stream-json result event carries `total_cost_usd`, parsed into
  `AgentResult.cost_usd` / `ClaudePResult.cost_usd`
  (`utils/claude_agent.py`). Flows to `ref_events.cost_usd` + `llm_call_log`
  unchanged. Nothing to do.
- **Local proxy + OpenRouter/OpenAI-compat — tokens, and currently dropped.**
  `LlmClient.complete` reads `usage.total_tokens`
  (`workers/llm_summarize.py`), but the router's `result_from_openai`
  hardcodes `cost_usd=None` (`utils/llm/router.py`) — so the token count is
  thrown away. Two small fixes make it authoritative: (1) stop dropping
  `usage` (carry `prompt/completion/total_tokens` through `LlmResult`); (2)
  convert **tokens → $** via a per-model price table (`$/1M in`, `$/1M out`),
  *or* read OpenRouter's own `cost` field where present. Local is priced `$0`
  (free band).
- **Perplexity — response has usage/cost, but code uses a flat estimate.**
  `_fetch` sets `cost_usd = self.cost_per_call_usd` (a ClassVar
  `$0.001/$0.005/$0.50`). The Sonar response body carries a `usage` object
  (tokens; newer Sonar also a `cost` breakdown) that is ignored. Switch to
  the response's real `usage`/`cost`; keep the ClassVar as a fallback only.

**Rule:** authoritative cost = the number the provider returns for *that*
call. Estimates (ClassVars, price-table conversions) are fallbacks used only
when the provider doesn't return a dollar figure. A per-model price table is
the one piece of conversion machinery this needs (tokens → $ for the OSS/local
paths).

## Design

Three pieces, each independently shippable.

### Piece A — the cost-band affordance (the "sense")

A small pure module (`src/precis/budget/bands.py` or similar) with:

- Two axes: **cost** `free · cheap · expensive`, **pace** `fast · slow`.
  (Decision: `expensive`, **not** `steep` — unambiguous to small models; matches
  Reto's original framing.)
- A table mapping each `Tier` and each paid kind to a `(cost, pace)` band:
  - `LOCAL_SMALL` → `free · fast`
  - `CLOUD_SMALL` (haiku judge) → `cheap · fast`
  - `CLOUD_MID` (sonnet tick) → `cheap · slow`
  - `CLOUD_SUPER` (opus agent) → `expensive · slow`
  - `websearch` → `cheap · fast`; `perplexity-reasoning` → `cheap · slow`;
    `perplexity-research` → `expensive · slow`
  - anything cache-hit → `free`
- `cost_from_usd(usd) -> Cost` for paths that only know a dollar figure
  (thresholds: `0` → free, `≤ ~$0.02` → cheap, else expensive; env-tunable).

Surfaced **uniformly** in `Next:` trailers and tool responses as words, *not*
dollar figures in the agent's face — qualitative, so a lesser model gets a feel
without obsessing. Paired with the one-line permissive policy (above) in the
guidance text so "expensive" reads as *information + permission-when-needful*,
never *forbidden*.

### Piece B — the global circuit breaker (the hard rail)

A `src/precis/budget/breaker.py` module:

- **Two windows:** a fast **hourly** meter (catches a tight loop *within* the
  day before it eats the daily cap) and the primary **24h** meter. Each is a
  `SUM(cost_usd)` over the union of the ledger sources (`llm_call_log` +
  `cache_state` fetches in the window; `ref_events` is the agentic subset of
  `llm_call_log`, so pick one to avoid double count — see Open questions).
  Cheap, ts-indexed.
- **Two numbers:** `hourly_cap_usd` and `daily_cap_usd`. Both bound
  *everything with a runtime cost together* — the router LLMs (claude,
  OpenRouter/OpenAI-compatible) **and** the paid fetch kinds (perplexity,
  future) contribute to the same `$X`.
- **State:** `ok` (< cap) / `tripped` (≥ either cap).
- **On trip:** refuse *new expensive* dispatches + paid fetches; **cheap /
  free / interactive still flow**. **Auto-clears** as the rolling window ages
  the spend back under the cap — no manual reset. Emit one `alert` (the
  `alert` kind) on each trip; the alert→news pipeline surfaces it to **Discord**
  (all alerts currently route to the news channel — fine for now).

**Chokepoints** (both already funnel all spend):

- `router.dispatch` — gate before provider `.run()`. It already returns a
  normalized `LlmResult` with an `error` field, so a trip is a graceful
  `error="budget: daily cap $X reached"` result, not an exception. Only gate
  `expensive`-band tiers; cheap/free pass.
- Cache-backed `_fetch` (paid kinds) — check before the HTTP POST; on trip
  raise the existing `Upstream`-style clean error pointing at the cap, or fall
  back to `[cost: free]` cached data if present.

**Config — env default + web override:**

- Env floors/defaults: `PRECIS_BUDGET_DAILY_USD` (e.g. `$20`) +
  `PRECIS_BUDGET_HOURLY_USD` (e.g. `$5`) — safe defaults chosen so normal
  operation never trips.
- Web-editable runtime override so the caps change without a redeploy. Mirror
  the `/secrets` vault-editor precedent: a `/budget` page showing current
  hourly + 24h spend against each cap, with a form to set them. The exact
  persistence store (a one-row `app_settings` table vs the existing settings
  mechanism) is a **build-time detail, resolved when Piece B is built** — the
  design is identical either way. The DB value overrides the env default; env
  is the boot floor.

### Piece C — quest attribution (later, free hook)

Do **not** build per-quest budgets now. Just ensure the `source` field on
`LlmRequest` (and the paid-fetch path) can carry a quest id, so per-quest
totals become a query over the *same* ledger when the quest layer lands.

**Key simplification:** a *global* breaker (Piece B) needs **zero attribution
math** — it sidesteps the quest-overlap double-counting problem that
`docs/proposals/quest-layer.md` flags as its sharpest open question (#1: a sim
serving two quests burned one sum of GPU). Start global; add per-quest *views*
(not caps) for free later; only add per-quest *caps* if a real need appears.

## The website tote

The rolling tote rides the **existing** status-page rollup
(`status.py:_claude_usage`, already a 24h/7d spend-by-model query) — extended
to add `cache_state.cost_usd` so it's *whole* runtime spend, not just claude,
and shown against the caps as a bar (green under / amber near / red tripped).
Breakdowns, so it's diagnosable at a glance:

- **by model** — which model is burning the budget (opus vs haiku vs OSS).
- **by source/kind** — which subsystem (`dream`, `plan_tick`, `chase:verify`,
  `perplexity-research`, …) is the spender; `llm_call_log.source` +
  `cache_state.provider` already carry this.

Both windows (hourly + 24h) surface, so a spike is visible before it trips.

## Behavior summary

| Situation                         | free/cheap | expensive |
| --------------------------------- | ---------- | --------- |
| Under caps                        | ✅ flows   | ✅ flows  |
| Over hourly or daily cap (tripped)| ✅ flows   | ⛔ refused (graceful) |
| Interactive user (any tier)       | ✅ flows   | ✅ flows (never blocked) |

Autonomous loops (dream, plan_tick, future quest loop) are the ones that back
off; a human at the keyboard is never stopped by the breaker.

## Out of scope (deliberately)

- Per-task / per-quest hard budgets (the quest "tote" — later, views first).
- Token-level accounting (dollars are the honest unit; tokens are a proxy).
- Rigid weekly proportional allocation across quests (quest-layer slice 4).
- Blocking interactive user work. The breaker only ever throttles autonomy.

## Slice ladder

1. **Affordance (Piece A)** — the `free/cheap/expensive · fast/slow` bands +
   uniform surfacing + the permissive policy line. No enforcement. Pure sense.
2. **Real-cost capture + read-only meter** — make the OSS/local + perplexity
   paths log their *actual* returned cost (carry `usage` through
   `result_from_openai` + a per-model price table; read perplexity's response
   `usage`). Then the hourly + 24h `SUM(cost_usd)` query + the tote on the
   status page (spend vs display-only caps, breakdowns by model + source).
   Watch real numbers before enforcing.
3. **The breaker (Piece B)** — wire the gate into `router.dispatch` + paid
   `_fetch`; web-editable cap; the `alert` on trip. The hard rail goes live.
4. **Quest attribution (Piece C)** — `source`-carries-quest hook + per-quest
   spend *views*. Only when the quest layer lands.

## Open questions

**Resolved in discussion (2026-07-15):** authoritative cost = the provider's
returned number (not the per-call caps, not the ClassVar estimates); hourly +
24h windows both; auto-clear on window roll-off (no manual reset); trip alerts
go to Discord via the existing alert→news channel; the tote shows on the status
page with by-model + by-source breakdowns; cap persistence is a build-time
detail, not a design fork.

**Remaining:**

1. **Ledger union without double-count.** `ref_events` agentic costs and
   `llm_call_log` router costs overlap (the agent path logs both). Confirm the
   SUM's single source of truth so a call isn't counted twice. Likely:
   `llm_call_log` for router spend, `cache_state` for paid fetches,
   `ref_events` excluded from the SUM (it's a per-ref view of the same claude
   calls).
2. **Per-model price table — source & upkeep.** The tokens → $ conversion for
   the OSS/local (and OpenRouter, if not reading its `cost` field) paths needs
   a `{model: ($/1M in, $/1M out)}` table. Where does it live and how is it
   kept current as prices drift? (Lean: a small checked-in constant with an
   env override; OpenRouter's returned `cost` preferred when present, so the
   table only covers models that don't report cost.)
3. **Cheap-band threshold for `cost_from_usd`.** `~$0.02` is a guess; tune once
   the read-only meter shows the real distribution.
4. **Cap defaults.** `$20/day` + `$5/hr` are placeholders — set the real
   numbers from a week of observed spend on the read-only meter (slice 2)
   before the breaker (slice 3) goes live.

**Resolved (2026-07-16) — the OAuth-vs-money split (the "right" gate).**
The first live run exposed a category error: in this deployment *every* model
call is `claude-opus-4-8` over the `claude -p` **OAuth subscription**
(`claude_agent`/`claude_p` transports), so the `total_cost_usd` the CLI reports
is an API-list-price **notional** figure — those calls don't spend money, they
draw down the account's rate-limit **quota** (five_hour / seven_day …). Gating
that lane on a dollar cap paused ~$77/day of useful dream/review work against a
$20 cap while the subscription sat at `five_hour: allowed`. The fix splits the
breaker by what a call actually spends (`budget.breaker.gate_tier(transport=…)`):

* **Real money** — OpenRouter/OpenAI-compat + paid fetches → the dollar meter,
  which now **excludes** `meter.OAUTH_TRANSPORTS` from its `llm_call_log` sum
  (open question #1 sharpened: notional rows are out of the money SUM).
* **Claude subscription** — gated on the quota snapshot
  (`budget.quota.evaluate`, reading `claude_quota_snapshot`): pause only when a
  window is `rejected` (or ≥ `PRECIS_QUOTA_CEILING_PCT`, default 100, when the
  CLI reports `used_percentage`), auto-clearing on window reset. Its own
  `budget:quota` critical alert mirrors the dollar one.

A web **resume override** (`/budget` → "Resume paid work now",
`budget.resume_until` in `app_settings`) lifts a *soft* trip (dollar cap or the
quota ceiling) for N hours so the operator can unstick the factory; a genuine
Anthropic rejection still 429s at the provider. This makes cap-default tuning
(#4) matter only for the real-money lane; the claude lane self-bounds on quota.
