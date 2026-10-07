---
status: draft
title: Ideal smooth surface first, then tile it — the surface is fixed, the tiler only follows
pillar: 3d-design
prio: high
---

# Ideal smooth surface first, then tile it

Reto, 2026-10-03 (via nanobuds-paper-25): he likes the `hexa-smooth-drum`
design, but its smooth surface looks non-ideal, as if the tiler had
co-optimised it. He wants the ideal surface defined on its own (a flat
sheet, a fillet of a chosen radius into the tube, a chosen radius into the
ball or cap), and the tiler then tiles that fixed surface. The tiler works
backwards from the ideal shape and does not bend it. This is the path to a
smooth graded flange, and the nanobuds hero (dr173020) waits on it.

## Motivation / why

The code never moves the target surface: `drum_meridian` returns a frozen
`Meridian`. Reto's reading is still right in effect, for two reasons.

- **The target's shape comes from the tiler's needs, not from authored
  radii.**
  - The foot and the flare are catenoids (zero mean curvature, chosen
    because they tile).
  - The fillet radius is the *largest icosahedral-table radius that fits*
    (`precis_surface.revolution.drum_meridian`, `rho = max(fits)` over
    `fillet_candidates`).
  - Nobody chose these radii, and the author cannot.
- **The atoms leave the target.**
  - `rowfit.realise` lofts rows about 7 % sparser than graphene.
  - The relax tether is deliberately weak (`smooth_drum._RELAX_K_SURFACE =
    0.01`), so atoms sit a mean 0.7 Å off the surface with bonds
    1.31–1.68 Å.
  - Pinning them (k 0.2, mean 0.17 Å) stretches bonds to 1.2–1.9 Å.
  - The rendered carbon is therefore a compromise surface, not the target.

The fix is to make the surface an authored, analytic object and to judge
the tiling by how closely it follows that surface, with no trade-off knob.

## In scope

1. **Surface spec (S1).**
   - Analytic meridian pieces with authored radii:
     - flat sheet;
     - torus fillet of radius `R_f` into a cylinder of a lattice tube's
       radius;
     - cylinder;
     - fillet or sphere cap of radius `R_c` into a ball or lid;
     - a stepped neck as two fillets.
   - Several axisymmetric features on one flat sheet, each with its own
     axis, so the hero scene can be written down: sheet, pill, bump, and a
     pillar with its ball.
   - A deviation metric, run on any structure: atom distance to the
     surface (mean, 95th percentile, max per region), and bond spread.
   - First use: run it on today's hero5 and on `hexa-smooth-drum-v2`, to
     put numbers on "non-ideal".
2. **Defects from the surface (S2).**
   - For each annulus, the integrated Gaussian curvature fixes the defect
     charge (Gauss–Bonnet). A sheet→tube fillet carries −2π, which is 6
     heptagons, spread over rows where the curvature accumulates.
   - Output is authored-defect lists (gr459928's format, Horizon 11's
     "solving").
   - Defects are C_k symmetric on each row.
   - Straight parts are exact `(n, m)` tubes (as in
     smooth-drum-engineered-mode.md).
3. **Tile and pin (S3).**
   - Build the hexfold net from S2, seed it on the surface, then relax it
     with a strong normal tether.
   - The surface does not move. Strain shows up in bond and angle numbers,
     not as drift off the surface.
4. **Hero render (S4).**
   - The hero scene from the S1 spec, built through S2–S3, rendered with
     the surface overlay.

## Explicitly NOT in scope

- Non-axisymmetric features (a saddle bridge between two features). Each
  feature has an axis, and the sheet is flat between them.
- Choosing the radii for the author. S1 takes them as input and reports a
  radius that cannot be tiled, rather than snapping it.
- Retiring the organic `smooth_drum` mode: it stays as the reference.

## Acceptance criteria

Every deviation bar is measured after removing a rigid z-offset and
nothing else. There is no fitted rotation or scale, which would let the
judge co-optimise the surface again (orchestrator, S1 verdict 2026-10-03).

