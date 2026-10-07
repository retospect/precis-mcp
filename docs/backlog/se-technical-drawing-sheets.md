---
status: draft
---

# SE technical drawing sheets and scientific figures

Draft for Reto/coordinator review; not ready for implementation. Owners:
`se-3d-viewer` with `se-machine-design` for authored drawing intent.

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
- `src/precis_se/datums.py` provides authored geometric frames. Check reuse
  before inventing a second frame system; a drawing plane need not be a
  mechanical datum with physical constraints.
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
- Patent-inspired style: monochrome linework, sparse leaders, numbered
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
4. Scientific/patent-inspired styling, atomic slab conventions and diagnostic
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
