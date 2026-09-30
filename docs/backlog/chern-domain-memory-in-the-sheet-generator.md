---
status: idea
pillar: 3d-design
---

# Arbitrary memory storage written into a gate-defined Chern domain pattern

IDEA. The hexfold sheet generator builds graphene/hBN-family layer stacks as
geometry — lattice, twist angle, stacking register, seam. It has no notion
that a stack can *hold state*. This item asks whether the generator should be
able to emit a stack whose purpose is storage, with the stored bit being the
sign of the Chern number in a gate-addressed region rather than a charge or a
magnetic moment.

## Why this is more than an analogy

The physical ingredients are all demonstrated, not speculative:

- A gate pulse flips the magnetisation, and with it the sign of the Chern
  number, in an orbital Chern insulator — non-volatile (the state survives the
  gate returning to zero) and directional (the polarity of the sweep chooses
  the final state). Polshyn et al., Nature 588, 66 (2020) [pa448535].
- The mechanism has a theory: voltage-controlled magnetic reversal in orbital
  Chern insulators, Zhu, Su and MacDonald, PRL 125, 227702 (2020) [pa448557].
- Reading a bit does not require reading the bulk. At a boundary between two
  regions of opposite Chern number a quantised one-way channel runs along the
  wall, so the domain *pattern* is the readout path, not just the data.
  Yasuda et al., Science 358, 1311 (2017) [pa448520]; manipulation of chiral
  interface states in a moire quantum anomalous Hall insulator, C. Zhang
  et al., Nature Physics 20, 951 (2024) [pa448559]; electrical switching of
  edge-current chirality, W. Yuan et al., Nature Materials 23, 58 (2024)
  [pa448555].
- The Chern number in a moire stack is set by carrier density and
  displacement field — two independent gate knobs — rather than by growth,
  which is what makes an *arbitrary* pattern addressable at all. Serlin,
  Tschirhart et al., Science 367, 900 (2020) [pa448517]; G. Chen et al.,
  Nature 579, 56 (2020) [pa448518].

So a written domain map is simultaneously the stored data and the wiring
diagram: the conducting interconnect is drawn by the same gates that store the
bits, and it is dissipationless and one-way where it runs.

## What the generator would have to gain

1. A **layer-stack descriptor** richer than geometry: which layers are gates,
   which are dielectric (hBN), which is the active twisted pair, and which is
   a proximity source. The talk's stack was top gate / hBN / monolayer
   graphene / bilayer graphene / WSe2 / hBN / bottom gate, where the WSe2 is
   present only to induce spin-orbit coupling the graphene lacks (Island
   et al., Nature 571, 85 (2019) [pa448579]).
2. A **gate-region layer** — a 2D map over the sheet, not a 3D structure —
   naming which patches are held at which (n, D) and therefore which Chern
   number. This is the thing that carries the stored word.
3. A **domain-wall graph** derived from that map: nodes where walls meet,
   edges with a chirality. This is the natural handle for a route/pathway kind
   and is what a downstream simulator or a device netlist would consume.
4. Enough for a **capacity estimate**: bits per unit area given a minimum
   domain size set by the gate pitch and the magnetic coherence length.

## The honest objection, which must go in the item

Every result above is at millikelvin. The talk's measurements were taken at
about 20 mK. Orbital Chern insulator gaps in moire graphene are of order a few
kelvin at best and the quantised plateaus are far colder than that, so this is
dilution-refrigerator memory, not memory. That does not make the generator
work pointless — the same descriptor serves any study of these stacks — but
the item must not be written as if a room-temperature device were in reach.
The open physics question, and the one worth tracking, is whether any
higher-temperature platform (rhombohedral multilayer graphene on hBN, moire
transition-metal dichalcogenides) lifts the scale.

## Open questions

- Does the gate-region map belong in the structure kind, or is it a separate
  kind that *references* a structure? A gate pattern is not atoms.
- Is the domain-wall graph a `route`/`pathway`, or its own thing?
- Where does the generator stop — does it emit the stack only, or also the
  gate pattern needed to write a given word?
- Minimum writable domain size is the whole capacity argument and nobody in
  the cited work optimises for it; is there a number anywhere?

## Owner

The hexfold sheet generator; related in-tree: `docs/backlog/hexfold-sp3-seam.md`,
`docs/backlog/structure-layering.md`.
