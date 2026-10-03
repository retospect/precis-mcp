---
status: draft
title: stamp a precis version on realize and silk output so pre/post-deploy boards are tellable apart
pillar: 3d-design
prio: normal
---

# stamp a precis version on realize and silk output so pre/post-deploy boards are tellable apart

## Motivation / why

`generators.VERSIONS` versions generator EXPANSIONS (ewod_pad_array),
and a tripwire test forces a bump when their output changes. Nothing
versions the realizer's copper (`realize.realize` → `pcb_routes`) or the
silk placement. So a board routed before a deploy cannot be told from
one routed after it, and a router change that alters copper is invisible
on stored boards. This has already happened once: the breakout-removal
change. The orchestrator's negotiation review (R3) asked for a version
bump before negotiation is ever default-on. The round-1 diff review
(2026-10-02) found there is nothing to bump.

## In scope

- `realize.REALIZER_VERSION` (int) and `silk.SILK_VERSION`, stamped on
  what the route job writes (the route digest / `last_route` and the
  per-net route rows' meta) and on silk output where it is persisted.
- `view='route-status'` / `view='congestion'` read the stamp and say
  STALE when it is older than the running code. This is the same pattern
  as `stale_generators`.
- A tripwire like `test_pcb_generator_version_tripwire.py`: a digest of
  a fixed fixture's realized copper pinned to `REALIZER_VERSION`, so
  changing copper without a bump reddens the gate.

- The same versions (plus the DRC pad model's) feed `session.content_hash`,
  the content half of the job idempotency key. Without it a resubmitted
  route or export on an unchanged board dedups to the job that ran under
  the old code. Round-2 review found this live: 29feaef48 changed oblique
  pad geometry (polygon ring, `land_min_mm` annular ring) with no version
  input, so a re-put returns the old pad geometry while a fresh board gets
  the new DRC/gerber output. Each bump changes every board's key once,
  which is the point. The route job's own half exists: `pcb_route.
  CODE_VERSION` goes into the `op='route'` dedup key (`_enqueue_op`), hand
  bumped. Still open: the realizer, silk and DRC pad-model versions, and
  any key for the export ops. Fold `CODE_VERSION` into `REALIZER_VERSION`
  or derive one from the other, so two counters cannot drift.

## Explicitly NOT in scope

- Automatically re-routing stale boards. A route op moves parts (thread
  trap), so the stamp only reports.

## Acceptance criteria

- A board routed at version N shows STALE in `view='route-status'`
  after the code moves to N+1, and does not before.
- A re-put of `op='route'` on an unchanged board enqueues a new job after
  a version bump and dedups before it.
- The tripwire fails when realize output changes on its fixture
  without a version bump.

## Target + blast radius

`src/precis/pcb/realize.py`, `src/precis/pcb/silk.py` (ewod-pcb's file:
coordinate the silk half), `src/precis/workers/job_types/pcb_route.py`,
`src/precis/handlers/pcb.py` (route-status/congestion views). No
migration if the stamp rides in existing JSON meta. Thread:
`pcb-easyeda-round-trip` (router half). The silk half is ewod-pcb's
call.
