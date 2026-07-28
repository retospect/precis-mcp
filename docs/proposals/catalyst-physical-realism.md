---
status: draft
title: Physically-realistic catalyst screening — defect ensembles and poisoning-awareness
model: opus
---

# Physically-realistic catalyst screening — defect ensembles and poisoning-awareness

> Migrates gripes 161623 (defect/bulk-alloy realism) and 161622
> (poisoning-awareness) out of the bug tracker. Both extend the
> catalyst/autocatpath screening pipeline (`docs/design/autocatpath-integration.md`,
> `docs/design/catalyst-discovery-quest.md`) on top of the `structure` kind
> (ADR 0043) and, where an external DFT/ML-potential library is the source of
> comparison data, ADR 0053's import path.

## Motivation / why

The catalyst-discovery pipeline (quest `164903`, autocatpath as the rented
kernel) screens candidates two ways that are cleaner than the chemistry it's
supposed to predict:

1. **Idealized placement.** A candidate is one precise atomic arrangement —
   e.g. a single-atom dopant at a named site, or a clean-metal slab — scored
   as if that exact configuration were what gets synthesized. Real bulk
   catalysts are statistical: a "1% Cu-doped" material is an ensemble of
   defect configurations at that concentration, not one placement. Screening
   the single placement answers a question ("is *this* arrangement
   catalytic?") that isn't the one a synthesis chemist can act on
   ("is *this composition*, made in bulk, catalytic?").
2. **No poisoning model.** The pipeline scores catalytic activity
   (adsorption energetics, barriers) but has no notion of catalyst
   *poisoning* — species that block active sites and degrade activity over
   time. A candidate ranked purely on activity may be a poor real-world
   choice next to a slightly-less-active candidate that resists poisoning.

Both gaps push screening results toward "simulates cleanly, doesn't survive
contact with a real reactor." Fixing either is a real deliverable on its
own; they're captured together here because both extend the same pipeline
and were filed as siblings, not because they're one build.

## In scope

**Direction 1 — defect ensembles (gripe 161623).**

- Treat a low-concentration dopant/defect (e.g. "1% Cu in a host lattice")
  as a *stochastic surface-texture* parameter, not a single atomic
  placement: sample multiple random defect configurations at the target
  dopant fraction over a cell/supercell.
- For each sampled configuration, compute the resulting surface geometry
  and adsorption energetics (reusing the existing `structure`/run-cube
  machinery — this is a sampling strategy over inputs, not a new solver).
- Aggregate across the ensemble and flag *compositions* (not individual
  placements) where the defect-induced texture reliably meets the
  catalytic criteria the pipeline already uses.
- Net effect: the pipeline can argue "this alloy composition, as
  commercially producible, is catalytic" instead of "this exact atom
  arrangement is catalytic" — the claim a synthesis chemist can act on.

**Direction 2 — poisoning-awareness (gripe 161622).**

- Simulate poisoning energetics the same way activity is already
  simulated: adsorption/binding energetics for known poison species (CO,
  S, halides, and other usual suspects for the material class being
  screened) at the candidate's active sites.
- Compare each candidate's catalysis-relevant barriers/energetics against
  its poisoning-relevant ones, and rank candidates by poison resistance
  alongside (not instead of) activity.
- Optionally, an "unpoison" workflow: given a poisoned site, compute
  desorption/regeneration barriers — how hard is it to recover the site,
  not just how likely is it to get blocked.

## Explicitly NOT in scope

- **Any specific relaxer/DFT-engine/ML-potential choice.** Both directions
  reuse whatever backend the pipeline already dispatches to (autocatpath/MACE
  today); backend selection and any external-library import rides ADR
  0053's ladder, not this proposal.
- **New DFT methodology.** This is a *sampling and comparison* strategy
  over the existing energetics pipeline, not a new physics model. No novel
  relaxation, uncertainty, or reaction-network method is proposed here.
- **Full kinetics / microkinetic modeling.** Comparing activity-barrier vs.
  poisoning-barrier magnitudes is not a rate model — turnover frequencies,
  coverage-dependent kinetics, and time-resolved poisoning dynamics are out
  of scope.
- **Non-(111) facets, solvation, or other envelope work already tracked as
  out-of-scope in `docs/design/catalyst-discovery-quest.md` §9** — this
  proposal doesn't widen that envelope.
- **Deciding between the "unpoison workflow" and "poisoning-barrier
  ranking" sub-directions of gripe 161622** — see open questions.

## Acceptance criteria

1. A candidate composition can be screened as a **defect ensemble**
   (N sampled configurations at a target dopant fraction) rather than a
   single atomic placement, and the pipeline's catalytic-criteria check
   operates on the ensemble aggregate, not one member.
2. Candidates carry a **poison-resistance ranking** alongside their
   existing activity ranking, computed from poisoning-species
   adsorption/binding energetics over the same site set used for activity.

## Target + blast radius

- The catalyst/autocatpath screening pipeline (quest `164903`,
  `docs/design/autocatpath-integration.md`, `docs/design/catalyst-discovery-quest.md`).
- The `structure` kind and its run-cube (ADR 0043) — ensemble sampling and
  poisoning runs are new *uses* of `struct_relax`/run-cube content-addressing,
  not new storage shape.
- ADR 0053 (external DFT catalyst libraries) — if poison-species reference
  data or defect-ensemble baselines are sourced externally, they land via
  that import path.
- The catalyst-discovery quest's frontier/ranking logic
  (`docs/design/catalyst-discovery-quest.md` §7.2-7.3) — poison resistance
  is a new named measure a candidate can be ranked on, alongside
  `{barrier, formation_e, …}`.

## Open questions / decisions log

- **Ensemble sample size vs. cost.** How many defect configurations per
  composition are enough to trust the aggregate (statistical significance)
  without blowing the compute budget autocatpath/MACE already runs against?
  autocatpath's existing `seeds:[0,1,2]` pooled-uncertainty pattern
  (`docs/design/autocatpath-integration.md` §3.4) is a plausible template but
  answers a different question (numerical spread of one geometry, not
  spread over many geometries) — not yet decided whether it's reusable
  as-is or needs its own pooling.
- **Which poison species per material class.** The gripe names the usual
  suspects (CO, S, halides) but the actual list is material-class-specific
  and not enumerated here.
- **Unpoison workflow vs. poisoning-barrier ranking — or both.** Gripe
  161622 named two directions: (1) compute desorption/regeneration
  barriers for an already-poisoned site ("unpoison"), and (2) simulate
  poisoning energetics up front and rank by resistance. Not decided
  whether both ship, which ships first, or whether they're independent
  enough to become separate proposals.
- **Split candidate.** Per the proposals README split heuristic, defect
  realism and poisoning-awareness are separately testable/shippable
  deliverables bundled here only because they were filed as sibling
  gripes against the same pipeline. If either is picked up for a build
  before the other, split into two proposals (`catalyst-defect-ensembles`
  / `catalyst-poisoning-awareness`) and wire the split with `blocked-by`
  if a real ordering emerges — none is asserted yet.
