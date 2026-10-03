---
status: idea
title: a carbon channel with a functionalized interior holds each intermediate of a staged reaction in one pose from pocket to pocket
pillar: 3d-design
prio: low
---

# Functionalized-interior carbon channel for staged catalysis

**Dormant.** Reto, 2026-10-03 (via review session): "needs more work on the
carbon side". It is blocked on hexfold building a three-arc cross-section
with inward ribs; the carbon-side asks go to hexfold-toolkit separately.
**No quest is minted until hexfold can build that cross-section** (Reto).
Later it feeds the catalysis quests. Reto's full brief:
`~/.claude/projects/-Users-reto-precis-mcp/scratch/review-msgs/cnt-channel-context-2026-10-03.md`.

## Motivation / why

A tube whose interior wall carries attached groups could pass an
intermediate from pocket to pocket and hold it, sterically and
electrostatically, in the pose each step needs, with no escape into bulk
solvent. A plain CNT interior is inert, so the attachment sites are the
point of the design.

## Structural concept

- The cross-section is curved graphene arc segments, not one rolled sheet.
  Arcs meet at 120° sp2 Y-junction seams running along the axis: two
  branches continue the wall, the third points inward as a short rib.
- Rib edges and Y-junction carbons are the attachment sites. Payloads:
  sugars as rigid four-site pendants (then functionalised: hydroxyls,
  stereochemistry, H-bonding), other small molecules, possibly lipids (one
  site, tail into the lumen).
- Each arc can carry different chemistry (one polar/negative, one greasy,
  one positive), which keys the intermediate into one rotational pose.
- Chemistry can vary along the axis (an electrostatic or hydrophobic ramp),
  so traffic moves one way.

## Design principles (agreed with Reto)

1. Substrate channelling exists in nature; this is an engineering problem,
   not an existence problem.
2. Cross-section pattern sets orientation; the axial gradient sets
   direction.
3. The main failure is clogging: (a) the intermediate sticks to the wall;
   (b) a side product is too big to go on or back out; (c) wrong-way or
   misfolded entry. Mitigations: a bland wall except at pockets; ratcheted,
   downhill energetics; gating so nothing enters until committed; an escape
   route so a stuck species leaves instead of jamming the line.
4. Short tunnels: most of the benefit arrives once pocket 1's exit sits
   inside pocket 2's capture radius.
5. In a sub-nm channel the wall is the solvent. Match the lining to the
   intermediate; expect at most a few ordered waters.
6. The final pocket binds more strongly than the tunnel floor (the exit is
   the deepest well), or the product will not leave.
7. Optional drive (parked): light-driven unidirectional rotors
   (azobenzene-derived, hemithioindigo, Feringa-type) as wheels or gates,
   speed set by LED intensity.

## Open questions

- Inner diameter against payload size: the lumen left after
  functionalisation, and the risk of self-clogging.
- Strain and stability of the 120° seams and inward ribs; rib-edge
  termination.
- Arcs per cross-section (3? 6?) against attachment density and keying
  resolution.
- How chemistry varies along the axis: segmented build or seam pattern.
- Gating at pocket boundaries.
- Synthesis: bottom-up from nanographene arcs, or functionalisation after
  the fact; whether the four-point sugar anchor is feasible.
- Whether the target chemistry needs two stages or more.

First step when it wakes: cross-section geometry (arc counts, inner
diameters, rib lengths), then which sugar, small-molecule and lipid pendants
fit without blocking the lumen, then the axial gradient and the pocket-1 →
pocket-2 handoff.

## Papers

Requested 2026-10-03 (fetch queue head):
- Channelling overviews: pa463573 Miles, Rhee & Davies 1999 · pa463574
  Wheeldon 2016 · pa463575 Liu 2017 (artificial electrostatic channelling).
- Tryptophan synthase (25 Å indole tunnel, allosteric gating): pa463584
  Hyde 1988 · pa463576 Mueller & Dunn 2022 · pa463577 Rhee 1996.
- Carbamoyl phosphate synthetase (~100 Å tunnel, deliberate clogging
  mutation): pa463578 Holden 1999 · pa463579 Thoden 2002 · pa463580
  Thoden 1997.
- Light-driven motors: pa179415 azoimidazolium 2025 · pa463581 Gerwien
  2019 · pa463582 Klok 2009 · pa463583 Kistemaker 2015 · pa345694 Klok 2008
  (Feringa MHz, held) · pa463588 Guentner 2015 (kHz, was not held,
  requested).
- Selectivity analogues: pa35139 Sui 2001 (AQP1: ar/R filter, NPA dipole;
  held) · pa463587 Zhou 2001 (KcsA filter, desolvation by the carbonyl
  wall) · pa265225 Szejtli 1998 (cyclodextrin cavities).

A read-for-question pass over these extracts the design numbers (tunnel
lengths and diameters, gating mechanisms, what clogged and why) into
findings the future quest starts from.

First pass, 2026-10-03, over the 4 papers held by 14:40Z:
- AQP1 (pa35139): fi464246–fi464250 and fi464252. They give the
  2.8 Å constriction at the start of a ~20 Å filter, the ~4 Å × 15 Å pore
  beyond it, the ar/R lining, the inward helix dipoles, four non-contiguous
  filter waters, and ten carbonyls along 25 Å.
- Tryptophan synthase (pa463584): fi464253 (the active sites are about
  25 Å apart) and fi464254 (the tunnel is indole-sized). Both rest on the
  abstract only.
- HTI motor (pa463588): fi464255 (ΔG‡ 13.1 kcal/mol, giving a 1 kHz
  maximum), fi464256 (405/490 nm and sunlight PSS), fi464257 (quantum
  yields) and fi464258 (16-year E half-life).
- pa345694 (Feringa MHz) is held as SI only, and pa463584 as its abstract
  plus references: gr464259. No hub covers gating or clogging yet; those
  wait on the carbamoyl phosphate synthetase (CPS) and KcsA papers.
- The other 14 were re-queued at 10:40Z and are still unfetched. Run a
  second pass when they land.

## Explicitly NOT in scope

- Minting a quest (held by Reto until the cross-section can be built).
- The carbon-side builder capabilities: hexfold-toolkit owns those.
