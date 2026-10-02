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
