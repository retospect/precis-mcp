---
status: idea
pillar: 3d-design
---

# Berry phase and topological defects as first-class model properties

IDEA. Précis models molecules and materials by their energies, geometries and
bonds. Nothing in the structure, dft_calculation or material kinds records a
*geometric* or *topological* property — a quantity that depends on the path
taken through parameter space rather than on the state at a point. This item
asks which of those properties are worth representing, and where.

## The physics, briefly, because the term is used loosely

Transport a quantum state adiabatically around a closed loop in parameter
space and it returns with a phase that is not the dynamical phase: the Berry
phase, the integral of the Berry connection around the loop, equivalently the
flux of the Berry curvature through it. It is gauge-invariant, it depends only
on the *geometry* of the path, and when the loop encircles a degeneracy it is
quantised. Everything else in this item is a consequence:

- **Berry curvature** is a field over parameter space; over a Brillouin zone
  its integral is the **Chern number**, an integer counting chiral edge
  channels (see `chern-domain-memory-in-the-sheet-generator.md`).
- **Conical intersections** in molecular photochemistry are degeneracies in
  nuclear-coordinate space. A loop around one returns the electronic
  wavefunction with a sign flip — the geometric-phase effect — which changes
  branching ratios in nonadiabatic dynamics. This is the molecular-physics
  face of the same object, and it is where M. Boggio-Pasqua's work on conical
  intersections and photochemical funnels sits.
- **Vortices** are point defects of a complex order parameter in two
  dimensions: the phase winds by an integer multiple of 2π around a core where
  the amplitude vanishes. **Vortex lines** are the three-dimensional version,
  the core being a curve rather than a point — superfluid helium, type-II
  superconductors, Bose-Einstein condensates.
- **Skyrmions** are not phase defects but texture defects: a field of unit
  vectors (spins, most often) that wraps the sphere an integer number of
  times, the winding measured by a topological charge. They are particle-like,
  they cannot be smoothly combed flat, and that stability under deformation is
  the whole interest.

The unifying claim, and the reason to file one item rather than four: each is
an integer that survives continuous deformation. An integer that cannot change
continuously is exactly the kind of property worth storing, because it is
robust to the approximations in whatever calculation produced it.

## The classical analogy the user raised, and its limit

A Foucault pendulum at latitude λ precesses by 2π(1 − sin λ) per day. That is
a holonomy — parallel transport of a vector around a closed loop on a curved
manifold, here the rotating Earth's surface — and it is the classical
antecedent of the Berry phase, not a metaphor for it. Both are anholonomy: the
failure of a transported object to return to itself after a closed circuit.
The limit of the analogy: the pendulum's holonomy comes from the curvature of
a real spatial manifold, the Berry phase from curvature of a connection over
an abstract parameter space, and only the latter can be quantised by
encircling a degeneracy. Useful for exposition; do not let it drive the data
model.

## Where this could attach in Précis

Ranked by how load-bearing it would be, not by effort:

1. **Conical intersections in the reaction/pathway layer.** This is the one
   with real consequences for existing work. `catpath` and the reaction
   enumeration treat pathways as ground-state energetics. A photochemical step
   routed through a conical intersection has a branching ratio the energetics
   alone cannot give, and the geometric phase can suppress the very channel a
   naive surface-hopping estimate favours. If Précis ever ranks photochemical
   routes, ignoring this is a correctness problem, not a missing feature.
   Adjacent: `docs/backlog/neb-barriers-in-the-catpath-pipeline.md`, which adds
   kinetics to a thermodynamics-only pipeline — the same shape of gap.
2. **A topological-invariant slot on `dft_calculation`.** The existing
   `derived` subkey, populated by `derive_*` job types, is the right home: a
   Chern number or a Z2 index computed from the converged wavefunction is a
   derived property in exactly the sense `derive_adsorption_energy` already
   is. Same argument as
   `docs/backlog/bond-critical-points-in-structure-model.md`, which wants QTAIM
   critical points in the same slot — both are topology of a field that the
   calculation already produced and then discarded.
3. **Defect textures as structure content.** A skyrmion or a vortex line is a
   field configuration over a lattice, not a set of atomic positions. The
   structure kind has no representation for a per-site vector field, so this
   needs either a new kind or an accepted overlay. Do not start here.

## Sources in the corpus

From the Lemeshko talk at the OePG-CMD Joint Meeting 2026, the rotor systems
where these invariants are carried by molecular rather than electronic degrees
of freedom:

- Topological charges of periodically kicked molecules [pa448268].
- Anomalous multi-gap topological phases in periodically driven quantum rotors
  [pa448271].
- Molecular impurities as a realization of anyons on the two-sphere [pa448262].
- Rotor lattice model of ferroelectric large polarons [pa448266] — the
  variational state diagram whose phase boundaries are where an invariant
  changes.

For the Chern-number face of the same physics, the quantum anomalous Hall
references are gathered in
`docs/backlog/chern-domain-memory-in-the-sheet-generator.md`.

## Open questions

- Is a Berry-phase quantity a *property of a calculation* or a *property of a
  material*? The Chern number is basis-independent and belongs to the
  material; the Berry phase around a specific loop belongs to the loop, and
  the loop has to be stored with it.
- Conical-intersection geometries come from CASSCF-class methods that this
  stack does not run. Does supporting item 1 mean importing published
  geometries rather than computing them?
- Is there any existing consumer? If nothing in Précis would read a stored
  Chern number today, item 2 is speculative storage and should wait for item
  1's use case to be real.

## Owner

`src/precis_dft/jobs/derive.py`, `src/precis_dft/handlers/dft_calculation.py`
for item 2; the catpath reaction layer for item 1.