- S1, **built** (`revolution.authored_meridian`, `arc_curvature`,
  `precis_surface.deviation`; the judge is exact on lines and arcs to 1e-9):
  - `hexa-smooth-drum-v2` is a mean 0.70 Å off its own target. The flats
    are 0.14–0.28 Å off, the bends 0.67–1.10 Å, and the flare max is
    4.96 Å.
  - hero5a's feet are 0.16–0.30 Å rms off a best-fit fillet of
    1.25–3.0 Å. They are faithful to a one-ring turn Reto ruled out.
- S3 tether, **built** (`stick(net, tether=, k_tether=)`, opt-in, byte-
  identical when off; `precis_surface.deviation.surface_foot`). Under the
  tether the deviation column is small by construction, because the judge
  and the spring target the same surface. The evidence of a sound carbon
  net is therefore the columns the tether does not act on: bonds, angle
  rms and max, and pyramidalisation (C60 is 12°). On the 3+3 foot,
  measured as fillet-zone mean / max:
  - (12,0), R = 5, k_t ≥ 0.2: bonds 1.385–1.470 Å, pyramidalisation
    about 2°, deviation 0.061 / 0.160 Å.
  - The pillar (6,0), R = 3, k_t = 1.0, which is 3 heptagons on a 6-atom
    rim: bonds 1.366–1.467 Å, angle rms 1.70°, pyramidalisation 7.9°,
    deviation 0.042 / 0.169 Å.
- S3 planner, **built** (`precis_se.atomic.generators.authored_foot.plan_foot`).
  - k is not chosen by fitting R. The narrowest frustum that builds wins
    at every R tried, `k_min(n)` = the smallest even k ≥ n/3 + 2, and
    R is carried by the hexagons outside the outer heptagon row.
  - The planner still measures each candidate from k_min to k_min + 6.
  - Regression anchors, all meeting every bar:
    - (12,0) R = 8 → k = 6;
    - (6,0) R = 3 → k = 4 (pillar);
    - (24,0) R = 8 → k = 10 (pill).
  - At k_min, R reaches about 3–8 Å inside the bars. Past about 12 Å the
    outer hexagons stretch past 1.50 Å.
  - **(18,0) is an open row, cause unknown.** It crumples (pyramidalisation
    > 88°) at k = 8, 10 and 14. k = 12 at R = 8 comes closest, with bonds
    1.345–1.512. The next probe re-seeds the relax from a different
    phase, to separate a seam-phase fault from a folded minimum.
- S3 on one feature (sheet → `R_f` fillet → (n,0) tube):
  - atom-to-surface mean ≤ 0.10 Å, max ≤ 0.3 Å;
  - bonds 1.36–1.50 Å;
  - the census integrates to −2π per foot, and the rows sit where the
    build can place them (3+3, narrowest frustum). The bars, plus the
    ring-ideal angle and pyramidalisation columns, judge the result.
    This replaces "census matches Gauss–Bonnet per annulus", which a 3+3
    foot cannot meet (orchestrator, planner verdict, 2026-10-03). The
    per-annulus form is the next rung below; it has been moved, not
    dropped;
  - defects C_k symmetric within 0.1 Å.
- Next rung: per-annulus Gauss–Bonnet rows.
  - 2+2+2 or 1×6 rows placed where the curvature accumulates.
  - This needs the irregular hole (3,3,6,3,3,6) and tube-wall surgery
    (Risks, gr459928).
  - It is the route to R ≳ 12 Å, where the narrowest 3+3 frustum
    stretches bonds past the bar.
- Changing `R_f` changes the built shape, and the deviation stays within
  the bar. That is the "tiler follows the surface" test.
