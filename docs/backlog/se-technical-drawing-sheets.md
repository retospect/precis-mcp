---
status: draft
title: Authored SE technical drawing sheets and scientific figures
thread: se-3d-viewer
pillar: 3d-design
---

# SE technical drawing sheets and scientific figures

Canonical authored drawing-sheet spec in the SE-viewer worktree. Adopted
from communicator's same-named draft, preserving its original file untouched.
Premise check complete; ready for Reto/coordinator design review, **DRAFT,
not build-ready**. No renderer, schema, UI or route-removal approval.
Owners: SE-viewer17 for drawing workflow/export presentation; SE design
owner for source geometry/datums; atomic-core for atom identities/section
semantics. Coordinate shared seams before any implementation.

Current inspection, gr466345/gr464342 triage and the completed cross-block
proximity review remain in [se-drawing-inspection.md](se-drawing-inspection.md).
This file is the canonical authored sheet concept, not a second clash spec.

## Purpose

Keep 3D as the primary inspection surface. Give 2D a useful drawing-sheet
role: standard projections, authored sections and enlarged details, exported
as editable SVG for technical communication and scientific figures.
Reto wants cross-section planes parked in the 3D scene and a patent-drawing
inspired style with hatching and deliberate atom appearance by type.
This supersedes the tentative request to retire SE 2D outright.

## Checked starting point

- `src/precis_web/blocktree_svg.py`: existing 2D projection tessellates
  envelopes at world poses and draws their convex hulls. It already supports
  subtree isolation and abstraction levels; concavity is simplified.
- `src/precis_web/static/blocktree-3d.js::_exportSvg`: current 3D SVG export
  embeds a scene PNG plus vector scale-bar elements. It is a screenshot
  export, not editable vector scene geometry.
- `src/precis_se/datums.py` resolves measurement selectors against posed
  envelopes (frame/port/face/axis/patch/ring), not arbitrary stored drawing
  planes. Its atom/site selectors parse but do not resolve atom coordinates
  there; atom indices have structure-version pins. Reuse resolved references
  where applicable; do not silently convert drawing planes into constrained
  mechanical datums.
- Existing geometry, block poses and bound structures remain authoritative;
  drawings are derived views, never another editable geometry store.

## User workflow

1. In 3D, choose an assembly, state and representation: envelope, realized
   solid, atomic geometry or smoothed surface. Show available geometry
   honestly; do not silently replace missing realized geometry.
2. Add a named section plane, e.g. A–A. Position and rotate it, choose its
   viewing direction, and preview the cut. Keep its marker in the 3D scene;
   showing/hiding the marker does not delete the authored plane.
3. Mark a detail region, e.g. B, on the scene or a drawing view; create an
   enlarged linked view. Moving the section/detail updates its drawing.
4. Compose a sheet from top/front/side, an optional isometric overview,
   sections and details. Align standard projections at a shared scale;
   give enlarged details their own explicit scales.
5. Add dimensions, labels and leaders; choose a technical or scientific
   style; export SVG. Reopen the sheet with the same selections and layout.

## Drawing intent to preserve

Proposed semantic fields, not a decided schema/API: stable view/plane IDs,
source design revision, selected state and subtree, representation, section
frame and cut direction, detail region and parent view, projection convention,
scale, sheet layout, annotations and style. Declare whether views follow the
current design or pin a revision; show stale/broken references visibly.
Specify frame anchoring under block movement, array instances and deletion.
Reuse the existing verbs; persistence and any migration need owner review
and applicable threshold approval before implementation.

## Rendering requirements

- Orthographic top/front/side views with an explicit first/third-angle
  convention. Use world-composed placement and declared state consistently
  with 3D; dimensions refer to source geometry, not SVG pixels.
- True vector paths, strokes, text and symbols. Separate groups for geometry,
  section contours, hatching, atoms/bonds, dimensions and annotations.
- Technical style: clear silhouettes, centre lines, optional hidden lines,
  section labels/arrows, dimension units and scale. Mark simplified envelope
  views as schematic; do not present them as fabrication geometry.
- 1850s patent-inspired style: monochrome linework, sparse leaders, numbered
  details and hatching; preserve a machine-readable legend. This is an
  aesthetic preset, not a claim of patent-office compliance.
