---
status: draft
title: precis_surface — lattice-agnostic smooth-surface kernel alongside se, with hexfold as its carbon binding
prio: high
model: opus
blocked-by: hexfold-integration
---

# precis_surface

Design 2026-09-16 (Reto + agent). **The design lives in
`src/hexfold/spec.md` Part III** (§20 smooth layer, §21 `smooth:`
grammar, §22 budget/placement, §23 editing model, §24 se interchange,
§25 MCP surface, §26 catalogue, §27 self-intersection). This item is the
build plan and the precis-side decisions only.

## Package split (decided)

- `src/precis_surface/` — lattice-agnostic: patches, rims as Dirichlet
  curves, seams as Plateau film clusters, curvature bound in caller
  units, direction fields with prescribed singularities, embedding
  checks, mesh realisation. Metres, se frame conventions. Useful for any
  surface-shaped thing (membranes, the 30 nm oval box tiling), not only
  carbon.
- `hexfold.smooth` — the carbon binding: hex-lattice singularity charges,
  σ, the discretiser that emits the discrete `.hx` section from a solved
  surface, the `smooth:` parser/emitter. `precis_surface → hexfold`;
  hexfold never imports precis.
- Storage: a `surface` **binding kind** on se blocks (alongside
  `cad | structure | component | part`), not a new precis kind (no
  totality pincer, no migration for a kind). Rim frame = se datum
  (`se-datum-measure-eval`); cross-scale addressing =
  `se-pick-hierarchy`.
- Cache: `CatalogueStore` protocol (`get`/`put`), DB first — table
  `hexfold_cache(key, format_version, generator, fidelity, authored_json,
  generated_json, created_at)`, one migration; se block `topology`
  references the key. File backend arrives with the pip re-export.
- MCP: reuse `generate` (with `fidelity`), `add_port`, `connect`,
  `set_binding`, views; new small `view='surface'`, `view='catalogue'`,
  op `move_handle`; the one new handler is `options(handle, wish)` =
  `fit.alternatives` surfaced as ranked candidates.

## State (the ordering is spec §28 — steps 4–6 and 8; this list is ticks only)

- [ ] §28.4 stage 1 — symbolic chain solver with a stub geometry
  backend; proves the interface claim before any geometry.
- [ ] §28.5 — straight tubes, symmetric collars, caps from the cache;
  then the discrete-mesh smooth solve, the two-part curvature bound,
  seams as film clusters. Closed forms as seeds and oracle only.
- [ ] §28.6 — direction field, then the bent collar behind the `fit`
  family interface.
- [ ] §27 self-intersection tiers (BVH in the loop, CCD in relaxation,
  repulsive energy on finalists; from the papers). Rides with §28.5.
- [ ] §28.8 valve tool set (`rotary-ratchet-valve.md`): clearance field
  → pocket extractor → attachment-site enumerator → complementarity
  scorer stub → bond-energy audit → drag-vs-torque. The clearance stub on
  `stick` atoms + vdW radii can start before §28.5's mesh exists (valve
  Q4); the enumerator is discrete and can ride with hexgen (§28.3).
- [ ] `strain_max` default from a literature lookup (spec §29 Q5).

## Open (spec §29, verify early)

Q1 bent-collar twist well-defined on the rim loop; Q2 field ≡ ring
placement holds for charges (Poincaré–Hopf), positions are hex-remeshing
integrability; Q3 bent-collar search — instrument before trusting
interactivity.

## Slice 1 — the dual route (designed 2026-09-24, Reto + agent)

**Decision: no MIQ, no direction field.** The dual of a degree-controlled
triangulation *is* the hex tiling — each triangle is an atom, each
degree-n vertex an n-ring, and the dual's 3-valence is sp2 for free. The
geometries agree: equilateral triangles of edge `a` dualise to atoms
`a/sqrt(3)` apart, so `a = bond*sqrt(3) = 2.46 A` is exactly graphene's
lattice constant. §20.5's integrability problem is sidestepped, not
solved — it returns only if a later slice wants a *prescribed* field.

