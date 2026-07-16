---
id: precis-nursery-help
title: precis — nursery detector of todo-tree incoherence
summary: per-minute tree-incoherence detectors — orphans, stale claims, long waits, stuck doable, spin loops, plan-tick spins — raised as alerts
applies-to: precis worker --only nursery; kind='alert' (alert-source:nursery:*)
status: active
---

# precis-nursery-help — tree-incoherence detectors → alerts

The nursery is the first of three review tiers in
`docs/design/todo-tree-plan.md` (Slice 3). It walks the todo tree
(and the worker fleet) every pass, surfaces local incoherence via
SQL-only detectors, and raises a `kind='alert'` per condition (see
`precis-alert-help`). No LLM call. The only proactive push is a
one-shot Discord ping on a *new* `critical` condition (a thrashing or
dead worker), delivered as a `kind='message'` to the Discord channel in
`PRECIS_OPS_ALERT_TARGET` (a channel target `discord/<guild>/<channel>`,
*not* a webhook URL; the deprecated `PRECIS_OPS_ALERT_WEBHOOK` alias is
still accepted) — the same asa_bot channel as the daily news
briefing — unset by default, so the push merges dark and everything
else stays pull-only. It
used to write a `kind='memory'` digest tagged `tier:nursery`
— that conflated ops telemetry with reflective *thought*, polluted the
memory namespace, and (because the spin-loop finding set churns every
second) spun on itself writing thousands of near-dup memories a day.
Alerts dedup per *condition* instead.

## Detector catalogue

| Category | Triggers when | Threshold |
|---|---|---|
| `orphan` | open todo whose top-level ancestor isn't `level:strategic` | — |
| `stale-claim` | leaf carries `claimed-by:*` older than threshold | 3 h |
| `long-wait` | leaf carries `waiting-for:*` older than threshold | 7 d |
| `stuck-doable` | open leaf, no claim, no wait, no blocker, >threshold old | 24 h |
| `stalled-recurring` | recurring's most recent spawned child has been open >1 h | 1 h floor |
| `spin-loop` | one `(ref_id, source)` emits >threshold `ref_events` in 24 h | 200 / 24 h |
| `plan-tick-spin` | a planner parent mints >threshold `plan_tick` jobs in 24 h without converging | 16 / 24 h |
| `worker-restart` | a `(host, process)` emits >threshold `worker: started` boot rows in 1 h (restart storm) | 8 / 1 h · **critical** |
| `dead-worker` | a continuous daemon (`precis-worker` / `precis-worker-agent`) silent >threshold while its host is alive | 10 min · **critical** |
| `dispatch-stall` | `claude_inproc` jobs `STATUS:queued` >threshold with **zero** live-lease jobs running (executor stopped claiming) | 15 min · **critical** |

`orphan` enforces the strategic invariant (knob #6 in the plan).
`stale-claim` catches workers that died mid-task — the claim's age
is read from `ref_tags.created_at` on the open tag row.
`stalled-recurring` surfaces the Slice-4 collision-skip pile-up: a
spawned child stuck open will silently prevent further ticks.
`spin-loop` is the only cross-kind detector — it scans `ref_events`
rather than the todo tree, catching a background worker that
re-claims the same ref every pass (a broken retry window, a no-op
outcome that never clears the claim predicate). The detail names the
source + last event + rate so triage starts at the worker. The same
loops are also surfaced on the web Status page's "Background health"
panel for pull-style monitoring.

The three **worker-health** detectors watch daemon liveness / work
flow, not the todo graph, and are the only `critical` categories (a
new one fires the one-shot Discord ping). `worker-restart` and
`dead-worker` read `worker_logs`; `dispatch-stall` reads the job
queue. `dispatch-stall` is the planner-SPOF guard: minting runs on
every node, but a `plan_tick` can only *execute* on melchior's
agent-profile worker, so if that executor dies / 401s / never starts,
jobs pile up `STATUS:queued` with no failure bubble and the planner
goes silently dark. The "nothing running with a live lease" gate is
what separates a dead executor from a healthy-but-backlogged one, and
being symptom-level it also catches an agent worker that never
started (which has no log rows for `dead-worker` to age). These raise
non-ref-scoped alerts (`ref_id=None` + an explicit `fingerprint_key`).

Recurring subtrees (children of `level:recurring` roots) are
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

In production, hourly via the `precis_nursery` Ansible role on
melchior. See `cluster/roles/precis_nursery/README.md` for the
multi-host story.

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
  decomposition budget — those are the structural tier (every
  6h, opus call, future Slice 3 pass-2).
* Not a deep review — no archive moves, no prune recommendations.
  That's the weekly deep tier.
* Not a worker dispatcher — the nursery describes; asa-bot
  decides whether to act on a finding when next chatting.

## Related skills

* `precis-alert-help` — the `alert` kind (lifecycle, dedup, tab)
* `precis-tasks-help` — the tree shape + level gradient
* `precis-decomposition-help` — the GTD interrogation (Slice 2)
* `precis-recurring-help` — `level:recurring` + the Watches umbrella
* `precis-auto-tasks-help` — `meta.auto_check` leaves