- Atom symbols are driven by element, with optional semantic-role overrides.
  Distinguish elements in monochrome using outlines, size or symbols as well
  as colour. Representation size must not imply a physical radius silently.
- Hatch cut material in solids. For sheets and atomic structures, define a
  separate section/slab convention and disclose slab thickness. Do not hatch
  empty space as bulk carbon or silently turn a sheet into a solid.
- Distinguish authored geometry from smoothing/interpolation. A smoothed
  drawing cannot replace atomic geometry for measurement or validation.
- Atomic clashes are a separate diagnostic overlay from bond/angle strain.
  Use 3D world-coordinate proximity, including pairs across blocks without
  a bond; projection overlap alone is not a clash. Track diagnostics with
  gr466345 and the SE owner; drawing work must not mask that gap.

## Small delivery slices

1. Standard vector projections, labels/scale and one coherent sheet preset;
   compare against current 2D/3D geometry on a solid and an atomic example.
2. Named section plane with 3D preview, one saved section view and SVG export.
3. Linked detail view, dimensions and saved sheet layout.
4. Scientific/1850s patent-inspired styling, atomic slab conventions and diagnostic
   overlays after their underlying checks exist.

Keep current 2D links usable during the transition. Decide redirect or
deprecation only after the replacement's review; no removal in this spec.

## Acceptance examples and open decisions

- A mechanical assembly: projections agree with 3D placement; a moved section
  changes its contour; dimensions retain units and detail scales are labelled.
- A Hexfold sample: cut plane reveals a tube/cap profile; sheet/slab semantics
  are explicit; atomic detail remains legible in monochrome and export.
- `hexfold-dogfood-r4`: isolate or lay out the two overlapping samples without
  changing their source poses; drawing layout must not imply the assembly
  passed clash checks. Any clash badge uses measured 3D diagnostics.
- Export has editable vector scene elements, no required raster scene image,
  escaped labels and stable view references. Missing geometry/references are
  visible. Viewing/export does not mutate the source design.
- Before implementation, choose the first target geometry, revision-following
  policy, frame anchoring, projection convention and atomic section semantics.
  Verify reuse of existing export/frame code and bound rendering cost.
- Validation belongs to the implementing worker: focused geometry/export and
  viewer checks, required lint/types; full release checks coordinator-scheduled.

## Premise-check results and reuse boundaries

| Existing seam | Reuse candidate | Verified limit / decision needed |
|---|---|---|
| `precis_se.datums::resolve`, `stamp_region_pins` | Feature references, world points/normals, stale atom-version pins | Envelope-based geometry; no general authored plane store, detail linkage or atom/site coordinate resolver. A normal alone does not fix in-plane rotation. |
| `precis_se.ops::compose_world_pose` | Parent-relative → world placement | Apply once. Drawing-frame anchoring to world/block/datum remains undecided; arrays/templates require occurrence identities. |
| `precis_web.blocktree_svg::project_point`, `build_block_draws` | Axis mapping, visibility selection and current projection fallback | x→yz side, y→xz front, z→xy top; convex hulls hide holes/concavity. This is not a first/third-angle sheet convention or realized-solid hidden-line renderer. |
| `precis.viz3d.primitives::Scene3`, `camera::Camera`, `render::render_svg` | Vector balls/sticks/polylines/labels, orthographic cameras, painter depth sorting, scalebar | Current primitive set has no filled section-face/hatch system, solid contour extraction or full hidden-line removal. Do not assume an arbitrary section plane can be rendered by clipping the current scene. |
| `precis.viz3d.stickfig::stick_scene` | Element-aware CPK colors/covalent-radius ball scaling, mono option | Monochrome has one neutral color; no explicit element glyph/hatch legend. Drawing radius is representation scale, not an approved clearance radius. |
| `precis.cad.probe::probe_section_z` | Existing feature-attributed outer/hole loops for limited solids | z-constant only, per-primitive loops; general plane, boolean union/intersection fidelity and field/smoothed geometry coverage are not established. Hatching cannot treat unmerged primitive loops as proven material occupancy. |
| `precis.utils.figure_source::resolve_figure_source` | Existing linked SVG canvas and cached blob consumption in drafts | No saved design-view recipe branch. `se-view-figures.md` has shipped render core only; view recipe/caching/source wiring remains draft. |

