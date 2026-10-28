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
  at the step), not more margin. `step: cone` builds (193b023e0,
  fdc9087d4: p = rails cone, `step.cone.symmetry`, washer-zone tags) but
  its relaxed gate is open. Left: §"H2 cone gate follow-up" below
  (zigzag rims are the separate bullet above).
- **H3: relaxed geometry with per-seam strain** — landed 2026-10-19:
  the `hexfold_relax` job type (`ssh_node`, in-process like
  `struct_relax`'s rungs) runs the relax pipeline of
  [hexfold-relax-pipeline](hexfold-relax-pipeline.md) on the compute
  group — a whole-block polish on any relax-ladder rung (`ml` default;
  GFN-FF has no backend) plus `xtb` on H-capped, buffer-frozen junction
  fragments (`precis_se.atomic.relax_block.relax_block`), and stores
  `junction.strain`/`seam.strain` before/after on the ref's
  `meta.generated.relax` while the relaxed geometry becomes the next
  design version. Junction/seam bookkeeping persists at mint
  (`topology.junctions`/`seam_atoms`/`sigma_A`) so `tunnel_strain`
  re-scores without the stick seed. Mint via `put(kind='job',
  job_type='hexfold_relax', executor='ssh_node',
  parent_id=<structure ref>, params={'structure_ref_id': N})`. Prod
  relax jobs may run any time (Reto 2026-10-21).

test: a spec with one tube, two sheets, three inner rails and two pores
builds, checks clean, and its site list round-trips to (z, θ) within
0.1 Å of the relaxed atoms. (The `rail-k3-120-z` scene route already
covers the rails, pores and site list; `tunnel-reservoirs` covers the
reservoir sheets; the radius-changing joins are H2.)

## H2 cone gate follow-up (relaxed acceptance for `step: cone`)

Reto 2026-10-21: `step: cone` landed with its relaxed gate open (the
contract, geometry and refusals are in `src/hexfold/spec.md` §11.4 and
`precis-hexfold-help.md`); this item closes the gate. The in-test gate
`tests/test_se_tunnel_steps.py::test_stepped_cone_gate_after_relax` is
`xfail(strict=True)`.

**Measured 2026-10-20** (test container, `relax_block` EMT polish 30
steps, xtb stubbed): 16→8 cone outer seam max 2.09 Å / angle dev 20.4°,
inner 1.63 Å / 59.8° (over the 45° bar), washer zone 19 `geom.clash`
pairs + 8 `geom.nearest_not_bonded` atoms (uncapped). 18→8 washer: outer
1.69 Å / 23.9°, inner 1.56 Å / 35.7°, zone 25 + 4; 18→8 cone: outer
2.12 Å / 47.1°, inner 1.69 Å / 39.5°, zone 19 + 10. EMT is not a
credible judge here (200 steps degrade the seams to 4.6 Å).

**Landed 2026-10-21.** The gate fixture is 18→8 `rails: 3` for both step
kinds; `geom.summary` carries `zones` (per zone, uncapped clash pairs and
nearest-not-bonded atoms) and the xfail gate reads it. Re-measured after
the gr502249 tie-grid fix (EMT 30 steps, xtb stubbed): cone outer
2.00 Å / 46.8°, inner 1.66 Å / 35.1°, zone 22 + 12; washer outer
1.98 Å / 42.6°, inner 1.49 Å / 103.7°, zone 28 + 8.

**Left.**
- Judge after a real-rung relax: a `hexfold_relax` job (xtb fragments +
  `ml`/MACE polish) on the compute group. Reto 2026-10-21: prod hexfold
  relax jobs may run any time. Pass = per step `seam_bonds_max_A ≤ 3.0`,
  `angle_dev_max_deg ≤ 45`, no `step.overstrained`, zero zone clash /
  nearest-not-bonded under `step: cone`; the washer numbers are reported
  beside it. If the cone still fails after the real rung, report and stop
  (no margin widening); the next lever is p ≠ N or a wider slant.

**Not in scope.** Changing the cone construction before the real-rung
numbers are in. The host-vs-container build difference was gr502249:
a same-ray near-tie in `rim_loop_end`'s angular order flipped between
Accelerate and OpenBLAS and picked a different twist, cut and seam per
platform — fixed by rounding every solver branch key to a tie grid
(positions 1e-9 A, angles 1e-9 rad, costs 1e-6 A^2) with ordinal
tie-breaks; topology is now pinned by
`tests/test_hexfold/test_tunnel_steps.py::test_stepped_rail_tunnel_topology_is_deterministic`.

**Target.** `src/hexfold/check.py` (per-zone counts),
`tests/test_se_tunnel_steps.py` (fixture, gate reads the counts),
one `hexfold_relax` job per fixture via `put(kind='job',
job_type='hexfold_relax', …)`; no migration.

## Open questions

Ready-check 2026-10-20 (scope: H2 outer cone/Y transition + star-notch residual). Verdict: needs-work; resolved: `step: cone` landed 2026-10-21; the open gate is §"H2 cone gate follow-up".

- blocker: finish line names `examples/tunnel_two_steps.hx`, but that file (`hexfold/examples/`, NOT `src/hexfold/examples/`; the spec's `hexfold/examples/` path only resolves at the repo root) is the armchair `fuse{reducer}` grammar path (18->12->6, no rail stars). `tests/test_se_hexfold_h2.py` already pins it ERROR-free; the star-notch residual, `step.overstrained` WARN and 18->8 measurement live only in the `tunnel-reservoirs` rail-step scene (`src/precis_se/atomic/generators/tunnel.py`, `tests/test_se_tunnel_steps.py`, `src/hexfold/tunnel.py::tunnel_steps`). The finish line checks the wrong artifact; pick the rail-step scene (e.g. `tube.rail [[18,4],[8,4]]`) or state the .hx is a regression guard only.
- blocker: "with the cone transition selected" is undefined: no grammar token, scene key or default is named. Today the stepped rail accepts only `rail/rails/rows/pores` and `tests/test_se_tunnel_steps.py::test_stepped_unsupported_key_refused_by_name` pins `cone` as refused (that test must be rewritten, spec does not say so). Name the knob (e.g. `tube.transition: "cone"`, default washer vs default cone) and the refusal for armchair/.hx steps.
- blocker: "wedge excision + radial fuse" is not buildable from the text: which wedge (angle, count, site, per rail/notch), where it is cut (wide segment end? washer?), how the radial fuse is seeded, and whether it reuses `hexfold.defects` surgeries or `opening.py` stitching are all unstated. The stepped build is a flat annulus from the opening solver (`tunnel_steps`); the cone is a new geometry/abstraction, not a parameter. Needs a design paragraph (geometry, ring census, why it shortens the notch) before an unattended build.
- blocker: "washer zone" has no measurable definition. `test_stepped_rail_tunnel_builds` tolerates `geom.nearest_not_bonded`/`geom.clash`/`geom.contact.short` ERRORs anywhere (it asserts only the code set, not location). Define the zone (atoms of `reducer:*` junction records plus N bonds? distance from the step plane?) and the end state (zero such ERRORs in the whole report, then tighten that test to `errors == []`).
- blocker: acceptance is not stated as pinned checks. Required: relaxed vs unrelaxed? `seam_bonds_max_A <= 3.0`/`angle_dev_max_deg <= 45` are only computed on the stick preview in `tunnel.py` (lines ~600-665); the 18->8 figure comes from a relaxed preview with no fixture. State which geometry the finish line reads and which test asserts it (and that the 18->8 step, not the test's 18->8 `[[18,4],[8,4]]` only by accident, is the case; the code comment says 16->8, the spec 18->8: reconcile).
- blocker: no thresholds are pinned by any test: `_STEP_STRAIN_BOND_A`/`_STEP_STRAIN_ANGLE_DEG` have no test asserting the WARN fires or is absent. The finish line "no `step.overstrained` WARN" needs a test on the cone build and ideally one proving the WARN still fires on the washer build (so the bar is not vacuously met).
- blocker: scope bundles three deliverables (cone transition, notch-wall clash closure, zigzag rims) behind one "Left:" line, and the umbrella item is a multi-slice `in-progress` spec whose acceptance `test:` covers only slice 1. Zigzag (n,0) is excluded by the coordinator but the spec still lists it as Left; split into a cone-transition spec (with the notch closure as its acceptance) and leave zigzag separate. Also the "Deferred" vs "in scope" boundary is not written (no Not-in-scope section).
- advisory: the spec's claim that the cone "shortens the notch" so clashes close is unverified in the repo (no experiment or fixture); builder may find the ~1 A residual persists. Add a stop condition: if clashes remain after the cone, report and stop rather than widen margins.
- advisory: the H3 `hexfold_relax` job claim holds (landed per spec; `src/precis_se` relax path exists), but the finish line does not say whether the cone must pass through relax; state "stick preview only" or "after hexfold_relax" so the builder does not wire a job (prod relax timing is still an open Reto call).
- advisory: spec text references the external-repo layout (`hexfold/examples/tunnel_reservoirs.hx`, "Same blocker as ...") and states `Reto 2026-10-16 option (a)` without a link; refresh paths to the monorepo (`src/hexfold/`, `tests/test_hexfold/`, `tests/test_se_tunnel_steps.py`).
- advisory: model tier: new geometry/abstraction work; declare `model: opus` (front matter has none).
