---
status: draft
title: SE inspection, DRC and nonbonded proximity
thread: se-3d-viewer
pillar: 3d-design
---

# SE inspection, DRC and nonbonded proximity

3D remains the primary inspection surface; preserve useful projections and
existing links. Authored planes, sheets, details and scientific/patent styles
are specified only in [se-technical-drawing-sheets.md](se-technical-drawing-sheets.md),
the canonical drawing DRAFT. No route retirement or drawing implementation
is approved. This spec owns current inspection, DRC presentation and the
bounded nonbonded proximity review.

## Verified capabilities

- `precis_web.blocktree_svg::build_block_draws` projects posed envelope
  meshes as convex hulls. `project_point` supplies x→yz, y→xz, z→xy.
  `/se/{slug}/2d` and `view.svg` retain level/isolate/override controls;
  these are envelope projections, not sections or realized-solid drawings.
- `precis_web.static/blocktree-3d.js` exports the canvas as PNG; its SVG
  wraps that bitmap and vector scale-bar/axes annotations. It cannot supply
  vector atom geometry or editable technical linework.
- `precis.viz3d` already supplies vector primitives, cameras, depth sorting,
  stick figures and scale bars. `se-view-figures.md` owns the pending figure
  recipe wiring; reuse must be evaluated there before adding a renderer.
- `precis_web.routes.blocktree_view::_bound_structure_atoms` places bound atoms in
  world Å using the existing metres seam. `_strain_arrays` and the client's
  `applyStrain` operate on each block's bonds/angles, not nonbonded pairs.
- `hexfold.check::_clash_pairs` excludes modeled bonds and shared bonded
  neighbors (1–3 pairs), and finds close pairs within its supplied net.
  Structure preflight has its own clash
  floor. Neither alone establishes a general cross-block assembly check.

## Approved template slice — local-complete / integration-ready

The approved template slice labels navigation **Envelope projections**,
describes envelope/hull scope on the projection page, and labels the 3D
export **SVG snapshot**. URLs, controls, fallback and export implementation
are preserved. The exact three-file template/rationale diff was reviewed
by the coordinator; no se-print backend overlap. This slice is locally
complete and ready for integration, with patch-version metadata in its
separate source commit. Deployment and real-design browser dogfood remain
pending; this does not ship the drawing, DRC or proximity proposals.

## Authored drawing seam

Use [se-technical-drawing-sheets.md](se-technical-drawing-sheets.md) for the
canonical authored sheet concept, premise-check/reuse limits and unresolved
revision/frame/section/projection/renderer decisions. Keep its figure recipe
coordination with `se-view-figures.md` there. This inspection spec's separate
cross-block proximity review below remains authoritative for diagnostics.

## Print dialog seam

`print-file-scale.md` owns route/backend changes. Consume its proposed
`print/{block}.json` fields `real_size_mm`, `suggested_scale`, `min_scale`,
`models`; submit `?scale=&model=` and display 422 `min_scale` / 409 reasons.
Do not duplicate scale/floor math in JavaScript. Await se-print confirmation
of eligibility discovery (current `print_files` is fdm-only), model-specific
floor retrieval, and unavailable atom models before implementing the dialog.
Coordinator contract request queued 2026-10-04; se-print not registered then.

## Validation / browser dogfood

Focused existing blocktree route tests and Ruff for the template slice;
coordinator schedules full-suite/type checks and integration. After the
coordinator announces a verified deployed SHA, open real designs
`unicycle-c1` and `hexfold-dogfood-r4` in a browser. Expected: default 3D;
Envelope projections opens preserved `/2d`, all three axes work and caption
names envelope scope; SVG snapshot opens with the current scale bar/axes.
Record SHA, input handles, expected/actual and screenshots. Actual: pending
deployment; no claim of deployed behavior or of clash detection.

