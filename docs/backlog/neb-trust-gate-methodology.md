---
status: draft
title: Inspectable NEB barrier trust methodology — decide multi-seed-at-dispatch before crediting the barrier axis
pillar: 3d-design
thread: chemistry
prio: high
---

# Inspectable NEB barrier trust methodology (gr536970, gr541116)

Source gripes: gr536970, gr541116 (same capability filed twice; the
second is a flagged dup — fold, don't track separately).

A `reto:ask`, not a build item: qu164903 ticks filed
`needs-tool: inspectable-neb-trust-gate-methodology-with-multi-seed-at-dispatch`
and the recommend comment is "decide on an inspectable NEB trust-gate
methodology for multi-seed-at-dispatch before any barrier-axis
conclusion is drawn".

## Motivation / why

Every independently-rerun barrier credit in qu164903's history — bare
dopants, composites, and the champion-to-beat Ag-subsurf-1 — has flipped
to a materially different or untrustworthy value: 6-for-6 across three
distinct fault families. Across 270+ structures, every bare-dopant and
composite barrier measurement has failed or non-reproducibly passed the
`endpoint_agreement` / `saddle_verified` / seed-registration /
`detachment` trust gate. Until a methodology decides how a barrier is
dispatched and credited, no chemistry signal on the barrier axis can be
trusted and the chemistry-proposal search keeps cataloguing fault
instances instead of mechanism evidence.

What already exists (so this item is a ruling over assembled pieces,
not a new read or gate):

- `view='trust'` on pathways renders each gate's input, threshold and
  verdict (`neb-trust-diagnostic.md` slice 1, shipped); its slice 2
  pooled "would it pass" over sibling `endpoint_identity` records is
  open.
- `gr508948` (shipped bb4216879) stamps `tol_eV` on
  `endpoint_agreement` records and `seeds=[...]` on reruns.
- `gr517804` (ruled 2026-10-25) gates a neb-tier barrier on ≥2
  independent same-engine seeds pooled across ready siblings
  (`_flag_few_independent_seeds`, `seeds:unknown` when unprovable);
  `scripts/neb-seed-trust-drylist` dry-lists what would flip.
- `neb-structure-builder.md` stores the same trust record shape on
  `struct_neb_runs`, so whatever methodology lands applies to the
  structure-kind lane too — or must say why not.

The gap the gripe names is the *methodology*: which seeds get dispatched
and when, what the credited anchor is, and how a quest knows a barrier
is done rather than one more fault instance.

## In scope

- A recorded decision on multi-seed at dispatch for neb-tier barrier
  work: seeds minted up front vs a single seed plus rerun-on-fail, and
  how pooled sibling seeds count toward the credit.
- A recorded definition of the trust-passing barrier anchor for a quest
  frontier: the gates that must pass, on which evidence, rendered where.
- Naming the fault families the methodology covers
  (`endpoint_agreement`, `saddle_verified`/seed-registration,
  `detachment`) and what a quest does with a barrier that fails each
  one — rerun with fresh seeds, change endpoint placement, or stop.

## Explicitly NOT in scope

- Changing any gate threshold or verdict semantics — `ENDPOINT_TOL_EV`,
  twin tolerance, the absurd-barrier bound, `barrier_trusted` semantics.
  `neb-trust-diagnostic.md` holds the same boundary.
- New compute, job types or dispatch machinery before the ruling —
  "multi-seed at dispatch" may end up reusing `put(mode='rerun',
  args={'seeds': […]})` unchanged.
- The endpoint-placement redesign aimed at the agreement failures:
  `neb-barriers-in-the-catpath-pipeline.md` Horizon 1 points at the
  catpath-repo item `global-endpoint-sampling-nebscape` (AGOX/
  global-optimised endpoints). This item decides the credit rule, not
  the endpoint generator.
- qu509651's structure-side NEB build (`neb-structure-builder.md`).

## Acceptance criteria

- A decision is recorded (in this item's decisions log or an ADR)
  naming: the seed rule at dispatch, the gates a barrier must pass to
  be credited on the frontier, and the surface where a quest inspects
  them (default candidate: `view='trust'`).
- The decision says which fault families it covers and the prescribed
  action per family on failure. (TBD — hinges on open question 1.)
- After the ruling, one qu164903 barrier measurement can be walked from
  dispatch to credit/decline on recorded evidence alone — no tick spends
  a confirmation rerun whose verdict is already stored. (TBD — the walk
  exists once the methodology does.)

## Target + blast radius

Docs/decision only at filing. Whatever the ruling is, the consumers are
`quest/compute.py` (harvest and the seed/dispatch flags) and
`quest/tick.py` (what a tick is told to dispatch); the inspectable
surface is `src/precis/utils/trust_gate.py` + `precis_pathway`
`view='trust'`. No code changes until the ruling lands.

## Open questions / decisions log

1. **Multi-seed at dispatch: up front, or single-seed plus
   rerun-on-fail?** Options: (a) always dispatch ≥2 seeds at the neb
   tier (GPU cost up front, but the trust gate can pass on first
   credit); (b) keep single-seed dispatch and rerun on gate failure
   (today's shape — every credited barrier has flipped so far); (c)
   single-seed dispatch with sibling pooling under `sibling_key` as the
   only allowed second source.
2. **What is the credited anchor?** Options: (a) the existing per-gate
   composite (engine `trust_summary` + the three precis flags) declared
   sufficient once seeds are independent; (b) a named single anchor
   artifact a quest cites (pathway id + gate rows + seed set).
3. **Does the methodology bind `struct_neb` runs too?** They store the
   same trust record shape; a different credit rule would split the
   frontier's barrier axis between pathway-lane and structure-lane
   results.
4. **Per-fault-family action.** `endpoint_agreement` failing on a
   reproducible second basin (the st336035 pattern) reads as placement
   or chemistry, not noise — does the methodology route that to
   endpoint redesign rather than more seeds? Which families get "rerun"
   vs "stop and fix the endpoint"?
