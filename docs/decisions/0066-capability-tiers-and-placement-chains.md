# 0066 — Capability tiers + operator-owned placement chains

- **Status**: accepted (2026-07-25)
- **Deciders**: Reto + agent
- **Builds on / supersedes**:
  - [ADR 0046 — the LLM routing layer](./0046-llm-routing-layer.md) — this
    ADR **replaces 0046's five-tier `Tier` enum** with four pure-capability
    tiers and moves placement off the tier. The seam (`resolve_model` /
    `select_transport` / `dispatch` / `FailoverProvider`) stays; only the
    tier vocabulary and the placement mechanism change.
  - `docs/proposals/glm-fleet-flip-safety.md` (Phase 1, landed main
    `2905d567`) — **partly superseded**: its *global* `llm.backend` flag +
    `live_config.GLM_OPENROUTER_PRESET` roster is replaced by per-tier
    chains (§Decision 3). Its call-site fixes (backend-aware `resolve_model`,
    `_hosted_small_remap`, `result_from_openai` cost capture, the
    `fix_gripe`/`claude_docker` subprocess gates) are **foundational and
    carry forward**.
  - `docs/proposals/llm-openrouter-bypass.md` (the DECIDED 2026-07-23
    cascade roster) — this ADR is the enum/config formalization of that
    per-tier local→OpenRouter→Anthropic cascade.

## Context

**Tiers are location-coupled.** `router.py::Tier` names *where* a model
runs, not just *what it can do*: `LOCAL_SMALL` / `LOCAL_BIG` / `CLOUD_SMALL`
/ `CLOUD_MID` / `CLOUD_SUPER`. A caller literally picks cloud-vs-local — the
`LLM:local` vs `LLM:opus` todo tag routes through `PLANNER_TIER_BY_ALIAS`
(`local → LOCAL_BIG`, `opus → CLOUD_SUPER`), and ~23 `LlmRequest` /
`DispatchClient` sites hard-code `tier=Tier.CLOUD_*` / `LOCAL_*`.

That coupling is the root cause of the fleet-flip breakage (gripe 171782): a
"local" pick got dragged onto a cloud endpoint, and a "cloud" pick sent a
GLM slug to a `claude` transport. Placement leaked into the caller's
vocabulary.

**Reto's framing.** *"As a user I don't care whether the model is local or
cloud — I want the small or the frontier model. Budgeting placement right is
the system operator's problem. Big job → big model, small job → small
model."* The caller picks a **capability**; the operator owns **placement**.

## Decision

Collapse to **four pure-capability tiers**, and make placement a per-tier
**operator-owned ordered backend chain** the dispatcher walks on
failure / throttle / budget-exhaustion.

### 1. `Tier` → four capability rungs (strongest → weakest)

`class Tier(StrEnum)` becomes `FRONTIER` / `BIG` / `MEDIUM` / `SMALL`. A
tier names capability only; no rung encodes a location.

| Tier | Capability | Today's analogue |
|---|---|---|
| `FRONTIER` | heavy reasoning + tools; the trusted-answer tier | opus-4.8 (`CLOUD_SUPER`) |
| `BIG` | general agentic workhorse — planner, tex-fix, weave | sonnet-5 (`CLOUD_MID`) / qwen-heavy (`LOCAL_BIG`) |
| `MEDIUM` | one-shot JSON judge / cheap triage | haiku (`CLOUD_SMALL`) |
| `SMALL` | categorizer / classifier — per-chunk gloss, inject-scan | summarizer (`LOCAL_SMALL`) |

`FRONTIER` = Claude Opus 4.8, **cloud-only** — Opus has no local mirror on
the Mac cluster, so its chain never has a local rung (§3, an asymmetry to
keep in mind).

### 2. Two menus — the hard acceptance criterion

Placement must be **invisible to callers**. Two distinct surfaces:

