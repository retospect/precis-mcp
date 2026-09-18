---
status: draft
title: cad — rounding at the SDF leaf, a sampled-field leaf, and a field → marching-cubes export backend (no work on the mesh, ever)
prio: high
model: opus
---

# cad: rounding at the SDF leaf + sampled-field leaf + field export

Decided with Reto 2026-09-18. Two slices, independently shippable; slice 2
is what `structural-solution-space.md` slice 4 (`realize(strategy='simp')`)
binds its result to, so it is that item's `blocked-by`.

## Motivation / why

Every existing cad design has sharp corners because the kernel has no
fillet, and the planned organic (SIMP) parts arrive as voxel density
fields with no DSL. Both resolve the same way: the kernel already holds an
exact signed distance per leaf (`primitives.Primitive.distance_local`,
`Placed.distance`) and folds booleans as min/max
(`relate.component_sdf`), so rounding is a leaf parameter plus a constant
field offset, an optimiser result is one more leaf that answers
`distance(p)`, and a mesh is extracted **once** at export from the folded
field. The mesh is an output artifact; nothing reads it back, nothing
edits it.

## The rounding rule (representation contract — keep verbatim)

- **Rounding lives on the leaf.** A primitive carries `round=r`. It is
  applied as *parameter shrink + field offset*: build the primitive shrunk
  by `r` on every side, evaluate its exact SDF, subtract `r`:
  `d(p) = sd(p, params − r) − r`. Zero set = Minkowski sum of the shrunk
  shape with a ball of radius `r`; bounding dimensions unchanged; edges
  → cylinders, corners → sphere caps.
- **Shrink in the parameters, never in the field.** `+r` then `−r` in the
  field cancels (the inside of a box SDF has square level sets). Rounding
  appears only when the field being grown is an exact distance to the
  shrunk shape — build it shrunk, or re-distance in between.
- **Base-at-`z=0` convention** (`box`/`cyl`/prisms): the shrink also lifts
  the base by `r`, otherwise the rounded solid sits `r` lower than the
  sharp one. Test pins this.
- **Thin features vanish.** Validator refuses `r ≥ ½·min_dimension` at
  put/parse time (names the primitive and the dimension). Never clamped.
- **Offsets compound.** `round` is a leaf-only key in v1; a `round` on a
  boolean node is refused with a message naming this rule. An offset above
  a boolean needs an exact or re-distanced field under it — that is the
  slice-2 field leaf's job, not a node option.
- **Booleans are exact only at level 0.** min-union / max-intersection of
  exact leaves have the exact boundary at `d = 0` and only bounds
  elsewhere. Extraction is always at level 0 above booleans; non-zero
  level sets are legal only on a leaf or on a re-distanced field.
- **Opening rounds convex corners only.** Inside corners where two nodes
  meet stay sharp under leaf rounding. Concave fillets: `blend=k`
  (smooth-min) on a union node — cheap, local, **not an exact radius**,
  says so in its render — or a closing (grow → re-distance → shrink) on a
  field leaf (slice 2, exact).

## Slice 1 — `round` on leaves + field export backend

In scope:

- `round` key in the config DSL (`box:w40mmd20mmh10mmr2mm` — key `r` is
  taken on `cyl`/`sphere`/`torus`; use `rd` for round, longest-first
  matching already handles it), stored canonical metres like every other
  dimension. Every `primitives.Primitive` gets a rounded evaluation per
  the contract above; `chamfer`/`HalfSpace` and `Torus` refuse `round`
  (a half-space has no extent to shrink; a torus is already round).
- `blend=k` on union nodes (`add`); refused on `cut`/`intersect` in v1.
- **Field export backend** in `cad/export.py`, selected automatically when
  the design contains any `round`, `blend` or (slice 2) field leaf;
  otherwise the analytic `tessellate` + manifold3d path stays as-is
  (exact, small files). Pipeline: AABB from `relate._bounds` → coarse
  sample of the folded SDF → **narrow band** refinement (only cells
  straddling zero) at the export pitch → marching cubes on the band →
  `_orient_outward` → watertightness check (every edge shared by exactly
  two triangles, else `ExportError` naming the cell) → existing
  `_write_binary_stl` / `_write_3mf`. numpy only; no skimage, no scipy.
- Export pitch is a parameter (default from the caller; se passes the
  house `layer_height` from `se_capabilities.json`, the cad kind's own
  export takes `pitch=`). Refuse a pitch that would exceed a sample budget
  (named constant, ~50M band cells) rather than swap.
- `view='printability'` on the cad kind and `precis_se` `view='print'`
  consume the field mesh unchanged — both are mesh-in
  (`cad/printability.py`).

