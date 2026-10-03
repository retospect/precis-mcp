---
status: draft
pillar: local-compute
title: A standing graph-maintenance queue that soaks up idle local capacity
prio: high
---

# A standing graph-maintenance queue for idle local capacity

Reto, product-plan review 2026-09-30: "we use the box continuously to
improve the graph — adding summaries, inserting, meshing, linking,
categorizing."

Mining pass, 2026-09-30: `cluster-scheduling.md`'s north star does the
*inverse* of that — "model servers are worker-spun on demand and torn down
when the backlog drains" — and no monitor answers "is capacity idle right
now." Peer session abstract-knitting-wren: all 20 nursery watchdog
detectors are job-lifecycle pathology detectors (stuck, starved, crashed);
none observes spare capacity.

## Motivation / why

`cluster-scheduling.md`'s tear-down-when-drained clause optimizes for cost
when the fleet is otherwise idle — correct for a bursty external workload,
wrong for a graph that always has more summarising/meshing/linking/
categorising work available if capacity exists to run it. The two policies
conflict: tear-down-on-drain treats "nothing urgent queued" as "spin down,"
this item treats it as "spend the idle cycles on standing maintenance."

**This item supersedes `cluster-scheduling.md`'s tear-down-when-drained
clause for the graph-maintenance lane specifically** — the north star's
other six laws (one substrate, one scheduler, one control surface, thin
worker, resumable units, the rest) are unaffected; only the "torn down when
the backlog drains" sentence is narrowed to exclude this lane. `cluster-scheduling.md`
itself is being amended to say so in the same review round (not by this
item — flagged for whoever holds that file).

## In scope

- A standing queue of graph-maintenance work: the mesher pass
  (`knowledge-mesh.md` §4), `graph-gardener.md`'s proposal passes, dreaming
  (`dreaming.md`), chunk summarise, taxonomy categorise, embedding backfill.
- The queue's consumer is **local capacity specifically** — this work never
  competes for cloud/paid tiers.
- **Gate = frontier review**, the same shape `curation-gate.md` already
  uses — maintenance output (a proposed merge, a new summary, a new link)
  is proposed, not auto-applied, until reviewed.
- A utilisation/queue-depth signal ("is the box hot") so the standing
  queue's effect is measurable, not just assumed — the same gap
  `graph-health-metrics.md` names for the graph's own health, applied here
  to capacity.

## Explicitly NOT in scope

- Changing `cluster-scheduling.md`'s policy for any lane other than graph
  maintenance — the three singletons, worker-spun model servers for
  request-path work, and the rest of the north star are untouched.
- Building the mesher pass, gardener, dreaming, or summarise passes
  themselves — all exist or are separately specced; this item is the
  standing *queue* that feeds them local capacity, not their logic.

## Acceptance criteria

- A utilisation/queue-depth number exists and is visible (status surface
  or report) — "the box is idle" and "the box is hot on maintenance work"
  are both answerable.
- Graph-maintenance passes run against local capacity without competing
  with request-path or paid-tier work.
- Maintenance output (merges, new links, new summaries) passes through a
  review gate before landing, matching `curation-gate.md`'s posture.
- `cluster-scheduling.md`'s tear-down-when-drained clause is confirmed
  amended (by its owner) to exempt this lane — this item's acceptance
  includes checking that amendment landed, not just building the queue in
  spite of the conflicting text.

## Target + blast radius

`src/precis/workers/` (queue + consumer wiring); `cluster-scheduling.md`
(amendment, owned elsewhere); `knowledge-mesh.md`, `graph-gardener.md`,
`dreaming.md`, the chunk summarise pass (existing passes this item
feeds capacity to, unmodified in their own logic).

## Open questions / decisions log

- Whether "idle" is measured per-host or fleet-wide — fleet-wide risks one
  busy host masking three idle ones; per-host is the more useful signal
  but needs the queue to be host-aware, which the current standing-daemon
  model may not support cleanly.

- **[2026-10-03]** `local-mesh-upkeep.md` (draft) decides which actions
  this queue may give a local model, and at which bar (auto-apply or
  reviewed by a bigger model). Its `reviews` ledger is the review gate
  named in scope above.

Closest existing items: `cluster-scheduling.md` (the policy this item
narrows — supersession noted above), `knowledge-mesh.md`, `graph-gardener.md`,
`dreaming.md`, `curation-gate.md`, `local-summarizer.md`.
