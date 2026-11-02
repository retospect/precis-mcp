---
status: idea
pillar: platform
---

# Machine kind slice 2 — capability sourcing

Slice 1 of gr549585 landed the `machine` kind: one `ma_<host>` ref per
host, stable facts (`platform`, `release`, `role`, `gpu`) in `meta` +
card chunk, live load/temperature/last-seen overlaid from
`host_heartbeat` at read time, `put(mode='sync')` refresh run by hand.

What slice 1 deliberately did **not** build: **capabilities**. A
machine's real shape — GPU model/count, worker lanes it serves,
services it runs — is today scattered across deploy config
(`deploy/inventory*`, `group_vars`), `service_config`, and the worker
lane registry. Reto's ruling (gr549585 comment 4): capabilities must be
**sourced** from those, never hand-entered, so they don't drift.

## Acceptance criteria

- A `machine` ref's capability facts come from a reader over deploy
  config / service_config / worker lanes — identified and named here —
  not from `put` args.
- `search(kind='machine', capability=…)` (or the settled filter shape)
  answers "which hosts have a GPU / serve lane X".
- Sync (or the chosen refresh path) is still operator-run; no cadence
  wires a deploy-config read against prod.
- `precis-machine-help` and the gripe are updated; real hostnames stay
  out of code/tests/docs.

## Explicitly NOT in scope

- New collectors for load/temperature — `host_heartbeat` already covers
  them.
- `fleet` host rows — agent coordination state, unchanged.

## Open questions / decisions log

- Which source wins per fact: `deploy/inventory` host vars for GPU and
  role, `service_config` for served lanes, worker lane registry for
  claimable work? Slice 1 found **no existing inventory reader in
  `src/`** — sync today sources `host_heartbeat` + `hosts=` args only.
  Decide whether the reader lives in `precis` (new, stdlib YAML parse
  of the gitignored inventory — but the repo is public and the
  inventory path isn't shipped) or in `deploy/` as an export step that
  pushes facts (like `scripts/fleet-report` does for `fleet` rows).
- Does `search` need a `capability=` axis or does `role=`/`platform=`
  plus tags suffice?

Thread: [`threads/deploy-fleet-ops.md`](threads/deploy-fleet-ops.md) ·
gripe `gr549585` (slice-2 ruling in comment 4).
