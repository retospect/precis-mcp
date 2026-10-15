---
status: draft
pillar: 3d-design
title: hexfold seam vertices — joining three or four seam lines at an atom
prio: medium
model: opus
blocked-by: hexfold-integration
---

# hexfold: seam vertices (sp³ seam lines shipped)

Reto, 2026-09-16: "we want to add the sp3 join variant in the backlog
also, i think that can join 3 or 4 sheets. We'll add it after the rest
is done." Spec context: `src/hexfold/spec.md` §6.2–6.4 (the sp² seam
model, per-sheet χ, and the 0.2 exclusions), §11.3 (`seam … atoms=sp2`;
`sp3` is the reserved value).

**Shipped (this branch, 2026-10-13):** the sp³ seam *lines*. Both rows
are built privately in `src/hexfold/sp3_seam.py` and routed through
`hexfold_scene` as exclusive feature types `k4-sp3-90-a` (four
armchair-rimmed sheets at 0/90/180/270°, one degree-four sp³ atom per
period, alternating up/down sheet bonds → the catalogue's squashed
tetrahedron 104.48°×4 + 120°×2) and `k3-sp3-z` (three zigzag-rimmed
sheets at 120°, seam atoms paired into a dimer chain along the axis
carrying the fourth bond; even period counts only; seeded at the
xTB-relaxed seam lengths, seam angles 106.54°×3 + 112.24°×3).
Wrong rim type is `port.mismatch`; unequal dihedrals, odd
k3 period counts and k ≥ 5 are `fit.unsolvable` before build. Seam ring
censuses are derived from the built graphs and recorded in
`hexfold-seam-type-catalogue.md`'s row table. Public `.hx` grammar was
not extended — the construction is private, exactly like the Y and the
fins. What remains in this item is the vertex, not the line.

**Added 2026-10-14:** `k3-sp3-a` (scene feature of the same name;
`hexfold.sp3_seam.straight_k3a`) — three armchair-rimmed sheets at 120°
on TWO seam atoms per armchair period, one per dangling class, chained
cross-period. Free GFN2-xTB (Mac-local, the `a-full` model) holds the
authored graph; it is the all-consumed counterpart of the `k3-sp3-z`
chain. Census 3·(n−1) hexagons + 3·(n−1) decagons per seam; literature
calls the armchair-ribbon junction a 6-6-6 triple — the hexagon set
matches, the decagons are the corridor faces the label does not see.

## Scope (remaining)

- **Seam vertices.** Points where seam curves meet (Plateau's second law:
  four lines at the tetrahedral angle). Excluded in 0.2 because the atom
  there needs four sheets. The pyramidal join of four planes is
  `cone(P)` with creased edges plus one seam vertex at the apex.
- Extend `seam`'s consistency check and the per-sheet χ rule to the
  vertex case; decide whether seam-vertex atoms get their own path-ID
  class. (The line rows already carry per-sheet χ residuals and
  `seam.rings` through the private path.)

## Prior art — checked 2026-10-13

- Krainyukova & Zubarev, *Phys. Rev. Lett.* 116, 055501 (2016),
  doi:10.1103/PhysRevLett.116.055501 — the deposited carbon honeycomb:
  zigzag-terminated graphene-nanoribbon walls interconnected through
  single carbon atoms along the junction lines, proposed **all-sp²**.
- **Erratum**, *Phys. Rev. Lett.* 118, 029901 (2017),
  doi:10.1103/PhysRevLett.118.029901 — following the DFT instability
  analysis (all-sp² junctions relax to sp³; RSC Adv., doi:10.1039/
  C6RA27833G), the junction lines carry **sp³ bonding**: two atoms per
  junction cell alternately shift toward/away from their neighbours
  along the line. That alternation is exactly the `k3-sp3-z` dimer
  motif — **verified correspondence**.
