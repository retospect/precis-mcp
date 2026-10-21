---
status: ready
pillar: 3d-design
prio: medium
model: opus
title: se cylinder wrap + mold negative — wrap a relief, height map or 2-D shape around a cylinder, check it pulls without undercut, and cut the mold halves from it
---

# se cylinder wrap + mold negative

Reto, 2026-10-21: "in se, the capability to wrap a pattern (relief, z map,
or just a shape) around a cylinder. And take the negative of it. Let's say
it's just injection mold stuff (no undercut). Ideally crossdimensional, but
macro only is fine."

## What exists (2026-10-21)

- Kernel `src/precis/cad/`: analytic SDF/CSG, lazy boolean DAG
  (`fold.py::Union/Diff/Inter`), sampled-SDF leaf `primitives.py::Field`
  (`field:<sha256>` blob), export by analytic tessellation + manifold3d or
  narrow-band marching cubes (`fieldmesh.py`).
- `probe.py::probe_draft(design, pull, min_deg)`: per-face draft on
  analytic faces; no caller, no field support.
- `manufacture.py` cavity machinery (print-in-place `FORM_CAVITY_*`); no
  molding mode in `modes.py`.
- Missing: wrap, relief, height-map input (Pillow is already a dep via the
  override), mold op, parting line, undercut check on freeform surfaces.
- Design intent already written down:
  [multiscale-design-system-spec](multiscale-design-system-spec.md)
  §2.3 rows "Pull direction / undercut" and "Draft" (one normal-vs-pull
  test, pull direction as a variable, Lipschitz bound on warped SDFs) and
  "The mould is another milled part" (negate, embed, split).

## Model: everything becomes a height field on the unrolled cylinder

Unrolled coordinates `u = θ/2π ∈ [0,1)` (periodic), `v = z/H ∈ [0,1]`.
All three pattern forms reduce to one `h(u, v)` (length, signed):

| Input | Becomes |
|---|---|
| relief / z map | 16-bit grayscale PNG or float array → `h = depth · gray` (bilinear) |
| analytic pattern | generator: `knurl:straight|diamond`, pitch, depth, angle; `ribs:`; `text:` later |
| 2-D shape (sketch polygons in u,v) | `h = depth · clamp(d_in / (depth·tan α), 0, 1)`: a plateau whose flanks carry draft angle α built in |

Surface `ρ(θ, z) = R + s·h(u, v)`, `s = +1` emboss, `−1` deboss.
Mapping knobs: `repeat n` (pattern tiles n times around), `fit` (stretch
to circumference vs keep aspect, the op reports the stretch), `twist`
(helical skew, deg per turn). Seam rule: the map must be periodic in `u`
(edge mismatch above a tolerance is refused, or `seam: blend` cross-fades
a stated width).

New kernel leaf `CylRelief(R, H, hmap_sha, depth, s, repeat, twist)`,
DSL `relief:R..H..map:<sha>..`. Distance is `(ρ − R − s·h) / L`, with
`L = sqrt(1 + |∇h|²)` computed once from the map, so the field is a
conservative bound (sphere tracing and the narrow band stay correct). The
map stays 2-D (a 4k×2k map is 32 MB float32; a voxelised 50 mm cylinder at
0.05 mm features is not affordable), and the mesh comes from `fieldmesh`
over the narrow band only. Inner bores (relief on a hole wall, molded
by a core pin) are the same leaf, subtracted.

## Undercut test (the "no undercut" rule, checkable)

Surface normal on the relief in cylindrical components ∝
`(1, −h_θ/ρ, −h_z)` (radial, tangential, axial). A pull `p` is clean at a
point when `n̂·p ≥ sin α` (draft α). Three pull layouts:

- **axial** (part slides out of a one-piece sleeve along z): clean only
  for patterns constant in z (straight ribs, splines) with drafted ends;
- **two halves** (parting plane through the axis, pulls ±x): every
  point within ~α of the parting line is at zero draft even on a bare
  cylinder, and relief there is undercut;