- **Caller-facing picker** — the `LLM:` todo-tag vocab, a service's tier
  pin — offers **only the four capability rungs**. No `cloud-*` / `local-*`
  name is ever a caller choice.
- **Operator-facing config** — a Phase-2 chain editor on `/status` + the
  Models tab — **owns placement**: which cloud model, which local mirror,
  failover order, cost, the throttle. Invisible to callers.

### 3. Placement = a per-tier ordered backend chain

Each tier resolves to a list of rungs `[(placement, model_id, transport),
…]` that `dispatch` walks on failure / throttle / budget-exhaustion.
Illustrative (final picks are Phase 3, per the DECIDED roster):

    frontier = [cloud/claude-opus-4-8]                    # no local mirror
    big      = [cloud/glm-5.2 → local/<qwen-heavy>]       # failover + soft-switch
    medium   = [local/glm-flash → cloud/glm-flash]        # local-first
    small    = [local/<cheap classifier> → hosted-small]  # qwen-80B is OVERKILL here

Chains live in `app_settings` (live-switchable, ~15s TTL via
`live_config`). This **replaces** the single global `llm.backend` flag +
`live_config.GLM_OPENROUTER_PRESET` — a global backend switch is exactly the
"drag a local pick onto cloud" bug; per-tier chains scope placement to the
tier.

### 4. Reuse the existing failover mechanism

`router.py::_failover_ladder` already builds a rung list `FailoverProvider`
walks. **Extend it** from its fixed 2-rung shape (`Rung(primary)` +
`Rung(claude-fallback)`) to the configured per-tier chain. `Rung`,
`FailoverProvider`, and the `paused`/`interrupted` skip-not-fail semantics
are unchanged — the chain is just a longer, config-sourced rung list. No
parallel mechanism.

### 5. The throttle dial — and chains are *within-tier only*

A new `llm.cloud_enabled = false` app_setting (and/or a tripped budget
breaker) forces every chain to **skip its cloud rungs** → drop to local.

**Decided (Reto, 2026-07-25): chains are within-tier only; a tier never
silently falls through to a *different* capability.** So:

- `BIG` / `MEDIUM` / `SMALL` drop to their local rung and keep flowing.
- `FRONTIER` (cloud-only — no local rung) **`paused`s**: the call queues via
  the existing skip-not-fail breaker semantics and resumes when cloud is
  re-enabled / budget clears. It is **never** silently degraded to a local
  `BIG` model. The contract is honest: a caller that asked for `FRONTIER`
  gets Opus or waits, never a quietly-worse answer.

This kills cross-tier fall-through as a mechanism (Open Q2): a chain is a
tier's *placement* options, not a capability ladder. (A caller that *wants*
"best-effort, downgrade if needed" expresses it by asking for the lower tier,
not by relying on frontier degradation.)

**One carve-out (ready-review advisory a):** the frontier-`paused` above is
*transient* — it clears when cloud returns / budget resets. It is distinct
from a *permanent* block on frontier's only rung: **local-only content at
`FRONTIER`** (the §6 constraint prunes frontier's sole cloud rung, leaving no
rung at all). That is not an infinite pause but an **unsatisfiable request** —
rejected at request-assembly time, with the caller directed to a
locally-servable tier. The constraint layer owns that rejection (sensitivity
proposal §5), not the throttle path; §5's "never silently degrades" applies to
the transient case.

### 5a. Failure & congestion semantics — "wait until available", uniformly

The chain must make the intuitive model true: **a todo that can't run right
now waits and retries; it does not spin, and it does not park.** Three rules:

- **Congestion → `paused` (skip-not-fail).** A full local slot
  (`local_serving.acquire()` → `paused`), a tripped budget breaker, or a
  throttled cloud rung returns the existing `paused` `LlmResult` — "the call
  simply didn't run." The todo stays claimable and is retried next worker
  cycle; no spin, no failure. This is already how slots + the breaker behave;
  the chain preserves it.