Local template validation passed after coordinator-authorized escalation:
`scripts/test tests/precis_web/test_blocktree_view.py -n0`: 61 passed;
`uv run ruff check .` and `uv run ruff format --check .`: passed (2557 files).
The initial sandbox gate/cache/Docker denial was not an auto-review
rejection. Source is unchanged since those checks; no tests were repeated
for commit preparation and no full suite run. The combined integration
candidate owns full type/release gates. Deployed browser dogfood is pending.
Both linked planning specs retain `status: draft` and all implementation holds.

## gr466345 — owner / repro / proposed remedy (triage only)

**Owner:** SE-viewer17 owns inspection/DRC presentation triage. Atomic-core
owns gr464342's generated-report roll-up; coordinate the shared handler
seam before either implementation. No fix implementation is authorized.

**Repro:** native Precis reads on 2026-10-04, same `hexfold-dogfood-r4`:
`view='validate'` returns 0 errors, 3 warnings: `unconnected_port` for
`ball12.s_rim` and `pill12r.s_rim`, plus `undeclared_interpenetration` for
`ball12—pill12r`, overlap `2.36472e-09 m` (2.36472 nm). `view='drc'`
returns `✓ no DRC findings` and envelope/scenario context only. Current
served SHA was not independently checked; gr466345's original repro
records e0b75bdc7c4b. Read gr464342 and both gr466345 follow-up comments.

**Trace:** `SeHandler.get` dispatches these views separately.
`SeHandler._render_validate` collects `se_validate.validate(tree)`, then
store-aware port/atomic checks. `validate::validate` emits the overlap as
a warning; it is an envelope finding, not an atom-pair result.
`handler::_render_drc` collects `drc::drc` plus precedent, kinematics,
region-pin and chain findings, but never collects those base validator
warnings. Its empty-list branch unconditionally prints the check-mark
headline. Viewer `_se_block_findings` separately uses the base validator
with a 3 s budget, so it is not the DRC result source either.

**Smallest proposed remedy:** in the handler response seam, scope the
headline to "No findings from DRC checks" and append a separately labeled
"Envelope/scaffold validation" summary plus its existing error/warn rows
from `se_validate.validate(tree)`. On this input: DRC 0 findings;
envelope/scaffold validation 0 errors, 3 warnings, with the overlap and both
port rows. Preserve rule, severity, subject and measured detail; keep
DRC-specific counts distinct and include `view='validate'` for the full
store-aware/atomic report. Apply the context on both DRC branches; an
incomplete or failed check must say so rather than imply zero warnings.
Do not reuse the viewer's capped badge list as a complete validation result;
the existing validator budget/unchecked findings must remain visible.

Acceptance for a future authorized build: the above native repro displays
all three base warnings in DRC context; clean designs use scoped wording;
nonempty DRC findings retain their counts/rows alongside validation context;
budget skips/failures remain qualified. No geometry, threshold, response
envelope schema or pure `drc::drc` checker change is needed.

**Separate work:** gr464342 concerns `meta.generated.report` findings that
`atomic.validate::validate_atomic` currently does not roll up (only its
composite-part check consumes generated records). Its proposed helper and
DRC error rows belong to atomic-core. Assembly atom clashes and new
proximity rules/layers remain separate; neither a connect nor a new bond
is a remedy for a physical collision. This proposal does not claim atomic
coverage from envelope warnings or cure gr464342 by itself.

Provenance update (communicator relay, 2026-10-04; already recorded in
gr466345 comment 1): read-only cross-block atomic review at the shared
origin found minimum separation ~0.425 Å, 52 pairs below 1 Å and 36 below
0.912 Å. Individual sampled blocks had no overlap errors. These are
assembled atom-pair observations, separate from the validator's 2.36472 nm
envelope warning and intra-block quality. Investigation is complete;
no repeat scan, refiling, geometry mutation or new threshold is authorized.

## Cross-block proximity — bounded SE/backend/viewer review

Requested by `inbox/coordinator-cross-block-clash-review.md`, 2026-10-04.
Review only; existing evidence above is preserved, not remeasured. The
observed 1 Å / 0.912 Å counts are not approved physical criteria.

### Coordinate and identity contract

