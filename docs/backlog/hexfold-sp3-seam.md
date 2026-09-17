---
status: draft
title: hexfold sp³ seam lines and seam vertices — joining three or four sheets at an atom
prio: medium
model: opus
blocked-by: hexfold-integration
---

# hexfold: sp³ seam variant + seam vertices

Reto, 2026-09-16: "we want to add the sp3 join variant in the backlog
also, i think that can join 3 or 4 sheets. We'll add it after the rest
is done." Spec context: `src/hexfold/spec.md` §6.2–6.4 (the sp² seam
model, per-sheet χ, and the 0.2 exclusions), §11.3 (`seam … atoms=sp2`;
`sp3` is the reserved value).

## Scope

- **sp³ seam line.** A seam atom with four bonds: one into each of four
  sheets, or three sheets plus a bond along the seam chain. Four
  half-planes meeting on one line put the four bonds in a plane at 90°,
  which is not tetrahedral — so the four-sheet variant needs either
  alternating seam atoms along the line or sheets that do not all meet
  on the same axis. That is the design question this item answers first.
- **Seam vertices.** Points where seam curves meet (Plateau's second law:
  four lines at the tetrahedral angle). Excluded in 0.2 because the atom
  there needs four sheets. The pyramidal join of four planes is
  `cone(P)` with creased edges plus one seam vertex at the apex.
- Extend `seam`'s consistency check, `seam.rings`, and the per-sheet χ
  rule to the sp³ case; decide whether seam-vertex atoms get their own
  path-ID class.

## Prior art to check first (cite with DOI, per the sources rule)

- Carbon honeycomb: Krainyukova & Zubarev, *Phys. Rev. Lett.* 116, 055501
  (2016), doi:10.1103/PhysRevLett.116.055501 —
  graphene walls joined along lines; both sp³ and sp² junction variants
  are studied in it and its follow-ups. Which papers treat which variant
  is unverified.
- The sp² octagon census derived in spec §6.2 must be compared against
  the sp² honeycomb junction before the sp³ one is designed.

## Acceptance

- A three-sheet sp³ seam and a four-sheet sp³ seam each `check` clean
  with residual 0 per sheet, and their seam census matches the literature
  object they correspond to.
- One seam-vertex example (three seam lines meeting) builds, or the spec
  records precisely why it cannot on the hex lattice.