- **Hang → timeout → rung failure → failover.** Every rung MUST carry a
  wall-clock timeout so a hung backend converts to a rung *failure* the
  `FailoverProvider` walks past to the next rung — never a wedged worker.
  Today `claude_agent` (600 s, `PRECIS_CLAUDE_AGENT_TIMEOUT_S`) and
  `openai_tools` (120 s) have one; **Phase A must confirm the
  `litellm`/`openai_compat` rung (`LlmClient.complete`) carries a request
  timeout too** — the tool-less / `SMALL` path — or a hang there won't fail
  over. (A 600 s claude timeout means a hang costs up to ~10 min of
  wall-clock before failover — acceptable, not free.)
- **Exhaustion-by-unavailability → `paused`, never `failed`.** When *every*
  rung is unavailable (all paused, all timing out, or transport/availability
  errors — a real outage), the dispatch result resolves to **`paused`
  (back off + retry)**, NOT `failed`. Only a genuine *semantic* error (a
  malformed request, a 4xx on a well-formed call) is a real failure. This is
  a **behavior change to close a gap**: today an exhausted chain can bubble a
  job failure that *parks* the todo (the `spend_limit_parks_todos` pattern —
  a `child-failed:` tag permanently parks user work until un-parked). Under
  the new rule "todos wait until available" holds for an outage, not only for
  congestion. Phase A implements the paused-not-parked exhaustion outcome.

### 6. Local-only is an orthogonal *constraint*, never a tier

Decoupling placement from capability removes the one thing `LLM:local` did
that wasn't about capability: **force a job to stay off cloud APIs**
(`backlog_proprietary_local_only` — proprietary content must never reach a
cloud provider). That requirement doesn't disappear; it moves to an
**orthogonal constraint** that prunes cloud rungs from *whatever* tier's
chain a job uses:

    job: LLM:big + <local-only>
      big chain   = [cloud/glm-5.2 → local/qwen]
      constraint prunes cloud rungs ⇒ effective = [local/qwen]

Capability stays the tier; "must not leave the cluster" is an independent
flag/tag. This is the load-bearing invariant that keeps decoupling from
*reintroducing* the privacy leak it was meant to prevent.

**Decided (Reto, 2026-07-25): adopt the orthogonal constraint — but its full
shape is a complex design in its own right and is carved out to
[`docs/proposals/content-sensitivity-placement.md`](../proposals/content-sensitivity-placement.md).**
It is not a binary flag: it needs a notion of *how secret* content is (a
sensitivity level), how that level is assigned and how it **propagates to
derived artifacts** (a chunk / summary / card minted from proprietary source
inherits the constraint), and how the constraint prunes rungs + is audited.
This ADR commits to the *mechanism boundary* (capability = tier, sensitivity
= orthogonal constraint on placement); the sensitivity model itself lands via
that proposal before the constraint is wired. **A tier migration must not go
live until the local-only constraint exists** — otherwise a `LLM:local` job
silently gains a cloud rung (see Rollout gate).

### 7. Relationship to Phase 1

Phase 1 hardened the *global* flag; Phase 2 (this ADR) **supersedes that
mechanism** with per-tier chains. Mark `glm-fleet-flip-safety.md` partly
superseded. The Phase-1 fixes carry forward unchanged.

## The 5 → 4 migration mapping

The decision, with the judgment calls flagged. Verified against each tier's
`Tier` docstring, `_TIER_MODEL`, `PLANNER_TIER_BY_ALIAS`, `budget/bands.py`,
and actual call-site usage.