- S4: the hero scene builds with no `geom.seed_overlap`, no ERROR
  `geom.clash`, and the S3 bars per feature; a render goes to the
  nanobuds-paper thread.
  - **Scene built** (`authored_foot.plan_scene`; judged on the tethered
    coordinates through `hexfold.check.Relaxed`, `geom.summary.relax`
    = `tethered`). The hero with a (12,0) R = 8 pill (pillar (6,0)
    R = 3 + C60, bump (12,0) R = 5 + lid, [2+2] bud; 2898 atoms, 36 s)
    meets all of it:
    - no ERROR;
    - clash_min 1.05 Å;
    - every foot inside the bars, with fillet max ≤ 0.17 Å, bonds
      1.367–1.485 Å and pyramidalisation ≤ 10.7°.
  - The (24,0) R = 5 pill meets every bar after the relax, but its k = 10
    frustum is seeded onto the sheet (3 × `geom.seed_overlap`, gr464358).
  - Tops are reported, not barred (`ScenePlan.tops`). A free (6,0) + C60
    neck is about 60° pyramidalised on its own. The tube's last 4 bonds
    below a ball relax untethered: holding them to the cylinder folds the
    neck to a 0.83 Å clash.
  - Hole cells whose sheet seam gains 5–7 pairs are refused (gr464341).
  - Rendered by nanobuds-paper; Reto's pick between the two pills is
    pending.
  - The render shows the pillar's (6,0)→C60 neck at 8 non-bonded pairs
    of 1.05–1.33 Å. The neck predates S4: hero5 has 8 pairs from 1.053 Å,
    and a free tube+ball has 7 pairs at 1.07–1.23 Å.
  - MACE-MP small opens the fused (6,0)→C60 joint (gr464391): for k = 0–3,
    4 of its 6 seam bonds go to 4.7–4.9 Å, both free and inside the scene
    with the feet pinned. That is not yet a verdict on the joint.
    - The same protocol also opens the literature-stable [9-6] nanobud on
      (10,10), its control.
    - It holds the [2+2] control (seam bonds 1.654 Å) and opens a [2+2]
      bud on the (6,0) sidewall (2.14 and 2.49 Å).
    - GFN2-xTB agrees on all three cases:
      - the fused neck opens 4 of 6 seam bonds to 4.59–4.77 Å;
      - [2+2] on (10,10) holds at 1.578 Å;
      - [9-6] on (10,10) opens, to 2.27–3.21 Å.
    - An MLIP and tight binding agreeing makes a shared artefact unlikely,
      so the fused (6,0) neck is not a bonded joint. The failing [9-6]
      control points at hexfold's [9-6] construction (gr464405).
    - The hero's pillar is Reto's call through nanobuds-paper item 31: a
      (12,0) lid pillar, or the stick neck shown as an idealised model. No
      relaxed top ships.

## Target + blast radius

### R15 — authored sphere reach correction (gr469873)

Reto's 2026-10-06 ruling: membership uses the full radial extent described
by the authored target, including the sphere radius beyond an initial
meridian `r0`. `Feature.reach` is the maximum radial coordinate of the
meridian. Line/catenoid maxima are at endpoints; circular arcs also include
their radial maximum when that angle is within the authored sweep. No grid,
atom fitting, threshold, new target field or schema. Sheet ownership is the
complement; expanded feature discs retain the existing overlap refusal.
`surface_foot` and `surface_distance` share this same rule.
An existing feature with an empty meridian explicitly refuses evaluation
with ValueError in both judge paths; absent target geometry must not become
zero reach, false sheet matches, infinite distances or zero normals. An
empty feature list remains a valid sheet. This is direct-kernel validation;
the public parser already refuses empty pieces and is unchanged.

Pinned replay is the exact native ball12 request in
fleet-state/inbox/hexfold-dogfood-curved-ready.json: sphere radius
10.067387222435341 Å exceeds r0=9.69732218899367 Å. Atom aC343 at
[9.47976239,-8.68156672,9.06851434] Å must be owned by q and measured
against its spherical arc, rather than the sheet (~31.50 Å). Permanent
kernel and registered public-get regressions pin this call; analytic sweep,
boundary and expanded-overlap cases protect the membership rule.

