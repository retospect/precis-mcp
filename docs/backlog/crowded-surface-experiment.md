---
status: draft
title: "catalysis: the crowded-surface experiment (NO→NH3 intermediates and barriers on an NO-saturated Pd(111) layer vs dilute), sequenced after the catpath 0.30.0 re-run"
pillar: quests
prio: normal
---

# Crowded-surface experiment

Blocked on catpath 0.30.0 deployed (spectator mode, PR #8, plus the NH2/NOH
template fix of gr491459).

Source: gr482313 (Reto's ask 2026-10-13, scope rulings 2026-10-14 and
2026-10-15). Engine work: gr489182 (catpath spectator-layer mode; the catpath
item is `/tmp/gripe-fleet/w6/catpath-spectator-spec.md`, outside this repo).
Paper behind the ask: pa191108 (coveragedriven26; *H crowding on HEA HER
surfaces), corroborating finding 349694.

## Rulings in force (quoted)

- 2026-10-14 (gr482313 comment 2): coverage only the extrema, a single
  adsorbate (dilute) and a full monolayer, no 1/9, 1/4, 1/2 steps; *H
  crowding out of scope ("H goes subsurface and is part of the Pourbaix
  continuum the SPSO thread already covers"); *NO at a full monolayer wanted:
  compare NO→NH3 intermediates and barriers on the NO-saturated surface
  against the single-molecule case. Master-session defaults (Reto may
  override): surfaces = the current SPSO top candidates; ML sweep first, DFT
  only where the ranking flips between dilute and monolayer.
- 2026-10-15 ~18:25Z (gr489182 comment 3, adopting comment 2's revisions):
  (1) saturation = 0.75 ML p(2x2)-3NO, labelled the UHV structural value
  adopted for the electrode; cells 2x2 with 3 NO for energetics, 4x4 with
  12 NO (or 4x2 with 6) for barriers, one or two NO removed for the reacting
  site; 1.0 ML at most as a stress row. (2) Spectators relax with each
  intermediate; record the relaxed-layer energy per footing. (3) NO-only
  spectators for v1, meaning no background H layer; the reacting H* sits in
  or beside the vacancy and is part of the ensemble (state its site); add one
  sensitivity row with a single spectator H* in a second vacancy. (4)
  Conditional barriers are acceptable, each reported with its footing
  (species, coverage, cell, arrangement hash, removed NO, H* site), the
  dilute value beside it, the MLIP model and the medium/large band, and
  whether the layer stayed intact; nothing pooled across footings. (5) DFT
  flip threshold = the medium-vs-large model band (about 0.25-0.3 eV) plus
  two non-flips as controls; v1 DFT = single points on MLIP-relaxed dense
  endpoints (3-5 structures) once a single-point rung exists; DFT NEB only
  for the one step that flips a candidate ranking, planned as its own
  request; no per-candidate dollar cap. Also adopted: add HNO, NH2OH, N2O and
  the NO dimer to the intermediate list; check dense-layer relaxes for NO
  desorption or site switching before any NEB; report the work-function
  change per footing.

## State of the evidence (all on st261823, Pd111-Ir-subsurf-1, qu164903 config)

| Run | Result | Source |
|---|---|---|
| jo489448 / jo489449 (mace medium / large), 0.26.0 | Engine capped "1 ML" at 3/15 (gr491177); crowded question not answered | gr482313 comment 5 |
| jo491457 (medium, 0.28.0), 1 ML arm | NH* at 1 ML rests at -0.30 V_RHE, no boundary in -1.00..+0.50 V; NH2*(9), NOH*(9) detached and dropped | comment 6-7 |
| jo491458 (large, 0.28.0) | NH2* at 1 ML rests at -0.30 V_RHE, none detached, no boundary | comment 8 |

Caveat (comment 9, gr491459 comment 2): the medium answer holds only because
NH2*(9) was dropped as detached, caused by catpath template and placement
bugs (NH2 H atoms 0.39 A apart as placed; NOH falls back to a linear chain,
O-H 1.25 A). Treat NH* vs NH2* at 1 ML as uncertain until the template fix
lands and both models are re-run. Large has no gas-correction set (gr486949);
NOH* has no thermo data.

## Sequence

1. **Re-run the extrema scans after catpath 0.30.0 is deployed** (mace
   medium and large; same st261823 and config as jo491457 / jo491458;
   `structure_ref` alone). This also supplies the baseline "dilute vs 1 ML"
   gamma table with the template fixed. Reto's sequencing: this precedes the
   spectator runs.
2. **Spectator-layer runs** once the engine ships `spectators` (gr489182):
   the pathway lane with `spectators {species: NO, coverage: 0.75, vacate: 1}`
   on the 2x2 cell for energetics, then the 4x4 / 4x2 cell for barriers,
   medium and large. Intermediates: `[NO, NOH, N, NH, NH2]` plus HNO, NH2OH,
   N2O and the NO dimer (the last four are catpath network-template work and
   are not in the first engine PR).
3. **Precis wiring** (after the engine ships; none of it is built, there is
   no engine to test against): feature-detect `spectators` on
   `autocatpath.config.Config`, as `runner.coverage_mixtures_supported` does
   for mixtures; pass the block through the pathway lane; key quest results
   and anchors by `footing.arrangement_hash`; show dilute and dense rows side
   by side; refuse pooling across footings (the engine's
   `aggregate_partials` refuses mixed footings already). The coverage scan
   ignores `spectators`.
4. **Surface-Pourbaix confirmation** (gr489182 comment 2): confirm NO stays
   at 0.75 ML at -0.30 V_RHE on st261823 before the dense footing is fixed.

## Explicitly NOT in scope

- *H crowding and subsurface H (SPSO covers it); mixed NO*+H* resting phases
  (`docs/backlog/mixed-coverage-precis-wiring-residue.md`).
- Cluster-expansion / kMC, potential-dependent grand-canonical DFT, a
  microkinetic coverage loop (deferred in the research primer, gr489182
  comment 1).
- Multiple arrangements, the single spectator-H* row and the work-function
  report are later engine slices (gr489182 comment 4, item 6).

## Acceptance criteria

- Step 1: a table of dilute vs 1 ML gamma per adsorbate for both models, with
  no adsorbate dropped for template reasons, and the -0.30 V resting
  termination named per model.
- Step 2: for each candidate intermediate, dilute and dense dE and barrier
  side by side, each row carrying the footing fields of ruling (4); flips
  flagged where the margin exceeds the medium/large band.
- No result pooled across footings anywhere in the quest output.

## Open questions / decisions log

- Reto decides: does the current SPSO top set still equal the single Pareto
  member st261823, or is the surface list widened (master default, not a
  ruling).
- Reto decides: the cluster-compute budget for step 2, whose barriers are MLIP
  NEBs on about 90-atom cells (the review in gr489182 comment 2 puts DFT NEB
  at hundreds of node-hours per candidate, so it stays a separate request).
- Reto decides: whether a DFT single-point job type is built (none exists;
  me470247) or ruling (5)'s DFT refinement waits indefinitely.
- Reto decides: whether to verify the [pplx] DOIs of gr489182 comment 1
  before any draft cites them (comment 2 lists the unchecked ones).
