# PCB preview connectivity and selection

Reto approved this next step on 2026-11-04 after inspecting the latest
TAZ leaf previews. Make the viewer agree with actual copper before measuring
new router search moves. Programme: [pcb-platform](threads/pcb-platform.md).

## Scope and acceptance

- `gr565255`: airwires join electrically disconnected copper components,
  using the shared connectivity result. A plane-connected PV1 must not
  receive an internal airwire when three other GND pads lack fanouts.
  Preserve real missing connections, layer spans, source-copper geometry,
  scoping, highlight and selection handles. Unknown geometry must remain
  visible as unknown rather than an inferred connection.
- `gr565256`: hiding a layer excludes its objects from pointer selection,
  hover and overlapping-object selection. An instance fallback hit follows
  its represented
  artwork's visible layers, including bottom-mounted parts. Keep a part
  selectable while some represented layer remains visible. Verify this
  with the existing JavaScript test harness and renderer tests.
- Diagnose and fix the actual GND fanout failures on the saved JLC poses
  (2x C14.2/C3.2/C4.2), preserving placement locks, supplier bindings,
  footprint geometry, clearance and the final no-new-failure gate.
- Audit footprint identity through intake, including the 1x extra-pad
  report `gr564928`. The old production-reference renderer's larger
  generated silk is separate from supplied package geometry. Retain
  existing local vendor-silk work; do not silently change fabrication rules.

Use focused regressions that exercise real copper components and visible
selection. Inspect and reuse existing APIs before introducing a parallel
connectivity implementation. Use the reviewed hole-aware contact correction
from candidate 626b1b962; a bore void must never establish a plane connection.

The bounded direct-fresh fallback at 39ca8c3d5 passes 314 affected cases,
full types and Ruff. A C14-only saved-board check now emits a new
0.45/0.15 mm via at (218.5240972086252,64.764225007), with a 5.810269 mm
stub and 5.540269 mm outside its own pad copper, below the unchanged
10 mm limit. The actual shared planter certifier and existing whole
stub/barrel/outline/join/span checks pass. The source and sealed inputs
remain hash-identical. This reconstructs the pre-fanout design and mesh
against final saved routed copper/pours; the exact intermediate route state
was not saved. It establishes the focused connection, not a new full-board
route, fabrication approval or production update. Receipts live under
`scratch/review/c14-direct-probe-1104/candidate-39ca8c3d5-fixed-compose/`.
The first attempt failed before the worker because its probe generator
materialized the wrong Compose project name; it is preserved separately.

## RF placement acceptance

The saved source leaves RF passive translations free (`fixed='rot'`);
the production import's full locks preserve its inspection snapshot. The
matching shunts must be considered with their neighbors when seeking short
ground access outside the antenna keepout. Preserve original position locks,
rotations, sides, part bindings, clearances and hard distance constraints.
An old authored seed is a proposal, not a verified RF reference layout.

The bounded seed probes are under `scratch/review/rf-seed-probe-1104/`.
They show local fanout access but fail placement, so they authorize no route
or manufacturing claim. The one joint 2x pass removes placement DRC errors,
but fails the 0.20 mm pair gate and retains nine violated hard measures.
Require all original hard measures and placement gates to pass before routing
a candidate; an unchanged count of old violations is insufficient. Seek a
coupled constraint-neighborhood proposal rather than increasing the ground
escape length cap to hide the placement problem.

A read-only C10 repair trace at c1a970cf5 examines all 64 generated
candidates: each closes C10's own distance measure, 61 overlap fixed U1,
and the remaining three conflict with X1 (one also with C8). The existing
blocker-clearing pass runs; increasing its candidate limit is not a remedy.

The original X1 orientation makes the U1-only subproblem infeasible under
the current canonical courtyards. Its two 3 mm bounds imply center
`x <= 198.9494`, while east-side nonoverlap requires `x >= 199.0488`;
west, north and south branches also contradict necessary distance bounds.
Astra independently derived a minimum possible worst pin distance of
3.0994 mm at fixed rotation. This is reservation-model infeasibility, not
physical-package impossibility.

The supplier parser's pad-and-graphic envelope is documented as a hint,
then promoted to an authored body claim by
`src/precis/pcb/ir.py::footprint_body_bboxes`. X1's cached asymmetric box
matches the silkscreen centerline extrema. The observed layer-99 rectangle
is not independently certified mechanical geometry. Do not remove body
occupancy, shrink margins or substitute that rectangle without a verified
geometry contract; doing so could restore the documented PV1/L1 collision.

A reviewed proposal rotates the whole X1 footprint to 270 degrees and
places its center at (198.6004,54.55), retaining pin identities, both 3 mm
bounds and all current geometry. The two distances are 2.5009 and
1.839495 mm; its local canonical legality passes against saved neighbors.
Reto's November 5 continuation approves this source orientation change. Keep
`fixed='rot'` at the new angle. Other placement failures remain,
so this proposal alone passes no full-board gate. Read-only diagnostics,
input hashes and the review are in `scratch/review/rf-repair-trace.json`,
`rf-x1-model-review.json`, and `astra-geometry-review-1104.md`; the
reproducible rotation preview is `rf-x1-rotation-preview.py`.