`ops::compose_world_pose` composes parent-relative `local_pose`/`local_rot`
top-down, using parent transform × child transform. `persist::load_tree`
and `tree_from_json` invoke it. Consumers must use resulting world
`pose`/`rot` exactly once; recomposing parents would double-transform.
Corrupt parent cycles currently terminate with arbitrary placement: such
a tree must be unsupported for a proximity verdict, not treated as valid.

For direct bindings, `atomic.render::hydrate_bound_scenes` loads local
structure scenes; `blocktree_view::_bound_structure_atoms` converts
fractional coordinates through the cell to Cartesian Å, multiplies by
1e-10 to metres, applies `_block_pose`, then divides by 1e-10 back to Å.
`_build_atomic_block_payload` uses the same placement but then applies
display scale, quantization and smoothing. Physical queries must use
float64 original coordinates before those display operations. The earlier
`_world_atoms` anchor in this draft was incorrect; `_bound_structure_atoms`
is the existing atom-file helper, not yet a sufficient shared backend API.

Both current atom loops visit direct `tree.blocks` bindings; they do not
expand inherited template chemistry or array occurrences. Do not claim
instance coverage from the existing export/overlay. Propose a shared
SE-owned assembly adapter over hydrated scenes that emits placed atomic
occurrences and explicit omitted/unsupported entries. Atomic-core must
settle template expansion, whether the authored template itself counts as
physical content, and cross-design template resolution before implementing.
Every repeated occurrence needs its own identity and composed placement;
loading a shared structure once must not collapse its physical copies.

Endpoint proposal: design identity/revision, occurrence path rooted at
stable block UID (including array member identity), bound structure
identity/version, atom label and ordinal. Existing pick tokens
`<se:UID#ORD>` (`precis_se.pick`) work for direct blocks only with their
structure version pinned; viewer `pickAtom` returns UID/scene-order ordinal.
Expose a mapping rather than inventing an unversioned global atom index.

`_atomic_block_key` already includes structure id/version/updated_at,
world pose/rotation and block UID, but is a display cache key. The proposed
physical key must cover design revision/state, all occurrence transforms,
binding membership, structure/cell/bond content revisions and query/rule
version; exclude camera, smoothing and visibility. `_tree_at` restores
old SE snapshots while atom loading resolves bound structures live: past
SE revision alone does not establish historical atom coordinates. Require
matching structure snapshots or report historical coverage unsupported.
Reject stale asynchronous results if their assembly key no longer matches.

### One proposed backend/viewer seam and ownership

**Backend:** atomic-core owns an SE assembly-coordinate adapter and a pure
bounded neighbor query over that adapter's result; reuse the existing
hydration/transform mathematics without moving a checker into web code.
SE design-check integration owns validate/DRC projection of that result.
**Viewer17:** owns a read-only on-demand route adapter and distinct proximity
overlay/pick presentation consuming the same result. Coordinate ownership
of `handler.py` and `routes/blocktree_view.py` before coding. No concrete
route, response schema or UI design is approved by this review.

Proposed result semantics: assembly key, coordinate source `original`, Å
units, query scope/cutoff and rule provenance, eligible/checked/omitted
occurrences and atoms, unique unordered endpoint pairs with measured
distance, and explicit complete/incomplete/truncated/unsupported status
with reasons. Pair identity is canonical endpoint order, so A–B and B–A
appear once; identical structures in different occurrences remain distinct.
Candidate cutoff is a declared query parameter, not an invented clash
criterion. Until scientific rules are agreed, call this proximity and
report measured distances without assigning physical severity/clearance.

Use spatial bins or another existing bounded neighbor search, with atom,
candidate-comparison, memory, elapsed-time and returned-pair budgets. Dense
cells can still cost quadratically; reaching any limit returns partial
coverage and the reason. Returned count is a lower bound when search stops;
do not label a truncated traversal as the globally closest K pairs. A
complete empty query means only no qualifying pairs within its declared
cutoff/scope for included geometry. Missing bindings, empty expected scenes,
failed transforms and unsupported occurrences must appear in coverage;
no empty partial result implies clearance. Backend verdict is independent
of viewer isolate, abstraction, hidden blocks or atoms toggle.