Pipeline, all inside `src/precis_surface/`:

    level set -> periodic marching cubes -> [split | collapse | flip for
    degree | tangential smooth | reproject]xN -> dualise -> hexfold net
    -> stick relaxation

**Relaxation is inside the loop, not after it** (Botsch-Kobbelt). Degree
decisions are curvature decisions and curvature is geometric, so each
smoothing pass makes the next degree decision better-posed; a flip
decided on a marching-cubes sliver is arbitrary. Two distinct
relaxations, different constraints: *mesh* relaxation (tangential smooth
+ reproject onto the level set) inside the loop, and *net* relaxation
(`hexfold.stick`, unconstrained) once topology settles — unconstrained
too early and the net leaves the surface that subsequent curvature
measurements are taken against. Invariant asserted every iteration:
**chi must not change** (flips preserve V/E/F; splits and collapses
preserve V-E+F).

### Interface contract (fixed here so parallel work agrees)

- `Mesh = (verts: (N,3) float64, tris: (M,3) int64)`, CCW outward.
- Periodic identification is carried separately as
  `wrap: (K,2) int64` pairs of identified vertex indices plus the
  `(3,3)` cell matrix — never by duplicating vertices. chi is computed
  on the welded quotient.
- Curvature functions take `Mesh` and return per-vertex or per-face
  float arrays. Pure functions, no store access, unit-agnostic
  (`precis.structsolve` house rules).
- **Widened during the build (2026-09-24), additive, not breaking.**
  `periodic_mesh()` gained an optional `grad=` callable (the level set's
  analytic gradient, used for exact orientation — see the traps below;
  it falls back to central differences when omitted, so an arbitrary
  caller-supplied field still works). `PeriodicMesh` gained
  `orientation_fallback_count`, the number of faces whose orientation
  had to come from combinatorial propagation because `|grad|` was near
  zero — 0 for P/D/G at every resolution tested, but reported rather
  than hidden so a surface that does hit field critical points says so.
- The dual returns a hexfold-shaped net: ring list (vertex -> ring size)
  and atom list (triangle -> atom), handed to hexfold across the
  existing precis->hexfold direction only.

### Acceptance

Mesh-derived, nothing hardcoded: welded quotient of
`cos x + cos y + cos z = 0` on one **simple-cubic** cell has every edge
in exactly 2 triangles; `V-E+F == -4`; angle-defect sum / 2pi == -4;
`hexfold.defects.counting_residual({7:24, 6:80}, 0, -4) == 0` and
`({8:12, 6:80}, 0, -4) == 0`.

**Met, 2026-09-24.** Gauss-Bonnet closes to ~1e-13 on all three
families at two resolutions each (P -4, D -16, gyroid -8) — and note
this is the two modules cross-validating, since `curvature.py` and
`periodic_mesh.py` were built in isolation from each other.

**Dropped from slice 1: "K <= 0 on every face".** It is a true
statement about a TPMS and a false one about its marching-cubes
discretisation, so asserting it here would have been unmeetable. See
below.

**Centring trap:** body-centring swaps the two labyrinths of P/D. Tag it
and chi comes out -2 with a 12-heptagon "requirement". Pin the
simple-cubic cell; the future periodic fuse must not tag body-centring
translations.

### Heptagon placement is CONSTRAINED, not curvature-greedy

**Correction to the first draft of this slice (2026-09-24, prior-art
pass).** "Bias 7s toward where Gaussian curvature is most negative" is
wrong on its own: the isolated-heptagon rule (the IPR analogue) needs
roughly 2:1 hexagons:heptagons to avoid heptalene units, and a
curvature-greedy remesher clusters 7s exactly where the chemistry
forbids it. Published models *dilute* heptagons with extra hexagons
instead.

The constraint is already designed — spec §20.4's spacing bound
(`smooth.singularity_spacing`, singularities at least two ring-steps
apart) and hexfold's disjoint cut-disk rule (§8, `defects.disks_disjoint`).
Carry it into the remesh loop as a hard filter on candidate flips, not
as a post-hoc check: curvature supplies the *ranking*, spacing supplies
the *admissibility*.

### Prior art (found 2026-09-24; cite, do not imply novelty)

