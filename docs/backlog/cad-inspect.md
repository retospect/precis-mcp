# cad-inspect — the inspection verb family (ray / section / overlap)

Source gripes: gr526015.

**Status:** kernel module + tests landed in `src/precis/cad/inspect.py`
(gripe 526015's shaped-beam inspect tool). This spec records the design
contract and what is deliberately not built.

## The set

One verb family, three probes, named by **output** — the naming is the
non-confusion (a plane and a swept ribbon are one probe; a beam and a
cylinder are one probe; a zero-radius beam is a ray):

| probe | output | answers |
|---|---|---|
| `ray(design, expr, origin, axis)` | `RayInspection.hits` (entry/exit t, points, normals), `min_distance_m`, optional SDF `profile` | path, clearance, directional distance |
| `section(design, expr, origin, normal, pitch)` | `SectionInspection.loops` (points, length, signed area), `hole_count`, `wall_pairs_m` | cross-section curves, holes, wall thickness |
| `overlap(design, expr, probe, grid)` | `OverlapInspection.hit`, `volume_m3`, `aabb_m` | boolean/volume vs any probe solid — the stamp-verification probe |

## Design decisions (the why)

- **Everything evaluates the folded SDF** (`fold.expr_sdf_np`), so all
  three probes work identically on sharp designs and field-routed ones
  (`rd`-rounded nodes, `field:` leaves) with no analytic special cases.
- `ray` sphere-traces; `min_distance_m` is the polished march minimum —
  inspection-grade, not metrology. Feature attribution along the ray is
  `fold.ray_spans`' job; these reports stay unattributed.
- `section` marches squares with three robustness rules learned the hard
  way: sdf `<= 0` counts as inside (flat faces coincident with grid lines
  section exactly); the grid is **half-offset** and padded one pitch
  beyond the projected AABB (a boundary on the outermost nodes straddles
  no cell and would never be emitted — the half-offset also keeps
  "nice" geometry off nodes deterministically); segments are **directed**
  material-on-left so the shoelace sign is the material sign (hole_count
  = count of negative loops, no probing).
- `overlap` integrates `bulk._integrate` over the **AABB intersection** of
  part and probe, because `bulk.expr_aabb` stays conservative for `Inter`
  — a small probe would be invisible on a big part's grid.
- Wall-pair distances are point-sampled (± pitch): exact
  segment-to-segment all-pairs was 10M calls / 67 s on one tube section.
- **Attribution** (Reto's trace-ref requirement): every encounter —
  `RayHit.entry/exit_features`, `SectionLoop.features`,
  `OverlapInspection.features` — carries `FeatureRef(label, role)`.
  `label` is the leaf's node name, the handle the se workspace resolves
  to the generating code. `role` follows `fold.classify`: `owner` = a
  direct face; `blocker` = a boolean-created (indirect) face. For
  `A − B` the bore wall attributes to **B** (its zero-set; the operator
  is never attributed); the material behind it is A's. Pinned by
  `test_ray_attributes_bore_wall_to_the_cutter`.

## First consumer

`se-box2/verify_holes.py` (boxmake2 tree) runs the box's 40 boolean
feature checks entirely on `inspect.overlap` — the ad-hoc script from the
boxmake campaign is now this module's dogfood.

## Deliberately not built (later rungs)

- MCP surfacing (a `view='inspect'` or verb on the cad/se kinds) — the
  kernel API is the contract; wire it to the server once a second
  consumer wants it without a python bridge.
- Exact analytic section via manifold `slice` for sharp designs (the
  marching-squares loops already carry the grid's fidelity; exact arcs
  are an optimization, not a capability).
- Swept probes — pose-sampling composes; the one truly sweep-shaped
  question (body travelling A→B) lives in `fastener_insertion_path`.
- The draft/parting-line validator (gripe 526015 comment 2) — a composed
  checker on top of this family; promote "regions with attributes" to a
  fourth output only when a second consumer needs it.