Independent review precedes root integration/full gate/version/deploy.
After exact deployment, same-owner native ball12 remeasurement and an export
replay compare whole-scene fraction within0.5 Å to 414/2406 (17.20698%).
That replay preserves the reconstructed/fitted-target provenance limit;
it does not recover the original generation target. Fresh different-radius
dogfood sphere plus cylinder/saddle is Melchior-only after deployment,
stored under se:hexfold-dogfood-*; authenticated viewer/access and supported
bounded compute remain prerequisites. No workstation build, model/science,
private-k3 public extension, hero regeneration or hold changes.

### R13 — S1 read-only exposure (bounded implementation)

S1's judge is built: `precis_surface.deviation.surface_distance` + `summary`
already produce mean/p95/max and ownership per region. Current source
`c731e2fa4033193c0c78a849ac544ca62dfd9500` has no SE surface-deviation view;
stored block reads show S3/top planner records, not an S1 measurement of current
bound atoms against an explicitly authored target. This is the user-visible
gap; do not rebuild the judge or turn planner numbers into new measurements.

API: `get(kind='se', id=<design>, view='surface_deviation',
args={'name':<ordinary bound block>, 'target':{'features':[...]},
'z_offset_A':0})`. `name` uses existing block/uid addressing. Read stored
bound structure atoms in their structure-local Å frame; do not apply SE pose,
rotation, scale or world-frame transforms. Each target feature has unique
`name` (not `sheet`), `centre_A:[x,y]`, `r0_A`, and `pieces` of
`['line',length_A]` or `['arc',radius_A,turn_deg]`, passed to the existing
`authored_meridian`. A feature owns its disc; outside is the plane z=0.
An explicit `features:[]` is a sheet-only authored request, not an inferred
target. This request supplies the authoring definition, **not proof of the
original generation target**; report that provenance explicitly. Do not infer
from generated scene parameters, smooth-drum's derived meridian, atoms or a
best-fit surface. No target replacement/snapping, no fitted rotation/scale.

The finite `z_offset_A` is the only allowed alignment, subtracted by the judge
(default zero; no fit). Report exact supplied offset, units Å, region atom
count/mean/p95/max, source design/block/bound structure version and target
origin `caller-authored request; original target provenance unverified`.
Lines/arcs use the existing analytic distance path; no curvature, bar or
stability threshold is introduced. No synthesis/report persistence or jobs.

Omitted target, missing/dangling binding or empty/nonfinite stored coordinates
returns an honest unknown result, never zeros/PASS. Invalid authored target,
duplicate names, unknown keys, nonfinite inputs or overlapping feature discs
returns actionable BadInput using existing judge validation. Template/array
instances are unknown: this slice does not guess placement/target frames.
Missing/blank selector cannot list all boards/designs as a substitute.
The registered public get refuses id None/empty/whitespace/'/' specifically
for this view with a complete id/name/target correction; ordinary SE listing
remains unchanged. Source provenance includes SE slug and block UID where known.
The owning store's narrow read-only `structure_positions_snapshot(ref_id)`
returns identity/version/lattice/live fractions from ONE SQL statement. Label
and coordinates both use that row snapshot, not `structure_load`/separate ref
metadata. The earlier version bracket is rejected: concurrent imports can
rewrite cell/atoms at the same supplied version. Missing/unverifiable snapshot
identity/version returns unknown/retry before metrics; no automatic retry or
later-label repair. No writer/schema/global-pool/isolation rewrite is involved.
Canonical DB coverage commits a same-version rewrite after the statement is
evaluated but before result retrieval: the read retains coherent old cell/atoms,
and a following statement reads coherent new cell/atoms at that SAME version.