**Viewer consumption proposal:** a separate diagnostic overlay highlights
both endpoint atoms and provides stable handles plus measured distance.
Any connecting guide is a diagnostic glyph, never a chemical bond or spring;
strain colors and physical coordinates remain unchanged. Hide/isolate may
hide glyphs, but preserve assembly counts and identify off-screen endpoints.
For smoothed/interpolated display, retain original-coordinate endpoint
markers or explicitly show that the physical markers differ from the
displayed surface; never recompute verdicts from `blk.lerped`. Distinguish
partial/truncated/unsupported/stale/not-run from complete results. Missing
atom identities cannot fall back to a nearby atom. This is a seam proposal,
not approval of controls, colors or layout.

### Unresolved criteria requiring atomic owner / Reto review

- Physical criterion, element/radius source, missing element handling,
  pair-specific cutoff and severity. Observed carbon cutoffs are evidence,
  not defaults for all chemistry.
- Exclude only genuine modeled endpoint bonds, never a generic block
  connect or interaction. Ports have `bound_atom`, but bound endpoints
  alone do not establish the physical bond graph. Decide inferred versus
  explicit structure bonds and 1–3 exclusions across block boundaries;
  hexfold's exclusions are domain-specific, not automatically universal.
- Periodic cells/minimum-image semantics versus finite assembly geometry;
  existing XYZ export's nonperiodic flag does not settle this science.
- Template/array occurrence semantics, supported historical structure pins,
  state selection and whether intra-block results share a query or remain
  a separately labeled scope. No blanket intra-block correctness claim.
- Resource budgets, query/result transport and invalidation/pin lifecycle;
  mark unsupported scope explicitly until each seam is decided.

### Meaningful acceptance cases for a future authorized build

1. Individually valid unbonded sheets placed together produce a cross-block
   pair with both endpoints and a measured distance, despite no modeled
   bond. Preserve gr466345 observations as provenance; do not regenerate
   or mutate that design to make a fixture.
2. A separated control outside an explicitly supplied test cutoff has no
   qualifying pairs with complete coverage. Applying one common rigid
   transform to both sheets preserves distances; moving one changes them.
3. Equal-distance intra-block and cross-block pairs have identical distance
   arithmetic and explicit scope; if intra-block is excluded, it is marked
   outside query scope, not implicitly clean. No duplicates on reversed
   endpoint traversal.
4. An analytical Å fixture nested under translated/rotated parents matches
   hand-calculated world distances with the unit conversion applied once.
   Repeated instances of one structure retain separate occurrence handles;
   unsupported arrays/templates return unsupported coverage, never success.
5. A genuine mapped bond and a 1–3 pair exercise the eventually agreed
   exclusions; a generic connect, interaction or absent bond must not
   suppress an otherwise qualifying pair. Unresolved exclusion semantics
   are surfaced rather than guessed.
6. Missing/empty bound scenes, invalid parent cycles, unsupported periodic
   geometry and every resource limit yield explicit noncomplete status.
   Zero returned pairs in these cases never reads as clearance.
7. Hidden/isolate/atoms-off and smoothing endpoints 0/1 preserve the backend
   assembly key, distances and counts; endpoint markers/readout remain
   tied to original atoms and expose hidden endpoints.
8. Rebinding/reposing, ancestor motion, atom/cell/bond edits and state/revision
   changes invalidate a result; late responses do not paint a different
   revision. A past SE snapshot with unresolved historical structure content
   reports unsupported instead of combining old poses with today's atoms.

Warning propagation for gr466345 and generated-report roll-up for gr464342
remain separate changes. No implementation, physical threshold selection,
artificial bonds/springs, geometry mutation, full suite or deploy is
authorized here. Backend ownership above is proposed; atomic-core seam
review must precede code. Current template WIP remains untouched.
