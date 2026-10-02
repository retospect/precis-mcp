---
status: draft
title: Pathway objective is "the target route wins every fork", read over the potential and pH window
pillar: 3d-design
prio: high
blocked-by: pathway-nh3-network-completeness
---

# Pathway objective is "the target route wins every fork", read over the potential and pH window

## Motivation / why

Reto, 2026-10-02: for NH₃ the objective is not minimum eV. At each step
the move toward NH₃ must be the most favourable outcome, thermodynamically
and kinetically. The engine already scores the per-fork version at one
potential (`results.score.selectivity`: per branch point, side climb −
on-route climb; catpath `viz.py`), and `P_side` for kinetics. What is
missing is the lever: at a fork between a proton-coupled step (shifts by
eU under CHE) and a chemical step (does not), the winner depends on U,
so the objective is a window, not a point. pH is asked for too, but under
pure CHE on the RHE scale pH drops out of every PCET step; it moves the
answer only through three named routes (below). A bare pH slider would
be a no-op.

## In scope

1. **Fork margin as a function of U.** For each branch point on the
   target route, ΔG_side(U) − ΔG_on(U) (closed form: each side's n_H
   difference times eU). Report `U_sel` = argmax over U of the worst fork
   margin, `sel_margin_at_Usel`, and the U interval where every fork
   margin > 0 (`U_sel_window`, possibly empty), intersected with U ≤ U_L.
   Post-processing over stored energies; no compute.
2. **Kinetic half** once barriers exist: the same per-fork comparison on
   Ea (U-independent under CHE), reported beside the thermodynamic
   margin; the window is where both hold.
3. **pH routes, each a named term:**
   - **fixed-SHE operation** — report the window on SHE at a chosen pH
     (`U_SHE = U_RHE − 0.0592·pH`), for a cell run at a fixed electrode
     potential;
   - **product protonation** — NH₃ ⇌ NH₄⁺ (pKa 9.25), NH₂OH ⇌ NH₃OH⁺
     (pKa ~6): the desorption step's free energy gains
     −kT·ln(1 + 10^(pKa−pH)), so the final desorption competes
     differently by pH;
   - **HER competition** — H* + H⁺ + e⁻ → H₂ as a competing branch at
     every supply step; the window must also beat HER (needs the H*
     energy the network already computes).
4. **Surfaces.** `view='analysis'` prints the window; the explorer
   shades the U window on the slider and adds a pH input that moves only
   the three routes above; the scalars are harvested onto quest
   candidates under the existing trust gate.

## Explicitly NOT in scope

- Explicit solvation, field effects, or a potential-dependent barrier
  model (CHE stays the model; say so in the report).
- Microkinetic steady state. The per-fork test is right when steps are
  effectively irreversible; where a reversible intermediate sits before a
  fork, the report flags it rather than solving the kinetics.
- Choosing quest rubric weights (human-set per quest).

## Acceptance criteria

- On a fixture graph with one PCET-vs-chemical fork, `U_sel_window`
  matches the hand-computed crossing potential to 1 mV.
- On a graph where no U makes the target win every fork, the window is
  reported empty with the losing fork named.
- Changing pH changes nothing on the RHE scale except the protonation
  and HER terms (test asserts the PCET-only fork is pH-invariant).
- pw455722 re-run (complete network) prints the window and the worst
  fork at `U_sel`.

## Target + blast radius

`src/precis/utils/reaction_graph.py` (`at_potential` and the route-step
reads), the pathway handler's `analysis`/`compare` views, the explorer
template (slider shading, pH input), quest harvest of the new scalars
(`electro_trusted` gate). Engine side optional: catpath could emit the
same scalars in `results`; precis computes them if absent.

## Open questions / decisions log

- HER needs a defined H₂ desorption reference per slab; confirm the
  network's H* energy is the one CHE uses before relying on it.
- **Measured 2026-10-02 (R2, catalysis-selectivity design note §11):**
  replaying catpath's `viz.score_report` on 213 neb-tier candidates with
  barriers stripped, the thermo-only branch-point margin ranks unlike the
  barrier-based one (Spearman 0.139, top-10 overlap 0/10, 99/213 exactly
  0). Method: rebuild each stored `meta.graph`, check that
  `branch_points[0].margin` reproduces the stored margin (299/322 rows do;
  the misses are all 0.13.0-era), delete `barrier`/`barrier_std` from
  non-supply edges, rerun. Two separate statements: (i) running screening
  on coadsorbed buys no ranking (shown); (ii) the barrier-based margin is
  a good ranking (NOT shown — see the next point). The span-based
  `_selectivity_section` margin reached ρ = 0.50: an unexamined lead, not
  a result. Screening needs a criterion of its own (BEP estimate, or the
  span form); this item owns that choice. Also: in the
  barrier-based margin the worst branch point is a +H supply edge for
  112/213 candidates (`HNOH -> HNOH+H`, `H2NO -> H2NO+H`), so today's
  criterion mostly compares an H-adsorption energy against a barrier. The
  window criterion must not inherit that. Repeatable with
  `~/.claude/projects/-Users-reto-precis-mcp/scratch/catsel-r2/replay.py`.
- **Decided, Reto 2026-10-02 (review-queue `catalysis-selectivity-3`):**
  qu164903's current selectivity rankings are read as unreliable until
  this item drops +H supply edges as branch-point competitors. No
  ranking change ships before this item; `selectivity_margin` stays in
  the rubric meanwhile.
- **Input from the Pd-hydride plan (2026-10-02, design note §15.7):**
  under CHE a +H supply step's ΔG at U is set by U whether the H comes
  from solution or from bulk β-PdH (bulk H at equilibrium has the same
  chemical potential). The reservoir changes kinetics (subsurface H → surface,
  no proton transfer) and H* coverage, not the thermodynamic supply. So on a
  hydride substrate the criterion takes the β-equilibrium H* coverage, and
  supply edges are still not branch-point competitors.