The full pipeline appears unpublished, but every ingredient is not, and
two results bear directly on the build:

- **NanoCap** (Robinson, Suarez-Martinez, Marks, *Comput. Phys. Commun.*
  2014, doi:10.1016/j.cpc.2014.06.014) is this exact dual trick — build
  the triangular dual, relax it with a **dual-lattice force field**,
  extract the 3-coordinate carbon net — but spherical/capped-tube only
  (12 degree-5 vertices), no TPMS, no periodic cell. The closest
  methodological ancestor. Read their dual force field before assuming
  `hexfold.stick` on the extracted net is the right relaxation.
- **Delaunay triangulation on a TPMS is an acknowledged unsolved tooling
  problem** (Teillaud/Inria). Read carefully: that is about *intrinsic*
  Delaunay on hyperbolic surfaces. Our loop is *extrinsic* isotropic
  remeshing of an embedded surface in E³ (Botsch–Kobbelt), which is
  routine. Keep the distinction — do not import an intrinsic-hyperbolic
  formulation, and do not treat the Inria statement as a blocker.
- **The symmetry-decoration route is the published one**: Hyde &
  Pedersen, *Proc. R. Soc. A* 477 (2021) 20200372,
  doi:10.1098/rspa.2020.0372 — enumerate (n,3) reticulations of H², project
  onto the TPMS, symmetrize in E³. Their (7,3) P-net sits at Wyckoff 8g
  + 24k + 24k in P432, 56 vertices. Database: EPINET
  (doi:10.1107/S0108767308040592). This is slice 2's method, already
  done by someone else — slice 2 should reuse it, not reinvent it.
- Their warning, which our route hits from the other side: projecting a
  hyperbolic tiling into E³ produces **"collar cycles"** around the
  channels whose edges do not match the tile size, so the tiling is
  combinatorially right and geometrically strained. Isotropy in E³ and
  correct valence topology are in tension. Expect it.
- **Atoms do not stay on the surface anyway** (Braun et al., *PNAS* 115
  (2018) E8116, `pa341376`): schwarzites placed exactly on the TPMS move
  off it under relaxation. Reinforces the scaffold-not-answer reading of
  §20.1 — exact-on-surface meshing buys less than it appears to.
- Leapfrog transformation (King, *J. Phys. Chem.* 100 (1996) 15096,
  doi:10.1021/jp9613201) is the published dual-based schwarzite
  construction, purely combinatorial — C168 is the leapfrog of Klein's
  quartic.
- Terrones & Terrones NJP 5 (2003) 126 is now **`pa449642`**. `[S27]`
  `pa343409` is confirmed chunk-less — cite-able but unreadable.
  Unrelated doc bug: `spec.md:1318` lists the NJP paper as "Related"
  under [S29] rather than as its own entry.

### C216 asymmetric unit — still unverified

The arithmetic decomposition (216 = 48·4 + 24; 80 hexagons = 48+24+8;
one mirror-straddling heptagon) is self-consistent and the space group
is reported as Pm-3m (221), point group order 48, which agrees. But no
published Wyckoff table for C216 was found. Check EPINET
(epinet.anu.edu.au) and the RSPA supplementary directly before slice 2
relies on it.

### Measured, 2026-09-24 — use an ODD grid n

chi is correct at every resolution tested (P -4, D -16, gyroid -8 on the
simple-cubic cell, n = 16/17/20/24/32, edge-manifold closed throughout,
each verified against the analytic gradient with zero misoriented
triangles). Geometry quality, however, splits sharply on grid parity:

| grid | near-degenerate triangles |
|---|---|
| odd n  | 0.0 - 5.6 % |
| even n | 18 - 49 % (worst: Schwarz D at n=16) |

At even `n` the grid planes coincide with the surface's own symmetry
planes, so a large sample population lands in the float-noise band and
marching cubes resolves those cells degenerately. An odd grid misses
those planes. **Default to odd `n`.** Topology is unaffected either way,
so this is a quality knob, not a correctness one — but feeding 49%
slivers into the remesh loop wastes its entire first pass.

Two traps recorded from getting this wrong once each:

