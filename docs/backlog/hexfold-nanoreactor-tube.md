---
status: idea
pillar: 3d-design
title: hexfold builds an instrumentable nanoreactor tube — a tube between two sheets with Y-junction attachment rails, H-inlet pores, and a site list
---

# hexfold: instrumentable nanoreactor tube (T2 of the nanoreactor chain)

Reto, 2026-10-04. Umbrella: [nanoreactor-no-nh3](nanoreactor-no-nh3.md).
Same blocker as [cnt-channel-staged-catalysis](cnt-channel-staged-catalysis.md):
k = 3 Y-junction seams ([hexfold-seam-type-catalogue](hexfold-seam-type-catalogue.md)).

What the nanoreactor needs from hexfold:
- A circular tube running between two graphene sheets, the gap between
  sheets left open as a reservoir.
- Several radii along the tube (existing joins/cones), with an optional
  outer Y-junction where a radius change would otherwise strain the wall.
- Inner Y-junction seams along the axis as attachment rails, at 3–4
  angles.
- Pores at given z: real openings (vacancy rings of 8 or more atoms), not
  heptagons, which are closed to H2.
- A site list as output: every attachment carbon with (z, θ, inward
  normal), its seam, and pairs of sites that can anchor one two-point
  pendant.
- Relaxed geometry from the MLIP rung (carbon strain is in the regime
  MLIPs handle well), with per-seam strain reported.

test: a spec with one tube, two sheets, three inner rails and two pores
builds, checks clean, and its site list round-trips to (z, θ) within
0.1 Å of the relaxed atoms.