| Old tier (`.value`) | model today | → New tier | Rung role | Judgment call |
|---|---|---|---|---|
| `CLOUD_SUPER` (`cloud-super`) | opus-4.8 | **`FRONTIER`** | the (only) cloud rung | clean — the heavy-reasoning tier |
| `CLOUD_MID` (`cloud-mid`) | sonnet-5 | **`BIG`** | cloud rung of BIG's chain | clean — the general-agentic workhorse |
| `LOCAL_BIG` (`local-big`) | qwen-heavy+tools | **`BIG`** (local rung) | **folds into BIG**, not its own tier | ⚠ see below |
| `CLOUD_SMALL` (`cloud-small`) | haiku | **`MEDIUM`** | cloud rung of MEDIUM's chain | ⚠ naming collision w/ roster — see Open Q1 |
| `LOCAL_SMALL` (`local-small`) | summarizer | **`SMALL`** | local rung of SMALL's chain | clean — the categorizer |

**`LOCAL_BIG` folds into `BIG` (the load-bearing judgment call).** `BIG`
(sonnet) and `LOCAL_BIG` (qwen-heavy) do the **same work class** — general
agentic. `PLANNER_TIER_BY_ALIAS` already treats them as interchangeable
placements of one duty (`sonnet → CLOUD_MID`, `local → LOCAL_BIG`, both the
"planner tick" rung), and the DECIDED roster's *medium* row lists qwen-80b
(local) / glm-4.7 (OpenRouter) / sonnet (Anthropic) as three placements of
**one** tier. So under location-decoupling they are one capability rung with
two placements — exactly what a chain expresses. This is the whole point of
the collapse: `LOCAL_BIG` was never a capability, only a placement.

**Capability gradient holds.** `FRONTIER > BIG > MEDIUM > SMALL` maps to
`opus > sonnet > haiku > summarizer` — a clean monotone ordering, so
`CLOUD_MID → BIG` above `CLOUD_SMALL → MEDIUM` preserves sonnet-above-haiku
(Open Q1 covers the one naming wrinkle).

## Call-site inventory (the migration scope)

~23 `LlmRequest` + ~18 `DispatchClient` sites plus the tier-resolving
helpers, grouped by the old tier they pin. Every `tier=Tier.X` reference
below flips to its new-tier column above; a `DispatchClient(tier=Tier(str))`
site that reads a string tier (`quest_tick`, `email`) needs the string
vocab migrated too.

**`CLOUD_SUPER → FRONTIER`** (heavy reasoning / trusted answers):
`workers/structural.py`, `workers/deep_review.py`, `workers/dream_agent.py`
(+ its `_DEFAULT_MODEL`), `workers/briefing.py`,
`workers/job_types/fix_gripe.py`, `.../cad_propose.py`, `.../cad_discuss.py`,
`.../sandbox_run.py`, `diagram/agent.py`, `figure/turn.py`, `mermaid/turn.py`,
`reading/briefing_cast.py`, `reading/cards.py`, `reading/meditation.py`,
`utils/claude_agent.py` (the generic `claude_agent` default),
`precis_web/ask.py`, `asa_bot/claude_invoke.py`, `quest/review_fanout.py`
(the `LLM:opus` persona route).

**`CLOUD_MID → BIG`** (general agentic):
`utils/tex_llm_fix.py`, `workers/job_types/structure_propose.py`,
`asa_slack/bot.py`, `workers/job_types/quest_tick.py` (default `"cloud-mid"`
weave), plan_tick's `LLM:sonnet` path (`PLANNER_TIER_BY_ALIAS`).

**`CLOUD_SMALL → MEDIUM`** (one-shot judge / triage):
`workers/_chase_llm.py` (verify / disambiguate / locate),
`workers/job_types/good_search.py`, `anki/fix.py`, `quest/tick.py`,
`utils/llm/requirement.py` (`_JUDGE_TIER`).

**`LOCAL_SMALL → SMALL`** (classifier / categorizer):
`cli/classify.py`, `cli/email.py` (`inject_scan`), `cli/worker.py`
(summarize / classify / paper-glossary / chunk-tag / inject clients).

**`LOCAL_BIG → BIG` (local rung)**: plan_tick's `LLM:local` path,
`quest_tick`/`quest/loop.py` default `"local-big"`.

