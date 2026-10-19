---
status: in-progress
pillar: 3d-design
title: hexfold builds an instrumentable nanoreactor tube — a tube between two sheets with Y-junction attachment rails, H-inlet pores, and a site list
---

wip: slice 2 (reservoir sheets, armchair-rim opening solver, junction
sites + validation) landed 2026-10-14; H2 radius step landed as
`fuse{reducer}` 2026-10-16; H2 multi-radius tunnel (reducers + `opening(port=)`,
per-junction `junction.strain`) landed 2026-10-16; the outer Y-junction
option is unassigned

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
- **Zigzag `(n,0)` rims for `opening(port=)`**: armchair `tube(n,n)` ports
  build today; zigzag and chiral ports are refused as
  `opening.rim.unsupported`.
- **H2: several radii along the tube**, via existing joins/cones, with
  an optional outer Y-junction where a radius change would otherwise
  strain the wall — the catalysis staging wants diameter steps as the
  binding-pocket analogue. The radius step itself is built: `fuse{reducer}`
  (Reto 2026-10-16 option (a), coaxial 5/7 ring), e.g. (12,12)→(6,6)
  with 12 pentagons + 12 heptagons. Landed: the multi-radius tunnel,
  `opening(port=)` composing with `fuse`/`fuse{reducer}` lines
  (`examples/tunnel_two_steps.hx`: sheet | 18 → 12 → 6 | sheet), with a
  `junction.strain` read per opening mouth and per reducer seam
  (`opening:<sheet>`, `reducer:<wide>-><narrow>:outer|inner`). Landed: the
  stepped rail tube in the `tunnel-reservoirs` scene feature —
  `tube: {rail: [[rows,periods],…], rails, rows, pores:[[seg,arc,u,v],…]}`
  composes N-arc ribbed segments with a solver-minted washer per step
  (the `fuse{reducer}` role: flat annulus, narrow rim stitched into the
  hole, wide rim onto the outer edge; rail ends are stars so the `cap`
  registry cannot fuse them), sites labelled per segment
  (`seg<i>.strip<k>`), `junction.strain` per step as
  `reducer:seg<w>->seg<n>:outer|inner`. A step over-strains when the
  read's `seam_bonds_max_A > 3.0` or `angle_dev_max_deg > 45` (measured
  on a relaxed 18→8 rail step; WARN `step.overstrained`). Known residual:
  the star-notch walls at a step seam relax to ~1 Å unbonded neighbours,
  so `geom.nearest_not_bonded`/`geom.clash` ERRORs appear in the washer
  zone — closing those needs the cone transition (it shortens the notch
  at the step), not more margin. Left: build the outer cone/Y transition
  the WARN names (wedge excision + radial fuse, refused by name until
  then), the notch-wall residual above, H3 relax, and zigzag rims.
- **H3: relaxed geometry with per-seam strain** — MLIP/GFN-FF relax of
  the assembled tunnel (tblite is wired for the `xtb` rung but absent on
  this host), reporting strain per seam and junction; a 7k-atom preview
  build relaxes in seconds under stick but a GFN2-xTB pass is hours, so
  this belongs to a dedicated compute slice, not a generation op.
  The strain read is in place: `tunnel_strain` (in
  `precis_se.atomic.generators.tunnel`) takes coordinates plus the
  junction/seam bookkeeping only and emits `junction.strain` per mouth
  and `seam.strain` per rail seam (incident bond lengths, 120° angle
  deviation, pyramidalisation), so a relaxed geometry can be re-scored
  without the stick seed. Left: run the xtb/MLIP relax on the compute
  group and wire `tunnel_strain` onto the relaxed atoms.

test: a spec with one tube, two sheets, three inner rails and two pores
builds, checks clean, and its site list round-trips to (z, θ) within
0.1 Å of the relaxed atoms. (The `rail-k3-120-z` scene route already
covers the rails, pores and site list; `tunnel-reservoirs` covers the
reservoir sheets; the radius-changing joins are H2.)
