---
status: draft
pillar: 3d-design
title: cad STEP import — OnShape/any-CAD solids become native editable designs
prio: normal
model: opus
---

# cad: STEP import as native editable solids

Reto, 2026-10-18: import 3D designs from OnShape as modelled shapes, not
STL. Rulings: STEP is the route; feature history is not needed; the
result must be modifiable and be "a smooth space volume native in our
format"; STEP output follows eventually (export exists for analytic
designs, `[cad-step]`). Reto believes the cad kind supports all STEP
primitives. It does not (next section).

## Primitive gap (verified against the code)

The kernel (`src/precis/cad/__init__.py`, `primitives.py`, `dsl.py`)
admits: `Sphere`, `CircularFrustum` (cyl / cone / tcone), `PolyFrustum`
(box / hex / ngon / frustum / pyramid — **convex** polytopes between two
parallel rings), `Torus` (full ring only), `HalfSpace` (the `chamfer`
tool: `cut`/`intersect` only, never a base node), `Rounded` (convex
edges, constant radius, no sphere/torus), `Field` (sampled SDF). Poses
are rigid: no scale, no mirror.

| STEP surface | native equivalent | gap |
|---|---|---|
| plane | `PolyFrustum` faces; `HalfSpace` as cut tool | only convex outlines; a face with holes or a concave outline has no primitive |
| cylinder, cone | `CircularFrustum` | full revolution only; a cylindrical patch needs intersect with half-spaces |
| sphere | `Sphere` | partial patches via intersect only |
| torus | `Torus` | full ring only; a fillet arc between faces is a torus *patch*, not a ring |
| surface of revolution | none | no revolve-a-profile primitive |
| surface of linear extrusion | none | no extrude-a-profile primitive (profile = lines + arcs + splines) |
| B-spline / NURBS, Bezier | none | refused (slice 3: exact freeform primitive) |
| offset surface | none | `fieldops.offset` works on a grid only |
| elliptic cylinder/cone, general quadric | none | |

So: box/cylinder/cone/sphere/torus parts and their CSG are native;
anything with a sketch profile (extrude/revolve), a non-convex planar
face, a variable fillet or a freeform face is not. Typical OnShape parts
(extruded sketches with holes, fillets, lofts) are mostly in the "not"
column. Rounded convex edges are the one native fillet; concave fillets
are `blend:` (smooth-min, not an exact radius) or `fieldops.close`.

## Existing pieces this builds on

- **Export path**: `src/precis/cad/_occt.py` builds an OCCT B-rep per
  component and writes named solids through `STEPCAFControl_Writer` +
  XCAF; `export.py::export_step` refuses `rd`/`blend`/`field:` designs.
  No STEP *reader* exists anywhere in the tree.
- **Extra**: `cad-step = ["cadquery-ocp-novtk>=7.8"]` in `pyproject.toml`
  (~200 MB wheel; `_occt.available()` gates, Unsupported error otherwise).
- **Field leaf** (not used by import; Parked): `field:<sha256>` = float32 SDF grid + isotropic pitch +
  origin, trilinear inside its box, exact boolean with analytic nodes
  (`Field`, `precis.cad.fieldops`). Stored via `Store.put_field(ref_id,
  fld, provenance=)` as a `chunk_kind='field'` chunk + `chunk_blobs`
  payload (`store/_cad_ops.py`); the DSL only carries the sha. A
  `put(kind='cad', text=...)` resolves `>= 12` hex chars. Caps:
  `MAX_REDISTANCE_VOXELS = 10M` (`fieldops.py`), `MAX_BAND_CELLS = 50M`
  (`fieldmesh.py`).
- **Design storage**: `put(kind='cad', id=<slug>, text=<source>)` builds
  eagerly (`CadHandler._put_impl`), saves via `cad_save`. `put` has no
  `mode=` today; `mode='import'` is a new branch.
- **Assemblies**: `component <name>` blocks, `use <slug> as <name>` with
  pose, `port`/`mate`/`joint` (`precis-cad-assembly-help`). STEP export
  already writes one named solid per component.

## Slice 1 - exact import of natively expressible bodies; loud refusal for the rest

Ruling (Reto, 2026-10-18): an unsupported surface fails loudly. No
sampled-field fallback, no approximation. Slice 1 imports exactly the
bodies whose faces all have a native equivalent and refuses everything
else.

`put(kind='cad', id=<slug>, mode='import', ...)` takes one STEP file.