Fixtures: local deterministic atoms on a vertical authored cylinder plus
sheet atoms with known normal offsets. Assert nonzero mean/p95/max, exact
rigid-z removal and absence of rotation/scale fitting; use existing analytic
S1 tests as kernel regression. Instrument build/relax/persistence/job boundaries
and preserve input target/coordinates. Unknown/malformed/empty cases and SE
get dispatch/args rejection and same-version read coherence require focused
canonical tests/types/Ruff.
No hero/r4 regeneration, live relax, k3 solver, science, provider or threshold
change. Native exact-deploy/same-owner read dogfood follows root's reviewed
integration/deployment; local fixtures are not generated scientific evidence.
Code review must precede merge. Preserve H1 branch/scratch; R13 branch is
`work/hexfold/r13-authored-deviation` is preserved. Source-review corrections
use `work/hexfold/r13-s1-review-fixes` from the original S1 commit, without
the separately held k3 work, in the existing isolated task worktree.

- New: a surface-spec module, which may live in `precis_surface` beside
  `revolution`.
- New: the curvature→defect-row placer, which emits hexfold authored
  defects.
- Changed: hexfold's relax (stick), which gains a surface tether.
- Not touched: the organic `smooth_drum` generator.

## Risks

- S2 needs defects on tube walls and in frustum rows. gr459928 found that
  the 2+2+2 grading needs an irregular hole (3,3,6,3,3,6) that the grammar
  lacks, and that tube-wall surgery is unbuilt. If S2 cannot place a row
  where the curvature asks for it, the bar is missed there, and that shows
  up as deviation, not as a moved surface.
- A fillet tighter than about one ring row cannot be followed by any
  tiling. S1 reports the smallest radius each row pitch can follow.

test: tests/hexfold/test_ideal_surface.py (new); tests/test_precis_surface_*.py
## R17 stored authored-target read — gr470905

Assignment: window9, isolated `work/hexfold/stored-surface-target` from
`7265b9ac0502cd007affdae26f61256877b547d9`. Coordinator accepted the future-generation prerequisite after independent
contract review at docs pin dfa52780. This is authorized implementation,
not source PASS or a deployed feature. Existing kernel,
explicit-target semantics and n18/n24 sphere budget refusal stay unchanged.

Requested API: `get(kind='se', id='<design>', view='surface_deviation',
args={'name': '<block>'})` may use the matching persisted **actual** generated
target when `target` is omitted. An explicit caller `target.features`,
including an empty sheet-only list, remains the override. Never substitute
requested `top_R` for selected `plan.top_plans[name].R`, fit coordinates,
rebuild, relax or infer a successful target from old trial measurements.

### Checked premise: persisted frame is missing

Native gr470905 and a real public read of `se:hexfold-dogfood-r5`,
`cyl12open` UID211, reproduce the missing-target boundary. The r5 cylinder
binds `st470904`; the sphere ball12 is UID209. Native served source is the
R16 deployment, while this task is based on staged R17; exact local source
is used after native Python search/symbol discovery, not assumed identical.

`authored_foot._relax_scene` builds its target using seed-derived feature
centres and, for authored tops, a seed-junction-derived dome start `z1`.
`hexfold_scene.build_hexfold_scene` persists scene inputs and plan rows,
including actual selected R/fillet/drop, but neither those centres/z1 nor
the complete evaluated target and its coordinate frame.

`hexfold_spec._block_from_net` then invokes `_canonical_frame`: a PCA-based
rotation, centroid translation and z shift determined by the relaxed
coordinates. Only transformed coordinates/envelope are returned; that
transform is not persisted. `generate.generated_record` has no frame key.
The existing web `surface_meridian` seam is emitted by smooth_drum, not
hexfold_scene, and is a display polyline, not an exact scene target.

Therefore scene/plan alone does **not** identify the target in the stored
structure-local frame. Existing r5 blocks cannot honestly supply positive
matching metrics through the requested read-only/no-fit/no-rebuild path.
This was the premise blocker; the approved future-generation receipt
contract below resolves it without repairing legacy data. Missing frame stays explicitly
unavailable with a corrective hint; it must not yield misleading numbers.

### Approved future-generation prerequisite

