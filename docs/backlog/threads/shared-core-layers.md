---
roster: dormant
pillar: 3d-design
effort: high
design-review: yes
---
# shared-core-layers

## Resume

- **Next:** S2: the hex run-failure Diag and the hex selector probe
  (gr522031, gr522032), in `hexfold` itself.
- **Blocked by:** Nothing. S1 (S1a core + CAD run failure, S1b pcb
  adapter, S1c probes) shipped 2026-10-29 on `worktree-diag-probe`
  (8514c4333, 744ff91e1 and the S1c pcb adapter commit).
- **Unblocks:** Structured run failures and probes for hex (gr522031,
  gr522032), and the laminar-common stackup for EWOD, flex and
  cartridge kinds.
- **Acceptance:** Per slice in [shared-core-layers](../shared-core-layers.md) §Slices.
- **Worktree:** per slice, on the GRIPE_FIX builder lanes.
- **Builds:** Not estimated here; use the spec's slice list.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when cad, se nano/hex and pcb report run failures, probes and
checks through one shared `Diag`/`RunResult` core, and the layering
(core < se-common / laminar-common < domains) is enforced by import-linter
contracts and per-layer conformance suites.
This thread owns `backlog/shared-core-layers.md`; its parent gripe is gr522030.
**Last reviewed:** 2026-10-27
**Allocation decision:** created on Reto's ruling of 2026-10-27 (gr522030
comment 2). It is not a fleet window: slices run on the GRIPE_FIX builder
lanes, and the orchestrator reviews the design before each slice lands.

Rulings (gr522030 c2):
- `precis_diag` is a new stdlib-only top-level package in the precis-geom wheel.
- `Diag` is a superset of `hexfold.report::Finding`, with one tested conversion.
- S1a adds no Diag section to `view='trace'`.
- In S1a only `precis_diag` becomes an import-linter root. `precis_se`,
  `hexfold` and the family packages become roots in later slices.

## Do next

1. **S2: hex run failure + probe** (gr522031, gr522032). The run-failure
   half already passes the shared adapter (hexfold `BuildError` carries a
   serialized report); S2 freezes the Diag field set after the third
   domain fits and adds the hex selector probe.
2. **S3: stable handles and selector core.**
3. **S4: checks harness, with DRC as the proof.**

## Horizon

1. **S3: stable handles and selector core.**
2. **S4: checks harness, with DRC as the proof.**
4. **S5: laminar-common extraction.** Moves the stackup model out of pcb
   before EWOD, flex or cartridge kinds grow their own.
5. **S6: typed params, units adoption, run diff.**

## Parked

- (none)

## Seam

`hexfold-toolkit` owns hexfold's `Finding` and the hex domain adapters (S2).
`pcb-platform` owns the pcb adapter (S1b) and the stackup that S5 extracts.
`ewod-pcb` is the first laminar-common consumer.
