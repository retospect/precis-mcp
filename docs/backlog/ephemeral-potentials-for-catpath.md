---
status: draft
title: ephemeral data-derived potentials as a catpath pre-screen — train a throwaway potential from the search's own single-points, refine only the survivors with DFT
prio: normal
---

# Ephemeral data-derived potentials as a catpath pre-screen

Prompted by Peter Cooke's talk at the OePG-CMD Joint Meeting 2026, Graz
(notes: draft `graz-oepg-cmd-2026`). Sources: Pickard, *Ephemeral data
derived potentials for random structure search*, Phys. Rev. B 106, 014102
(2022) [pa450223]; Salzbrenner, Joo, Conway, Cooke et al., *Developments and
further applications of ephemeral data derived potentials*, J. Chem. Phys.
159 (2023) [pa450224] — note the 2025 erratum, doi 10.1063/5.0313262.
Structure-search context: AIRSS [pa3786], USPEX [pa3727], CALYPSO
[pa1691], XtalOpt [pa450322].

## Motivation / why

catpath's DFT side is expensive per elementary step, and the cost is the
reason the kinetics gap in `docs/backlog/neb-barriers-in-the-catpath-pipeline.md`
is still open — that item's own closing question asks whether to "seed with a
cheaper method first ... and only refine promising saddles with DFT". This
item answers that question with a specific method rather than leaving it
open.

The usual objection to a machine-learned interatomic potential in a pipeline
like ours is that training one is a project: you need a large, carefully
curated dataset, the potential has to generalise, and validating it is its
own research problem. The EDDP position rejects that framing. The potential
is **ephemeral** — trained for one system and one search, used, and thrown
away. It never has to generalise beyond the region the search is already
visiting, because it is only ever asked about that region. That removes the
curation problem: the training set is whatever the search generated.

The consequence that makes it attractive here is that **training is the
search**. There is no separate data-collection phase to fund. Structures
generated during exploration are evaluated with DFT **single-point energies
only** — never relaxations — which is the cheapest DFT call there is, and
those same points become training data. Local curvature is captured by
perturbing ("shaking") structures around a configuration and taking more
single-points, rather than by computing forces or a Hessian.

The reported working ratio is roughly: train on 1,000–10,000 structures,
search 100,000 with the resulting potential, and surface ~100 candidates for
expensive treatment. If that ratio carries over even approximately, it
changes what catpath can afford to enumerate.

## In scope

- A job_type that, given a catpath system, trains an ephemeral potential
  from single-point DFT data generated for that system, and reports the
  potential as an artifact scoped to that system only.
- Using it as a **pre-screen**, not an oracle: the potential ranks or filters
  candidates, and DFT remains the arbiter for anything that reaches a
  published ranking. The potential's output never lands in a `derived` field
  that downstream analysis treats as ground truth.
- Reusing single-points the pipeline already computes, so the marginal cost
  of training is the shake-perturbation sampling rather than a fresh
  campaign.
- An explicit ephemerality contract: the potential is keyed to its system and
  its training set, and is invalidated rather than reused when either
  changes. A stale potential silently reused on a different composition is
  the main way this goes wrong.

## Explicitly NOT in scope

- A general-purpose or foundation interatomic potential for the corpus. The
  whole argument depends on *not* trying to generalise.
- Replacing DFT anywhere in the existing thermodynamic path
  (`src/precis_dft/jobs/derive.py`). This adds a filter upstream; it does not
  change how adsorption energies or free energies are computed.
- NEB saddle search itself — that is
  `docs/backlog/neb-barriers-in-the-catpath-pipeline.md`. This item is a
  candidate supplier to it, and should ship after or alongside it, not
  instead of it.
- Crystal structure prediction as a précis feature. AIRSS/USPEX/CALYPSO/XtalOpt
  are cited as context for where the method comes from, not as things to
  integrate.

## Acceptance criteria

1. Training a potential for one catpath system consumes only single-point DFT
   records; no relaxation is triggered by the training path.
2. The trained potential is stored keyed to its system and the hash of its
   training set, and a request for a potential whose training set has changed
   returns a miss rather than the stale artifact.
3. A screening run reports, for a held-out set of structures with known DFT
   energies, the rank correlation between potential and DFT — so the
   pre-screen's quality is measured rather than assumed.
4. No value produced by the potential appears in any field consumed by the
   volcano or Pourbaix analyses.
5. A screening run that the potential ranks highly but DFT then rejects is
   recorded, not discarded — the false-positive rate is the number that
   decides whether the pre-screen is worth its cost.

## Target + blast radius

- New job_type under `src/precis/workers/job_types/`, modelled on the
  existing `gpaw_*` shapes.
- `src/precis_dft/` for the single-point driver and the shake sampler.
- Storage for the potential artifact — needs a decision (below).
- No migration if the potential is stored as a job artifact; one if it
  becomes a first-class ref.

## Open questions / decisions log

- **Which potential form.** The EDDP papers use a specific feature/fitting
  scheme; whether to reimplement, wrap the published implementation, or
  substitute an off-the-shelf form is unresolved and is most of the
  implementation risk.
- **Where the artifact lives.** A job artifact is simplest and matches the
  ephemerality story. A first-class ref would make it searchable and
  citable, which conflicts with "throw it away". Leaning job artifact.
- **Whether the ratio survives the domain change.** The reported
  1e4/1e5/1e2 funnel comes from crystal structure search over a composition.
  catpath explores adsorbate configurations on a fixed slab, which is a
  different and probably easier landscape — but "probably" is doing work
  there, and the first slice should measure it on one system before any
  pipeline wiring.
- **Interaction with the erratum.** The 2025 erratum to the JCP paper should
  be read before implementing; unknown whether it touches the method or only
  reported numbers.
