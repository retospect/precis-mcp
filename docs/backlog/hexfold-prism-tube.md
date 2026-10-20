---
status: ready
pillar: 3d-design
prio: high
model: sonnet
title: hexfold prism tube — a closed tube of flat zigzag-edged graphene strips joined at equal-120 sp² seams, each seam's third sheet an outward fin or an inward rib
---

# hexfold: straight-strip prism tube (`prism-k3-120-z`)

Reto, 2026-10-20 ("the prism is cool, let's build it and see"; prototype,
straight to prod). Umbrella: [hexfold-nanoreactor-tube](hexfold-nanoreactor-tube.md)
(T2 of the nanoreactor chain). Sibling of the arc tube `rail-k3-120-z`
(`src/hexfold/railtube.py`, `src/precis_se/atomic/generators/rail120.py`):
same k3 equal-120 seams (`hexfold.join._seam_k3`), same rib strips, same
pore authoring and site list — the wall sheets are **flat strips instead of
arcs**, and every seam's third sheet may point **outward (a fin) or inward
(a rib)**.

## Why

The arc tube pays the cross-section's turning with wall curvature. Flat
strips carry no curvature, so all residual strain sits on the seam lines,
the lumen is bounded by pristine graphene planes, and outward fins are
ready-made attachment rails for the reservoir sheets (the tube item's
"Y-junction attachment rails"). Inward ribs put a line of open-valence
edge carbons along z inside the lumen — the station sites.

## Geometry (the part the builder must not invent)

- **Strips.** Each wall is a flat `_sheet` strip of `rows` zigzag rows
  (`rows` even, as `rail_tube` requires) and `periods` periods along z;
  its two long edges are zigzag rims. Fins and ribs are strips of
  `strip_rows` rows with one zigzag edge in the seam and one free zigzag
  edge (open-valence carbons = sites), exactly the rail tube's strip.
- **Seams.** Every vertex of the cross-section is one k3 equal-120 seam:
  the two adjacent wall strips and the seam's third sheet divide the 360°
  around the seam line into three dihedral angles of 120°. Zigzag rims
  only (the seam catalogue's rule: three sheets at 120° need zigzag rims;
  armchair gives 97° and non-coplanar bonds).
- **Sign.** A seam is `+` (fin) when the third sheet bisects the exterior
  angle: the walls meet at an interior angle of 120° and the cross-section
  turns by +60°. A seam is `−` (rib) when the third sheet bisects the
  interior angle: the walls meet at a reflex 240° and the turn is −60°.
- **Closure rule.** Flat strips carry no turning, so `n_plus − n_minus = 6`
  is necessary, not sufficient: the cross-section closes iff the equal
  side vectors sum to zero AND the polygon is simple. Refusals by name:
  `prism.closure` — `net turn <deg>°` when `n_plus − n_minus ≠ 6`;
  `open polygon (gap <Å>)` when the vector sum is non-zero;
  `self-intersecting` otherwise. `len(seams)` is bounded to 3…24.
  Consequences: `++++++` is a hexagonal prism with six fins;
  `+++-+++-+++-` is a 12-seam prism with nine fins and three inward ribs
  at re-entrant notches (3-fold lumen); `++-++-++-` does not close (net
  180°) and is refused.
- **Seed.** Walk the polygon as a turtle: headings change by ±60° per
  seam; vertex-to-vertex side length is `y_max + 2σ`, where `y_max` is
  the strip chord width `rail_tube` computes from `rows`. The seam line
  sits at the vertex; the three sheets at a seam are 120° apart around
  the seam line, so each wall's dangling atoms land `σ` from the seam
  atom at ±120° off the seam's third-sheet direction — equivalently σ
  along their own sheet direction — and the third sheet runs along the
  bisector (outward for `+`, inward for `−`). An inward rib keeps
  `INWARD_TIP_CLEARANCE_A` off every wall and every other rib
  (`prism.rib.clearance`). No arcs, no `r_wall`; the polygon's mean
  vertex radius is reported as `radius_A` for the viewer only.
