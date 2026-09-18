# se: print-in-place groups — one build frame and one 3MF for a whole-assembly fdm group
**Superseded 2026-09-18** by the print `intent` table in
`structural-solution-space.md` §Slice 4 bridge (`manufacture` = this item
+ fusion + cavities; `model` = fit-test stand-ins). Kept for the test
sketch below until that slice ships, then delete.

Follow-on filed at the ship of `se-print-implementer` (2026-09-17; the
per-block chain `realize` → `view='print'` → `view='fab'` is in
`precis_se.printing` / `precis_se.printsolid` / `precis.cad.printability`,
see the `precis_se` package docstring, L5). That item stops at one block
per file. Owner anchor: `precis_se.printing`.

**What.** A `whole-assembly` fdm mode on an ancestor block → the group
(every fdm member below it, membership derived from the mode on the tree,
no schema change) gets one shared build frame scored on the union of the
members' meshes in their world poses, one 3MF with an object per member
in world pose (`cad.export.export_mesh` already writes per-component
3MF), and the in-place clearance rule: a `connect` joint with DOF
between two members of the group needs a gap ≥ the process floor
(`capabilities.resolve(..., 'min_feature')` / the clearance-hole
compensation) — the `printed-` port convention and the same-print-step
lint from `cad-print-in-place.md` are the cad precedent.

**Why.** The unicycle-mk2 dogfood (hub + spokes + rim) is a group, not a
set of separate prints; today `view='fab'` lists each member with its
own `view='print'` handle and nothing says they print together.

**Test:** a hub-and-spokes fixture under a `whole-assembly` ancestor
writes one 3MF with N objects; two members joined by a `revolute`
connect with a sub-floor gap get an `in_place_clearance` finding;
`view='fab'` collapses the group to one row with the group handle.
