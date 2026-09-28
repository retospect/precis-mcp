---
status: draft
title: diamondoid pattern language — the sp³ volume lattice as a third atomic mode beside hexfold (sp² surfaces) and precis_surface (freeform sp²)
prio: normal
model: opus
---

# diamondoid pattern language

Reto, 2026-09-27: "The target is not any given box, but a generic framework
to build arbitrary shapes. That's the goal and the aim. Design
Drexler's/Diamond Age nano machines."

## Motivation / why

The generic half of that framework already exists: **se** is the
scale-agnostic part system (blocks, typed ports, joints, envelopes,
tolerances, the six-level IR), and each *shape language* plugs in as a
generator behind a port type. "Arbitrary shapes" then resolves into three
regimes, of which the tree has one and a half:

1. **Composed from sp² parts** — tubes, caps, sheets, junctions, folds,
   discrete defects; valid by construction. This is hexfold
   (`src/hexfold/spec.md`). Covers bearings (the valve rotor/shell),
   struts and axles (capped tubes), housings (`junction(k)`), lids.
2. **Freeform sp² surfaces** — a target field tiled and relaxed, validated
   after. This is `precis_surface`, spec §28 steps 5–6; the corrected
   energy plan for it is parked (`precis-surface-kernel.md`).
3. **Diamondoid solids** — Drexler's canonical *Nanosystems* machines (the
   bearings, gears, the fine-motion controller) are sp³ diamond lattice: a
   **volume** with terminated facets, not a surface with rims. Nothing in
   the tree starts this. It shares no lattice with hexfold; it shares only
   se. The fullerene/nanotube branch (Merkle; the NASA Ames nanotube-gear
   simulations) is what hexfold serves — both are legitimate Drexler,
   different materials.

What makes the framework generic across lattices is the **port type**: a
rim `(edge-word, N)` for sp² (`hexfold-seam-type-catalogue.md` owns the
sp² standard), a `(hkl)` facet with a termination pattern for diamondoid,
a mating face at metres. Same `GeneratedPort`, different `roles` and
payload, typed adapters between them. This item exists so that the port
type system is designed for two lattices from the start rather than
retrofitted — the decision it forces is recorded under Open questions.

## In scope

- A notation for decorated **volume** patches of the diamond lattice:
  region + facet set + termination (H, or a reconstructed surface), the
  sp³ analogue of hexfold's "decorated lattice patch" (§7). Primitives at
  minimum: a rod along a lattice direction, a plate bounded by named
  facets, a bearing sleeve (cylindrical shell cut from the lattice with a
  chosen axis), a shaft that fits it.
- **Facet ports**: `(hkl)`, termination pattern, in-plane lattice phase.
  Two facets join iff the lattices are in register across the interface;
  refusal otherwise, the same posture as `port.mismatch`.
- A generator behind `GENERATORS` (`precis_se.atomic.generators`) emitting
  a `GeneratedBlock` whose ports are facet ports; bonds `order=1.0`,
  `kind="pairwise"`, hyb `sp3`, so the existing valence rules hold without
  a new convention.
- Feasibility check analogous to hexfold's `check`: register across every
  facet join, termination completeness (no dangling sp³ valence unless
  declared), self-intersection.
- The rim ↔ facet **adapter class** — how an sp² rim docks onto an sp³
  facet (a graphene sheet bonded edge-on to a diamond surface is a known
  structure; cite before designing). Without it the two languages cannot
  share one machine.

## Explicitly NOT in scope

- Any mechanosynthesis / fabrication claim. This is design notation and
  validation, the same footing as hexfold.
- Electronic structure. Geometry and valence only; the fidelity ladder
  (`geo`/`emt`/`ml`) is where physics enters, as for hexfold.
- Replacing hexfold or `precis_surface`. Three shape languages, one part
  system.
- The freeform sp³ regime (a diamondoid solid approximating an arbitrary
  SDF). Composed-from-parts first, exactly as the sp² side did.