Coordinate with [se-view-figures.md](se-view-figures.md) for one shared
source/camera/style/provenance recipe rather than inventing independent
figure and drawing geometry stores. A sheet adds view IDs, parent/detail
links, section intent and layout; a scientific figure may consume a single
view. The existing editable figure canvas is a possible presentation/export
consumer, not evidence that SE plane semantics or regenerable recipes ship.
Choose preservation of authored recipe versus free SVG editing explicitly;
a detached edited SVG cannot silently masquerade as a regenerable view.

The 3D PNG-in-SVG snapshot is not the technical vector export. Drawing
output should contain selectable paths/text/symbols and retain view/source
provenance; optional raster effects cannot replace required vector geometry.
Reopening/exporting a sheet must not change source poses to fit its layout.

## Decisions still open — required before a build-ready slice

| Decision | Choices / unresolved consequence | Review owner |
|---|---|---|
| Source revision and refresh | Pin vs follow; bound structure revisions/state/geometry representation; explicit stale/broken references. Historical SE revision alone may resolve current bound structures. | SE + atomic-core + Reto |
| Plane/frame anchoring | World, block occurrence, or resolved datum; origin/normal/in-plane axis, cut direction; behavior after movement, rename, deletion or predicate re-resolution. Parked marker is display of authored intent, not editable source geometry. | SE + Viewer17 + Reto |
| Section semantics | Solid material contour/cap versus atomistic slab selection/thickness and bond clipping; how a smoothed surface is labeled and whether it can supply a section. No invented bulk fill for atom sheets. | SE + atomic-core + Reto |
| Projection convention | First/third-angle, signed view direction, handedness, page alignment and units/scales. Current axis projections do not settle this convention. | Viewer17 + Reto |
| Renderer and persistence | Extend viz3d, adapt geometry/export seams or another justified route; semantic recipe ownership, migration/API shape, figure integration and caching budgets. No choice approved. | Architecture/SE + Viewer17 + coordinator |
| Atom-by-type and hatching | Explicit element-to-glyph/line/size mapping and legend; optional role override precedence; monochrome distinction; solid-only material hatch regions and pattern conventions. 1850s inspiration is aesthetic, not filing compliance. | atomic-core + Viewer17 + Reto |

These decisions remain visible even if one standard projection can be built
sooner. Suggested slice order above is a review sequence, not approval to
implement. No UI mockup/code or schema has been created by this pass.

## Acceptance refinements for future authorization

- Save two parked section markers and one linked detail; hide/show markers
  without deleting intent. Reopen the sheet with stable section/detail
  references, units, source representation and explicit revision policy.
- An analytical solid with a hole must retain that hole in vector projections
  and an applicable section; hatch only proven cut material. If the selected
  backend cannot resolve the cut, mark it unsupported rather than draw a hull
  as a section. Test a general rotated plane separately from the z-only probe.
- Match orthographic top/front/side using a reviewed convention and composed
  transforms; a rigid assembly rotation exercises handedness and alignment.
  Linked detail magnification changes sheet scale, never source dimensions.
- A scientific atom view and a technical view reuse source identity and camera
  semantics. An atomic slab discloses thickness/inclusion and clipped-bond
  treatment; distinguish source atoms from smooth depiction. Element glyphs
  remain identifiable in monochrome using an explicit legend.
- Refresh/reopen after a parent move, structure edit, binding replacement,
  deletion or datum re-resolution follows the reviewed anchoring/pin policy;
  stale/unresolvable references never silently attach to another atom/feature.
- Export SVG contains editable geometry/text groups and no required scene PNG;
  escaped labels and view provenance survive export. No raster/smoothing
  change silently alters source measurement or physical diagnostic results.

## Review handoff

Concepts preserved: 3D primary; parked planes; standard vector projections;
linked enlarged details; shared scientific/technical views; 1850s patent
linework/hatching; explicit atom-by-type styling. Reuse is partial and the
above decision gates prevent a false claim that current hull projections,
z-only cuts, vector atom renderers or blob-backed figures already implement
authored sheets. Preserve existing 2D routes/links and current inspection WIP.
This draft is ready for design review; implementation remains unauthorized.