1. Read with `STEPCAFControl_Reader` (product names, unit conversion to
   metres, per-instance locations) in a **subprocess** (Safety). Result:
   list of `(name, TopoDS_Solid, location)`.
2. Per solid, walk the faces (`BRep_Tool::Surface` -> `GeomAdaptor`
   type) and rebuild a primitive tree (box / cyl / cone / tcone / sphere /
   torus / polytope frustum, `chamfer` half-spaces, `add`/`cut`/
   `intersect`) in which every face is accounted for. Verify the rebuilt
   design against the OCC solid (volume plus probe points); a mismatch
   is a refusal, not a fallback.
3. Refusal contract: a body with any face of a type in the "none" rows of
   the gap table, or any face the reconstruction cannot account for,
   is refused. The error names the body, the face (index and location),
   its STEP surface type, and a `next=` step (what the kernel lacks;
   re-export without that feature; or the slice that will add it). Whole
   file refused when no body imports; when some do, the reply is a
   per-body table (imported / refused + reason) and the imported bodies
   are **not** saved unless `args={'partial': True}` - the default is
   all-or-nothing so a design never silently lacks a part.
4. Multi-body STEP: one `component` per body, name = XCAF product name
   (sanitised, numeric suffix on collision; node names are globally
   unique). Locations are baked into poses; a left-handed (mirrored)
   location is refused (kernel poses are rigid) unless the primitive is
   symmetric under the mirror.
5. Reply per body: route (`analytic`), node count, dimensions recovered,
   volume (OCC `GProp` vs `get(view='volume')`), bounding box.
6. Keep the original STEP as a `chunk_blobs` payload on the design ref
   (Open question 4).

What "modifiable" means: the result is ordinary cad source with named
dimensions (`box:w..`, `cyl:r..`), so it edits like any hand-authored
design. Honest consequence: with the current primitive set, extruded
sketches with holes, fillets, lofts and freeform faces - most OnShape
parts - are refused. Slice 1 alone imports box/cylinder/cone/sphere/torus
CSG parts and little else; see Open question 2 on ordering.

Hard cases the recogniser must handle or refuse by name:

- Tolerance: STEP carries about 1e-7 m geometry tolerance; classify
  surfaces with a tolerance scaled to the body, not exact equality.
- A cylindrical/conical/spherical *patch* (partial revolution) is
  expressible only as an `intersect` with half-spaces; try, verify,
  else refuse.
- Planar faces with holes or concave outlines: expressible as CSG only
  if the reconstruction finds the cutting solids; verify, else refuse.
- Fillet faces (cylinder/torus patches between faces, variable radius):
  native only as convex constant-radius `rd`; others refused.
- Mirrored placements, scaled placements: refused.
- Repeated parts (same product, many locations): `use` instances with
  rigid poses (may defer, Open question 6).
- Threads, draft angles: draft on planes is a tilted plane (native via
  `frustum`/half-space where convex); threads refused.

Acceptance (numbers are proposals, Open question 7):

- A corpus of >= 10 small STEP files: hand-built analytic parts (box with
  bolt circle, stepped shaft, flange, sphere-in-block, torus ring) plus
  OnShape samples Reto supplies. Each either imports or is refused with a
  face-level reason; none is silently approximated.
- Imported bodies: volume within 1e-4 relative of OCC `GProp`; sampled
  surface deviation (probe points on the B-rep vs `distance`) within
  1e-6 m; probe results (point/ray/section) agree with OCC classifiers.
- Each refusal names body, face index, STEP surface type, `next=`.
- STEP -> import -> `get(view='step')` -> re-read volume matches (the
  existing exporter closes the loop for analytic designs).
- Import of a 20-solid analytic assembly in under 30 s.

## Slice 2 - widen the kernel so more bodies import

Add the primitives that make sketch-based parts expressible, each with an
exact SDF and the full `Primitive` contract (`contains`, `ray_hits`,
`distance`, `aabb`, `faces`, vectorised `distance_local_np`) under rigid
transform:

- `Extrusion`: 2D profile of lines and arcs (holes allowed), covers
  surface of linear extrusion and non-convex planar faces.
- `Revolve`: profile revolved about an axis, covers surface of revolution
  and partial revolutions (cylinder/cone/sphere/torus patches).
- Concave and variable-radius fillets, general quadrics as needed.

Needs its own ADR or an ADR 0041 amendment; this item only fixes the
import-side contract (a new primitive removes rows from the refusal
list). Acceptance: each new primitive passes the kernel parity suite
(`tests/test_cad_parity.py` pattern) and the corpus's refused-count drops
by the expected faces.

