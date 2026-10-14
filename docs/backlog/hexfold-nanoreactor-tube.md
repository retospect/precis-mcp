---
status: in-progress
pillar: 3d-design
title: hexfold builds an instrumentable nanoreactor tube — a tube between two sheets with Y-junction attachment rails, H-inlet pores, and a site list
---

wip: slice 2 (reservoir sheets, armchair-rim opening solver, junction
sites + validation) landed 2026-10-14; next slice H2 (several radii along
the tube + optional outer Y-junction) unassigned

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

Landed (slice 2, `tunnel-reservoirs`, 2026-10-14): the closed tunnel —
sheet | tube | sheet via the armchair-rim opening solver
(`hexfold.opening`): cut a hole shaped by the rim's projected polygon
(twist × scale scan), stitch the rim's dangling atoms to the hole
boundary in cyclic order, mint defect-ring faces (pentagons through
nonagons where the lattices disagree), leave leftover boundary atoms for
H-termination. Armchair rims only; a rail tube's ribbed end follows the
star outline (seam-gap faces that cannot close are reported
`opening.face.open`, never faked). Non-armchair (n,m) refuses by name.
`generated.sites` labels every junction atom (fused / defect /
unbonded) plus rail-tube lumen and seam sites with (z, θ, normal) and a
`to_stored` frame; `junction.strain` per mouth reports seam-bond and
face-angle residuals; `termination.covered` + `geom.contacts` gate the
stored block. Grammar special case kept in
`hexfold/examples/tunnel_reservoirs.hx`.

Still needed from hexfold (ranked):
- **H2: several radii along the tube**, via existing joins/cones, with
  an optional outer Y-junction where a radius change would otherwise
  strain the wall — the catalysis staging wants diameter steps as the
  binding-pocket analogue.
- **H3: relaxed geometry with per-seam strain** — MLIP/GFN-FF relax of
  the assembled tunnel (tblite is wired for the `xtb` rung but absent on
  this host), reporting strain per seam and junction; a 7k-atom preview
  build relaxes in seconds under stick but a GFN2-xTB pass is hours, so
  this belongs to a dedicated compute slice, not a generation op.

test: a spec with one tube, two sheets, three inner rails and two pores
builds, checks clean, and its site list round-trips to (z, θ) within
0.1 Å of the relaxed atoms. (The `rail-k3-120-z` scene route already
covers the rails, pores and site list; `tunnel-reservoirs` covers the
reservoir sheets; the radius-changing joins are H2.)