- **Pores** as in the rail tube, authored per strip at a lattice cell,
  refused at a seam rim, a guard column or another pore.
- **Ends** are polygonal zigzag outlines; `opening(port=)` and the `cap`
  registry do not apply (refused `opening.rim.unsupported` by name in v1).
  Reservoir attachment via the fins is a later item.

## Feature contract (se scene)

`{"type": "prism-k3-120-z", "rows": 6, "periods": 8, "strip_rows": 4,
"seams": "++++++", "pores": [[strip, u, v], …]}` — `seams` is a string
of `+`/`−` (`-` accepted), one per seam, read counter-clockwise from the
+x axis. `generated.sites` lists every free-edge carbon of every fin and
rib and every seam atom with `(z, θ, inward normal)` plus a `to_stored`
frame, and same-strip pairs in the 2.5–3.0 Å two-point pendant band —
the `rail-k3-120-z` site contract verbatim, with `seam` entries carrying
their sign. `topology.junctions`/`seam_atoms`/`sigma_A` persist at mint
so `tunnel_strain` re-scores without the stick seed, as for the rail tube.

## Slice (one Devin build, in-repo, no migration)

1. `src/hexfold/prism.py::prism_tube(periods, rows, strip_rows, seams,
   pores=())` returning `(Net, PrismTube)` with the `RailTube`-shaped
   bookkeeping (`seam_atoms`, `strip_atoms`, `wall_atoms`, `pore_rims`,
   `signs`).
2. `src/precis_se/atomic/generators/prism120.py` mirroring `rail120.py`
   (feature type, params vetting, site list, relax adapter with seams
   pinned and strips tethered to their half-planes, walls free).
3. No `.hx` examples — the shipped-example tests pin the exact set; the
   two fixtures live as scene params in `tests/test_se_prism120.py`. The
   `hexfold_scene` docstring's feature table gains the row.
4. Tests, `tests/test_hexfold_prism.py` + `tests/test_se_prism120.py`:
   - `++++++` (rows 6, periods 8, strip_rows 4) builds; `check` reports
     no ERROR; every seam atom is trivalent and its three analytic bond
     angles are 120° ± 5°; the cross-section's six interior angles are
     120° ± 1° on the seed.
   - `+++-+++-+++-` builds; the three `−` seams have interior angle
     240° ± 1°; their rib free edges point at the axis (inward normal dot
     radial < −0.9).
   - `++-++-++-` is refused `prism.closure` naming "net turn 180°";
     `seams` length mismatch, odd `rows`, a pore on a seam rim are
     refused by name.
   - the site list round-trips to the stored atoms within 0.1 Å; seam
     entries carry `sign`.
   - relax (existing adapter, `ml` rung or in-test `relax_block` on a
     short periods=2 block): seam-bond max and face-angle residuals are
     **reported** in `junction.strain` per seam, not gated — prototype.

Acceptance is the test list above. Landing: `/qgo` is acceptable (no
migration, no `safe_fetch`) — Reto 2026-10-20: "it can go right on prod
for prototype".

## Explicitly NOT in scope

Ports/openings on prism ends; reservoir sheets on fins; chiral or
armchair-edged strips; radius steps (`fuse{reducer}`, cone); diffusion
of the pore authoring beyond what `rail_tube` has; any Y-junction other
than the k3 equal-120 seam.

## Target + blast radius

New `src/hexfold/prism.py`, `src/precis_se/atomic/generators/prism120.py`,
two examples, two test files; one-row edits to the `hexfold_scene`
feature table and `src/hexfold/__init__.py` exports. `railtube.py` and
`rail120.py` untouched (shared helpers imported, not moved).

shipped: 1691230ef + 15cce3b28 + c708978e0 (squash-landed by the coordinator
2026-10-20; the ready-check findings were resolved by the brief's
corrections, now in §Geometry).