- Diachenko & Krainyukova, *Low Temp. Phys.* 48, 232–238 (2022) —
  honeycombs "built from graphene nanoribbons connected by sp³-bonded
  carbon junction lines", both wall chiralities modelled. Follow-up
  band-structure work names the zigzag-ribbon junction a **5-5-8** ring
  triple and the armchair one 6-6-6; the built `k3-sp3-z` census
  (3·n/2 pentagons + 3·(n/2−1) octagons per seam) is the same 5/8
  family — **consistent, not yet matched cell-for-cell**.
- `k4-sp3-90-a` (four sheets at 90°) has **no verified literature
  correspondence**: the PRL's four-wall crossings were the sp² variant.
- **Direct dynamical evidence, 2026-10-13 (coordinator):** free
  GFN2-xTB relaxes (tblite, Te 1000 K, FIRE fmax 0.05) of both the small
  equal-120 sp² Y (`hexfold_scene` `k3-sp2-120-z`, sheet [8,8],
  H-terminated, 572 atoms, converged in 120 steps) and the 120° seam
  tube did **not** keep the planar sp² seam. The seam atoms paired into
  dimers along the axis at 1.60–1.64 Å, sheet bonds stretched 1.42 →
  1.50–1.53 Å, and each seam atom pyramidalised (angle sum 360 → ~341°):
  the xTB minimum of the three-sheet 120° seam is the `k3-sp3-z` sp³
  dimer chain — the real seam atom is a flattened tetrahedron. The built
  row seeds at those lengths (dimer 1.60 Å, sheet 1.51 Å, gap 3.32 Å).
  Coordinates `~/.claude/projects/-Users-reto-precis-mcp/hexfold-corner/
  relax1013/{y-a,seam-out}.after.extxyz`, report `y-a.json` (read-only).
  Finite-segment artefacts ignored: three guard-column H crowding at
  each seam end, two guard carbons bonded across sheets at the start.
  The public pass pins seam atoms and neighbours at this seed, so the
  reported seam/rim angle tables are the held registration, not relaxed
  evidence; H caps landing inside another atom's 1.0 Å neighbourhood on
  converging rims are dropped (`terminate.dropped`), leaving bare edges.
- Still open: comparing the sp² octagon census (§6.2, derived only)
  against the all-sp² junction described in the PRL.

## Acceptance (remaining)

- One seam-vertex example (three seam lines meeting) builds, or the
  spec records precisely why it cannot on the hex lattice.

## Seam vertices: the box corner cannot be a regular 90° corner (2026-09-27)

The rectangular box is the seam-vertex test piece (6 sheets, 12 creases,
8 corners; creases are k=2, K=0, geometry not topology — the bend is the
smooth layer's). Before designing the vertex, the counting law fixes what
a corner may carry, and the answer is not the obvious one:

- Closed trivalent net, genus 0: `P − H = 6χ = 12`. A cube has 8 corners
  and `12/8` is not an integer, so **eight identical corners with
  defect-free edges do not exist**; the residual check would correctly
  refuse it.
- Two symmetric ways out, both shape changes: (a) 12 pentagons, corners
  alternating 2 and 1 in a tetrahedral pattern, no heptagons — but a
  2-pentagon corner (2π/3) is sharper than 90° and a 1-pentagon corner
  (π/3) flatter, so the object is a tetrahedrally distorted cube;
  (b) 3 pentagons per corner (24) plus exactly 1 heptagon per edge (12),
  `24 − 12 = 12`, full cubic symmetry — but every edge then carries a
  heptagon, so the edges are seams with charge, not creases.
- General statement: a corner's angular defect is quantised in π/3, so
  **shape is quantised, not only dimensions** — a 90° corner is not on the
  menu, only multiples of 60°. The feasibility conversation (spec §25.2)
  should return the nearest realizable corner type the way it returns the
  nearest realizable radius.

Consequence for this item: the seam-vertex motif has to be designed per
corner *type* (1, 2 or 3 pentagons meeting three creases, or a 3-pentagon
vertex meeting three charged edges), not as one "box corner". Pillbox and
rectangular box both enter the example set once the vertex exists (Reto,
2026-09-27: "just examples, fine to add them to the set").
