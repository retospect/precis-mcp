---
status: idea
pillar: 3d-design
---

# NEB transition-state barriers as a catpath pipeline step

Prompted by Laerte Patera's talk at the OePG-CMD Joint Meeting 2026, Graz
(notes: draft `graz-oepg-cmd-2026`, section `dc4087528`), where nudged elastic
band calculations supplied the barriers for on-surface nitrene chemistry. The
on-surface precedent is Hellerstedt et al. (2019), *Aromatic Azide
Transformation on the Ag(111) Surface Studied by Scanning Probe Microscopy*
(`pa448144`, doi 10.1002/anie.201812334), where the product distribution is
explained by which channel out of a shared nitrenoid intermediate has the lower
barrier; the synthetic context is Dequirez et al. (2012), *Nitrene chemistry in
organic synthesis: still in its infancy?* (`pa448145`, doi
10.1002/anie.201201945).

## Why

catpath ranks pathways, but the DFT side currently supplies **thermodynamics,
not kinetics**: `derive.py` computes adsorption energies and harmonic free
energies, and the volcano/Pourbaix analyses run off those. Nothing computes a
transition state. That means two routes with identical endpoint energetics score
identically even when one is barrierless and the other has a 1.5 eV saddle —
the selectivity question is exactly the one an experimentalist asks, and it is
the one we cannot currently answer. A barrier per elementary step turns a
thermodynamic ordering into a rate ordering.

## Shape of the change

A `gpaw_neb` job_type alongside `gpaw_relax` / `gpaw_scf`: takes an initial and
final relaxed structure, interpolates an image chain, runs climbing-image NEB to
the saddle, and writes a calc record whose `derived` carries E_a forward and
reverse, the saddle structure, and the imaginary-frequency check that confirms a
first-order saddle. catpath's pathway edges then carry a barrier alongside the
delta-G they already have.

## Open questions

- (Cross-link, 2026-10-02.) `surface-pourbaix-staircase-optimizer.md` §6.2
  needs sparse and active-learned NEBs for its reset contour. Its design
  asks for an audit of our NEB setup against NEBscape: minima-hopping IS/FS
  generation, permutation-reduced atom mapping, FS→IS symmetry alignment,
  μ/τ reaction-distance ranking, and the fidelity criteria. The audit
  belongs here.

- Endpoint pairing: NEB needs a matched initial/final pair with consistent cell
  and atom ordering. Who produces that — the reaction enumerator in
  `src/precis_dft/reactions/enumerate.py`, or a new mapper?
- Cost. NEB is roughly (number of images) times a relaxation, so it is the most
  expensive thing we would run. Needs a budget gate and probably an opt-in tier
  rather than running on every enumerated step.
- Convergence failure is common and silent-ish. What does a failed saddle search
  write, so a bad barrier never reaches a ranking?
- Whether to seed with a cheaper method first (a string method, or an ML
  interatomic potential) and only refine promising saddles with DFT.

Design call, Opus-tier. Owner `src/precis_dft/jobs/`,
`src/precis_dft/reactions/`, and the catpath pathway kind. Relates to
`autocatpath-integration.md` and `reaction-kind-and-synthesis-cost.md`.
