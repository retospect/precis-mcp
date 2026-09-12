---
status: draft
title: pattern groups — one prototype, a transform group, orbit-deduped checks
prio: high
blocked-by: design-state-core
---

# Pattern groups — symmetric repetition as a first-class tree node

Reto 2026-09-12: design one unit, repeat — linear arrays along 1/2/3
axes (arbitrary basis vectors, so angled planes come free), rotational
n-fold about an arbitrary axis, hierarchically composable (a unit made
of n identical subunits differing only by placement transform).
Examples: laser-breadboard hole array, tripod legs (author ONE leg),
same at nano. Cuts LLM authoring effort by the pattern's order and —
the deeper win — cuts *checking and optimisation* cost the same way.

## The model

A **pattern group node** in the design tree: one prototype child
subtree + a transform generator — `linear(basis v1[, v2[, v3]],
counts n1[, n2[, n3]])` or `polar(axis, n)` (mirror and point groups
are later extensions; sphere tilings explicitly later). Expansion to
placed elements is DERIVED on demand (the `expand_instances` posture),
never stored per element. First-class, not macro expansion: DRC, cost,
BOM, viewer, and the LLM all see "3 × leg", and edit-one-edits-all is
automatic.

- **Identity**: the prototype carries the uids (design-state-core);
  an element is addressed as (uid, element-index) — cross-refs and
  note/argument anchors can target the prototype (= every element) or
  one element ("leg 2's foot").
- **Ports**: the group exposes its prototype's ports per element (a
  breadboard IS a port array — mount = pick hole (i,j)).
- **Any subtree patterns** (Reto 2026-09-12): the prototype is an
  arbitrary assembly subtree — internal blocks/joints/connects
  replicate whole; patterns nest (module-with-hole-array, arrayed);
  the prototype may be a blocktree library instance. Joints CROSSING
  the pattern boundary are declared once against the prototype and
  replicate under the same transform — valid iff the counterpart side
  is correspondingly symmetric (offers a matching port array); a
  boundary joint whose counterpart cannot follow the group is a
  structured error, not a silent single joint.
- **No per-element overrides in v1.** A variant element breaks the
  pattern via an explicit `explode` op into plain copies; keeps the
  symmetry claim honest and the semantics trivial.
- **Orbit-deduped checking** (the mathematical core): a pair-check
  (clearance, situation verdicts, interface) is evaluated once per
  ORBIT of pairs under the group action and the verdict claimed for
  the orbit. Validity requires the *context* be symmetric too —
  edge/interior elements of a finite array are different orbits by
  construction (the group action on a finite array is not transitive);
  the machinery classifies orbits, never assumes uniformity. This is
  the same dedup `situation-rule-tables.md` item 5 wants by
  volume-equivalence — patterns give it exactly, by symmetry instead
  of by hashing.
- **BOM/cost**: qty n of one part falls out; fabrication cost models
  get the pattern natively (setup + n × unit op — drilling 100 holes
  is one pattern row, not 100 features).
- **Optimisation**: the pattern is a symmetry constraint the optimiser
  gets for free (SIMP symmetry, sizing shared across elements).

## Relation to what exists — DRY constraint (Reto 2026-09-12)

ONE placement mechanism, not siblings — **in the code AND on the tool
surface**. No new op family: the existing placement ops grow the
generator (e.g. `instance_block` takes an optional
`repeat=linear(...)/polar(...)` — a single placement is the trivial
group), so the LLM learns one vocabulary and every existing
example/skill stays valid. Same rule for views: patterns appear in
the existing tree/BOM/drc views, never a parallel `pattern` view. A pattern is a *generator of
instances*: it must expand through the SAME path as
`instance_block`/`expand_instances` (a single explicit placement is
the trivial group of order 1), and blocktree slice-1 cross-design
instancing composes as the prototype. No parallel expansion code, no
second transform representation — cad's pose/transform math is the
only one. Structure's atomistic symmetry ops (`ops.apply_ops`) stay
the atomistic analogue inside the enclave; no unification attempted,
but also no third symmetry vocabulary at the design layer. Viewer
renders a pattern collapsed as an "n ×" badge, expanded when refined
— through the existing plan/level machinery, not a new branch.

