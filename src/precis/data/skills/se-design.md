---
id: se-design
family: se
title: precis — route any se/cad design task through the se skills
summary: the repo entry point for se/cad work — the product's own se skill family is the spec, so search it before writing geometry, joints or holes; covers the rd rounding trick, live sharp-edge caveats and the box2 worked example
answers:
  - how do I author or edit an se design without guessing the DSL?
  - which se skill covers fasteners, print DRC, assembly fit or checks?
  - how do I round edges in se when there is no fillet op?
  - what known sharp edges should I check before trusting a number?
  - what do I do when no se skill covers my task?
applies-to: authoring or changing se designs, cad geometry, joints, bosses, enclosures, molds
status: active
tags: [workflow, design]
kinds: [se, cad]
governs: factory
---

# se-design — the product's own skills are the spec

**When.** Any task that authors or changes an `se` design, cad geometry,
fasteners/threaded joints, bosses, enclosures, molds, or asks a
print/mold/fabrication question about one.

**Recipe.**
1. `search(kind='skill', q='<the task>', args={'family': 'se'})` — or go
   straight to the two below.
2. [[precis-se-help]] — the op grammar. Read before writing DSL: units on
   every vector (`"[x y z] m"`), one workspace slug per design (folder
   ownership is exclusive), `@z` is a node's BASE, envelopes are what
   non-member geometry sees.
3. The task skill — fasteners/stack-ups [[precis-se-fasten-help]] (read §3
   far-end strategies and §5 realization together); print DRC
   `precis-se-print-help`; assembly fit `precis-se-fit-help`; views/checks
   `precis-se-check-help`; design-tree leaves `precis-se-dtree-help`.
   Anything else: search the family.

## Rounding without fillets (`rd`)

se represents solids as signed distance fields, and offsetting an SDF is
adding a constant: shrink by `rd` (erosion) then grow back (dilation) =
every convex edge becomes an arc of radius `rd` with faces back on their
nominal planes — mathematical opening, no fillet geometry. The op grammar
and per-primitive support live in [[precis-se-help]]; the rules to
remember here:

- `rd < ½·min(dimension)` (parse-time refusal); bounding dims unchanged;
  cones lose their sharp apex to a sphere cap.
- Rounded nodes route the whole design through the sampled-field backend
  (`needs_field_backend`): mesh export works (a 50×100×65 enclosure ≈
  5 s, 17 MB STL) and SDF probes verify the exact offset math. Field
  grids round by `fieldops.open`/`close`/`offset`.
- The classic enclosure build: exterior cuboid `rd` + cavity cuboid `rd`
  (same `rd` as the wall for constant wall on arcs) → subtract →
  flat-cut the rim with a planar `cut` → bosses/gussets/holes as usual.

## Sharp edges — open gripes

**Check the live gripe before trusting a number** — these are filed,
time-sensitive caveats:

- Stamped fastener holes are cut by YOU into the authored realization
  (skill §5) — and `view='print'` was reported mis-firing
  `feature_not_cut` on correctly pre-cut holes (gr525812).
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

## Worked example

`box2-fdm` / `box2-molding` on the server; generating code in their
workspaces (`main.py` → `design.py` recipe → `out.run` + exports;
`check.py` runs the views on a scratch DB; `verify_holes.py`
boolean-probes the folded solid). Follow that shape: math-pure se ops,
formats only at export. Domain rules for the bosses/gussets in that
build: [[boss-design]].

**Missing a skill?** That's a gripe (link it
`raises-concern-about` the nearest skill).

## See also

- [[precis-se-help]] — the op grammar every se task starts from.
- [[precis-se-design-help]] — the product-side design skill.
- [[boss-design]] — domain numbers for screw bosses and ribs.
- [[precis-se-fasten-help]] — fasteners and threaded stack-ups.
