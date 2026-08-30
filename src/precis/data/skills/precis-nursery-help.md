---
id: precis-nursery-help
title: precis — nursery detector of todo-tree incoherence
summary: per-minute tree-incoherence detectors — orphans, stale claims, long waits, stuck doable, spin loops, plan-tick spins, quest-loop failures — raised as alerts
answers:
  - why did a nursery alert fire on my todo tree?
  - what does an orphan or stale-claim alert mean?
  - how do I tell if a worker is dead vs just quiet?
  - why is a leaf stuck in doable and not getting picked up?
  - what counts as a spin loop and what threshold triggers it?
applies-to: precis worker --only nursery; kind='alert' (alert-source:nursery:*)
status: active
---

# precis-nursery-help — tree-incoherence detectors → alerts

The nursery is the first of three review tiers (nursery / structural /
deep). It walks the todo tree (and the worker fleet) every pass,
surfaces local incoherence via SQL-only detectors, and raises a
`kind='alert'` per condition (see `precis-alert-help`), deduped per
*condition* rather than as a rolling digest. No LLM call. The only
proactive push is a one-shot Discord ping on a *new* `critical`
condition (a thrashing or dead worker), delivered as a `kind='message'`
to the Discord channel in `PRECIS_OPS_ALERT_TARGET` (a channel target
`discord/<guild>/<channel>`, *not* a webhook URL) — unset by default, so
the push merges dark and everything else stays pull-only.

## Detector catalogue

| Category | Triggers when | Threshold |
|---|---|---|
| `orphan` | open todo whose top-level ancestor doesn't carry `meta.rotation_root=true` | — |
| `stale-claim` | leaf carries `claimed-by:*` older than threshold | 3 h |
| `long-wait` | leaf carries `waiting-for:*` older than threshold | 7 d |
| `stuck-doable` | dispatch-candidate open leaf (`meta.executor` / `meta.llm_tier` / `OPEN:executor:*`), no claim, no doable-exclusion tag (`halt`, `waiting-for:`, `ask-user`, `child-failed:` — the shared registry), no blocker, >threshold old | 24 h |
| `stalled-recurring` | recurring's most recent spawned child has been open >1 h | 1 h floor |
| `spin-loop` | one `(ref_id, source)` emits >threshold `ref_events` in 24 h | 200 / 24 h |
| `plan-tick-spin` | a planner parent mints >threshold `plan_tick` jobs in 24 h without converging | 16 / 24 h |
| `quest-loop-failing` | a quest's `quest_tick` loop rests `STATUS:failed` >threshold times in 24 h (RC1 backoff throttles but can't fix a persistent break) | 3 / 24 h |
| `worker-restart` | a `(host, process)` emits >threshold `worker: started` boot rows in 1 h (restart storm) | 8 / 1 h · **critical** |
| `dead-worker` | the continuous per-host daemon silent >threshold while its host is alive | 10 min · **critical** |
| `dispatch-stall` | `claude_inproc` jobs `STATUS:queued` >threshold with **zero** live-lease jobs running (executor stopped claiming) | 15 min · **critical** |
| `nas-denied` | a fresh `host_heartbeat` reports the NAS unreadable (EPERM) from the heartbeat's own launchd context — every launchd/cron daemon on that host is locked out of `/opt/nas` (usually a Full Disk Access grant broken by a `brew upgrade python` cdhash change) | <5 min · **critical** |
| `host-dark` | a host's own `host_heartbeat` row is stale, bounded to hosts with recent activity — the complement of `dead-worker`'s `host_alive` gate for the case where a dead single-writer host takes its own heartbeat down with it | 10 min · **critical** |

`orphan` enforces the strategic invariant: every open todo must trace to
a `rotation_root` ancestor. `stale-claim` catches workers that died
mid-task — the claim's age is read from `ref_tags.created_at` on the
open tag row. `stalled-recurring` surfaces a collision-skip pile-up: a
spawned child stuck open will silently prevent further ticks.
`spin-loop` is the only cross-kind detector — it scans `ref_events`
rather than the todo tree, catching a background worker that
re-claims the same ref every pass (a broken retry window, a no-op
outcome that never clears the claim predicate). The detail names the
source + last event + rate so triage starts at the worker. The same
loops are also surfaced on the web Status page's "Background health"
panel for pull-style monitoring.

