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
  at the step), not more margin. Left: the cone transition at a step,
  specified in §"H2 cone transition at a radius step" below (zigzag rims
  are the separate bullet above).
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
  relax timing is an open Reto call — no prod jobs submitted.

test: a spec with one tube, two sheets, three inner rails and two pores
builds, checks clean, and its site list round-trips to (z, θ) within
0.1 Å of the relaxed atoms. (The `rail-k3-120-z` scene route already
covers the rails, pores and site list; `tunnel-reservoirs` covers the
reservoir sheets; the radius-changing joins are H2.)

## H2 cone transition at a radius step (`step: cone`)

Reto 2026-10-20 (reviewed by chat, geometry corrected). Resolves the H2
"Left:" line above and the ready-check findings of 2026-10-20 below.

**Where it lives.** The stepped rail tube of the `tunnel-reservoirs` scene
feature (`src/precis_se/atomic/generators/tunnel.py`, composed by
`src/hexfold/tunnel.py::tunnel_steps` as `sheet | seg | washer | seg | sheet`).
Today every radius step is a flat annulus (the `fuse{reducer}` washer role,
`src/hexfold/menus.py::reducer_washer`) and the key `cone` is refused by name
in the tube-key validation (`tunnel.py` generator, "an outer cone/Y step
transition is not yet buildable"; pinned by
`tests/test_se_tunnel_steps.py::test_stepped_unsupported_key_refused_by_name`).
Not the armchair `fuse{reducer}` grammar path: `hexfold/examples/tunnel_two_steps.hx`
(18→12→6) has no rail stars and `tests/test_se_hexfold_h2.py` already pins it
ERROR-free.

