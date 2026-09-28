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

- [x] §28.4 stage 1 — symbolic chain solver with a stub geometry
  backend (`hexfold.chain`, 2026-09-28; state in
  `hexfold-integration.md` "Step 4 built"). The smooth layer plugs in as
  a `GeometryBackend`.
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

### Part (c) root-caused 2026-09-26: the net is scarred, not merely uneven

Six measurements, all at Schwarz P `cell_A=45.8, n=17` (the carbon-scale
remeshed config), replace the earlier guess that part (c) is an edge-length
term.

**1. Gauss–Bonnet is satisfied exactly, and it needs almost nothing.** Total
angle defect `-25.1327` rad `== 2*pi*chi` at `chi=-4`, to the printed digit.
The census `{5: 114, 6: 700, 7: 138}` gives `sum(6-k) = -24 = 6*chi`. So the
topology requires a *net* 24 defect rings; the mesh carries **252**.

**2. 89% of the pentagons touch a heptagon** (101 of 114). A 5-7 adjacent pair
is a dislocation: zero net curvature, pure strain. So ~114 of the 126 excess
pairs are marching-cubes scars that cancel, and nothing in the remesh loop
removes them. This is the cause of the 0.773–4.535 Å edge spread — not
"density" in the generic sense.

**3. Curvature cannot place the rings at any useful density.** `_ideal_degrees`
computes `round(6 - defect/(pi/3))`, but per-vertex defect here is
`-0.0264` rad against the `pi/3 = 1.0472` quantum — 40x under-resolved. The
unclipped ideal spans **6.00 to 6.10**: every vertex rounds to 6, the target is
uniformly hexagonal, and the 5s and 7s are placed by cost-function
tie-breaking. `corr(degree, K) = -0.086`. Any future placement criterion has to
*integrate* curvature until it accumulates `pi/3`, not sample it per vertex.

**4. The curvature ceiling is a statement about `n` alone.** A vertex absorbs
`|defect| <= pi/3`; `|K|max ~ 1/a^2` while area-per-vertex `~ a^2/n^2`, so the
margin is scale-invariant in `cell_A` — max defect is bit-identical across
`cell_A` 22.9 / 45.8 / 91.6 at fixed `n`. Margin is 2.9x at `n=9`, 5.3x at 13,
8.5x at 17, 17.4x at 25; overflow would need `n ~ 5`. Nothing *checks* it
though: `np.clip(ideal, 5, 7)` discards overflow silently, so an over-curved
input would report success while emitting a net that cannot be carbon — the
same failure shape as the 0.248 Å bond. Cheap gate, same principle as part (a).

**5. The annihilating move is already legal and already implemented.** For a
collapse of `(u,v)` with apexes `(c,d)`, `_eval_collapse_candidate` keeps
`deg(u)+deg(v)-4` and decrements both apexes, admitting anything that stays
inside `[5,7]`. That admits `(5,5,7,7) -> (6,6,6)`: four defects annihilated,
purity intact, no transient out-of-band degree and no new operator. Measured on
this mesh: **315 legal collapses exist, 25 strictly defect-reducing, and 0 are
offered to the pass** — the caller only hands `_eval_collapse_candidate` edges
with an endpoint at degree `<= 4`. Flips conserve defect count (two `-1`, two
`+1`), so `_flip_pass` is the glide step that brings pairs into contact. Part
(c) is therefore a *candidate-pool and scoring* change, not new machinery.

**6. Relaxation mitigates, it cannot fix.** `relax_graph` on the dual (1912
atoms, 192 rim atoms pinned, 8 bursts x 25 iters with surface reprojection
between) took hard validator errors 395 -> 16 with the cell extent unchanged
(rim pinning holds periodicity), but *doubled* advisory angle strain
1457 -> 2363: it spends angles to buy bond lengths, because a dislocation is
combinatorial and no atom move can remove one. 477 s.

