---
status: in-progress
title: graph-first sp2 construction — geo rung, spectral embed, nanobud generator, nomenclature
prio: high
model: opus
---

# graph-first sp2 construction + nanobud generator

Design session 2026-09-14 (Reto + agent, jolly-cooking-haven worktree).
Companion: `se-view-figures.md` (renders these structures into
dr173020). Absorbs the nanobud-fusion scope-check comment in
`precis_se/atomic/generators/sp2.py` (its three blockers are dissolved
by the graph-first approach below, not solved head-on).

Prior-art survey: `perplexity-research:339983` (2026-09-14). Key facts,
**verify primary sources before citing in the draft**: no systematic
nanobud nomenclature/registry exists anywhere (confirmed explicitly);
junction-type-dependent magnetism is established theory (Wang & Li,
Cases A–D: same-sublattice junction bonds → ~6 μB, cross-sublattice →
non-magnetic); nanobuds-as-computing/programmable-matter framing is
explicitly absent from the literature. Field emission is the one
experimentally solid device domain.

## Motivation / why

dr173020's structural figures need real generated nanobud geometry
(none exists in prod; all 10 figures are third-party reproductions).
Coordinate-surgery fusion is hard (the sp2.py blockers); building the
*bond graph* first and deriving geometry dissolves it: correctness
becomes pure arithmetic before any coordinate exists. The same move
yields a systematic nomenclature the field lacks — publishable
(defensive-publication posture per the standing FTO rule) and the
substrate for a nanobud library.

## In scope

### 1. `geo` rung + embed + canonical frame (`precis/structure/`)

- Promote `sugars._relax` (bond springs + non-bond repulsion + angle
  restoring) out of the cyclodextrin generator into the relax ladder as
  rung `geo`, below `clean`; angle targets via `vsepr.ideal_angle`
  keyed on hybridization (currently hard-coded sp3). Pure numpy;
  caches like every rung.
- **Embed-from-graph**: when a scene/fragment has bonds but no (or
  degenerate) coordinates, seed via spectral coordinates — three
  adjacency-matrix eigenvectors as xyz (Manolopoulos–Fowler
  "topological coordinates"; dense `numpy.linalg.eigh`, no scipy;
  fine to a few-thousand atoms — the 500 Å tube cap bounds v1;
  when it bites, embed the changed region only).
- **Canonical registration**, post-relax rigid re-registration (never
  pins during relax): anchor atom at origin, first neighbor along +x,
  *inward* plane normal (toward centroid/bud) = +z, right-handed
  parity with a deterministic residual-sign rule. Molecular scenes
  only (periodic scenes keep cell conventions). Anchor choice recorded
  in `topology`. Guarantees: camera params stay meaningful across
  relax reruns; the bud always pops the same way; mirror ambiguity of
  the spectral seed resolved reproducibly.

### 2. `nanobud` generator = graph surgery (`precis_se/atomic/generators/`)

- Compose the existing `cnt` + `fullerene` generators' *graphs*:
  delete vertices, stitch junction edges/rings per a **named stitching
  menu** — `[2+2]`, `[4+4]`, fused-neck variants seeded from the
  literature's Cases A–D. Fused-neck stitching is not unique; the menu
  choice IS the junction type, never an implicit pick.
- Assertions **before geometry**: valence 3 everywhere, Euler
  bookkeeping (P5 − P7 = 12 closed; rim accounting open), loud
  `GeneratorError` otherwise.
- Geometry: parent coordinates kept for untouched atoms, spectral
  embed of the junction region, `geo` relax, DRC (bond-length
  residuals, self-intersection), canonical registration. Deterministic
  default; seeded-multistart fallback only on DRC failure, seed
  recorded in provenance (stochastic-by-default would break
  content-addressed caching).
- Params double as the nomenclature entry: tube `(n,m)` + length, bud
  `C_n` isomer, junction type (menu name), site (azimuth + axial
  position in the canonical frame).

### 3. Graph-tier annotations (cheap, combinatorial, honest)

- **Sublattice parity of junction bonds** (bipartite 2-coloring of the
  host lattice): predicts magnetic vs non-magnetic class per the cited
  DFT results — screening-tier, provenance-tagged *cited*, the DFT
  rung is the truth layer. Never rendered as a computed moment.
- **Rigidity screen**: Maxwell count / pebble-game floppy-mode count as
  a DRC-tier annotation ("rigid" / "k internal DOF"). Candidate
  multistability only; confirming distinct minima is the
  mechanism-analysis phase (`se-atomic-round2.md`), not this item.
- **Chirality**: no automorphism maps embedding onto its mirror →
  chiral, two valid mirror forms; registration picks one, annotation
  says so.

### 4. Nomenclature / standard — three encoding levels