At generation time persist the exact evaluated feature meridians/centres
and a rigid stored-to-target frame map, using the transform already applied
by canonicalization, plus generated structure identity/version and an
unambiguous geometry binding. No schema change or second generation run.
Reading must retrieve target provenance with atoms/cell/version from the
existing one-statement snapshot, not an independent current-meta read.
Revisions, same-version reimports, identity changes or unproven provenance
must become unavailable. Default derivation is allowed only when that
binding and frame are complete. Numeric judge remains unchanged: evaluate
in the recorded target frame and label stored/target units, SE pose and
alignment precisely. Do not claim a caller-authored or world-frame target.

Chosen contract: capture exact evaluated Features and the actual
canonicalization inverse once, with row-vector `y = x @ Q + b`,
`Q = R @ F`, `b = (c - t @ R) @ F`, `F = diag(1,1,-1)`. Reflection is
required, not fitted or recomputed. A versioned analytic line/arc codec
retains feature order, centres, dome-start effect and actual selected R.
Finite orthogonality/isometry checking uses only a documented 1e-12
serialization-roundoff bound; no physical threshold changes.

Persist receipt and geometry in one outer `finish_generate` transaction
using `structure_save(conn=...)`. Bind ref/version, cell/PBC, ordered live
atom row ids/fractions and receipt contents with SHA256 of canonical UTF-8
JSON: sorted keys, compact separators, exact finite `float.hex()` strings,
integers unrounded, signed zero normalized to positive zero. No per-atom
metadata copy. Byte-identical row replacement invalidates by row identity.
Return binding inputs/provenance from the existing one-statement snapshot;
never read current generated metadata independently. Controlled stamping
failure rolls back structure and receipt. The separate SE-tree-save orphan
boundary remains documented and unchanged.

Only absent `target` chooses stored receipt; explicit null is BadInput.
Explicit target, including features=[], retains the caller local-frame
semantics. Stored mode uses only its recorded map: nonzero z_offset_A
requires an explicit target with a typed correction. Legacy r5 or any
missing/malformed/nonfinite/unsupported/stale receipt is unavailable with
explicit-target/new-generation guidance, never numeric zero or PASS. No
legacy fitting, PCA on read, rebuild, backfill or relaxation.

Independent reviewer /root/review_temperature_precision CONTRACT ACCEPTED
dfa52780 (not source PASS); immutable review.md SHA256
ed44a8013ba22ee4c9aedb7e48882e8078ec4b80f6f48e3e75e960e293cbc6d4
and review.json 018c75ceb7509bc22a9a36e5656c611adab767a3c2dec31f1a0993aefe38e94d.
After actual DRY END at 20:58Z, the owning atomic package rationale is refreshed
in this slice. The package map and atomic lifecycle text are reconciled with
the split docstrings at main 1c412f327584a2e81df0c81d4a5bd9f07022dc07,
keeping the stored-target rationale only in atomic. Executable ASTs are unchanged.
Root integration must retain the split module-owned notes and revalidate at
its returned post-DRY pin; earlier staged-base checks do not prove that gate.

### Acceptance and replay after the prerequisite

Use deterministic persisted fixtures through the registered public get
path: sphere, other requested-radius sphere with distinct actual selected
R, open cylinder, explicit caller override, missing/ambiguous target,
identity/revision mismatch and same-version interleaving. Assert correct
actual R, frame, atom counts, units, provenance and no writes/jobs. Existing
caller z-only semantics stay unchanged. Kernel-only tests are insufficient.
Canonical scoped tests/types/Ruff or exact permitted remote check verdict
must be recorded; never retry the known 9 GB container preflight refusal.

Postdeploy native checklist: r5 ball12, ball12r11 and cyl12open default reads,
explicit override, honest unavailable legacy provenance and mismatch
boundaries; exact runtime, UID/structure/version and metrics/context captured
durably on gr470905 with full native readback. Human Reto/orchestrator report
that these blocks look decent and were spaced 16 nm after an initial
pose-zero overlap is attributed human workflow evidence only. This owner
did not execute the folds/browser. gr469873 reach remains DONE; viewer
gr459593 and larger-pillar budget changes are separate. No live folds,
covered DRY paths or main landing here. Source implementation and focused
deterministic checks are authorized under the six reviewed clauses above.