The bond target is not the lever, which kills a tempting unification with
`nanostructure-check-tiers.md` step 3. A/B at identical budget: single-bond
1.52 Å (georelax's hybridization-blind default) gives 16 errors / 2363 strain /
mean 1.456; aromatic 1.42 Å gives **55** errors / 2158 strain / mean 1.446. The
target moved 7% and the resulting mean moved 0.7% — the surface constraint and
non-bond repulsion dominate, so a shorter target just crowds atoms and triples
geometric `over_valence` (13 -> 46). The hybridization-blind bond ideal in
`hexfold/check.py`, `hexfold/stick.py` and `precis/structure/georelax.py` is
still a real defect; it is not this one.

**Order of work implied:** annihilate dislocations combinatorially (extend the
collapse candidate pool beyond degree `<= 4`, score by defect reduction, glide
with flips) until the census approaches the required 24, *then* anneal
geometry. Equal-area triangles at this vertex count already imply a dual bond
of 1.412 Å against carbon's 1.42, so uniformity alone lands the chemistry. A
planarity term (minimise each atom's neighbours' distance to its tangent plane
— an sp2 pyramidalization penalty) is better targeted than bond springs for
the annealing step, since strain and twist are what degrade; it needs the
reprojection or the pinned rim to hold the shape, because planarity alone
minimises at a flat sheet.

### Part (c) prototyped 2026-09-26: two one-line relaxations halve the scars

Measured in-process (no tree edit) on Schwarz P `cell_A=45.8, n=17`. Neither
change adds an operator or weakens an invariant — the `[5,7]` purity gate, link
condition, boundary/wrap refusals and `abs(degree - ideal)` score are all
untouched. Defect count, with `chi=-4` and closed-manifold assertions holding
throughout:

- stock: **252** — rings `{5:114, 6:700, 7:138}`
- collapse pool widened to every edge: **214** — `{5:95, 6:697, 7:119}`
- score-neutral flips accepted: **150** — `{5:63, 6:804, 7:87}`
- both: **122** — `{5:49, 6:764, 7:73}`

**Relaxation 1 — the collapse candidate pool.** `_collapse_pass` skips any edge
whose endpoints both exceed degree 4 (its pool filter, plus the matching early
return in `_eval_collapse_candidate`, "neither endpoint is this pass's
degree<=4 target"). Measured on the stock mesh: 315 legal collapses exist and 25
strictly reduce the defect count — **0 are offered.** Widening the pool needs a
`keep` tie-break for the both-high case; everything else already admits these.
The strongest available move is `(5,5,7,7) -> (6,6,6)`: four defects gone,
`keep_deg = 5+5-4 = 6`, both apexes `7 -> 6`, every result inside `[5,7]`.

**Relaxation 2 — flips cannot glide.** `_eval_candidate` ends
`if improvement <= 0: return None`. A flip that *moves* a dislocation is
score-neutral (two `-1`s and two `+1`s, so it conserves the census exactly),
therefore refused — yet that move is the glide step that brings a surviving 5-7
pair into an annihilable configuration. Accepting score-0 flips alone beats the
widened collapse pool.

**Where it stalls, and why it is not the seam.** Converged at 122 by iteration
20 (iters=60 gives the identical census). `49 - 73 = -24` exactly, so the net
curvature is right and all 122 are 49 cancelling pairs plus the 24 required.
Seam-touching share of the residual is **34%, against a 33% base rate** — no
enrichment, so the wrap refusals are not the wall; 81 of 122 are interior local
minima. With `ideal` uniformly 6 almost every move scores 0 and the tie-break is
canonical index, so glide is an arbitrary plateau walk. Making it directional
needs a defect-pair-aware cost (reward reducing 5-to-7 separation), not a new
operator.

**Geometry is not improved by this alone** — bond std 0.278 to 0.319, because
`_apply_collapse_raw` relabels a removed vertex to its neighbour's position and
nothing has annealed. The combinatorial and geometric phases are separate; this
part only removes the strain *source*.

### Part (c) correction 2026-09-26: scar reduction via collapse made chemistry WORSE

The recommendation two sections up — annihilate dislocations first, then anneal —
is **not supported end to end.** Same anneal budget (`relax_graph`, sp2
hybridizations, 192 rim atoms pinned, 8 bursts of 25 iters with surface
reprojection between):

- scarred net, 252 defects, 1912 atoms: 395 errors annealing to **16**,
  angle strain 1457 to 2363, mean bond 1.456 Å
- scar-reduced net, 122 defects, 1780 atoms: 350 errors annealing to **79**,
  angle strain 1636 to 2366, mean bond 1.518 Å

Halving the dislocations left angle strain unchanged and made hard errors five
times worse. The confound is identifiable: **annihilating via collapse removes
vertices**, 952 to 886 canonical (atoms 1912 to 1780), and on a fixed-area
surface the vertex count sets the bond length. `sqrt(952/886) = 1.036`, so the
achievable bond rose 3.6% away from carbon; the pre-anneal net also arrived with
`bond_too_long` 189 (vs 114) and a 3.383 Å max edge (vs 2.375), because
`_apply_collapse_raw` relabels each removed vertex onto its neighbour and that
positional damage does not anneal out.

This also corrects an earlier reading in this document: the "294 vertices
over-dense vs 11 under-dense" measurement is about *local* triangle-area
variance, not global count. Globally this surface wants **more** vertices at this
cell size, not fewer — so any annihilation step has to be vertex-count-preserving
(collapse paired with a split) or avoid collapse entirely.

Flips are vertex-count-preserving and already cut 252 to 150 on their own, with
bond std 0.271 vs stock 0.278 — the only variant so far that improves the census
without degrading geometry. That is the controlled comparison against the
16-error baseline: identical atom count, identical target bond length, only the
ring arrangement differs.

### Part (c) refuted 2026-09-26: fewer scars, worse chemistry — the greedy is load-bearing

Controlled comparison at a fixed anneal budget. Flips preserve vertex count, so
the flips-only row matches the stock row on atoms and on mean bond length, and
differs only in ring arrangement:

- stock, 252 defects, 1912 atoms, mean 1.456 Å: annealed to **16** errors,
  strain 2363
- flips-only (score-neutral accepted), 150 defects, 1916 atoms, mean 1.457 Å:
  annealed to **47** errors, strain 2349
- flips + widened collapse pool, 122 defects, 1780 atoms, mean 1.518 Å: annealed
  to **79** errors, strain 2366

Hard errors get monotonically **worse** as the census improves, with angle strain
pinned near 2350 throughout — insensitive to census, to vertex count and (from
the earlier A/B) to the bond target. The dislocation-scar hypothesis in the two
preceding sections does not survive this, and neither relaxation should be
landed as prototyped.

**Why: `improvement <= 0` in `_eval_candidate` is load-bearing, not an
oversight.** The flip score is purely `abs(degree - ideal)`, so it is
geometry-blind; with `ideal` uniformly 6 nearly every flip is score-neutral, and
permitting those lets the mesh random-walk the cost plateau into arrangements
with identical degree cost and worse edge quality. Pre-anneal minimum bond:
stock 0.727 Å, flips-only 0.575 Å. The strict-improvement refusal is what pins
the mesh to the arrangement the smoothing and reprojection passes arrived at.

**What this implies for part (c).** A defect-reducing search needs a *geometric*
term in the flip and collapse scores — the classical isotropic-remeshing flip
criterion carries an angle or edge-length term precisely to break degree ties
toward better triangles. Adding permission to move on plateaus without that term
is strictly harmful. This supersedes the "order of work implied" recommendation
above: do not sequence combinatorial-then-geometric, score both together.

### Part (c) diagnosed 2026-09-26: one defect, not two — the triangulation is anisotropic

**`cell_A` is a pure rescaling.** Bond angles on the dual, re-based to a 1.42 Å
mean, are bit-identical at `cell_A` 45.8 / 91.6 / 183.2 (mean 120.10°, std 14.39,
min 76.6°, max 159.3°, 31.6% outside `ANGLE_TOL=15`). So the whole net *shape* is
scale-invariant, not just the ring census: `cell_A` selects only the scale — which
is exactly what part (a)'s refusal encodes — and cannot touch the bond spread.

**Mesh resolution `n` is the only shape knob, and strain asymptotes.** At
`cell_A=45.8`, sweeping `n` (carbon-scale cell in brackets):

- `n=11` [28.3 Å], 748 atoms: angle std 16.60, **42.2%** out of tolerance,
  rings `{4:2, 5:26, 6:288, 7:54}`
- `n=17` [45.8 Å], 1912 atoms: std 14.39, **31.6%**, `{5:114, 6:700, 7:138}`
- `n=23` [63.5 Å], 3724 atoms: std 14.20, **30.0%**, `{5:218, 6:1400, 7:238, 8:2}`
- `n=29` [78.8 Å], 5708 atoms: std 13.70, **27.1%**, `{5:288, 6:2250, 7:312}`

Falling like ~`1/n^0.4`, not the `1/n^2` curvature would give, and the extreme
angles (68°–162°) persist at every `n`. So the advisories are not curvature.
(The `n=11`/`n=23` purity violations are already refused by `tpms.py`'s
`ring_purity_enforced` gate; the tested `n=11` config is the raw, unremeshed one.)

**The unification.** The dual of an *equilateral* triangulation is a perfect 120°
hexagonal net, so bond-angle deviation measures triangle shape directly. Bond
spread and angle strain are therefore **one defect**: the loop optimises vertex
degree and leaves triangle shape to plain Laplacian smoothing. Botsch–Kobbelt
carries length-driven split/collapse *plus* tangential smoothing precisely to get
isotropy; this loop has the degree half only.

**Confirmed by breaking flip ties on shape.** Degree cost unchanged and still
dominant; among flips of equal degree cost, prefer the one making the two
triangles more equilateral (summed squared deviation of their six angles from
60°). Everything improves at once:

- stock: 252 defects, bond std 0.278, ratio 3.27x, min 0.727 Å, angle std 14.39,
  31.6% out of tolerance
- shape-broken ties: 190 defects, bond std **0.244**, ratio **2.93x**, min
  **0.801 Å**, angle std **12.47**, **24.2%**

Contrast with accepting ties *undirected* (the section above), which took min bond
to 0.575 Å: the permission was the same, the direction was the whole difference.
**Regression to fix before this could land:** two 4-rings leak through
(`{4:2, 5:80, 6:764, 7:108}`). The flip guard cannot emit those, so they are
residual from the split/collapse passes seeing new configurations —
`RemeshReport.collapse_rejected` / `split_rejected` already track exactly that.

### Part (c) solved 2026-09-26: two changes to the cost function, no new operators

Both defects come from the *cost function*, not the operator set. Two changes to
`_eval_candidate` / `_eval_collapse_candidate` fix purity and geometry together.

**Change 1 — break equal-degree-cost ties on triangle shape.** `ideal` is
uniformly 6 at useful densities, so nearly every flip ties at 0 and is refused by
`if improvement <= 0`. Among tied flips, prefer the one making the two triangles
more equilateral (summed squared deviation of their six angles from 60°).
`_flip_pass` already receives `verts`, so no plumbing is needed. Strictly-improving
flips still win outright; this only orders the plateau the degree cost cannot see.
Accepting ties *undirected* is harmful (min bond 0.727 to 0.575 Å) — direction is
the whole difference.

**Change 2 — price degrees outside [5,7] above every in-range deviation.**
`sum abs(degree - ideal)` prices one degree-4 vertex at 2 and three off-hexagons
at 3, so repairing a 4 (flip an edge it is the apex of: `4 -> 5`, both endpoints
`-1`) reads as 2 to 4 — **uphill**, and a strictly-improving greedy refuses it
forever. Measured on the tie-break-only run: `collapse_rejected` pins at 8 with
`converged=True` and two degree-4 vertices survive, *neither on nor beside the
seam* — so this is not the frozen-wrap-seam limitation, it is the cost shape. A
4-ring is chemically disqualifying while 5s and 7s are fine, so use a barrier:
`abs(d - ideal) + BARRIER * (max(0, 5-d) + max(0, d-7))`. Grade it by distance
outside the range — a flat barrier prices degree 3 and 4 identically, letting a
`4 -> 3` move pass as score-neutral on its shape gain.

Results at `cell_A=45.8, n=17`, with `chi=-4` and closed-manifold assertions
holding:

- stock: rings `{5:114, 6:700, 7:138}`, bond std 0.278, ratio 3.27x, angle std
  14.39, 31.6% outside `ANGLE_TOL`; annealed 395 errors to **16**, strain 2363
- shape ties only: rings `{4:2, 5:80, 6:764, 7:108}` — **purity violated** — bond
  std 0.244, ratio 2.93x, angle std 12.47, 24.2%; annealed 191 errors to **6**,
  strain **1952**
- shape ties + graded barrier: rings `{5:84, 6:763, 7:108}`, **no bad rings**,
  bond std 0.244, ratio 2.95x, angle std 12.39, 24.1%, `collapse_rejected` reaches
  **0** (the loop converges instead of giving up)

Angle strain had been pinned near 2350 across every census (252/150/122), vertex
count (1912/1916/1780) and bond target (1.52/1.42) tried. The shape term is the
only thing that moved it, which is the isotropy diagnosis confirmed: the dual of
an equilateral triangulation is a 120° net, so triangle shape is the only lever.

**Still open.** The remaining ~24% out-of-tolerance angles and 2.95x bond ratio
are inherited from marching cubes, whose triangle shapes are set by where the
surface cuts each grid cell. Fixing that at the source means an isotropic mesher:
advancing-front (published for isosurfaces as *Marching Triangles*, Hilton &
Illingworth), which takes target edge length as an input and yields near-equilateral
triangles by construction; or Lloyd/CVT relaxation of the ring centres, whose
Voronoi vertices are the atoms directly. For a periodic closed surface the hard
part in both is the wrap seam (front merging; periodic restricted Voronoi), so
these are projects, not tweaks.

**Also worth changing, independent of the above.** The surface is currently a hard
constraint — reproject after every step — which is why the bond-target A/B came
out flat: the target moved 7% and the resulting mean moved 0.7%, because
projection overrode chemistry. A signed soft penalty with a tolerance band would
let bond and angle terms win where they should, and is more honest physically:
real sp2 carbon on a curved surface puckers. The as-built mesh already straddles,
signed `f` from `-1.26e-2` to `+7.1e-3`.

**Perpendicular distance is cheap and well conditioned**, which makes a planarity
term (each atom's neighbours' distance to its tangent plane) essentially free.
Signed distance is `f/abs(grad f)` to first order — exactly what the existing
reprojection step moves. For Schwarz P, `abs(grad f)` *cannot* vanish on the
surface: it needs all three sines zero, hence every coordinate 0 or a/2, hence
every cosine +-1 and `f` odd-valued, never 0. Measured over the mesh: min 0.139,
max 0.237, ratio 1.7x. Newton converges quadratically — from 0.6 Å off, the
residual goes 1.875, 7.6e-2, 1.4e-4, 5.1e-10, 1.1e-14 Å.

**End-to-end confirmation of the solved config** (same anneal budget: `relax_graph`,
sp2, 192 rim atoms pinned, 8 bursts of 25 iters with reprojection between). The
purity-clean net at `cell_A=45.8, n=17`, rings `{5:84, 6:763, 7:108}`, 1918 atoms:
186 hard errors as built, annealing to **8** (`over_valence` 5, `bond_too_long` 3),
angle strain 1129 to 1943, bond std 0.244 to 0.140, mean 1.442 Å, 456 s. Against
stock's 395 to 16 with strain 2363. The tie-break-only variant (with its two
4-rings) reached 6 errors / strain 1952, so the purity barrier costs ~2 errors of
annealed quality and buys a legal net — worth it.

**Dual locality, which makes per-candidate scoring of the real objective viable.**
`dual.dualise` places exactly one atom per triangle, at the triangle centroid
projected onto the zero set (`dual.py:165-171`), and one bond per triangulation
edge. So a flip replaces exactly 2 atoms and rewires a bounded set of bonds; a
collapse removes 2 triangles hence 2 atoms; a split adds 2. Every discrete move's
effect on the dual is O(1) local — no global `dualise` is needed to score a
candidate.

### Part (c) independently verified 2026-09-26, with corrections that change the fix

A second model re-measured the four diagnostic claims from scratch at the same
config (`cell_A=45.8, n=17`, `iters=20`), reproducing the stock baseline exactly
(rings `{5:114, 6:700, 7:138}`, bond std 0.275, ratio 3.27x, min 0.727 Å, angle
std 14.29, 31.1% outside +-15°). All four mechanisms hold. Four substantive
corrections, in decreasing order of how much they change what to build.

**1. Bond-length spread is a SECOND defect: triangle SIZE, not shape.** The
"one defect, not two" section above is wrong on bond lengths (it stands on
angles). Evidence: among the 607 bonds whose *both* triangles have mean-ratio
quality q > 0.95, bond std is still 0.213 with ratio 2.31x, against 0.275 / 3.27x
overall. Triangle **area** cv is 0.31, and nothing in the loop controls it — the
shape-directed variants moved area cv only 0.310 to 0.288, and 60 extra
`_smooth`/`_reproject` iterations on the converged mesh raised q merely 0.918 to
0.923 while *worsening* area cv to 0.353 and bond min to 0.629 Å. Uniform
Laplacian smoothing is not a sizing tool. Bond L against the equilateral
prediction from the two adjacent triangle areas alone: corr 0.706, size-only cv
0.153 of the total 0.194. So a shape term caps the bond ratio near 2.3-2.9x and
then stops; the residual needs **length-driven split/collapse** (the half of
Botsch-Kobbelt this loop does not have), i.e. the coverage/area term, not a
shape term.

**2. The forbidden degrees are INHERITED from marching cubes; no operator can
mint one.** Raw MC is 30% degree-4-or-8 (`{4:158, 5:114, 6:532, 7:108, 8:158,
9:10}`). Flip refuses at `remesh.py:264`, collapse at `:365-373`, split at
`:525`/`:536` — every filter rejects any result <= 4 or >= 8. The two surviving
degree-4 vertices (canonical 190, 221) are raw MC vertices with all-6, non-seam
neighbours. Stock repairs such vertices only incidentally, when a neighbour
drifts to 7 and a strictly-improving route opens; shape-ordered flips flatten
neighbourhoods toward 6 and close that route, which is why the tie-break variant
*gained* two 4-rings. Consequences: the barrier's job is to make *repairing* an
inherited bad degree downhill, and a **flat** barrier is equivalent to the graded
one (nothing can land on 3, so pricing 3 above 4 is moot). Also `collapse_rejected
= 8` is exactly 2 vertices x 4 incident edges: the collapse cost has the same
tie flaw (collapsing a 4 into a 6 with 6-apexes reads 2 to 1+1, refused at
`remesh.py:386`).

**3. The barrier must go in the FLIP cost only.** Barrier in all three costs:
the collapse pass runs first (`remesh.py:849`) and eats the 4s by deletion —
atoms 1916 to 1898, bond max 2.315 to 2.49 Å, ratio back up to 3.06x. Barrier in
the flip cost alone: no 4-rings, `collapse_rejected` 0, atoms 1918, bond std
0.243, ratio 2.93x, angle std 12.54, 24.7% out. Consistent with the earlier
finding here that collapse-driven relabelling damages positions.

**4. Scoring ties on the actual dual objective beats every triangle-quality
proxy.** As tie-breaks, all triangle measures land within noise of each other
(squared-angle 12.54 / 24.7%, min-angle 12.57 / 25.2%, mean-ratio q 12.84 /
26.0%, Delaunay 13.0 / 26.5%) — so the squared-angle form is sound but not
special, and the trig-free `4*sqrt(3)*A / sum(l^2)` or a single Delaunay
inequality is cheaper. But scoring the **dual** directly — change in
sum((dual angle - 120°)^2) over the 2 flipped plus 4 outer neighbour atoms, on
live centroids — gives angle std **11.25** and **16.6%** out of tolerance against
24.7%, bond std 0.220, 150 defects, 18 s against 7 s. It needs a min-angle guard:
alone it produced 6 near-degenerate triangles and angle extremes 58.9°/173°. The
reason triangle quality is a leaky proxy is measurable: corr(dual angle
deviation, **own** triangle q) = -0.04, against -0.35 for patch-min q and -0.50
for patch-mean q. Atoms sit at centroids (`dual.py:166`), so the dual angle is a
4-triangle *patch* property. (At circumcentres it would be exact — dual angle
= 180° minus the own triangle's angle — but that needs Delaunay everywhere or
bond lengths go negative, and circumcentres leave obtuse triangles.)

**Smaller corrections to numbers recorded above.** `corr(degree, K)` is **-0.216**,
not -0.086 — the -0.086/-0.042 figure correlated against curvature *density*. The
-0.22 is a triangulation artefact rather than curvature information: seven ~60°
angles overshoot 2π, so degree 7 carries more negative defect by construction
(mean defect by degree: 5 -> -0.013, 6 -> -0.027, 7 -> -0.034). Dropping `round`
and `clip` for a fractional ideal does not help (224 defects, angle std 14.2).
The flip plateau is **small**, not "nearly every candidate": on the admissible
pool, ties are 24% of the raw mesh (236 of 966) and 10% of the converged mesh
(160 of 1550) — with `ideal == 6` a flip on a 6-6-6-6 quad costs 4, and the
score-neutral flips are exactly the dislocation glides, which is the point but a
narrow one. And "undirected ties make geometry worse" should be stated as **tail
damage**: bond min 0.356 Å (0.575 in my run — the value is ordering-dependent),
ratio 6.76x, one triangle at q=0.078, never converges (31 flips/iteration
cycling), while the *spread* statistics still improve against stock (bond std
0.259, angle std 13.54, 27.3% out).

**Two things this reframes as upstream.** Iteration 1 does 145 collapses + 21
splits + 29 flips against `ideal == 6` — every one of those mints 5s and 7s, so
the dislocation scars are manufactured in the first iteration on the raw MC mesh,
and length-aware scoring *there* is upstream of everything else. Second, the
textbook sizing operator is blocked by the purity filters: a plain **edge** split
creates a degree-4 vertex as an intermediate and every filter refuses <= 4, so
length-driven splitting needs a split+flip compound or gating only the final
mesh. The seam is not the wall for angles — 16% of raw vertices frozen and 20% of
triangles seam-touching with lower q (0.872 against 0.929), yet dual angle
deviation at seam atoms is 17.1° against 17.9° interior.
