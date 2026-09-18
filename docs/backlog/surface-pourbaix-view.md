---
status: draft
title: Surface Pourbaix view — coverage scan per candidate slab + CHE shift, lowest-G termination vs U with the pathway's U_L/U_opt overlaid, on the structure page
prio: high
blocked-by: qu164903-presentation-feedback
---

# Surface Pourbaix view (levels b + c)

Decision (Reto, 2026-09-18, presentation feedback item 1): build the
**surface** Pourbaix diagram of a candidate slab (which adsorbate termination
has the lowest free energy at a given potential and pH) and overlay the
candidate pathway's limiting/optimal potentials on it. Not the solution-
species diagram (that says nothing about the catalyst; a context panel at
most, out of scope here). Effects-report Phase 3.1 (`autocatpath_coverage`
job + `view='coverage'`) is the compute half of this item; the two ship
together or 3.1 first.

## Motivation / why
Every qu164903 pathway assumes a termination (clean or one co-adsorbate) and
the CHE lever reads its profile at U_L/U_opt. Nothing checks whether the slab
is actually that termination at that potential — the coverage question the
effects report could not answer. A surface Pourbaix (Hansen, Rossmeisl,
Nørskov, PCCP 2008, 10, 3722) is the standard answer, and the engine already
has the γ(θ) half.

## Physics (what the view computes)
Per termination T (clean, H*(θ), OH*, O*, NO*, N*, NH_x*, later pairs):
ΔG_T(U, pH) = ΔG_T(0, 0) − n_e·e·U_SHE − n_H⁺·kT·ln10·pH. Every PCET
termination has n_e = n_H⁺ = n, so on the RHE scale
ΔG_T(U_RHE) = ΔG_T(0) − n·e·U_RHE — straight lines in U, pH-free. pH enters
only as the SHE relabel (`autocatpath/electrochem.py::u_rhe_to_she`,
−0.0592 V/pH at 298.15 K) and, for non-PCET terminations, via
`decoupled_ph_shift`. ΔG_T(0) per θ = γ_ads·area from the coverage scan
(`autocatpath/coverage.py::scan`: γ_ads(θ) = [G(slab+nA) − G(slab) −
n·μ_A]/area, μ from the gas ledger: μ_H = ½G(H₂), μ_O = G(H₂O) − 2μ_H). The
lower envelope over T at each U is the diagram; the U axis is the pathway
lever's range (−1.5…0.5 V vs RHE).

## In scope
1. **`autocatpath_coverage` job type** (precis side; effects-report Phase
   3.1): `quest/compute.py::dispatch_autocatpath_coverage(store,
   structure_ref_id, config)` mirroring `dispatch_autocatpath` (job tree,
   idem key `autocatpath_coverage:{sha(config, slab_extxyz, model,
   version)}`); executor under `workers/job_types/` wrapping
   `coverage.scan(cfg)`; harvest of the scan's JSON (`facets[].adsorbates
   {frag: {mu, points[{n, theta, energy, gamma, converged}], best}}`,
   `ranking`, `winner`, `mari`). **Blocker to resolve in the engine first**:
   `scan` builds its slab from `cfg.slab.{element, a, miller}` via
   `build_slab()`; it must accept the candidate's own relaxed doped slab
   (the structure ref's geometry), or the diagram is of clean Pd, not the
   candidate. Species list = H, O, OH, N, NO, NH, NH₂, NH₃ at θ ∈ {1/9, 2/9,
   1/3, 2/3, 1} on the 3×3 cell; ~40 relaxations per candidate on the ML
   potential.
2. **Storage**: on the structure ref, `meta.coverage = {model, conditions,
   thermo, area, terminations: [{species, n, theta, dG0, n_e, converged}],
   job_id, version}` (structures have no `view=` mechanism; the page reads
   meta). Store ΔG_T(0) per point, not the envelope — the envelope is
   recomputed client-side at any U/pH.
3. **Renderer** (structure page, new section between "Compute runs" and
   "Quest context", mode buttons per `cad/detail.html.j2`): ΔG vs U_RHE
   lines per termination, lower envelope shaded and labelled by winner
   region; pH field relabels the axis to SHE (same `sheFromRhe` as the
   pathway page); vertical lines at U_L and U_opt of each pathway in
   `_quest_context` (already carries `{ref_id, tier, barrier}`; add `U_L`,
   `U_opt`); a one-line verdict "at U_L = −0.32 V the slab is H*(2/3 ML),
   the pathway assumed clean" when the winner at U_L differs from the
   pathway's `meta.params.coads`.
4. **`get(kind='structure', … )` TOON**: a `coverage` block listing the
   terminations and the winner at U ∈ {0, U_L, U_opt} so the tick can read
   it (the tick has no live get; the results-table row gains a `winner@U_L`
   column).
5. Provenance footer: model, T, pressures, thermo tier, "single-species
   terminations, no lateral interactions (engine v1)".

## Explicitly NOT in scope
- Solution-species Pourbaix (pymatgen): separate, optional context panel.
- Mixed terminations (H*+NO*): engine coverage v2 (effects-report Phase
  3.2); the view must state the v1 limitation.
- Grand-canonical / field / cation effects: beyond CHE.
- Re-running pathways on the winning termination automatically: the
  proposer decides (decision 4 of the effects report, agent-owned series).

## Acceptance criteria
- A candidate structure with `meta.coverage` renders the diagram; without it
  the section shows "not computed — ~N relaxations" and a dispatch button
  (same fidelity dropdown pattern as Relax).
- The winner at U_L is stated in words, and the mismatch verdict appears
  exactly when the winner's species differs from `meta.params.coads`.
- pH field moves the axis labels only; a test asserts ΔG lines are
  unchanged at pH 0 vs 7 on the RHE scale.
- Test fixture: a synthetic `meta.coverage` with three terminations whose
  crossings are known by hand; envelope + winner regions asserted.
- `get(kind='structure')` TOON includes the coverage block; results-table
  row shows `winner@U_L`.

## Target + blast radius
`quest/compute.py` (new dispatch), `workers/job_types/` (new executor),
`precis_pathway/` harvest, `precis_web/routes/structure.py` +
`templates/structure/detail.html.j2`, `handlers/structure` TOON,
`quest/results_table.py`. Engine: `coverage.py::scan` slab input (catpath
bump, separate ship path).

## Open questions / decisions log
- Engine slab input (item 1 blocker): add `cfg.slab.atoms_path` (extxyz) to
  `scan` — the same extxyz the seed jobs already ship. Reto to confirm this
  goes in the next catpath bump.
- Species/θ grid above is a proposal; the proposer may narrow it per
  candidate (agent-owned, decision 4).
