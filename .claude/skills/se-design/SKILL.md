---
name: se-design
description: "Use when authoring or editing se designs, cad geometry, screw joints, bosses, enclosures, molds, or print/mold process questions: the precis se skill family is the spec — consult it before writing geometry, joints, or holes."
---

# se-design — the product's own skills are the spec

**When.** Any task that authors or changes an `se` design, cad geometry,
fasteners/threaded joints, bosses, enclosures, molds, or asks a
print/mold/fabrication question about one.

**Recipe.**
1. `search(kind='skill', q='<the task>', args={'family': 'se'})` — or go
   straight to the two below.
2. `precis-se-help` — the op grammar. Read before writing DSL: units on
   every vector (`"[x y z] m"`), one workspace slug per design (folder
   ownership is exclusive), `@z` is a node's BASE, envelopes are what
   non-member geometry sees.
3. The task skill — fasteners/stack-ups `precis-se-fasten-help` (read §3
   far-end strategies and §5 realization together); print DRC
   `precis-se-print-help`; assembly fit `precis-se-fit-help`; views/checks
   `precis-se-check-help`; design-tree leaves `precis-se-dtree-help`.
   Anything else: search the family.

**Rounding without fillets.** se represents solids as signed distance
fields, and offsetting an SDF is adding a constant: shrink by `rd`
(erosion) then grow back (dilation) = every convex edge becomes an arc of
radius `rd` with faces back on their nominal planes — mathematical
opening, no fillet geometry. The DSL exposes this as the `rd` key on any
convex primitive: `box:w50mmd100mmh65mmrd3mm` rounds EVERY edge and
corner of the box to 3 mm (`cyl/cone/hex/ngon/frustum/pyramid` too;
`sphere`/`torus` refuse as no-ops; `chamfer` can't carry it). Rules:
`rd < ½·min(dimension)` (parse-time refusal), bounding dims unchanged,
cones lose their sharp apex to a sphere cap. Rounded nodes route the
whole design through the sampled-field backend (`needs_field_backend`):
mesh export works (a 50×100×65 enclosure ≈ 5 s, 17 MB STL) and SDF
probes verify the exact offset math. Field grids round by
`fieldops.open`/`close`/`offset`. The classic enclosure build:
exterior cuboid `rd` + cavity cuboid `rd` (same `rd` as the wall for
constant wall on arcs) → subtract → flat-cut the rim with a planar `cut`
→ bosses/gussets/holes as usual.

**Sharp edges today (open gripes — check them before trusting a number):**
- Stamped fastener holes are cut by YOU into the authored realization
  (skill §5) — and `view='print'` currently mis-fires `feature_not_cut`
  on correctly pre-cut holes (gr525812).
- The fasten pass measures members on AABB-sphere envelopes, not solids —
  stack thicknesses are fantasy, stamped hole origins anchor at the screw
  pose, and a `counterbore=true` can land in the wrong member entirely
  (gr524390; evidence in gr525812).
- The `mold` op refuses cut-modeled parts (the `rd` enclosure above is
  still cut-modeled — prints, doesn't mold; gr524645). `to_openscad`
  silently drops `rd` (renders sharp cubes — the SCAD twin lies;
  gr526014).
- Design numbers (core-hole ratios, engagement, boss diameter) trace to
  `finding`s under `SPACE:design-conventions`, linked from the skills'
  "the numbers" sections — read them before substituting your own.

**Worked example.** `box2-fdm` / `box2-molding` on the server; generating
code in their workspaces (`main.py` → `design.py` recipe → `out.run` +
exports; `check.py` runs the views on a scratch DB; `verify_holes.py`
boolean-probes the folded solid). Follow that shape: math-pure se ops,
formats only at export.

**Missing a skill?** That's a gripe (link it
`raises-concern-about` the nearest skill).
