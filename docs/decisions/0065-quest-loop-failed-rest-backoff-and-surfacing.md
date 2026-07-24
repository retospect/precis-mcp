# 0065 — A quest loop that rested on *failure* must back off and surface, not re-mint blind

- **Status**: proposed (2026-07-24). Closes the **RC1** residual named across
  the RC1/RC2 rest-reason thread and in
  [0064](./0064-dossier-thinking-substrate-and-paper-projection.md) ("the RC1
  residual … A is the RC1 *root-cause* fix; this is the *re-mint-guard* fix, one
  level up"). RC2 (self-rest when the quest goes non-active) and the
  reboot-orphan reap already shipped; this is the last of the four rest-reasons
  left unhandled.
- **Deciders**: Reto + agent
- **Builds on**:
  - the `quest` coordinator loop — one perpetual `quest_tick` coordinator job
    per active quest (`workers/job_types/quest_tick.py`), reconciled every
    worker pass by `reconcile_quest_loops` (`quest/loop.py`): mint one live loop
    per active quest, re-arm a rested one once its predecessor is terminal.
  - the reboot-orphan reap (`_reap_orphaned_loop`, shipped `8815d396`) — the
    *first* rest-reason distinction: a provably-orphaned slice is terminalized
    to `cancelled` (**not** `failed`) precisely so it never collides with this
    failed-loop question.
  - the nursery detector pattern (`workers/nursery.py`) — SQL-only, per-condition
    `kind='alert'`, dedup + auto-resolve; `plan-tick-spin` is the direct template
    for the surfacing prong here.

## Context

A coordinator loop can reach a terminal (`Done`) state for **four distinct
reasons**, and the reconciler re-mints an active quest's loop keying only on
*terminality* — it cannot see *why* the loop rested:

| Rest reason | How it terminalizes | Correct re-mint policy | Status before this ADR |
|---|---|---|---|
| **reboot-orphan** — slice died mid-run, lease expired past grace | `_reap_orphaned_loop` → `STATUS:cancelled` | re-mint **now** (want a fresh loop) | ✅ shipped `8815d396` |
| **quest went non-active** (RC2) | `_dispatch` self-rest → `Done(success=True)` → `STATUS:succeeded` | **don't** re-mint (quest drops out of `active_quest_ids`) | ✅ shipped `dae2ccc7` |
| **dry / punt exhaustion** | `Done(success=True)` → `STATUS:succeeded` | re-mint **now** (re-arm to pick up newly-landed evidence) | ✅ correct by construction |
| **genuine failure** — `_max_tick_failures` budget, or an uncaught slice crash | `Done(success=False)` / runner catch → `STATUS:failed` | ??? | ❌ **RC1 — re-minted now, unbounded, invisible** |

The failed case is the bug. `_max_tick_failures` (default 5) only rests the loop
after **5 consecutive hard failures** — transient `paused` states (breaker /
quota) don't count, so reaching this budget already means a *persistent* break
(a bad config, a dead endpoint, a code error the tick keeps hitting). Yet the
reconciler re-mints it on the very next worker pass, the fresh loop hits the
same break, burns another 5 failed slices (~25 min at the 300 s heartbeat
backoff), rests `failed`, and is re-minted again — **forever**. Two harms:

1. **Unbounded** — a permanently-broken quest re-mints indefinitely, each cycle
   spending 5 local-LLM/compute slices on a break that re-minting cannot fix.
2. **Nursery-invisible** — nothing surfaces the churn. The nursery watches
   `worker-restart` / `dead-worker` / `dispatch-stall` and `plan-tick-spin`, but
   there is no detector for a quest loop that keeps failing, so the operator
   never learns the quest is stuck.

The terminal `STATUS` already partitions the four reasons cleanly (`cancelled` /
`succeeded` / `failed`) — the reconciler simply never reads it. That is the whole
opening: no new marker is needed on the happy path; the fix is to *look*.

## Decision

**The reconciler distinguishes the failed rest-reason and treats it
differently: escalating backoff (bounded churn) + a nursery detector (make it
visible).** Both prongs key on the one fact the coordinator already records —
the terminal `STATUS` of the `quest_tick:<id>` loop job.

### Prong 1 — escalating backoff before re-mint (bounded)

Before `ensure_quest_loop`, `reconcile_quest_loops` consults a new gate
`_failed_rest_cooldown_active(store, quest_id)`. In one indexed query it reads
the quest's **most-recent terminal** `quest_tick:<id>` loop and, when that loop
is `STATUS:failed`:

- **when** it failed (the `STATUS:failed` tag's `created_at`), and
- **how many** consecutive failed rests precede it — the trailing run of
  `failed` terminal loops, counted most-recent-first until the first non-`failed`
  terminal. **The job history *is* the counter** — nothing to stamp or keep in
  sync.

The cooldown is exponential in that count `n`, capped:

```
cooldown(n) = min(BASE · 2^(n-1), CEILING)
   BASE     = PRECIS_QUEST_LOOP_FAIL_BACKOFF_S      (default 1800 = 30 min)
   CEILING  = PRECIS_QUEST_LOOP_FAIL_BACKOFF_MAX_S  (default 21600 = 6 h)
```

→ 30 min, 1 h, 2 h, 4 h, 6 h, 6 h, … If `now < failed_at + cooldown(n)`, the
pass **skips the mint** (tallied `backoff`); otherwise it mints as before. A
`cancelled` (reboot) or `succeeded` (dry/punt/RC2) most-recent terminal is *not*
a failed rest → no cooldown → immediate re-mint, exactly as today.

This converts the tight "re-mint every ~25 min forever" into a self-spacing
retry that **self-heals a transient outage** (spark down 3 h → one or two failed
rests, then recovers on the next uncool re-mint) while **throttling a permanent
break toward the 6 h ceiling**. A re-mint that finally *succeeds* resets the
trailing-failed count to 0 by construction, so the next terminal is `succeeded`
and cooldown vanishes.

### Prong 2 — a `quest-loop-failing` nursery detector (visible)

Mirror `plan-tick-spin`: one SQL detector counting **terminal `STATUS:failed`
`quest_tick` loops per quest in the last 24 h**; more than
`QUEST_LOOP_FAIL_24H` (default 3) raises a `warn` alert
(`nursery:quest-loop-failing`, deduped per quest, auto-resolving when the quest
stops failing). `warn`, not `critical`: a stuck quest loop burns local
compute but is not a cluster-wide outage (unlike the worker-health trio). The
alert body names the quest, the failure count, and where to look
(the loop's `job_summary` / `job_event` chunks), so triage starts at the cause.

Backoff caps the *rate*; the alert ensures a genuinely-broken quest doesn't burn
at the ceiling *invisibly* — a human sees it and fixes the config or abandons
the quest.

## Alternatives considered

- **Leave a failed-rested loop dead until a human revives it (no re-mint).**
  Rejected — brittle. A *long transient* outage (spark down for hours; every
  tick fails; loop rests `failed`) would then need manual revival even after the
  cause clears. The quest is still active; we want it to retry — just slower and
  visibly. Escalating backoff gives transient outages a free self-heal and
  reserves human attention for the persistent case (via the alert).
- **Classify the failure (transient LLM 502 vs persistent config break) and only
  back off the persistent ones.** Rejected as unnecessary complexity. Reaching
  `_max_tick_failures` already filters out `paused` (breaker/quota) states, so a
  `failed` rest is *already* ≥5 consecutive hard failures — persistent by
  construction. Escalation handles the duration difference (a 3 h outage costs
  one or two cheap cooldowns; a permanent break climbs to the ceiling) without
  the loop having to name the cause.
- **Stamp a `meta.loop.cooldown_until` (and a failure counter) on the quest.**
  Rejected — a writer to keep in sync and a second source of truth to drift.
  Deriving both the timestamp and the escalation count from the loop job history
  is stateless and race-free, and the four-way `STATUS` partition means the
  happy path needs no new marker at all.
- **A fixed (non-escalating) cooldown.** Rejected as strictly worse for free: a
  fixed 30 min still re-mints a permanently-broken quest 48×/day. Escalation is
  derived from a count the gate already computes, so it costs nothing and makes
  a dead quest cheap.

## Consequences

- **Positive**: a genuinely-broken quest retries at a 30 min → 6 h self-spacing
  cadence instead of every ~25 min, and raises a standing `/alerts` warning after
  3 failures/24 h — the churn is both bounded and visible. The reconciler now
  reads the terminal `STATUS` it always had, so the four rest-reasons are finally
  fully distinguished. `reconcile_quest_loops` gains a `backoff` tally in its
  summary dict.
- **Negative**: the reconciler runs one extra indexed query per active quest per
  pass (active quests are a handful; the query is btree-keyed on `meta->>'idem_key'`
  + the STATUS tag join the reap already uses). Two new env dials
  (`PRECIS_QUEST_LOOP_FAIL_BACKOFF_S` / `_MAX_S`) and one nursery threshold knob.
- **Neutral**: **no migration** — pure read of existing job `STATUS` tags + a new
  SQL detector. Extends the reap's rest-reason logic; the surfacing prong is a
  routine nursery detector addition.

## See also

- `src/precis/quest/loop.py` — `_reap_orphaned_loop` (the sibling rest-reason
  distinction) and `reconcile_quest_loops` (where the backoff gate lands).
- `src/precis/workers/job_types/quest_tick.py` — `_max_tick_failures` (the
  budget whose `Done(success=False)` this ADR governs the re-mint of).
- `src/precis/workers/nursery.py` — `_detect_plan_tick_spins` (the template for
  the `quest-loop-failing` detector).
- [0064](./0064-dossier-thinking-substrate-and-paper-projection.md) — §A is the
  RC1 *root-cause* fix (a dry-tick spin from a lost ledger); this ADR is the
  *re-mint-guard* fix one level up, for a loop that rested on real failure.