The continuation starts from a copy of the saved joint 2x proposal, applies
only the reviewed X1 angle and center, and rechecks the original component
flags, pad/net identities, all hard measures, courtyard gaps and placement
DRC. Preserve immutable prior inputs and receipts. A bounded pure placement
attempt may translate originally free parts, with the seven RF seed parts
temporarily held and all other angles/sides held. Start with the same single
2,000-evaluation budget and seed as the earlier joint probe; record calibration
evaluations separately. Validate the output using the authored flags rather
than the temporary holds, preserving every original position lock. The X1
and C11 corrections below are the only source angle changes. Do not change manufacturing
rules, footprint bodies, the escape cap or distance limits. Diagnose remaining
rejections before spending more search. Route only a candidate that passes
every original hard measure and placement gate; no application deployment,
production write or new matrix follows from a placement-only result.

The independent remaining-pair audit also proves C11/U1 infeasible at the
saved C11 angle of zero degrees: the least possible DEC1 distance outside
U1's canonical courtyard is 2.3989 mm, exceeding its unchanged 2 mm limit.
As part of the resumed local board design, correct C11's source orientation
to 180 degrees clockwise and start at (189.25,54.80), retaining top side,
`fixed='rot'`, pad 1 to DEC1 and pad 2 to GND. Astra independently verifies
distance 1.830500003 mm, courtyard gap 0.1768 mm, and no saved-neighbor
blocker. This whole-instance rotation assumes no interchangeable or
nonpolar terminals. It changes no manufacturing margin, limit, footprint
geometry, U1 pose or engine default. The single-part witness is not a
joint placement or routed decoupling-quality proof. Preserve the audit
and proposal artifacts under `scratch/review/rf-constraint-audit-1105/`
and `scratch/review/astra-rf-constraint-review-1105.md`.

The one 2,030-evaluation continuation (2,000 search plus 30 calibration)
finishes with zero placement errors and zero pair-gap violations, and closes
C10's hard measure. Six hard measures remain: C7/U1, C13/U1, both X2/U1
clock connections and C1/X2/C2/X2. Do not route this rejected candidate.
Receipt: `scratch/review/rf-approved-x1-1105/placement-2ffb51cb/worker/`.

One bounded joint constraint proposal may now use the existing SciPy MILP
solver, without changing engine defaults. Translate the originally free
parts near U1 and all free hard-measure operands; keep the RF seven,
original fixed parts, other angles and sides held. Use canonical transformed
courtyard bounding rectangles as conservative proposal geometry, with
disjunctive pair separation and the existing hard-pair gap exemptions.
An inscribed 128-sided polygon may approximate each upper-distance disk,
so proposal geometry is stricter rather than looser. Retain a small positive
separation reserve for numerical robustness. Minimize movement from the
rejected pose and cap the solve at 60 seconds. Reject unsupported hard
measure or travel-lock shapes rather than silently omitting them. The
unchanged authored-flag inspector, exact pad geometry, all hard measures
and final placement gates decide acceptance; solver feasibility alone is
not a board certificate. No router, matrix or production writes occur until
that original gate passes.

Independent analytic review then proves X1 at 270 degrees and C7 at 90
degrees jointly infeasible even without an extra courtyard gap. The
four-part audit extends this to X1/U1/C7/C10 with every capacitor quarter
turn, under the old silk-derived body claims. These certificates describe
the old reservation model, not the physical PCB; do not spend more search
on that known contradiction.

For a bounded local correction of gr565552, compile manufacturer-backed
maximum package BODY dimensions into a derived copy of the legacy
`courtyard.bbox` input: C307331 and C1546 are 1.05 by 0.55 mm, C18213028 is
1.70 by 1.30 mm. These are body dimensions, not assembly courtyards. Preserve
all original rows and evidence; preserve supplier pads, drills, graphics,
pin maps and centroid exactly. Preserve the existing union with actual pads,
0.40 mm offset, pair gaps, distance goals and every other rule. The nominal
centered frame is a documented engineering inference supported by the
supplier's symmetric pads and matching nominal closed graphic, not a mounted
body-position tolerance certificate. Do not extrapolate by package UUID.
Affected instances are C5/C7/C11/C30, C10 and X1 only.

Astra's guarded local-adapter review is
`scratch/review/astra-body-claim-contract-review-1105.md`; frame audit and
original capacity certificates are in `scratch/review/rf-constraint-audit-1105/`.
Manufacturer evidence is archived in `scratch/review/rf-body-evidence-1105/`.
Fresh IR must confirm unchanged complete pads and actual union/offset
geometry before use. The original importer and shared catalog remain
unchanged. With that corrected input, test a source proposal restoring X1's
original zero-degree angle and orienting C7 at zero degrees; retain C11 at
180 degrees, all three rotation-locked. No other angle or side changes.
Only an independently passing joint-placement gate may proceed to a route;
this input correction grants no production mutation or manufacturing approval.

## Subsequent experiment

After this correctness pass, compare existing search, indirect blocker
closure, directional bundles, and their combination on bounded known-legal
synthetics with equal charged search budgets. Keep final DRC and acceptance
unchanged. Regional pours are a later controlled option; adding physical
layers requires an explicit stackup choice. No application release or
manufacturing order is part of this item.

The bounded physical witnesses and current-search observations are in
[pcb-joint-corridor-experiment](pcb-joint-corridor-experiment.md). Fresh-start
capacity success does not exercise a specified blocked incumbent; require
that state to be observed before claiming a blocker-closure recovery test.