- **k slides** (k radial pulls at 360°/k): the general answer; k = 3–4
  clears a modest knurl.

The test evaluates the map, not a mesh: an undercut image over (u, v) per
layout, the undercut area, and the **smallest k** that clears it at α.
That answers "can this pattern be molded without undercut, and with how
many slides" before any geometry is cut.

## Slices

1. **Relief leaf.** `CylRelief` + DSL + blob storage (reuse
   `fieldops.encode_field` storage pattern), the three inputs (PNG via
   Pillow, array, generators knurl/ribs, sketch polygons with flank
   draft), seam check, export. Tests: h ≡ 0 equals `cyl:`; h ≡ c equals
   `cyl:` of R + c; seam continuity; enclosed volume equals
   `∫∫ ½(R + s·h)² dθ dz` within mesh tolerance; distance bound never
   overestimates against a dense brute-force sample.
2. **Pull check.** `precis.cad.mold.pull_check(design, layout, α)` on
   relief leaves (map-space) and analytic faces (extend `probe_draft`);
   se DRC finding `mold.undercut` naming the layout, area and minimum k;
   `view='mold'` returns the undercut image. Optional fix
   `relief_fit: clip` lowers h only where needed to clear the chosen
   layout (no new material, the op reports the depth removed).
3. **Mold op.** se op `mold` on a block: `layout` (axial|halves|k), `α`,
   `shrink` (% per material, default from the material card), `margin`
   (block around the cavity), output: one mold block per piece =
   `Inter(sector_i, Diff(box, scale(part, 1+shrink)))`, named
   `<part>.mold.<i>`, the parting surface recorded on each. Refuses when
   slice 2 finds undercut for the layout. Sprue, gate, vents, ejectors
   and cooling are out (named follow-up). Prototype route: SLA-printed
   halves (the existing `sla` mode); milled molds inherit the cutter
   radius per the multiscale spec. New mode family `molding` in
   `modes.py` for the molded part itself (draft, wall-thickness rows).
4. **Cross-dimensional (later, optional).** The wrap map (`u, v,
   repeat, twist`) is the same object as hexfold's roll-up
   (`hexfold/build.py::_patch_seed3`: chiral vector = the periodicity +
   twist condition, `lattice.py::wrap_tube`). Slice 4 moves the shared
   piece, mapping and periodicity check, into one module both use, so a
   pattern spec can be realised as a relief (macro) or as lattice sites
   (nano). The nano "negative" already exists as
   [pocket-from-molecule](pocket-from-molecule.md). The relief leaf is
   dimension-free (R and h scale together), so it serves µm to 50 cm with
   no code change; only map resolution and mesh budget set the floor.

## Decisions (Reto 2026-10-21)

- Q1 No undercuts, ever. The outer surface is pulled in 2 or 3
  directions (k ∈ {2, 3}), so the pattern adapts to the mold, not the
  mold to the pattern. Positional constraint (corrected 2026-10-21,
  Reto: "it depends which side of the feature we are on"): at a point
  at signed angle θ from its slide's pull (θ > 0 toward the nearer
  join), local frame radial `e_r`, tangential `e_t` (toward the join),
  axial `e_z`, the pull is `p = cos θ e_r − sin θ e_t`. A flank tilted
  β from the local surface, its normal leaning in direction ψ
  (ψ = 0 toward the join, 180° toward the pull centre, 90° axial), has
  `n = cos β e_r + sin β (cos ψ e_t + sin ψ e_z)` and releases at draft
  α when `n·p = cos β cos θ − sin β sin θ cos ψ ≥ sin α`. Cases:
  - join-facing flank (ψ = 0): `β ≤ 90° − α − θ` (29° at a k = 3 join,
    0° at a k = 2 join, where the feature must fade out);
  - centre-facing flank (ψ = 180°): `β ≤ 90° − α + θ`, i.e. it may be
    vertical or even lean over the surface once θ > α;
  - axial-facing flank (ψ = 90°): `cos β cos θ ≥ sin α`, nearly free
    (88° at θ = 60°) until close to the join.
  Which flank faces the join flips between emboss and deboss: on a
  ridge it is the flank on the join side; in a groove it is the wall on
  the centre side (its normal points into the groove, toward the join).
  So every feature is asymmetric off-centre, and mirrored across the
  pull centre. Slice 2's check evaluates `n·p` from the map gradient
  per point, so it needs none of these cases; the cases are what the
  flank generator uses. Bare cylinder at the join is exempt (zero draft
  accepted). The op picks k ∈ {2, 3} and the parting angle φ that
  remove least pattern.
