---
status: idea
title: Transition-state pocket — derive an se pocket spec from a reaction's transition state, realise it as a backboned scaffold
pillar: 3d-design
prio: normal
blocked-by: pocket-from-molecule
---

# Transition-state pocket

Reto, 2026-10-01 (pillar review): given the transition state of a reaction,
generate a pocket that stabilises it — specified in `se`, protein-like,
from the transition-state geometry plus (optionally) the in and out
molecules. "We spec the shape, then realization will put the right things in
the right places, and backbone them together somehow."

## Why it is the right shape of work here

A barrier drops when the pocket complements the transition state better than
it complements the endpoints. That makes the design target a *differential*
constraint, which is exactly what the se layer already expresses: a pocket is
a named set of regions, each region carries measures with `min`/`max` and a
`strength`, and a mismatch is a `validate()` finding
(`pocket-from-molecule.md` (region property layer, folded) §3, §5). So this item is a *generator* of
pocket specs plus one new constraint form, not a new modelling layer.

The pieces that exist: `pocket-from-molecule.md` (region property layer, folded) (pockets, per-region
hydrophobic/charge/field measures, spec-versus-realised, the pocket spec as a
`class` node), `pocket-from-molecule.md` slice 4 (intent-to-realize loop, folded) (the pick-and-join loop that
turns a spec into chosen groups), hexfold's pocket extraction (spec §28.8),
and NEB transition-state geometry in the chemistry thread
(`neb-barriers-in-the-catpath-pipeline.md`). Nothing joins them.

## In scope

1. **Input.** A transition-state structure (a NEB saddle point, or a
   hand-authored structure), and optionally the reactant and product
   structures of the same step. Source of record is the `pathway` step the
   chemistry thread already produces.
2. **Derive the pocket spec.** Sample the transition state's exposed surface
   into regions; per region emit the complementary measures the region layer
   already defines (shape envelope, charge sign and magnitude band,
   hydrophobic/polar class, field direction where a point-charge sum gives
   one). Output is an ordinary `add_pocket` call — no new op.
3. **The differential constraint, the one new form.** A measure that scores
   the spec against *two* structures and requires a margin: fit to the
   transition state must beat fit to reactant and to product by at least
   `margin`. Specified alongside the region measures; evaluated by the same
   registered-check path. Without it the generator designs a binder, not a
   catalyst.
4. **Realise.** Hand the pocket spec to `pocket-from-molecule.md` slice 4 (intent-to-realize loop, folded): each
   region's measures are a membership query against candidate groups
   (hexfold catalogue entries, substituent tiers, library parts), and the
   chosen groups are joined by a backbone. Two backbone modes, cheapest
   first: (a) rigid — mount the groups on an authored scaffold or a hexfold
   sheet, positions fixed by the pocket shape; (b) chain — a connected
   backbone threading the group anchor points, which is where the
   protein-like form comes in and where `precis_chain` / `precis_bio`
   already have the chain machinery.
5. **Acceptance is a measured number, not a picture.** A generated pocket
   is only interesting if the step's barrier with the pocket present is
   lower than without it, at a stated fidelity rung. The evaluation runs in
   the chemistry thread's existing NEB lane; this item owes the geometry
   handoff, not the engine.

## Explicitly NOT in scope

- Dynamics, solvation, or any time-dependent simulation.
- A sequence-design or folding engine. Mode (b) emits a backbone *spec*;
  whether `precis_bio`'s predictor is ever pointed at it is a later call.
- Claiming a designed pocket works. The output is a candidate plus its
  computed margin.

## Acceptance criteria

- From one `pathway` step with a saddle-point geometry, the generator emits
  an se pocket whose regions round-trip through `view='pockets'` with their
  specs.
- The differential measure computes on a worked example and reports the
  margin against reactant and product; `strength: hard` makes a negative
  margin a `validate()` error.
- The realize loop returns at least one group assignment per region, or
  names the regions it could not satisfy.
- One end-to-end example recorded: step, transition state, pocket, chosen
  groups, barrier with and without, fidelity rung.

## Target + blast radius

`precis_se` (the generator and the differential measure, on top of the
region layer), the realize loop, and a read of the `pathway` step geometry.
Owner: **se-machine-design** thread (Horizon, after
`pocket-from-molecule.md`, which carries the region layer and the
realize loop — it is blocked on it). Consumer and evaluator: the **chemistry** thread, which owns the
transition-state geometry and the barrier measurement.
