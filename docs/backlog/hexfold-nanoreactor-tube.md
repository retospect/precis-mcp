---
status: in-progress
pillar: 3d-design
title: hexfold builds an instrumentable nanoreactor tube — a tube between two sheets with Y-junction attachment rails, H-inlet pores, and a site list
---

wip: slice 1 (rail tube, pores, site list) landed b7fcf3328 2026-10-13; next slice (reservoir sheets, armchair-rim opening solver) unassigned

# hexfold: instrumentable nanoreactor tube (T2 of the nanoreactor chain)

Reto, 2026-10-04. Umbrella: [nanoreactor-no-nh3](nanoreactor-no-nh3.md).
Same blocker as [cnt-channel-staged-catalysis](cnt-channel-staged-catalysis.md):
k = 3 Y-junction seams ([hexfold-seam-type-catalogue](hexfold-seam-type-catalogue.md)).

Landed (slice 1, `rail-k3-120-z`, 2026-10-13): the cross-section — a
closed tube of N arcs (N = 2–6) meeting at N equal-120 sp² seams along
the axis, each seam's third sheet an inward rib (no far-side fuse);
authored per-arc pores (one hexagon removed per cell, six open-valence
rim atoms on an 18-atom boundary, refused at a rim or another pore); and
the site list in `generated.sites` — every open-valence rib carbon and
every seam atom with (z, θ, inward normal), plus same-rib pairs in the
2.5–3.0 Å two-point pendant band; round-trips to the stored atoms within
0.1 Å.

Still needed from hexfold:
- Reservoir sheets at both tube ends, the gap between sheets left open
  as a reservoir.
- Several radii along the tube (existing joins/cones), with an optional
  outer Y-junction where a radius change would otherwise strain the
  wall.
- Relaxed geometry from the MLIP rung (carbon strain is in the regime
  MLIPs handle well), with per-seam strain reported.

test: a spec with one tube, two sheets, three inner rails and two pores
builds, checks clean, and its site list round-trips to (z, θ) within
0.1 Å of the relaxed atoms. (The `rail-k3-120-z` scene route already
covers the rails, pores and site list; the full spec still needs the
reservoir sheets and the radius-changing joins.)