The three **worker-health** detectors watch daemon liveness / work
flow, not the todo graph; together with `orphaned-coordinator`,
`nas-denied`, and `host-dark` (NAS unreadable / heartbeat itself dark)
they make up the `critical` categories (a new one fires the one-shot
Discord ping). `host-dark` is the deliberate complement to `dead-worker`:
a dead single-writer host's own heartbeat goes stale right along with
it, so `dead-worker`'s gate self-suppresses (one dead host must not fan
out into an alert per daemon it ran) and `host-dark` raises exactly one
critical for that case instead. `dispatch-stall` is the planner
single-point-of-failure guard: a `plan_tick` can only *execute* on one
designated agent host, so if that executor dies, 401s, or never starts,
jobs pile up `STATUS:queued` with no failure bubble and the planner goes
silently dark — the "nothing running with a live lease" gate catches
this even when the executor never started at all. These raise
non-ref-scoped alerts (`ref_id=None` + an explicit `fingerprint_key`).

Recurring subtrees (children of a root carrying `meta.schedule`) are
exempt from the strategic invariant — they're scheduled work, not
strategic work. The Watches umbrella itself doesn't appear in any
detector.

## Where the findings land

Each finding becomes one `kind='alert'` (see `precis-alert-help`):

```
kind='alert'
title='[<category>] <headline>'
alert_source='nursery:<category>'        # e.g. nursery:spin-loop
fingerprint='<category>:<ref_id>'        # the dedup key
tags=[alert-state:open, alert-source:nursery:<category>, severity:<sev>]
meta.subject_ref_id=<the ref the alert is about>
meta.seen_count=<how many passes have seen it still open>
```

Severity: `spin-loop` / `stale-claim` / `stalled-recurring` → `warn`;
`orphan` / `long-wait` / `stuck-doable` → `info`.

Read the current open set with:

```
get(kind='alert', id='/open')
search(kind='alert', tags=['alert-source:nursery:spin-loop'])
```

…or browse the **Alerts** tab in `precis web` (`/alerts`).

## Dedup + auto-resolve

A condition is identified by `fingerprint = "<category>:<ref_id>"`.

* **Repeat sighting** of a still-open condition bumps that alert's
  `meta.seen_count` and `updated_at` — no duplicate row. This is the
  per-condition dedup that replaced the old per-digest fingerprint
  (which a churning spin-loop set defeated).
* **Cleared condition** — a finding that disappears from a detector's
  output auto-resolves its alert on the next pass (open → resolved;
  the row is kept for history, filtered out of `/open`).
* **Recurrence** raises a fresh open alert; the prior resolved one
  stays as history.

Empty findings still run the resolve sweep, so a fixed problem leaves
the open list promptly.

## Running it

The pass is in the default `precis worker` rotation alongside
`auto_check` and `schedule`. To run ad-hoc:

```
precis worker --only nursery --once
```

In production it runs hourly across the fleet.

## Surfacing

Open nursery alerts show on the **Alerts** web tab (`/alerts`,
grouped by source, severity-sorted) and feed the structural / deep
reviewers' context. The web Status page's "Background health" panel
still computes spin loops + failed passes live (independent of the
alert rows). An operator preamble can read the open set via
`get(kind='alert', id='/open')`.

## What it's NOT

* Not a structural review — leaf-level pattern matching only.
  Branches missing outcome lines, sibling contradictions, the
  decomposition budget — those are the structural tier (every 6h).
* Not a deep review — no archive moves, no prune recommendations.
  That's the weekly deep tier.
* Not a worker dispatcher — the nursery describes; asa-bot
  decides whether to act on a finding when next chatting.

## Related skills

* `precis-health-digest-help` — the slow-rot, non-paging digest sibling
  tier — outcome checks, cadence staleness, registry coherence
* `precis-alert-help` — the `alert` kind (lifecycle, dedup, tab)
* `precis-tasks-help` — the tree shape + level gradient
* `precis-decomposition-help` — the GTD interrogation
* `precis-recurring-help` — `meta.schedule` + the Watches umbrella
* `precis-auto-tasks-help` — `meta.auto_check` leaves