**Tier-parametric (all tiers):** `llm_eval/harness.py` builds `LlmRequest`s
across every tier (the eval loop, Phase 3) — not one old-tier group; it
follows the new enum wholesale (ready-review advisory b).

**Tier-shaped helper tables (rewrite from 5 rows to 4):**

| Site | What it holds | Change |
|---|---|---|
| `router.py::_TIER_MODEL` | tier → (env, default) | 4 rows; chain-sourced |
| `router.py::PLANNER_TIER_BY_ALIAS` | `LLM:` alias → tier | new vocab + legacy alias map (back-compat below) |
| `router.py::_LOCAL_ESCALATION_TIER` / `_LOCAL_ONLY_MODEL_ALIASES` | local-tier failover bridge | folds into the chain resolver |
| `budget/bands.py::_TIER_BANDS` | tier → (Cost, Pace) | 4 rows |
| `workers/job_types/plan_tick.py::_TIER_BY_ALIAS` | re-export of the alias map | follows router |
| `handlers/_todo_guards.py::_LLM_TAG_VALUES` | `LLM:` closed vocab | single-sourced from the new aliases |
| `store/types.py` (~790) | literal mirror of `PLANNER_MODEL_ALIASES` | follows router |

**Surfaces that render / edit tiers:**

| Surface | Reads | Change |
|---|---|---|
| `precis_web/routes/factory.py::_llm_models`, `set_llm_backend`, `_apply_glm_preset`/`_revert_glm_preset` | `GLM_OPENROUTER_PRESET` (keyed `CLOUD_SUPER/MID/SMALL`) | retire the global-flip preset; the chain editor replaces it |
| `status.py::_llm_override_ctx` | iterates `(CLOUD_SUPER, CLOUD_MID, CLOUD_SMALL)`, reads the GLM preset | becomes the per-tier chain panel |
| `status.py::_services_ctx` / `_models_ctx` / `_llm_card_view` | `tier_floor`, `_TIER_RANK` (`cloud-super/mid/small`), `is_cloud = tier.startswith("cloud")` | new 4-tier rank; `is_cloud` derives from the rung's placement, not the tier name |
| templates `_status_services.html.j2`, `_status_models.html.j2` | the above contexts | re-render for 4 tiers + chains |
| `precis_web/deps.py` (~98) / `drafts.py` (~115) | `planner_model_choices` / `PLANNER_MODEL_ALIASES` — the `LLM:` dropdown | new vocab |
| `cli/llm.py` | `--tier` defaults `cloud-super` / `cloud-small`; `Tier(args.tier)` | new defaults / values |
| `cli/quest.py` | `--tier` default `cloud-mid`, help text | new default / help |
| `llm_catalog.py` | `_SEED_PROSE` keys, `TIER_FLOOR_MODELS` rows, tier-anchor cards (all keyed on the 5 old `.value`s) | reseed to 4 tiers |
| `utils/llm/policy.py`, `requirement.py` | `tier_floor: Tier` | typed by the new enum |

**`llm` catalog cards** carry `meta.tier_floor = cloud-super | cloud-mid |
cloud-small | local-small | local-big`. A **forward migration** must relabel
these (`cloud-super → frontier`, `cloud-mid → big`, `cloud-small → medium`,
`local-small → small`, `local-big → big`). *Noted, not written here* (ADR
0005 forward-only discipline).

## Back-compat

- **Existing `LLM:opus|sonnet|haiku|local` todo tags.** The `LLM:` vocab is
  single-sourced from `PLANNER_MODEL_ALIASES`. Keep `{opus, sonnet, haiku,
  local}` as **legacy aliases** mapping into the new tiers (`opus →
  FRONTIER`, `sonnet → BIG`, `haiku → MEDIUM`, `local → BIG`) alongside the
  new caller vocab (`{frontier, big, medium, small}`), so existing todos
  keep dispatching with **no `ref_tag` rewrite**. A cleanup backfill can
  relabel later; it is not required for correctness. **`local → BIG`
  collapses two old aliases** (`sonnet` and `local`) onto one tier — by
  design; placement is now the chain's job (see Open Q5 for the
  proprietary-local wrinkle).
