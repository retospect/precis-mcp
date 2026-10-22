---
roster: active
pillar: 3d-design
effort: high
design-review: yes
---
# structure workspaces

## Resume

- **Next:** slice 0 of [structure-workspaces](../structure-workspaces.md) (importer + goldens, no container) and slice 0 of [pcb-workspace-code](../pcb-workspace-code.md) (builder + goldens, no container); neither waits on the sandbox lane.
- **Blocked by:** slice 1 (usable `run`) needs [sandbox-python-script](../sandbox-python-script.md) slice 4 (factory thread: the `mode:script` lane live); the proof slice needs [hexfold-multicell-prism](../hexfold-multicell-prism.md) slice 3.
- **Unblocks:** removal of the `generate`/`join`/`terminate` ops and the pcb batch construction put (W5); per-row call-site provenance on se and pcb designs; [pcb-platform](pcb-platform.md) Do-next 1.
- **Acceptance:** each spec's slice table acceptance column; slice 1 also needs the construction refusals live with `next=` naming the workspace path and no skill naming a retired op.
- **Worktree:** `structure-workspaces`
- **Builds:** structure slice 0: 1; slice 1: 2–3; pcb slice 0: 1–2, slice 1: 2–3 (specs' own estimates).
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when designs are built by Python scripts run in sandboxed workspaces and imported with per-row call-site provenance (`script_sha`, frame), the construction ops are gone, and relax is a cached service at both scales — [structure-workspaces](../structure-workspaces.md) and [pcb-workspace-code](../pcb-workspace-code.md), Reto 2026-10-22 (rulings W1–W6, Q1–Q6). Today both specs are `ready`, nothing is built; the list follows the specs' slice dependencies (importer before runner, trace before views, structures before pcb services).
**Last reviewed:** 2026-10-22
**Worktree:** `structure-workspaces`
**Allocation decision:** active — Reto 2026-10-22.

## Do next

1. **structure-workspaces slice 0** — importer + manifest + goldens with no container; every later slice (and pcb slice 1) imports through it, and it is the only slice that needs nothing above it.
2. **pcb-workspace-code slice 0** — `precis_geom.pcb` builder, `to_batch` byte-equal to the nano fixture's `build_design()`; parallel to 1, needs only the trace decorator from structure slice 2 for the trace-key assertions.
3. **structure-workspaces slice 2** — `precis-geom` wheel, warm venv, `@traced`; the pcb builder, trace views and relax cache all depend on it.
4. **structure-workspaces slice 1** — workspace kind + `run`; waits on the sandbox lane. Same change removes `generate`/`join`/`terminate` and their skill text (W5: no clock, no wrapper phase). Until it lands those ops stay as-is.
5. **pcb-workspace-code slice 1** — importer + replace + retirements; same change refuses the batch construction put and the eight construction ops (W5 applied). After 4.
6. **structure-workspaces slice 1b** — git objects in Postgres (ruled yes); makes `script_sha` a real FK and the host repo a cache. After 4.
7. **structure-workspaces 2t, 2p, 2a** and **pcb-workspace-code 2, 2t** — trace views, python-kind join, assembly API, place/route as cached requests; after 3–5.
8. **structure-workspaces slice 3** — the 12-wall H-column prism both ways; the proof that the script path is cheaper (needs hexfold-multicell-prism slice 3).

## Horizon

1. **structure-workspaces slices 4, 4p, 4m, 4a, 4u, 5** — relax interface, periodic ladder, motif templates, interface relax, unit catalogue, macro check-and-report; slice 4p also gates the held [hexfold-tethered-relax-op](../hexfold-tethered-relax-op.md) (see hexfold-toolkit).
2. **structure-workspaces 2e, 2r** — atom fisheye and observation rays; after 2t/2p.
3. **structure-workspaces slices 6, 7** and **pcb-workspace-code slices 3, 4, 5** — agent modules + skill, CI goldens, placed-module cache + hierarchical routing, offline kernel, dogfood migration.

## Parked

- **OPEN QUESTION — repetition owner: [pattern-groups](../pattern-groups.md) vs `pga.tile`.** pattern-groups (`repeat=linear/polar`, orbit-deduped checks) and the assembly API's `pga.tile` (packed and fused copies) both own "one unit repeated". UNDECIDED whether ops or the assembly API own repetition; unparks when Reto rules. Until then structure slice 2a's `array_block` rewiring stays unstarted and pattern-groups is not built.

## No action needed

- **[se-modules-and-assemblies](../se-modules-and-assemblies.md)** — superseded by the structure spec; kept as a stub until structure slice 6.
- **[pcb-meta-blocks](../pcb-meta-blocks.md)** — superseded by pcb-workspace-code (`@pcb.module`).
