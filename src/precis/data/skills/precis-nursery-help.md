---
id: precis-nursery-help
title: precis — nursery digest of todo-tree incoherence
summary: hourly tree-incoherence digest — orphans, stale claims, long waits, stuck doable detection
applies-to: precis worker --only nursery; tree-review:* tagged memories
status: active
---

# precis-nursery-help — hourly tree-incoherence digest

The nursery is the first of three review tiers in
`docs/design/todo-tree-plan.md` (Slice 3). It walks the todo tree
every hour, surfaces local incoherence via SQL-only detectors, and
writes a digest as a `kind='memory'` ref tagged `tier:nursery`. No
LLM call, no Discord push, no notification noise — the digest
reaches asa via her existing `internal-thought` slot in the
preamble.

## Detector catalogue

| Category | Triggers when | Threshold |
|---|---|---|
| `orphan` | open todo whose top-level ancestor isn't `level:strategic` | — |
| `stale-claim` | leaf carries `claimed-by:*` older than threshold | 3 h |
| `long-wait` | leaf carries `waiting-for:*` older than threshold | 7 d |
| `stuck-doable` | open leaf, no claim, no wait, no blocker, >threshold old | 24 h |
| `stalled-recurring` | recurring's most recent spawned child has been open >1 h | 1 h floor |

`orphan` enforces the strategic invariant (knob #6 in the plan).
`stale-claim` catches workers that died mid-task — the claim's age
is read from `ref_tags.created_at` on the open tag row.
`stalled-recurring` surfaces the Slice-4 collision-skip pile-up: a
spawned child stuck open will silently prevent further ticks.

Recurring subtrees (children of `level:recurring` roots) are
exempt from the strategic invariant — they're scheduled work, not
strategic work. The Watches umbrella itself doesn't appear in any
detector.

## Where the digest lands

```
kind='memory'
title=<the markdown digest>
tags=[tree-review:YYYY-MM-DD, tier:nursery, user:asa, internal-thought]
meta.nursery_fingerprint=<sha256 of (category, ref_id) pairs>
meta.nursery_finding_count=<int>
meta.nursery_date='YYYY-MM-DD'
```

Read recent digests with:

```
search(kind='memory', tags=['tier:nursery'], page_size=5)
```

The newest digest's text reads as:

```
Nursery digest 2026-06-14: 2 orphan, 1 stalled-recurring.

## orphan (2)

- #87 Build the platform
    open todo with no strategic ancestor — root needs a `level:strategic`
    tag or this leaf needs to be re-parented under one
- #94 Random side-quest
    open todo with no strategic ancestor — root needs a `level:strategic`
    tag or this leaf needs to be re-parented under one

## stalled-recurring (1)

- #12 Hourly arxiv check
    recurring #12 stalled: last spawn (child #143) has been open 5h —
    collision-skip will keep new ticks from piling up; resolve or
    auto-timeout
```

## Dedup discipline

Each digest carries a fingerprint of its `(category, ref_id)` pairs
on `meta.nursery_fingerprint`. The next pass computes the same
fingerprint on its current findings — if it matches the most
recent `tier:nursery` digest, no new memory is written. Empty
findings never write a memory either.

So a stable list of orphans + one resolved + one new orphan writes
a fresh digest (fingerprint changed). A stable list with no churn
writes nothing.

## Running it

The pass is in the default `precis worker` rotation alongside
`auto_check` and `schedule`. To run ad-hoc:

```
precis worker --only nursery --once
```

In production, hourly via the `precis_nursery` Ansible role on
melchior. See `cluster/roles/precis_nursery/README.md` for the
multi-host story.

## Pairing with chatter

asa-bot's preamble surfaces recent `internal-thought` memories in
its "## Inner life" block. The nursery digest lands there directly
— asa sees the latest digest in every conversation and can pull
its content into a reply when relevant. No new preamble slot
needed.

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

* `precis-tasks-help` — the tree shape + level gradient
* `precis-decomposition-help` — the GTD interrogation (Slice 2)
* `precis-recurring-help` — `level:recurring` + the Watches umbrella
* `precis-auto-tasks-help` — `meta.auto_check` leaves