- Q2 Pull direction is modelled in both places. **Design:** shape,
  line and text inputs get pull-aware flanks: each flank's slope is
  chosen per side from the Q1 inequality (join-facing side bounded,
  centre-facing side nearly free), so they come out moldable.
  **DRC:** `mold.undercut` checks every point against its own slide's
  pull (relief maps and anything hand-made); relief maps are refused
  with the undercut image, `relief_fit: clip` on request.
- Q3 Text in slice 1 (matplotlib `TextPath` / fontTools are already
  deps).
- Q4 SLA-printed mold pieces at macro scale; milling later.

## Pattern sources (Reto 2026-10-21: line art, buildings, stones)

All end as `h(u, v)` and then pass the pull-aware flank / DRC step, so
"moldable" is enforced after the art, not by the artist.

1. **Line art with widths** (SVG paths or polylines + width): strokes →
   ridges (emboss) or grooves (deboss) of a stated depth; flanks per
   Q2. Rules the op checks: line width ≥ 2·depth·tan(flank) + a flat
   top (else the ridge becomes a knife edge), gap ≥ the printer/mold
   minimum, and lines that cross a join zone get shallower there.
   Feasible now: this is the shape input of slice 1 with stroking.
2. **Photo → edges → engraving**: Canny/structured edges → cleaned,
   simplified polylines → source 1. Cheap, deterministic, reads as an
   engraving; quality depends on edge cleanup (min segment length,
   merge near-parallel edges).
3. **Photo → depth → bas-relief**: a monocular depth model (Depth
   Anything class, GPU) gives a depth map; bas-relief compression
   (gradient-domain, Weyrich et al. 2007) squashes it to the relief
   depth while keeping visible detail; then clip per pull. Gives a
   sculpted building, not an outline. Needs a GPU depth model on a node
   (not castor's LLM slot); slice of its own.
4. **Procedural masonry** (medieval stones, no photo needed): a
   Voronoi / jittered-course layout of stones, each a rounded dome with
   noise, mortar joints as grooves; parameters: course height, stone
   aspect, joint width/depth, weathering. Fully automatic and moldable by
   construction (domes have gentle flanks). Combines with 2/3: segment a
   photo's wall regions and fill them with masonry.

Ångström analogue (Reto 2026-10-21): the same `(u, v)` pattern carries
categorical fields instead of height: charge sign/ESP band, hydrophobic
vs polar, H-bond donor/acceptor, and so on (the region classes pocket-
from-molecule already paints). Slice 4's shared wrap map projects them
onto a hexfold tube's **outer and inner** wall as pocket patch regions,
repeating with the lattice period. No draft, no pull, no undercut rule
at this scale: atoms are placed, not demolded; the constraint that
replaces draft is the lattice itself (a patch edge lands on sites, so
pattern resolution is one site spacing) and the molecule's approach
pose.

## Explicitly NOT in scope

Sprue/gate/runner/vent/ejector/cooling design; mold-flow simulation (the
flow kernel is thin-chamber only); unscrewing or collapsible cores;
non-cylindrical carriers (cone, freeform) beyond noting that the cone is
the same map with ρ varying in z.

## Target + blast radius

`src/precis/cad/primitives.py` (new leaf), `dsl.py`, `fieldmesh.py`
(narrow band over the leaf), new `src/precis/cad/mold.py`,
`probe.py::probe_draft` (field-aware), `src/precis_se/ops.py` (`mold`
op + `_OPS`), `drc.py` (mold findings), `modes.py` (`molding`); skill
`precis-se-help` op catalog.