- **`llm` card `tier_floor`.** The forward migration above; until it runs,
  `status.py`'s renderers must tolerate both old and new values.
- **`app_settings` override rows.** The GLM preset writes `llm.model.<tier>`
  keyed on the old `.value`s. The chain schema replaces these keys; any live
  override rows migrate (likely none in prod steady-state — the flip was
  reverted, gripe 171782).

## Migration / rollout (phased)

- **Phase A — enum + chain resolver, behavior-preserving.** Rename `Tier` to
  the four rungs (keep the old `.value`s reachable as a compat shim during
  the sweep). Add the per-tier chain shape to `app_settings` and extend
  `_failover_ladder` to read it.
  **Byte-for-byte means: the compat shim keeps the FIVE old identities on
  FIVE distinct single-rung chains — it does NOT collapse `CLOUD_MID` and
  `LOCAL_BIG` onto one `BIG` chain yet** (resolves ready-review blocker 1).
  The merge is a *call-site* event, not an *enum* event: an unswept caller
  still passing `tier=Tier.CLOUD_MID` resolves to `[cloud/sonnet]` and one
  passing `tier=Tier.LOCAL_BIG` resolves to `[local/qwen-heavy]`, exactly as
  today. A caller only adopts the merged canonical `BIG` chain when Phase C
  rewrites it — and for the `local →` alias specifically, that rewrite is
  gated on the local-only constraint (Rollout gate). Concretely: seed the
  chains keyed by old identity (`chain.cloud-super`, `chain.cloud-mid`,
  `chain.local-big`, …), and let the canonical 4-tier keys take over per
  call-site during the sweep. So with no operator edit the fleet routes
  byte-for-byte as it does now.
- **Phase B — surface migration.** Split the two menus: the caller picker
  (4 rungs) and the operator chain editor on `/status` (Services + Models
  tabs). Run the `tier_floor` forward migration. Migrate `app_settings`
  keys. Add the `llm.cloud_enabled` throttle.
- **Phase C — call-site sweep + retirement.** Flip every `tier=Tier.CLOUD_*`
  / `LOCAL_*` to its capability tier (table above). Retire
  `GLM_OPENROUTER_PRESET` + the global `llm.backend` flip. Drop the compat
  shim once the sweep is complete.

## Rollout gate (hard sequencing constraint)

**A tier migration (Phase C — the call-site sweep that collapses `LLM:local`
into `BIG`) must NOT go live until the local-only constraint (§6) exists.**
Until then, `LLM:local` is the only thing forcing a job off cloud APIs;
flipping it to `BIG` without the constraint silently grants those jobs a
cloud rung — a privacy regression. Phase A/B (enum + chains + surfaces,
behavior-preserving) may land first; the `local →` alias keeps pinning local
until the constraint ships.

## Open questions — resolutions

1. **Roster naming (was Q1) — RESOLVED.** `MEDIUM` = one-shot judge / triage
   (ex-`CLOUD_SMALL`/haiku); `SMALL` = the pure classifier below it
   (ex-`LOCAL_SMALL`, "qwen-80B is overkill"). The 2026-07-23 roster's
   3-tier "small" naming is superseded by the 4-rung decision. `SMALL`'s
   chain has a hosted rung too (Phase-1 `_hosted_small_remap` already gives
   it one under the flip); Phase 3 picks the concrete cheap classifier.
2. **Cross-tier degradation (was Q2) — RESOLVED: no.** Chains are within-tier
   only (§5). A caller wanting best-effort-with-downgrade asks for the lower
   tier explicitly.