Explicitly NOT in scope: dual contouring / adaptive octrees (named
upgrade when the budget refusal starts firing on real parts); exact
concave fillets (slice 2); rounding on boolean nodes; OCCT B-rep fillets
(`_occt.py` is not touched); any editing of an extracted mesh.

Acceptance:

- Rounded box: bounding dims equal the sharp box's to 1e-9; volume from
  the mesh matches the Steiner formula for the shrunk box `w'×d'×h'`
  (`w' = w − 2r` etc.): `V = w'd'h' + 2r(w'd' + w'h' + d'h') +
  πr²(w' + d' + h') + 4/3·πr³`, within the pitch's discretisation error
  (state the bound in the test); base plane still at `z=0`.
- `r ≥ ½·min_dim` refused at parse with the dimension named; `round` on a
  boolean node refused naming the rule.
- Field-exported mesh is watertight and outward (signed volume > 0) for:
  rounded box, rounded box `cut` sharp cylinder (hole keeps a sharp edge),
  two rounded boxes `add` with `blend`.
- A design with no `round`/`blend` exports byte-identical to before (the
  analytic path is untouched).
- `printability.orientation_search` on a rounded box reports no
  `overhang` finding at 45° house rules (the round is under the limit at
  the pitch) — pins that the mesh, not the sharp AABB, is what DRC sees.

## Slice 2 — sampled-field leaf + re-distance + open/close

In scope:

- A `field` primitive: `(nx,ny,nz)` float32 signed-distance grid, pitch,
  origin, posed like any leaf; `distance_local` = trilinear lookup,
  outside the grid = distance to the grid box + the boundary value's
  sign (the field is only trusted inside its own AABB, and says so).
  Storage: an artifact (STL/3MF-sized, 200³ float32 ≈ 32 MB is the
  expected upper size) referenced from the design, never inlined in the
  DSL string; the DSL carries `field:<artifact-id>`.
- `redistance(binary_or_field) → field`: exact Euclidean distance
  transform, both sides, numpy-only (Felzenszwalb–Huttenlocher 1D
  passes). This is the *re-distance in between* the rounding contract
  names.
- Morphology on a field leaf, each returning a new field: `offset(r)`
  (dilate `−r` / erode `+r`), `open(r)` = erode → redistance → dilate
  (rounds convex), `close(r)` = dilate → redistance → erode (rounds
  concave, fills necks). Thin-feature rule applies: `open(r)` reports
  components that vanished (count + volume) rather than silently
  dropping them — for organic struts that is a design finding.
- `from_density(rho, threshold=0.5, pitch) → field` — the SIMP bridge:
  threshold → binary → `redistance`. Lives here (cad) so
  `structsolve/simp.py` stays store- and cad-free.
- Field leaves participate in booleans with analytic leaves unchanged
  (min/max over `distance`), so an optimised body can be `cut` by an
  exact bore and `add`ed to a designed seat, then exported by the slice-1
  backend.

Explicitly NOT in scope: sparse/narrow-band field storage; GPU; fields
as *inputs* to SIMP (the domain still comes from analytic keep-in/keep-out
sampling); any notion of a mesh leaf — a mesh never re-enters the kernel.

Acceptance:

- `redistance` of a voxelised sphere matches the analytic sphere SDF to
  within one pitch everywhere in the band.
- `open(r)` on a sharp voxel box ≡ (within pitch) slice-1's `round=r` box;
  `close(r)` on two touching boxes fills the concave seam with radius `r`
  (probe the SDF at the seam midpoint).
- `from_density` on a synthetic `simp_optimize` result (the engine's own
  cantilever test fixture) exports watertight; `open(pitch)` on it reports
  the vanished-component count.
- A `field` `cut` by an analytic `cyl` exports with the bore at the exact
  cylinder radius (measure on the mesh).

## Target + blast radius

`src/precis/cad/{dsl,primitives,relate,export,tessellate}.py`,
`precis.handlers.cad` (`view='printability'`, export verbs), `precis_se`
`view='print'` (pitch argument), `docs/reference` cad DSL table, skill
`precis-cad-help` (the `rd`/`blend` keys, the field leaf). No migration
(the DSL string and an artifact reference carry everything). Sibling
consumer: `structural-solution-space.md` slice 4.

## Open questions / decisions log

- 2026-09-18 (Reto): rounding is leaf-shrink + field-offset, no mesh work
  ever; rounding above a boolean is a separate operation over an exact /
  re-distanced field — decided, this item is the record.
- 2026-09-18: `rd` as the DSL key for round (not `r`) — mine, because `r`
  is taken; rename if a better one turns up before slice 1 ships.
- Open: whether the field artifact should be a `folder`-kind ref or a new
  small artifact table. Slice 2 picks whatever the SIMP run-summary
  storage in `structural-solution-space.md` picks; the two must agree.