1. **Parametric** (generator params, ~10 numbers) — primary
   interchange form, free (it is the generator input).
2. **Defect list on canonical lattice** (disclination positions +
   stitching; the ring-spiral generalization) — for non-generator
   structures; Euler-validatable.
3. **Full atom graph** (already authoritative in `Scene.bonds`) —
   import fallback.

Coordinates always derived (embed + relax + registration), never part
of the standard. Library entries = se designs via the
`blocktree-library-build-plan.md` machinery; properties follow the
declared/derived discipline — cited values vs computed-run values,
never blurred.

### 5. Fabrication posture — assembler-mode designs (Reto, 2026-09-14)

These structures are **forward-looking atomically-precise-manufacturing
artifacts**, and the design surface must say so rather than bend to
today's chemistry:

- Today's synthesis (aerosol CVD) yields stochastic *ensembles* —
  random junction types at random sites (`perplexity-research:339983`).
  A specific `(n,m | C_n | junction | site)` design is not reachable
  deliberately by any current route. That is a feature of the library,
  not a defect: the nomenclature entry IS the build spec an assembler
  would execute.
- se's fabrication axis (`se-off-the-shelf-fabrication.md`:
  realizability predicate per mode) gains an `assembler` mode. Its
  predicate is **physical validity, never route existence**: graph DRC
  (valence/Euler) + relax ladder converging to a stable minimum. The
  `route` retrosynthesis advisory (se-atomic-round2 later phases)
  applies only to today-tier modes.
- Junction menu is therefore NOT constrained to CVD-observed
  reactions. Each menu entry carries a **synthesis-accessibility
  annotation** (cited): `[2+2]` observed/dominant in CVD; fused necks
  observed in TEM; deliberate site-specific placement assembler-only.
  Honest, and it preempts the "you can't make that" review objection.
- The graph-surgery ops are already shaped like assembler step
  vocabulary (each stitch ≈ one placement/reaction step). Assembly
  *sequencing* is explicitly deferred (below), but the representation
  is the right substrate when it comes.

### 6. dr173020 + paper

- Gap note into the review: no nomenclature/registry exists
  (`perplexity-research:339983` — verify + cite primaries); our schema
  as the proposed systematic classification.
- Redraw via `se-view-figures.md`: dc3015720 (bonding-scenario panel),
  dc3015729 (junction geometries A–D), dc3015722 endpoint geometries.
  dc3015723/dc3015725 need a graphene-sheet generator (follow-on).
  Experimental figures (dc3015718/19/24/28) stay third-party — Reto
  obtains journal permissions. dc3015730 (MD snapshot) is not honestly
  redrawable. Redrawn captions state they show structures, not
  computed color maps.
- Methods/data paper: `nanobud-nomenclature-paper.md` (blocked-by this
  item).

## Explicitly NOT in scope

- The general store-aware `fuse` op on arbitrary bound structures
  (`se-atomic-round2.md` territory; inherits this item's junction
  math).
- Periodic nanobuds (dc3015725-class), graphene-sheet generator, MD.
- Assembly sequencing / mechanosynthesis step planning (§5 names the
  substrate; planning is future work).
- Computing any magnetic/electronic property (annotations are graph
  arithmetic + citations only).
- Hiding the `structure` kind (see `structure-kind-demotion.md`).

## Acceptance criteria

- `geo` rung relaxes a generator output with sp2-correct angles;
  ladder + cache tests green; `sugars.py` consumes the shared rung.
- Embed-from-graph reproduces C60 from its adjacency alone (RMSD to
  the closed-form generator below tolerance) — the canonical
  self-test.
- `nanobud` generator: each menu junction type builds, passes Euler/
  valence/DRC, is byte-deterministic, lands canonically registered
  with the bud on +z; a deliberately broken stitch fails loudly.
- Sublattice-parity annotation matches the published A–D
  classification on reconstructed Cases A–D.
- One prod nanobud se design minted (dogfood; write-path testing on
  dev DB first).

## Target + blast radius

`precis/structure/` (relax ladder, new embed module, registration),
`precis_se/atomic/generators/` (nanobud, sugars consuming shared
rung), `precis_se/atomic/generate.py` (op wiring). Migration only if
`topology` needs new persisted fields (leaning no — JSONB).

## Open questions / decisions log

- Decided: generator over general fuse op; graph surgery over
  coordinate surgery; determinism with recorded-seed fallback.
- Decided: registration is post-relax, molecular-only, parity-fixed.
- Open: exact fused-neck stitching menu — reconstruct Cases A–D from
  the primary papers before freezing names.
- Decided (2026-09-14, build start): `geo` sits BESIDE `clean` —
  `clean` stays the cheap overlap-only sanitiser.