3. **Throttle × cloud-only FRONTIER (was Q3) — RESOLVED: pause.** `FRONTIER`
   under cloud-throttle/budget `paused`s (skip-not-fail, resumes when cloud
   returns), never degrades to a local model (§5).
4. **Chain schema shape (was Q4) — RESOLVED (Phase-A detail).** One JSON
   chain per tier in `app_settings` (`llm.chain.<tier> = [{placement, model,
   transport}, …]`); local rungs **reference** `served_by` / `local_serving`
   host-scoping rather than inlining an endpoint, so host/slot config isn't
   duplicated. Finalize the exact keys in Phase A.
5. **`LLM:local` × proprietary-local-only (was Q5, the sharpest) — RESOLVED
   in principle, design carved out.** Adopt the orthogonal local-only
   constraint (§6); the full sensitivity model ("how secret is this content",
   assignment, propagation to derived artifacts, rung-pruning, audit) is a
   complex design of its own →
   [`docs/proposals/content-sensitivity-placement.md`](../proposals/content-sensitivity-placement.md),
   and gates Phase C (Rollout gate above).
6. **The roster's cross-cutting `coder` pick (was Q6) — DEFERRED.** A
   task-shaped model (kimi-k2.7-code) is a per-task pick *inside* a tier
   (`BIG`/`FRONTIER`), not a rung — belongs to the `select_offering` policy,
   out of scope here.

## Still genuinely open

- The **content-sensitivity model** itself (§6 / the carved-out proposal) —
  the one remaining design bag-of-worms, tracked separately and gating
  Phase C.

## Readiness review (ready agent, 2026-07-25)