## Consumer matrix — what expands, where (Reto asked: enumerate)

Stored form is always prototype + generator. Four expansion tiers:

**(a) Never expands — group algebra on aggregates:**
- bbox/envelope: prototype bbox ⊕ lattice/angular extent, closed form.
- mass/volume: n × prototype IF elements pairwise disjoint (one
  intra-pattern orbit clearance check certifies this; overlapping
  tilings fall back to tier-d boolean volume with an honesty note).
- inertia: parallel-axis sum over the n transforms — O(n) tensor
  arithmetic, no geometry.
- BOM/cost: qty n; process rows setup + n·unit.
- persistence, notes, cross-refs: (uid, index) addressing, symbolic.

**(b) Query-local — fundamental-domain SDF, O(prototype) per query:**
- overlap/clearance vs a pattern: map the query point into the
  generator's fundamental domain (lattice cell / angular sector),
  CLAMP the element index to the finite array (this is what makes
  edge elements exact, not approximated), evaluate prototype SDF.
- cast_ray / pick / argue-with-points: same trick; hit resolves to
  (uid, index) from the domain cell.
- field evaluation at a point (radiometric later): superpose the few
  near elements by locality, O(n) worst case, never geometric copies.

**(c) Stamped/instanced — expansion of the *raster or mesh*, cheap:**
- tessellation/viewer 3D: mesh prototype once, n instanced transforms
  (three.js/glTF/3MF instancing native; STL export bakes copies —
  export boundary, like ×1000).
- SIMP/voxel fields: voxelize prototype once, stamp n times.

**(d) Full expansion — where physics genuinely couples elements:**
- structsolve bridge (stability/complementarity/continuation): the
  member/node graph expands per element — loads may break the
  symmetry (lateral load on a polar tripod), so no symmetry-reduced
  solve in v1; honest O(n).
- swept-volume situations: sweep commutes with the group only when
  the motion is per-element identical or global — check per orbit,
  expand where it does not commute.

DRC pair-checks run per ORBIT in all tiers (finite-array group action
is not transitive — edge and interior elements are different orbits
by construction, so "check one, claim all" is exact, never assumed).

**Self-intersection (intra-pattern pairs) — separation classes:**
pair geometry depends only on the separation, not position:
translation makes (i, i+k) congruent to (0, k) for every i, and a
rotation by kθ maps {0, n−k} onto {0, k}. Hence: linear — one check
per separation k ≤ k* = ⌈prototype diameter / |step|⌉ (usually 1–2);
all pairs beyond k* certified clear by the aggregate bound, ZERO
checks; polar Cₙ — k = 1..⌊n/2⌋, same truncation via the angular
bound; 2D/3D lattices — separation vectors inside the diameter
ellipsoid; nested/mixed generators — no closed form, expand-and-
classify orbits (finite, small), one tier-b SDF check per class.
Intentional interpenetration (fused tilings unioning into one solid)
only via a declared per-pattern flag: exempts the named separations
from must_clear and demotes mass/volume to tier-d boolean. Note the
asymmetry: pair CONGRUENCE is position-independent even at array
edges (unlike pattern-vs-EXTERNAL checks, where edge elements are
genuinely distinct orbits).

## Dogfood

Tripod / camera-on-mount (phase 5's "just make one leg"), breadboard
mount plate (macro), and a nano lattice of identical binding modules
(nm). The unicycle spokes are a natural retrofit test (polar n-fold).

## Open questions

- Home: `src/precis/design/` (shared node, like states) with cad
  supplying transform math — presumed; confirm at design-core build.
- Orbit computation for nested patterns (pattern of patterns):
  compose group actions or expand-then-classify? Decide in-spec at
  promotion.
- Interaction with joints: can a joint target a pattern element's
  port parametrically ("every hole gets a screw" = joint × pattern)?
  Wanted for the breadboard case; scope at promotion.
