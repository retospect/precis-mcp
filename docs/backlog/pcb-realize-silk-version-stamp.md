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

## Explicitly NOT in scope

- Automatically re-routing stale boards. A route op moves parts (thread
  trap), so the stamp only reports.

## Acceptance criteria

- A board routed at version N shows STALE in `view='route-status'`
  after the code moves to N+1, and does not before.
- The tripwire fails when realize output changes on its fixture
  without a version bump.

## Target + blast radius

`src/precis/pcb/realize.py`, `src/precis/pcb/silk.py` (ewod-pcb's file:
coordinate the silk half), `src/precis/workers/job_types/pcb_route.py`,
`src/precis/handlers/pcb.py` (route-status/congestion views). No
migration if the stamp rides in existing JSON meta. Thread:
`pcb-easyeda-round-trip` (router half). The silk half is ewod-pcb's
call.