## Slice 3 - exact freeform leaf; STEP out for those designs

An exact B-spline/NURBS-bounded primitive (closest-point solver on the
control net, stored parametrically, not sampled) so freeform faces
import smooth and exact, plus STEP export of designs carrying it
(`export_step` currently refuses `rd`/`blend`/`field`). This is the slice
that widens what imports to freeform bodies and offset surfaces. Do not
start before slices 1-2 have real OnShape bodies through them.

## Parked

- Sampled-field fallback (sample OCC's SDF into a `field:` leaf at a
  stated pitch so any body imports): not wanted for now, Reto 2026-10-18.
  Recorded limits if revisited: `MAX_REDISTANCE_VOXELS = 10M` gives about
  diag/215 pitch, edges round off at pitch scale, no dimensions, no STEP
  out; per-point `BRepExtrema_DistShapeShape` over millions of points
  is far too slow without a classifier-plus-narrow-band scheme.

## Safety (all slices)

The STEP file is untrusted input and OCC parsers are native C++.

- Parse in a subprocess with `RLIMIT_AS`, `RLIMIT_CPU` and a wall-clock
  kill; the parent never loads OCP for import. Mirror `ingest/marker.py`
  subprocess handling.
- Size limits: file <= 50 MB (proposal), entity count and solid count
  capped. Over cap -> refuse with `next=`.
- Must be `ISO-10303-21` text; reject binary and other formats before
  OCC sees them. No network or external-file reference is followed.
- The compute runs as a worker job (like `workers/job_types/cad_propose.py`),
  not inline in the MCP handler; the handler returns a job handle.
- Not an agent-supplied URL fetch: slice 1 takes a file, no URL, so
  `safe_get` does not apply. A later "import by URL" must use it.
- No OnShape API keys or OAuth: the user exports STEP by hand.
  (OnShape API import is a separate later item if wanted.)

## Explicitly NOT in scope

- Feature history / parametric tree recovery (ruled out).
- OnShape API, auth, document sync.
- STL/mesh import (the thing this replaces), IGES, other formats.
- Any approximation or sampled fallback for unsupported surfaces (Parked).
- Colours, PMI, GD&T, sheet bodies, wire/surface-only STEP (solids only;
  non-solid shapes are reported and skipped).
- STEP export of freeform designs (slice 3).

## Target + blast radius

`src/precis/cad/` (new importer module + B-rep recogniser; kernel unchanged in
slice 1), `src/precis/handlers/cad.py` (`mode='import'`),
`src/precis/store/_cad_ops.py` (source-blob chunk),
a new worker job type, skills `precis-cad-help` and the verb help for
`put` (new mode), `[cad-step]` image presence on the worker.
Tests: `tests/test_cad_step_import.py` with `pytest.importorskip("OCP")`;
committed fixtures under 100 KB (box+hole, filleted bracket, a loft
for NURBS, a 3-body assembly); comparisons against OCC `GProp` volume
and `BRepExtrema` surface probes; hostile-input cases (truncated file,
non-STEP, size cap, entity bomb).

## Open questions / decisions log

1. **Slice order.** With no sampled fallback, slice 1 imports only
   box/cylinder/cone/sphere/torus CSG parts; typical OnShape output
   (extruded sketches, fillets) is refused until `Extrusion`/`Revolve`
   exist (slice 2). Build slice 2's primitives first or alongside? Needs
   an ADR 0041 amendment or new ADR either way.
2. **Partial import.** Default is all-or-nothing with a per-body
   breakdown; `partial=True` saves the importable bodies. Right default?
3. **File transport**: `PRECIS_ROOT` is a read-only mount for the
   session MCP, and a multi-MB STEP through `text=` hits MCP frame
   limits. Options: web upload at `/cad` (precis_web), a worker-visible
   drop path, or `text=` for small files only. Which?
4. **Keep the original STEP** as a stored blob? Enables re-import after
   the kernel widens; costs storage per design, and the prod store may
   hold proprietary parts.
5. **Where `[cad-step]` lives**: the extra is not in the base image;
   import needs it on the worker that runs the job. Add to the worker
   image (~200 MB) or a dedicated cad worker?
6. Instance de-duplication into `use` in slice 1 or later? Cheap when the
   STEP product structure is clean.
7. Acceptance numbers above are proposals (1e-4 volume, 1e-6 m deviation,
   30 s, 50 MB); confirm or replace.
8. Sampled fallback: not wanted for now (Reto 2026-10-18), see Parked.