## Acceptance criteria

- A shaft-in-sleeve bearing (two blocks, revolute joint) builds from the
  notation, `check`s clean, and round-trips through `generate` on the dev
  DB as two bound `structure` designs — the sp³ twin of the valve
  rotor/shell.
- A facet join with the lattices out of register is **refused** with a
  named code and a pinned test; an in-register join builds.
- The port type system carries both a hexfold rim and a diamondoid facet
  through se's `add_port`/`connect`/`bind_structure` with no per-lattice
  branch in se itself — the lattice lives entirely behind the generator.
- One sp² ↔ sp³ adapter builds, or the item records precisely why the
  chosen pair cannot.

## Target + blast radius

New package (working name `src/diamondoid/`, numpy-only, imports nothing
from `precis*`, the hexfold boundary rule), one generator module in
`src/precis_se/atomic/generators/`, `GENERATORS` registry, the
`precis-se-atomic-help` skill (one section), tests under `tests/`.
`GeneratedPort` may need a typed payload slot beside `atoms` (the rim
ring) — that is the one se-side change, and it is the port-type decision
below.

## Open questions / decisions log

1. **Is diamondoid in scope for *this* framework or a later one?** —
   **Ruled 2026-09-27 (Reto: "lets do that"): in scope for the framework,
   out of scope for the next build slices.** The assembly-conversation
   layer (`options`, `fit` domains, catalogue) is lattice-agnostic and
   unbuilt; it gets built on sp² first. What is reserved *now* is the port
   payload: `GeneratedPort` grows a `lattice` tag plus a typed slot beside
   `atoms`, so the sp² rim `(edge-word, N)` and the sp³ facet
   `((hkl), surface cell, termination, 2-D offset, dimer-row direction)`
   are two instances of one port type. Cheap to reserve, expensive to
   retrofit; nothing sp³ is generated until the sp² conversation exists.
   Sub-regime split recorded below.
2. Which diamond surfaces and terminations first — `(111)` H-terminated
   and `(100)` 2×1 reconstructed are the textbook pair; confirm against
   sources before the notation fixes them.