**Contract.** The `tube:` block of a stepped rail tube gains one key,
`step: washer | cone`, default `washer` (today's behaviour, unchanged). With
`step: cone` every radius step `seg<w> → seg<n>` (wide rail count `w`,
narrow `n`, both N-arc ribbed segments) is bridged by a cone instead of a
flat annulus.

**Geometry (binding).** A graphene cone with `p` pentagons carries a
disclination of `p·60°`, so its apex half-angle is `sin α = 1 − p/6`
(p = 1 → 56°, 2 → 42°, 3 → 30°, 4 → 19.5°, 5 → 9.6°). The pentagon count
is NOT fixed by the radius ratio: a cone of any `p` connects any two radii,
the ratio only sets the slant length between the rims (axial length of the
transition = `(r_w − r_n)/tan α`). `p` is therefore a design parameter,
constrained by symmetry: the rail segments are N-arc ribbed (`rails: N`)
and the star outlines are N-fold, so the pentagons must sit N-fold
symmetric — `p` must be a multiple of N with `p ≤ 5`. v1 builds `p = N`
only (`rails: 3` → p = 3, half-angle 30°; `rails: 2` → p = 2; `rails: 4`
→ 4; `rails: 5` → 5); `rails ≥ 6` is refused by name as
`step.cone.symmetry` (p = 6 is flat, no cone exists). Construction: mint
the cone with the existing `_cone_defects`/`_cone_seed` of
`src/hexfold/build.py` (p pentagons equally spaced in azimuth, one per seam
azimuth of the wide segment), truncate it at the two slant distances whose
circumferences match the narrow and wide rail-star outlines, and stitch
both rims through the opening solver exactly as the washer is stitched
today (`opening_join`, defect rings where the lattices disagree, leftovers
to H-termination). Do NOT cut a wedge from the solver-minted annulus:
removing a `p·60°` wedge shrinks both rims by `(1 − p/6)`, so the annulus
would have to be minted oversized and the construction duplicates what
`_cone_seed` already does. Heptagons (`w < n`, reversed steps) stay out of
scope. The cone replaces the washer in the `tunnel_steps` composition;
`junction.strain` keeps the keys `reducer:seg<w>->seg<n>:outer|inner` so
`tunnel_strain` re-scores unchanged.

**Washer zone (measurable).** The set of atoms within one hexagon pitch
(2.46 Å × 1.05) of any atom of either reducer seam of that step (the
`outer` and `inner` junction records' seam atoms). `geom.clash` and
`geom.nearest_not_bonded` findings are tagged with the zone they fall in
(`zone: reducer:seg<w>->seg<n>` or `zone: none`) so a test can filter.

**Acceptance (gate).** Fixture: the `16→8` rail step of
`tests/test_se_tunnel_steps.py` (`tube: {rail: [[16, 4], [8, 4]], rails: 3,
rows: 2, pores: []}`, so p = 3; the earlier "18→8" is corrected to the
code's 16→8), relaxed in-test through
`precis_se.atomic.relax_block.relax_block` with tblite stubbed as
`tests/test_se_hexfold_relax.py` does, on a `periods`-short block. With
`step: cone`: `junction.strain` per step reports `seam_bonds_max_A ≤ 3.0`
and `angle_dev_max_deg ≤ 45` (the `_STEP_STRAIN_*` thresholds in the tunnel
generator), no `step.overstrained` WARN, and no `geom.clash` /
`geom.nearest_not_bonded` finding inside the step's washer zone; with
`step: washer` the same fixture still builds as today (the residual ERRORs
are allowed to persist there and are reported). `18→12` with `rails: 3`
builds (p = 3); `rails: 6` with `step: cone` is refused
`step.cone.symmetry` by name. The refused-by-name test is rewritten so
`cone` is accepted and an unknown step kind is still refused. Site labels
per segment and the per-step strain keys are unchanged (existing tests
stay green). Stop condition: if washer-zone clashes remain under
`step: cone`, report the residual and stop; do not widen margins.

**Explicitly NOT in scope.** Zigzag `(n,0)` rims for `opening(port=)`; the
star-notch residual under `step: washer` (the cone is the fix for it, the
washer is not changed); a Y-junction at a step (`src/hexfold/y_junction.py`
is a different primitive); heptagon-bearing cones (`w < n` reversed steps);
`p ≠ N` multiples; prod relax jobs (`hexfold_relax` timing is Reto's call,
unchanged).

**Target + blast radius.** `src/hexfold/tunnel.py` (`tunnel_steps`, a
`cone` role in place of the washer), `src/hexfold/build.py` (truncated cone
from `_cone_defects`/`_cone_seed`), `src/precis_se/atomic/generators/tunnel.py`
(`step` key, `step.cone.symmetry`, zone tagging), `src/hexfold/check.py`
(zone tag on the two geometry findings), `tests/test_se_tunnel_steps.py`,
`src/hexfold/spec.md` §11.4 and §16,
`src/precis/data/skills/precis-hexfold-help.md`. In-repo; no migration;
landing `--checked`.

## Open questions

Ready-check 2026-10-20 (scope: H2 outer cone/Y transition + star-notch residual). Verdict: needs-work; resolved 2026-10-20 by §"H2 cone transition at a radius step" (step key, p = N geometry, zone, gate, stop condition, scope).

- blocker: finish line names `examples/tunnel_two_steps.hx`, but that file (`hexfold/examples/`, NOT `src/hexfold/examples/`; the spec's `hexfold/examples/` path only resolves at the repo root) is the armchair `fuse{reducer}` grammar path (18->12->6, no rail stars). `tests/test_se_hexfold_h2.py` already pins it ERROR-free; the star-notch residual, `step.overstrained` WARN and 18->8 measurement live only in the `tunnel-reservoirs` rail-step scene (`src/precis_se/atomic/generators/tunnel.py`, `tests/test_se_tunnel_steps.py`, `src/hexfold/tunnel.py::tunnel_steps`). The finish line checks the wrong artifact; pick the rail-step scene (e.g. `tube.rail [[18,4],[8,4]]`) or state the .hx is a regression guard only.
- blocker: "with the cone transition selected" is undefined: no grammar token, scene key or default is named. Today the stepped rail accepts only `rail/rails/rows/pores` and `tests/test_se_tunnel_steps.py::test_stepped_unsupported_key_refused_by_name` pins `cone` as refused (that test must be rewritten, spec does not say so). Name the knob (e.g. `tube.transition: "cone"`, default washer vs default cone) and the refusal for armchair/.hx steps.
- blocker: "wedge excision + radial fuse" is not buildable from the text: which wedge (angle, count, site, per rail/notch), where it is cut (wide segment end? washer?), how the radial fuse is seeded, and whether it reuses `hexfold.defects` surgeries or `opening.py` stitching are all unstated. The stepped build is a flat annulus from the opening solver (`tunnel_steps`); the cone is a new geometry/abstraction, not a parameter. Needs a design paragraph (geometry, ring census, why it shortens the notch) before an unattended build.
- blocker: "washer zone" has no measurable definition. `test_stepped_rail_tunnel_builds` tolerates `geom.nearest_not_bonded`/`geom.clash`/`geom.contact.short` ERRORs anywhere (it asserts only the code set, not location). Define the zone (atoms of `reducer:*` junction records plus N bonds? distance from the step plane?) and the end state (zero such ERRORs in the whole report, then tighten that test to `errors == []`).
- blocker: acceptance is not stated as pinned checks. Required: relaxed vs unrelaxed? `seam_bonds_max_A <= 3.0`/`angle_dev_max_deg <= 45` are only computed on the stick preview in `tunnel.py` (lines ~600-665); the 18->8 figure comes from a relaxed preview with no fixture. State which geometry the finish line reads and which test asserts it (and that the 18->8 step, not the test's 18->8 `[[18,4],[8,4]]` only by accident, is the case; the code comment says 16->8, the spec 18->8: reconcile).
- blocker: no thresholds are pinned by any test: `_STEP_STRAIN_BOND_A`/`_STEP_STRAIN_ANGLE_DEG` have no test asserting the WARN fires or is absent. The finish line "no `step.overstrained` WARN" needs a test on the cone build and ideally one proving the WARN still fires on the washer build (so the bar is not vacuously met).
- blocker: scope bundles three deliverables (cone transition, notch-wall clash closure, zigzag rims) behind one "Left:" line, and the umbrella item is a multi-slice `in-progress` spec whose acceptance `test:` covers only slice 1. Zigzag (n,0) is excluded by the coordinator but the spec still lists it as Left; split into a cone-transition spec (with the notch closure as its acceptance) and leave zigzag separate. Also the "Deferred" vs "in scope" boundary is not written (no Not-in-scope section).
- advisory: the spec's claim that the cone "shortens the notch" so clashes close is unverified in the repo (no experiment or fixture); builder may find the ~1 A residual persists. Add a stop condition: if clashes remain after the cone, report and stop rather than widen margins.
- advisory: the H3 `hexfold_relax` job claim holds (landed per spec; `src/precis_se` relax path exists), but the finish line does not say whether the cone must pass through relax; state "stick preview only" or "after hexfold_relax" so the builder does not wire a job (prod relax timing is still an open Reto call).
- advisory: spec text references the external-repo layout (`hexfold/examples/tunnel_reservoirs.hx`, "Same blocker as ...") and states `Reto 2026-10-16 option (a)` without a link; refresh paths to the monorepo (`src/hexfold/`, `tests/hexfold/`, `tests/test_se_tunnel_steps.py`).
- advisory: model tier: new geometry/abstraction work; declare `model: opus` (front matter has none).