Code-claim accuracy (§4, §1, "the migration mapping" table, and the
call-site/helper-table inventories) verified against
`src/precis/utils/llm/router.py`, `budget/bands.py`, `handlers/_todo_guards.py`,
`store/types.py`, `precis_web/routes/{status,factory}.py`, `deps.py`,
`drafts.py`, `cli/{llm,quest,classify,email}.py`, `llm_catalog.py`,
`utils/llm/{requirement,policy,tex_llm_fix}.py`, and every named call site
(`workers/{structural,deep_review,dream_agent,briefing,review}.py`,
`workers/job_types/{cad_propose,cad_discuss,sandbox_run,structure_propose,
good_search,quest_tick,plan_tick}.py`, `diagram/agent.py`, `figure/turn.py`,
`mermaid/turn.py`, `reading/{briefing_cast,cards,meditation}.py`,
`utils/claude_agent.py`, `precis_web/ask.py`, `asa_bot/claude_invoke.py`,
`asa_slack/bot.py`, `quest/{review_fanout,tick,loop}.py`,
`utils/llm/requirement.py`, `anki/fix.py`, `workers/_chase_llm.py`). All
symbols (`Tier`, `_TIER_MODEL`, `PLANNER_TIER_BY_ALIAS`, `resolve_model`,
`select_transport`, `_failover_ladder`, `Rung`, `FailoverProvider`,
`_LOCAL_ESCALATION_TIER`, `_hosted_small_model`/`_hosted_small_remap`,
`select_offering`) exist as described, and every migration-table /
call-site-inventory / helper-table / surfaces-list row checked resolved to
the claimed tier and file — the inventory is trustworthy as migration
scope. `docs/proposals/content-sensitivity-placement.md` exists (in this
worktree) and its §5 open question ("local-only content cannot run at
FRONTIER") matches the ADR's cloud-only-FRONTIER framing — no contradiction
with the carve-out.

1. **blocker** — Phase A's "seed each chain to reproduce today's placement
   … byte-for-byte" claim has a real gap for the `LOCAL_BIG → BIG` fold.
   Phase A renames `Tier` to the 4 capability rungs and "keeps the old
   `.value`s reachable as a compat shim" — but once `Tier` has only 4
   canonical members, both old `CLOUD_MID` and old `LOCAL_BIG` callers
   necessarily resolve to the *same* `Tier.BIG`, and hence the *same*
   chain, before Phase C's call-site sweep has run. If that chain is seeded
   as the illustrative `BIG = [cloud/sonnet]` (or even the 2-rung
   `[cloud/sonnet → local/qwen]`, walked rung-0-first via
   `FailoverProvider`), an unmigrated `LLM:local` caller — today routed
   directly to `LOCAL_BIG`'s local `qwen-heavy`, never touching a cloud
   transport — would in Phase A start hitting `cloud/sonnet` first. That is
   not byte-for-byte identical routing. The ADR doesn't specify whether the
   Phase-A compat shim (a) keeps 5 *distinct* single-rung chains alive
   under the old symbolic names until Phase C actually collapses them
   (preserves behavior), or (b) collapses immediately to 4 chains in Phase
   A with old symbols as pure aliases (breaks behavior for exactly the
   `LOCAL_BIG`/`CLOUD_MID` merge). A builder could reasonably do either —
   only (a) satisfies the stated acceptance criterion. Needs the compat
   shim's per-old-tier resolution behavior spelled out before Phase A is
   buildable as "behavior-preserving."

2. **advisory** — §5's "`FRONTIER` … is **never** silently degraded to a
   local `BIG` model" reads as an unconditional guarantee, but combined
   with §6's rung-pruning applied to `FRONTIER` (whose chain has no local
   rung — §1) under a *permanent* local-only constraint, pruning leaves an
   empty chain: not a transient throttle that "resumes when cloud is
   re-enabled," but a structurally permanent `paused` (queues forever,
   since the content will never become cloud-eligible). The sibling
   proposal's Open Q5 flags this exact tension ("local-only content cannot
   run at FRONTIER … caps at whatever capability runs locally," which
   sounds like the cross-tier degradation §5 rules out) but leaves it open,
   correctly gated behind Phase C. Since Phase C can't ship before that
   constraint exists (Rollout gate), this doesn't block *this* ADR's
   phases, but §5's prose should distinguish "transient throttle-pause"
   from "permanent local-only+cloud-only-tier structural conflict" rather
   than stating the "never degrades / always eventually resolves" guarantee
   without that carve-out — as written, a reader of §5 alone (without
   cross-referencing the sibling's Q5) could reasonably conclude the
   FRONTIER-pause path always eventually completes, which isn't true for
   the local-only case.

3. **advisory** — `src/precis/llm_eval/harness.py` builds `LlmRequest`s
   with a caller-supplied `tier: Tier` (generic across tiers, type-only
   import) and isn't named anywhere in the call-site inventory or the
   surfaces list. It doesn't need a "flip" itself (it's already
   tier-parametric), but its own callers (the eval CLI) do carry the
   `LLM:`/tier vocab and aren't accounted for in the Phase C sweep scope as
   written. Low risk (the harness fails loudly on an unrecognized `Tier`
   value rather than silently misrouting), but worth a one-line mention so
   the sweep's grep-scope doesn't skip it.

**Verdict inputs**: 1 blocker, 2 advisory. Split signal: none — the ADR is
a single coherent deliverable (enum + chain mechanism + phased rollout),
and the one genuinely separable piece (the content-sensitivity model) is
already correctly carved out to its own proposal with an explicit rollout
gate, not bundled here.

**Resolution (2026-07-25, all three addressed inline):**
- *Blocker* → Migration §Phase A now states the compat shim keeps the FIVE
  old identities on FIVE distinct single-rung chains; the merge is a
  per-call-site event at Phase C (gated for `local →`), not an enum event —
  so byte-for-byte holds.
- *Advisory a* → §5 gains a carve-out separating the transient throttle-pause
  from the permanent local-only+FRONTIER case (unsatisfiable request rejected
  by the constraint layer, not an infinite pause).
- *Advisory b* → `llm_eval/harness.py` added to the call-site inventory as
  tier-parametric.