3. Does a bearing need the lattice to be *incommensurate* between shaft
   and sleeve (Drexler's argument for low friction), and if so how does a
   register-refusing port model express "deliberately out of register"?
   Likely a joint attribute, not a port attribute.
4. Prior art to import first (DOI-cited, per the sources rule): Drexler
   *Nanosystems* (1992) ch. 10–11 for the bearing/gear geometry; Merkle's
   diamondoid bearing papers; Han/Globus/Jaffe on nanotube gears (the sp²
   side's precedent, for the adapter question).

## Two sub-regimes, and the hierarchical resolved block (2026-09-27)

Discussion with Reto, recorded so it is not re-derived.

**The volume is the easy half.** Bulk diamond has one bond length and one
angle; interior atoms are the lattice points inside the region. All design
freedom is at the surface: termination per facet ((111) H-terminated,
unreconstructed; (100) reconstructs to 2x1 dimers, so the uniform
`order=1.0` convention holds only for H-terminated facets), facet edges
and corners as motifs (the sp3 twin of hexfold's seam vertices), and the
register between two joining facets (standard library: in-register joins
plus the Sigma-3 twin on (111); every other grain boundary is refused).

**Diamondoid is two sub-regimes:**

- *Lattice-cut solids* (rods, plates, struts, housing walls): region +
  facet set + termination. Facet ports are 2-D periodic patches, so a join
  is a 2-integer offset, not hexfold's 1-integer phase.
- *Strained shells* (bearing sleeve, shaft, gear rim): a slab of k atomic
  layers rolled about an axis with n-fold symmetry -- the 3-D analogue of
  `tube(n,m)`, reusing hexfold's phase/fit machinery. Shaft and sleeve take
  different n on purpose (incommensurate = flat energy landscape); that is
  a joint attribute. Feasibility is quantised the same way as sp2: fibre
  strain scales as layer spacing times k over 2r, and diamond tolerates a
  few percent, so `options` answers "3 layers at r=0.5 nm is ~25% strain;
  use 1 layer or r >= 1.5 nm".

**Adapter lead:** graphene pitch 2.46 A vs diamond (111) surface pitch
2.52 A, ~2.4% mismatch -- a zigzag rim bonded edge-on to (111) is within
ordinary strain. Confirm against a source before it becomes a spec entry.

**Scale: the hierarchical resolved block.** A 5x3x2 nm block is ~5300
atoms; Drexler's designs run to 10^4-10^5. Reto's proposal (2026-09-27):
building blocks that are *relaxed once and frozen*, then composed as
rigid bodies whose interiors are invariant under composition. Only atoms
within a cutoff of a port (the seam neighbourhood) are re-relaxed on
join. This is se's existing block tree plus hexfold spec section 26's
content-hash cache with a resolved geometry attached, so relaxation and
validation cost scale with seam atoms, not total atoms. Consequences:

- A block carries two things: its resolved interior (frozen coordinates,
  keyed by content hash) and its ports with a declared *seam radius* --
  the shell of atoms the join is allowed to move. Interior atoms are
  read-only to the assembler.
- "Mostly stable" is a checkable property: after the seam relaxation, no
  interior atom may have moved more than a tolerance, else the block was
  too small for the strain the join imposes and the assembler refuses
  (a `seam.leak` code, same posture as `port.mismatch`).
- Validation (`probe`/`validate`, today O(N^2) over all atoms) runs once
  per block at resolve time and then only over seam-neighbourhood pairs
  at assembly time; a spatial hash is still owed for the seam pass.
- The seams become the load-bearing design objects: the interior of a
  block is trivial once resolved, so the catalogue of *seam types* (rim
  standard, facet register, rim-facet adapter) is where correctness and
  strain live. `hexfold-seam-type-catalogue.md` grows from an sp2 list
  into the framework's join catalogue.

## Three-zone resolved block, tolerance mapping, joiner (ruled 2026-09-27, "complete, plan, then build")

**No generator per kind of thing.** One generator per shape language
(lattice + composition rules), kinds as primitives inside it -- what
hexfold already does. `cnt`/`fullerene`/`cone` in `GENERATORS` are
subsumed by hexfold primitives; two more languages at most (diamondoid,
and the strained shell only if it is not hexfold with the lattice as a
parameter).

**Resolution is keyed to local environment, not to the block instance.**
A resolved block decomposes into: *bulk cell* (relaxed once per lattice +
curvature; a (10,10) tube interior is one number set for every length),
*edge motif* (relaxed once per edge type -- the seam catalogue's rows),
and *seam* (relaxed at join time from the two edge motifs as seeds). Size
is free; the section-26 cache stores environment types, not instances.

**Edges relax differently free vs joined** (dangling bonds pull the rim
inward, H-termination changes it again, (100) reconstruction is entirely
an edge effect). So a resolved block has three zones -- interior
(frozen), edge shell (provisional seed), port (replaced at join) -- and
the **seam radius** is the depth the edge perturbation penetrates.
Measure it, do not guess it: relax a hexfold piece free and fused, diff
per atom against graph distance from the rim, read off the decay length.
Expected 2-3 rings sp2, ~2 layers sp3. This measurement is step 0 of the
build order (`hexfold-integration.md`): it validates the frozen-interior
scheme before anything is built on it. Free edges that stay free in the
final machine resolve as terminated (their real state); ports that will
be joined never need a free-relaxed geometry.

**Generator output additions:** per-atom zone (interior / edge shell /
port; hexfold's `regions` is the carrier) and a per-port seam radius.
Resolved geometry is a fidelity-tier output cached by hash, not the
generator's job.

**Tolerance scheme needs no new design.** Discrete choice = the wish band
(`options` returns the quantised members inside [inner, outer], ranked).
Referential/additive tolerance (lid height set by the wall's bend) = the
`fit` chain, wired to se's L2 measures and `stackup`
(`src/precis_se/measures.py`) -- se's stack-up applied to hexfold's
fitted values, a data-flow question. Continuous tolerance survives only
at seams (strain vs `strain_max`, interior invariance behind
`seam.leak`) and at poses (clearance, an se joint/envelope attribute).
The hull-as-penalty belongs to the freeform regime alone (parked).

**Block joiner is the one new component.** Today: hexfold `fuse`/`seam`/
`bond` join covalently only inside one spec (whole net rebuilt and
relaxed -- correct, does not scale); se `connect` + `bind_structure` join
at pose level with no bonds across blocks (right for the bearing, wrong
for a housing). Missing: take two resolved blocks + a port pair + a seam
type from the catalogue, add the motif's seam atoms and bonds, re-relax
only the two seam radii, run the interior-invariance check, emit a
composite whose interior is the union of two frozen interiors. Interface
is port types and motifs (no lattice branch); se side, calling into the
shape language for the motif. Built on sp2 first, after `fit` chain
propagation; until then hexfold whole-spec composition is the joiner and
is adequate for valve-sized assemblies.

**Built** (`hexfold.join` + se `join`, slices 1-2, 2026-09-28): fuse/adapter
motifs, seam sub-graph re-relax, `seam.leak`/`seam.strain`, both relax
rungs. `seam.leak` thresholds by rung: stick (pinned guard band) zigzag
`(0.0001 Å, 0.025°)`, armchair `(0.0001 Å, 2.9°)` -- per-rim-type, doubled
from the measured stick-rung maxima (`hexfold/join.py`'s own docstring);
geo (no pinning, `LEAK_THRESH_GEO`) `(0.002 Å, 0.15°)` uniform across rim
type -- the SAME numbers this section's own table pins. Slice 3 (joint
placement across part-graph cycles) is still open.

## Seam decay, measured (slice 0, 2026-09-27)

The number behind the three-zone block, measured on hexfold tubes with
`precis.structure.georelax.relax_graph` (geo rung: bond springs at the
covalent-radius sum 1.52 Å, VSEPR angle term, non-bond repulsion; `sp2`
targets, `tol=1e-4`, converged). Instance `a` relaxed free versus relaxed
fused tip-to-tip (`a.out --fuse k=0--> b.in`) onto an identical `b`;
shells are graph distance from the fused rim within `a`. Pinned by
`tests/test_hexfold_seam_decay.py` (marked `slow`; (8,0) and (5,5) at
`len=3`).

Zigzag `tube(12,0, len=4)` (192 atoms free, 384 fused; 15 shells):

| shell | max displacement after Kabsch (Å) | max bond-length change (Å) | max angle change (°) |
|---|---|---|---|
| 0 (rim) | 0.0145 | 0.0033 | 0.17 |
| 1 | 0.0101 | 0.0005 | 0.24 |
| 2 | 0.0071 | 0.0025 | 0.26 |
| 3 | 0.0044 | 0.0005 | 0.15 |
| 4 | 0.0023 | 0.0017 | 0.19 |
| 5 | 0.0016 | 0.0004 | 0.08 |
| 6–13 | 0.0024 → 0.0067 (global mode, see below) | ≤ 0.0010 | ≤ 0.11 |

Zigzag `tube(12,0, len=6)` (288 free, 576 fused; 21 shells) — the longer
tube, where the two ends of `a` interact less:

| shell | max displacement (Å) | max bond-length change (Å) | max angle change (°) |
|---|---|---|---|
| 0 (rim) | 0.0315 | 0.0053 | 0.25 |
| 2 | 0.0185 | 0.0044 | 0.36 |
| 4 | 0.0100 | 0.0035 | 0.32 |
| 6 | 0.0037 | 0.0027 | 0.25 |
| 8 | 0.0030 | 0.0019 | 0.18 |
| 10 | 0.0061 | 0.0013 | 0.12 |
| 12 | 0.0083 | 0.0008 | 0.07 |
| 14–20 | 0.0096 → 0.0105 (global mode) | ≤ 0.0004 | ≤ 0.03 |

(Axial bonds — the 12-bond shells — carry the strain; the circumferential
24-bond shells change 3–5× less. The decay is a clean geometric one, ratio
≈ 0.75 per two shells.)

Armchair `tube(7,7, len=4)` (112 free, 224 fused; 7 shells):

| shell | max displacement (Å) | max bond-length change (Å) | max angle change (°) |
|---|---|---|---|
| 0 (rim) | 0.0128 | 0.0013 | 0.19 |
| 1 | 0.0021 | 0.0003 | 0.05 |
| 2–6 | ≤ 0.0016 | ≤ 0.0003 | ≤ 0.06 |

Readings:

- **The perturbation localises, but the zigzag radius is longer than the
  short tube suggests.** At thresholds 0.002 Å per bond and 0.15° per angle
  (about 10× the converged relaxer's noise on the untouched far rim):
  **zigzag (12,0): 5 shells at `len=4`, 8 shells at `len=6`** (≈ 4 hexagon
  rows, ≈ 9 Å; the `len=4` tube is shorter than twice the decay length, so
  its two ends interact and the amplitude is halved); **armchair (7,7): 1
  shell** (one row, ≈ 1.2 Å) at the same radius. The frozen-interior scheme
  holds for sp², with the seam radius a **property of the rim type at that
  radius**, not a lattice constant: zigzag rims lose an axial bond and the
  rim ring relaxes radially, which propagates as a thin-shell edge mode
  whose decay length is set by the bending/stretching balance (and so by
  the tube radius); armchair rims keep their circumferential DD bond and
  screen within one row. Consequence for the design: the seam radius is
  looked up per environment (rim type, N) — exactly what environment-keyed
  resolution already assumes — and must be re-measured per radius class
  and on an energy rung before any value is hard-coded. The pinned test
  uses short tubes ((8,0) and (5,5) at `len=3`) for runtime and therefore
  pins lower bounds (5 and 2 shells); the `len=6` table above is the
  number to design against for zigzag.
- **Displacement is the wrong metric past the seam.** Kabsch-aligned
  displacement rises again toward the far free end (0.0016 → 0.0078 Å over
  shells 5–14 on the zigzag tube) while bond and angle changes there stay at
  noise: a global breathing/length mode of the longer composite, not seam
  strain. The joiner's `seam.leak` must be defined on rigid-invariant local
  measures (bond lengths, angles), not on atom displacement.
- **Amplitude is a lower bound.** The spring model has no rim
  reconstruction, bond alternation or π effects, so the 0.013–0.015 Å rim
  amplitude understates what an energy rung (`emt`/`ml`) would show; the
  decay *length* is elastic screening by the lattice, which the model does
  carry, so the qualitative result (localised, a few rows) is expected to
  survive re-measurement on an energy rung. Re-measure there before pinning
  sp³ radii — diamond's stiffer, 3-D-connected lattice should screen faster
  still, but that is a claim, not a number.
- **Edges relax differently, quantified:** the free zigzag rim's own bonds
  shorten by 0.0012 Å relative to the interior on this rung, the fused ones
  by 0.0033 Å less than free. On the geo rung "edges relax differently" is
  a ~0.2 % effect; the reconstruction chemistry that makes real edges
  differ is exactly what this rung lacks.
- **Found on the way:** every multi-instance hexfold seed was mis-placed
  (`_place_seeds` keying bug, fixed in this slice; see `hexfold-integration.md`
  residuals). The measurement was impossible before the fix — the fused
  seed telescoped and the "seam" displacement read 4–11 Å everywhere.