- **Never assert `area == 0.0`** to detect degeneracy. The slivers have
  areas around 9e-23 against a median of 1e-3 — degenerate by twenty
  orders of magnitude, and never bit-exactly zero. A test written that
  way passes vacuously. Same trap as the sampling tie-break: symmetric
  grid points cancel algebraically to ~1e-16, not to 0.0, which is why
  the tie-break is a tolerance band (`|f| < 1e-8`) and not an equality.
  That band is safe because the measured gap between the noise
  population (max ~1.4e-15) and the smallest genuine sample (~2.8e-3) is
  twelve to fourteen orders of magnitude.
- **Orient by the level-set gradient, never by a finite probe along the
  normal.** A fixed-eps probe misorients ~36% of triangles because a
  TPMS's sheets pass close enough that the probe lands on the
  neighbouring sheet. `dot(normal, grad f)` at the centroid is exact and
  has no step size to tune. Near-zero-gradient faces (none occur for
  P/D/G at these grids, but the path is tested) fall back to
  breadth-first orientation propagation over the welded face adjacency.

Slivers that remain are ordinary marching-cubes output and are the
remesh loop's edge-collapse step to remove — the strict no-degeneracy
guarantee belongs to the post-remesh test, not the mesher's.

### Raw MC curvature — measured, and MILDER than first reported

**The first version of this section was wrong, and wrong in an
instructive way.** It reported 5-20% of vertices carrying positive angle
defect "up to a full 2*pi" (spike vertices), and concluded the remesh
loop could not use curvature until after a cleanup phase. That table was
produced by measuring angles on *welded* coordinates: a triangle
touching the high face refers to its low-face representative, so its
geometry was read across the whole cell. The bug was invisible to the
Gauss-Bonnet check, because `sum(defect) == 2*pi*chi` is a purely
combinatorial identity (`2*pi*V - pi*F` for any closed triangle mesh,
whatever the coordinates) — the total cannot detect a coordinate error,
only the distribution can, and the distribution is what the remesh loop
reads. `report.py` exists partly because of this.

Corrected, with `surface_report` (angles measured on original
coordinates, accumulated into welded slots):

| family | positive-defect vertices | max defect | typical min defect |
|---|---|---|---|
| P  n=17/25/33 | 20.0 / 20.4 / 20.9 % | +4.9e-3 → +1.3e-3 | -0.12 → -0.035 |
| D  n=17/25/33 | 4.1 / 4.1 / 4.1 % | +8.6e-3 → +1.9e-3 | -0.22 → -0.063 |
| G  n=17/25/33 | 0.3 / 0.6 / 0.6 % | +3.4e-4 → +8.0e-5 | -0.13 → -0.036 |

The positive defects are small and **shrink with refinement**, i.e. they
are noise about zero rather than structure: for P at n=17 the largest
positive defect is ~4% of the typical negative one. A TPMS still has
K <= 0 everywhere, so they remain artifacts — but not disqualifying ones.

**Revised sequencing consequence.** Curvature-driven degree optimisation
does not need to be deferred behind a cleanup phase on noise grounds.
Cleanup first is still right, for the reasons that actually hold:
`edge_len_cv` is ~0.43 on raw marching-cubes output (the dual wants
near-equilateral triangles at `bond*sqrt(3)`), and slivers appear at
some resolutions. So: equalise edges, collapse slivers, smooth,
reproject — then optimise degree, curvature-ranked and
spacing-constrained. `SurfaceReport.positive_defect_frac` remains the
honest gate to read before trusting per-vertex curvature; it just turns
out to pass much earlier than the bad table implied.

### The counting residual is automatic — it does not evidence a tiling

**Measured 2026-09-26, after the dual landed.** Slice 1's acceptance line
`counting_residual(hist, 0, chi) == 0` (and equivalently
`sum(6 - ring_size) == 6*chi`) is an algebraic identity of any closed
triangulation, not a property of a good tiling: ring sizes are the
triangulation's vertex degrees, so their sum is `2E`, and
`6V - 2E == 6*chi` follows from `E == 3F/2` and `chi == V - E + F` alone.
It therefore holds for the raw marching-cubes dual and would hold for an
arbitrarily bad one. Keep it as a cheap wiring check; do NOT read it as
evidence the net is schwarzite-like. **The ring histogram itself is the
measurement.**

