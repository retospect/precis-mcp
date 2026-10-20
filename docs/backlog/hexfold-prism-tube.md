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
- **Closure rule.** Flat strips carry no turning, so a cross-section with
  `n_plus` fins and `n_minus` ribs closes iff `n_plus − n_minus = 6`.
  Anything else is refused by name (`prism.closure`, naming the net turn
  in degrees). Consequences: `++++++` is a hexagonal prism with six fins;
  `+++-+++-+++-` is a 12-seam prism with nine fins and three inward ribs
  at re-entrant notches (3-fold lumen); `++-++-++-` does not close (net
  180°) and is refused.
- **Seed.** Walk the polygon as a turtle: side length = the strip's chord
  width `y_max` (as `rail_tube` computes it from `rows`), headings change
  by ±60° per seam; the seam line sits at the vertex, each wall's dangling
  atoms land `σ` from the seam atom at ±60° off the seam's third-sheet
  direction (the `rail_tube` seed with `theta0` replaced by the fixed
  polygon vertex), the third sheet along the bisector (outward for `+`,
  inward for `−`). No arcs, no `r_wall`; the polygon's circumradius is
  reported as `radius_A` for the viewer only.
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
3. `hexfold/examples/prism_hex6.hx` and `prism_tri12.hx`; the
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

## Open questions

Ready-check 2026-10-20 (verdict: needs-work, 6 blockers, 4 advisories):

- blocker: closure rule "n+ − n− = 6" fixes only the net heading turn, not position closure; equal-length sides need the six-direction unit vectors to sum to zero (e.g. `+++++++-` has n+−n−=6 yet its sides sum to 1+ω ≠ 0, the turtle does not return), and a closing sequence can self-intersect — name a second refusal (`prism.closure` position residual / self-overlap) and the bound on `len(seams)`.
- blocker: `prism_hex6.hx` / `prism_tri12.hx` cannot exist — `.hx` is the public hexfold grammar and the rail tube has "no public grammar" (railtube.py docstring); `tests/hexfold/test_nearest_not_bonded.py::test_shipped_examples_pass` globs every `hexfold/examples/*.hx` and `tests/hexfold/test_seam.py::test_all_pre_seam_examples_covered_by_snapshot` asserts the exact example-name set, so any added `.hx` reddens both. Say what the examples are (scene JSON elsewhere? drop them?) and where they live.
- blocker: registration is unspecified — a new type needs a line in `typed_features._BUILDERS` plus a `FEATURE_TYPE` constant, and `tests/test_hexfold_typed_features.py::test_every_registered_type_has_a_seams_help_section` requires a section in `src/precis/data/skills/precis-hexfold-seams-help.md`; the spec's blast radius lists neither (only the `hexfold_scene` docstring table) so the gate goes red or the type is undispatchable.
- blocker: scene contract contradicts "mirroring rail120": rail120 takes `tube: [rows, periods]` plus a feature with `rails`/`rows`(=strip rows)/`pores` as `{arc, at:[u,v]}` and refuses extra keys; the spec puts `rows`, `periods`, `strip_rows`, `seams`, `pores:[[strip,u,v]]` all in the feature and never says whether `tube`/`k_tether`/`terminate`/`name` stay. Also "strip" names both walls and fins/ribs in `pores [strip,u,v]` and "rows" means wall rows vs strip rows — fix one grammar.
- blocker: spec misstates the code — `rail120` persists no `topology.junctions`/`seam_atoms`/`sigma_A` (only `tunnel.py` does; `relax_block.strain_bookkeeping` raises without `junctions`) and emits `seam.geometry`, not `junction.strain`/`seam.strain` (those are tunnel-reservoirs findings); "existing adapter" for relax does not exist for this type. Either scope writing that bookkeeping (junction zone definition, `tunnel_strain` input shape) or drop the `junction.strain` and `tunnel_strain` claims; the last test bullet is otherwise unbuildable.
- blocker: seed geometry text is wrong/ambiguous — the third sheet makes 120° with each wall, so a wall's dangling atoms sit ±120° (not ±60°) off the third-sheet direction (rail_tube's "60° off radial" is relative to the opposite direction); and polygon side vertex-to-vertex is `y_max + 2σ` with seam atoms at the vertex, not `y_max`. Two builders will get different polygons; state the formulas (plus the rib tip clearance / refusal analogous to `INWARD_TIP_CLEARANCE_A`, absent for the 3-fold lumen ribs).
- advisory: acceptance has no check for the `seams` string parsing (`−` U+2212 vs `-`), `strip_rows`/`periods` bounds, or `to_stored` round-trip frame definition (rail120 uses theta_zero = strip0 half-plane; prism needs its own).
- advisory: `model: sonnet` but the work is novel geometry (turtle polygon, signed seams) and a new generator — Opus-tier per AGENTS sizing.
- advisory: "pores as in the rail tube" reuses private `_pore_cells(pores, periods, rows, rails)`, which validates arc < rails; name whether to generalise it or copy (spec says railtube.py is untouched).
- advisory: umbrella `hexfold-nanoreactor-tube.md` exists (no dangling link); no split needed, though the position-closure/geometry kernel and the se adapter/relax-bookkeeping are separable if relax scoring is kept.