Raw dual, Schwarz P at `a = 8`, `n = 17` (no remesh): `V,E,F = 1080, 3252,
2168`, `chi = -4`, every dual atom exactly 3-valent (the "3-valence is sp2
for free" claim holds), but the census is `{4: 158, 5: 114, 6: 532, 7: 108,
8: 158, 9: 10}` — 49.3 % hexagons, with 4- and 9-rings that are not real sp2
carbon. Gyroid `n = 17` is worse. So the remesh loop is the whole
difference between a scaffold and a tiling, and the histogram is how to tell
which one you have.

**Ruling (Reto, 2026-09-26): rings are `{5, 6, 7}` only.** Pentagons are
legitimate sp2 carbon — every fullerene is pentagons and hexagons — so
degree 5 is an acceptable remesh outcome, not a defect to reject. What is
forbidden is 4-rings and below and 8-rings and above, which is exactly the
population the raw dual produces. This replaces the earlier `{6, 7}`-only
reading of the heptagon-placement section: valence optimisation still ranks
toward 6, but only degrees `<= 4` and `>= 8` are inadmissible, and the
isolated-heptagon rule drops from a hard filter to a soft preference. Under
`{5, 6, 7}` the counting identity reads `count(5) - count(7) == 6*chi`
(`-24` on P), with both counts free to be nonzero — still automatic, so
still not evidence. The acceptance criterion for the remesh loop is
therefore: **every degree in `{5, 6, 7}`, asserted hard**, with hexagon
dominance (`count(6) / total >= 0.8`) as the quality goal.

### The remesh loop: flips are necessary but NOT sufficient

**Decided 2026-09-26, after two build attempts at the full loop collapsed
under their own size.** An edge flip replaces the edge shared by triangles
`(a,b,c)` and `(a,b,d)` with the edge `(c,d)`: degrees change by
`-1,-1,+1,+1` while `V`, `E` and `F` each stay put. So chi is preserved
*structurally* — there is nothing to assert-and-hope about — and, decisively,
the periodic `wrap` pairing never needs surgery. Split and collapse both
change vertex counts and drag wrap-pair bookkeeping in behind them, which is
where the complexity that killed the earlier attempts actually lived.

**That reasoning was wrong about sufficiency, and the flip-only loop measured
it.** Flips move the degree census but cannot change how many vertices exist,
so a degree-4 vertex in a locked neighbourhood has no admissible flip that
relieves it. Measured on Schwarz P `n=17`: 326 of 1080 welded vertices start
outside `{5,6,7}`; flip-only converges to 155, census
`{4:81, 5:108, 6:681, 7:136, 8:64, 9:10}`, hexagon share 49 % → 63 %.
Identical at 12 and 30 iterations, so it is a plateau, not an under-run. Of
those 155, **zero are on the wrap seam**, so the frozen-seam simplification is
not the limitation and should be kept.

The operation that removes a degree-4 vertex is an edge COLLAPSE; the one that
relieves a degree-8 or -9 vertex is a SPLIT. Both change `V`, `E` and `F` but
leave chi invariant (collapse: −1 vertex, −3 edges, −2 faces; split the
reverse), so the chi assertion still holds. They are therefore required for the
`{5,6,7}` ruling, not merely for edge-length isotropy, and were added on top of
the working flip loop rather than built from scratch. The guard that matters on
collapse is the **link condition** — the intersection of the two endpoints'
one-ring links must be exactly the two vertices opposite the edge — since
violating it silently produces non-manifold geometry. Keep refusing both
operators on seam vertices and wrap-crossing edges: nothing needing repair
lives there.

### Deliberately out of slice 1

Isotropic edge *length* as a target in its own right (split and collapse are
in, but driven by valence repair, not by an edge-length band). Area
minimisation (the nodal surface is taken
as given); the direction
field; `smooth:` grammar; the asymmetric-unit enumeration (slice 2 —
C216's unit is ~5 atoms and one mirror-straddling heptagon, so it is
enumerable rather than searchable); `view='surface'` on the se handler
(spec §25.3's is a per-atom uv lookup, a different thing).

### Open before the test can cite

Terrones & Terrones, *New J. Phys.* 5 (2003) 126 is not in the store and
`[S27]` pa343409 has no chunks. Import both, and search prior art:
TPMS-triangulate-and-dualise is very likely published — cite it rather
than implying novelty.

## The ring census cannot see the chemistry (dogfood, 2026-09-26)

Slice 1 shipped in `27f5fc3f` and its acceptance measurement was the ring
histogram: Schwarz P at `cell_A=8.0, n=17` gives `{5:114, 6:700, 7:138}`,
zero rings outside `{5,6,7}`, `count(5)−count(7) == 6·chi == −24`. All of
that is true and none of it is enough.

**The histogram is scale-invariant.** It depends only on `n` — the census at
`cell_A=8.0` and at `cell_A=45.8` is bit-identical. So it says nothing about
whether the atoms sit at carbon distances, and they do not: at the config
above the mean C–C bond is **0.248 Å** against graphene's 1.42, about 5.7×
too short, putting ~1912 atoms on a ~150 Å² surface at roughly 33× graphene's
areal density. `cell_A` and `n` are independent parameters and exactly one
pairing per `n` yields carbon (`cell_A ≈ 45.8 Å` at `n=17`). Filed as
gripe 451269; the input-surface friction found alongside it is 451270.

This is the *same trap* as the counting-residual note above, one layer up.
That note says `counting_residual == 0` is algebraically automatic and so
"does not evidence a tiling". The ring histogram is the next number of that
kind: automatic in the scale, and so it does not evidence a *structure*.
Bond length is the first quantity in this stack that is neither count- nor
scale-invariant.

Two consequences for how this package is accepted, not just for the bug:

- **A uniform rescale is not the fix.** At the corrected `cell_A` the mean
  is 1.420 Å but the spread is 0.726–2.373 Å. `remesh` equalises *valence*,
  not edge length — its tangential smoothing has no edge-length term. Either
  add one, or measure the achievable spread and gate on it explicitly.
- **Every future acceptance number gets asked what it cannot see.** The
  omission was structural, not careless: 13 tpms tests exist covering
  envelope, all-carbon, chi, bond count, reps, ports, even-n, family,
  histogram, raw scaffold and the gyroid refusal — and none asserts a bond
  length, though `tests/test_se_atomic_generators.py`'s own `_bond_lengths`
  helper is already used by the cnt, fullerene and cone families.

### Part (a) landed 2026-09-26 — the scale error is no longer generatable

`build_tpms` now measures the mean C–C bond of what it just built and
refuses outside `[1.2, 1.7]` Å, naming the `cell_A` that *would* work at the
requested `n` (derived, not tabulated: lengths scale linearly in `cell_A`, so
one measurement is exact). `cell_A=8.0, n=17` — the config this document
called known-good — now raises instead of emitting 0.248 Å carbon. Three
tests cover it, and `topology` gained `bond_mean_A` / `bond_min_A` /
`bond_max_A`.

Gated on the **mean**, not the spread, on purpose: gating on the spread today
would refuse every input and take the family offline, since part (c) below is
unfixed. The spread is reported instead, so the remesh work has a number to
move.

Every tpms test had to be re-based off `cell_A=8.0` — they were certifying
non-carbon output, and the scale-invariant histogram let them. The
carbon-scale `cell_A` differs per config (P/n=11 raw 31.7 Å, P/n=17 raw
47.9 Å, P/n=17 remeshed 45.8 Å, G/n=17 raw 47.2 Å), which is why the check
derives the value rather than holding a constant.

**New measurement, and it inverts the intuition: remesh makes the spread
worse.** Schwarz P at n=17 is 0.974–2.182 Å raw and 0.727–2.375 Å remeshed.
Ring purity is bought *with* edge-length uniformity, so part (c) is not
"tighten a loop that already nearly works" — the loop is actively moving in
the wrong direction on this metric, and an edge-length term has to fight the
valence term rather than merely supplement it.
