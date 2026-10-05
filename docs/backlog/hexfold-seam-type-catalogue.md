---
status: draft
pillar: 3d-design
title: hexfold seam-type catalogue — a closed set of supportable seam geometries, keyed by (k, dihedral pattern, rim edge-word, hybridisation)
prio: normal
model: opus
---

# hexfold: seam-type catalogue

## R13 selected contract — Horizon 7, spec review before implementation

Only the entry **(3, (120°,120°,120°), zigzag SD rim pattern,
sp²)** is selected now. The catalogue's multiplicity `k` means
`len(seam.rims)`; the existing authored `k=<phase>` and `Seam.k` mean
registration phase and MUST retain that meaning. No unequal-dihedral
solver, k=4 motif, census research or scientific stability claim is
authorized. This is a deterministic geometric construction contract.

**Step 2 approved:** use a private open zigzag segment input in
`hexfold.join`, with two explicit endpoint ordinals and an alternating
S-D-…-D-S walk. Six D sites are the fixture's six seam periods. Public
`Port`, `.hx` grammar/canonical JSON, multi-rim compiler and SE mutation
remain unchanged. Here the selector is `compose_k3(...,
seam_type='k3-sp2-120-z', dihedrals_deg=(120,120,120))`, not new parser
syntax. The proposed authored syntax/round-trip checks below are a later
port-grammar item, not this build's acceptance. Nonzero cyclic phase is
refused for the finite segment; no wraparound or endpoint guessing.

Nanoreactor T2 needs more than this fixture: named internal rails on a
closed tube, their segment/closed-curve endpoint topology and registry,
compatible attachment rails on the two reservoir sheets, placement around
the tube's curved surface, and per-sheet Euler/face bookkeeping through
those boundaries. It also has separate pores, inward site coordinates and
later scientifically authorized relaxation/measurement. None is supplied
by the straight six-period Y, and none is silently inferred from it.

**Local implementation evidence:** six seam periods, 198 atoms/270 bonds;
all six seam atoms degree-3 sp². Bond lengths 1.4199999999999977–
1.420000000000003 Å, seam angles 119.99999999999991–120.0000000000001°,
maximum absolute normalized triple product 1.63e-15. Existing geometry
checker reports no clash or bad bond lengths. Its regular-polygon angle
comparison emits `geom.angle.dev` WARN for the nonplanar eight-cycles;
these remain visible. This is a pinned bond/coplanarity/no-clash PASS,
not an all-warning-free geometry/stability result. The 15 local eight-cycle
faces come from the finite fixture graph, not a general carbon census.
Replay: `.scratch/k3/replay.py`/`.json`; canonical focused tests and
scoped types/Ruff recorded in fleet-state `inbox/hexfold-k3-ready` pair.

### Verified premise and owning seam

Current source at `0655b224f7de53249870fcb516b8458e353efa2e`:

- The implementation owner is the **tracked vendored `src/hexfold` package
  in precis**, not `precis_se.atomic.generators`. Its package docstring
  names root `hexfold/` as an export seed; `/Users/reto/hexfold` is absent.
- `precis_se.atomic.join.JOINERS` (`join.py:944`) is a sorted lattice-pair
  dispatcher, currently only `("sp2-hex","sp2-hex")`. Its operation takes
  two endpoints. This does NOT mean all hexfold seams are k=2-only.
- Pure `hexfold.join.compose` (`join.py:499`) accepts two `Block`s and
  performs seam-local relaxation. Do not call it to manufacture this
  no-relax positive case. `Block` (`join.py:149`) already carries local Å
  coordinates, topology and ports and is the natural input seam to reuse.
- `text.Seam` (`text.py:79`) and `build.py:4443` already implement multi-rim
  graph identification, equal dangling-count checks and edge-word skeleton
  checks. They do not expose `seam.type` or equal-dihedral typed placement.
  `build.py:4569` feeds consecutive pairs into ordinary placement;
  `build.py:4646` seeds new seam atoms at the neighbours' mean. Neither
  proves a metric three-sheet Y junction.
- `build.Port.rim_type` (`build.py:88`) reads alternating SD zigzag versus
  SSDD armchair. Both can have word `z^n`; equal edge-word alone is
  insufficient. The key therefore retains the authored/canonical edge-word
  AND the resolved pure zigzag rim type, not a string-based guess.

Python discovery used native search/outline/symbol first. Index root `/app`
differs from this task tree; the local anchors above were checked locally.
No Y build/relaxation was executed for this premise check.

### Selector and refusal API to review

Proposed authored syntax (and matching canonical JSON fields):

```text
seam y: a.edge == b.edge == c.edge type=k3-sp2-120-z dihedrals=[120,120,120] k=0 atoms=sp2
```

`Seam.type` / JSON `type` selects exactly this catalogue entry;
`dihedrals` declares the three cyclic sheet-sector angles in degrees.
The spelling `a.edge` is a fixture requirement, NOT a presently supported
sheet port (see the fixture hold below). Omitted dihedrals on this explicit
type mean its declared equal-120 entry. Explicit unequal/nonfinite angles,
wrong multiplicity or incompatible hybridisation are `fit.unsolvable`
ERROR with requested type/count/angles and supported correction. Unequal
input is refused before placement, seam atom/bond minting or any relaxer.
No averaging, clamping, symmetric guess or fallback to the pair joiner.
At least five rims are `fit.unsolvable` even without a type; k=4 remains
outside this selected implementation, with no new supported claim.
An armchair/mixed rim, unequal dangling counts or incompatible registered
edge-words is `port.mismatch` ERROR naming the offending rim and resolved
pattern/count. Unknown selector is `fit.unsolvable`, never ignored.

For existing untyped three-rim authored files, preserve the historical
topological interpretation and hashes; do not retroactively claim typed
120° geometry. Canonical serialization must omit absent new fields and
round-trip explicit fields. Typed input cannot silently downgrade to this
legacy path. Existing k=2 fuse/pair composition remains its own operation.

The bounded pure join entry proposed for review is
`hexfold.join.compose_k3(blocks, rims, *, seam_type='k3-sp2-120-z',
dihedrals_deg=(120,120,120), phase=0)`: exactly three existing resolved
`Block`s/ports, Å coordinates and existing sigma. It returns combined
coordinates/topology and named findings without IO/relax/model/catalogue
fetch. This is a separate cardinality-safe entry rather than changing the
meaning of `compose(a,pa,b,pb,k)`. Public SE multi-block mutation is NOT
added in this slice. Compiler typed placement should reuse the same pure
placement helper rather than duplicating the geometry.

### Positive fixture and concrete representation hold

The positive acceptance is **three finite graphene ribbons meeting on a
straight zigzag segment**, an analytic Y cross-section with sheet sectors
0°,120°,240° around the seam tangent. Use existing sigma=1.42 Å and
honeycomb spacing, at least six seam periods, identical registered SD
patterns; no catalogue-generated evidence or relaxed coordinates. New seam
atoms have exactly three rim neighbours, bond vectors in the plane normal
to the seam tangent and separated by 120°. Preserve each sheet's internal
coordinates under rigid placement. Report actual topology/census, without
assuming the backlog's derived octagon count.

**Current blocker to review before code:** built sheet ports represent
whole cyclic boundary walks (`build.py:1280`, `:1296`); a finite sheet's
outer rim has corners/mixed pattern. The parser/spec permits an open seam
in prose, but there is no verified named straight `sheet.edge` port with
open-end semantics. `Port` and `_seam_faces_k` currently assume cyclic
walks. A tube's pure zigzag end is closed and curved; three tubes or the
old sheet-pill fixture cannot be substituted as proof of a straight
equal120 three-sheet Y. Decide the smallest deterministic segment fixture
and endpoint representation during contract review. If a narrow local
fixture/helper suffices, pin its exact endpoints and graph in the fixture;
if public open-rim grammar/topology is required, that prerequisite must be
explicitly adopted before implementation. Do not claim this hold solved
by this spec or add an unrelated general solver.

### Acceptance/replay for the reviewed build

- New focused `tests/hexfold/test_seam_k3.py`: selector parse/JSON/text
  round-trip (legacy phase unchanged), positive Y fixture, deterministic
  repeated outputs, unequal k=3 and k>=5 `fit.unsolvable`, rim mismatch.
  Refusal tests trap seam minting/placement/relaxer calls.
- Verify every seam atom is carbon sp², degree three; vectors have
  normalized scalar triple product zero and pairwise dot products -1/2
  within ordinary numerical equality tolerances, not a new scientific
  threshold. Bond lengths use existing `Profile.DEFAULT.bond_tol_A`;
  coplanarity is geometric, not a stability assertion.
- Topological `check(spec)` runs without geometry relaxation.
  `geometry_findings(net, relaxed=Relaxed(coords, 0,
  'deterministic-unrelaxed'))` reuses existing geometric checks: max_force=0
  is an unused API carrier, NOT a measured convergence claim. Assert no
  `geom.clash` at either severity and no bad bond lengths. Never use
  `check(geometry=True)`, which calls `stick_info` when coordinates are
  absent. Any open-segment census/Euler limitation must be resolved or
  explicitly reported, never hidden to obtain a green fixture.
- Planned canonical replay after contract verdict:
  `scripts/test -n0 tests/hexfold/test_seam_k3.py tests/hexfold/test_seam.py
  tests/hexfold/test_rim_type.py`, scoped container types and Ruff.
  Nothing in this spec is an executed positive result.
- Codex code review precedes root merge. Exact deployment/same-owner native
  exposure is a later gate; a pure fixture does not claim native SE join
  support or nanoreactor T2 readiness. No relax campaign/hero regeneration,
  MACE/xTB/DFT/provider jobs, threshold change or new dependency/schema.

The selected entry supersedes the broad acceptance wording below only for
this bounded slice: no relaxer's tolerance, measured extent campaign or
carbon-census research gates this geometric fixture. Other catalogue rows
and their open questions remain parked.

Reto, 2026-09-26: "I just feel there are certain seam types we can
support, so we should have a ... class of those (120-120 vs 4x'90', vs
120)". And the scope ruling in the same exchange: **"joining 3 sheets
unequally we just don't do yet."**

Not blocked. This is design work that lands *into* `src/hexfold/spec.md`
§6.2/§6.4/§11.3; `hexfold-integration` roadmap item 2 (`seam` k ≥ 3) is
the consumer and wants this decided before it builds.

## Motivation / why

§11.3 admits `seam … k≥3 atoms=sp2` and defines the seam atom as one atom
bonded to the i-th dangling atom of *every* participating rim. A trivalent
atom serves exactly three rims, so `k=4 atoms=sp2` is grammatically legal
and arithmetically impossible, and §6.4 consequently exiles every
four-sheet case to the sp³ item. Meanwhile §22.2's bent-seam solver does
not exist, and today an asymmetric case is "solved symmetrically, with no
`fit.unsolvable` and no warning" — silently wrong output rather than a
refusal.

A closed catalogue fixes both: it says what the notation supports, and it
makes everything else an explicit refusal. It is also the shape hexfold
already uses — §7's primitive table is a closed catalogue of decorated
lattice patches, and §12.1 already has a families interface.

## The geometry, corrected (verified 2026-09-26)

The seam atom's bond into a sheet lies in that sheet's plane but is **free
to tilt within it**. Fixing it perpendicular to the seam line was an
unstated assumption in `hexfold-sp3-seam.md` (now corrected there). For
dihedral φ between two half-planes and tilt α from the seam axis, two
bonds on opposite sides along the axis subtend

    cos θ = −cos²α + sin²α · cos φ

Verified by direct computation (pure trigonometry, no census involved):

| config | α | bond angles |
|---|---|---|
| 4 planes at 90°, alternating up/down | 54.74° = arccos(1/√3) | 109.47° ×6 — **exactly** tetrahedral |
| 4 planes at 90°, alternating up/down | 60° | 104.48° ×4, 120° ×2 |
| 4 planes at 90°, all same side | 60° | 75.52° ×4, 120° ×2 — bad |
| 4 planes at 90°, perpendicular bonds | 90° | 90° ×4, 180° ×2 — the old wrong answer |
| 3 planes at 120°, perpendicular bonds | 90° | 120° ×3, coplanar — §6.2's sp² model |
| 3 planes at 120° | 60° | 97.18° ×3, **not coplanar** — not sp² at all |

**α is set by the rim edge-word, not chosen.** Measured off a flake by
taking each degree-2 rim atom's dangling direction against the rim line:
zigzag rim → α = 90°; armchair rim → α = 60°. The lattice therefore does
not offer the exact-tetrahedral 54.74°, but armchair's 60° lands ~5° off
on four bonds, which is ordinary strain rather than a degeneracy.

**The load-bearing consequence: seam type and rim edge-word are one
decision.** Three sheets at 120° require zigzag rims (armchair gives
97.18° and non-coplanar bonds, so it is not sp²); four sheets at 90°
require armchair rims (zigzag collapses to 90°/180°). This is strictly
stronger than §11.3's present rule, which only requires the k rims to have
edge-words compatible with *each other* and says nothing about matching
the seam's dihedral pattern.

## In scope — the catalogue

Each entry is keyed `(k, dihedral pattern, rim edge-word, hybridisation)`
and owes a seam-atom motif, a ring census and a strain figure.

| k | dihedrals | rim | seam atoms | status |
|---|---|---|---|---|
| 2 | crease | either | — | not a seam; `fuse` merges to one sheet (§6.4). Implemented |
| 3 | 120·3 | zigzag | 1 × sp² | §6.2's model. Unstrained, coplanar, trivalent. Census **derived only** |
| 4 | 90·4 | armchair | 1 × sp³ | un-excluded by the correction above. Census + strain owed |
| 4 | 90·4 | armchair | 2 × sp², bonded to each other, 2 sheets each | Reto's third option; had no home anywhere before this item |
| 3 | unequal | — | — | **OUT — "we just don't do yet" (Reto, 2026-09-26). Refuse, do not approximate** |

- A `seam.type` selector (or inference from the k rims' edge-words, with
  the mismatch as an ERROR) so an author cannot silently get the wrong α.
- **Refusal is a deliverable.** Anything off-catalogue — notably an
  unequal-dihedral k=3 seam and any k ≥ 5 — must produce
  `fit.unsolvable`/`port.mismatch`, never a symmetric guess. This
  supersedes §22.2's current silent behaviour *for seams* (the collar case
  stays §22.2's).

## Open questions / decisions log

1. **Ring census per entry — Reto, 2026-09-26: "the ring census we need to
   think about it."** Deliberately unresolved here; no number is asserted.
   §6.2 derives the k=3 sp² census as octagons in three families (AB, BC,
   CA) with the note "derived here, **verify by build**", and §29 Q6 already
   flags it as unchecked against carbon honeycomb [S2]. The k=4 entries have
   no census at all. Nothing downstream should quote a census until one is
   built and checked.
2. **Does the two-sp²-carbon motif keep §6.3's per-sheet χ rule?** With two
   seam atoms per period instead of one, the seam-face enumeration changes;
   whether seam faces still "belong to no sheet" needs re-deriving.
3. **What does "2 sheets each" mean** — four distinct sheets, or two
   carbons sharing sheets? Changes the census, the coplanarity of the two
   carbons' bonds, and the motif's dihedral. Pin this first; everything in
   that row depends on it.
4. **Is the squashed-tetrahedron 104.48°/120° within `strain_max`?** Ties
   to §29 Q5 (the `strain_max` number is a literature lookup).

## Explicitly NOT in scope

- Unequal-dihedral k=3 seams (ruled out above).
- Seam **vertices** — points where seam curves meet. Stays excluded per
  §6.4 and owned by `hexfold-sp3-seam.md`.
- The sp³ *isolation band* — `hexfold-sp3-isolation-band.md`, decoration
  within one sheet, unrelated.
- Any electronic-structure claim.

## Acceptance criteria

- Each catalogue entry builds and `check`s clean, with per-sheet
  `euler.residual` 0 for every participating sheet (§6.3).
- Every seam atom reports the bond count and hybridisation its row
  declares, and its measured bond angles match the table above within the
  relaxer's tolerance.
- An unequal-dihedral k=3 seam and a k=5 seam are both **refused** with a
  named code, and a test pins the refusal.
- A rim edge-word that contradicts the seam type is an ERROR, not a
  warning.
- The k=3 sp² census is checked against carbon honeycomb [S2] before any
  entry's census is treated as truth (§29 Q6).

## Target + blast radius

`src/hexfold/spec.md`: §6.2 (the α freedom + the edge-word coupling),
§6.4 (four-sheet seams are no longer excluded on geometry grounds — only
seam vertices are), §11.3 (`seam.type`, the strengthened edge-word rule,
the k ≥ 4 semantics), §29 (retire the fixed-α premise; Q6 unchanged).
Then `text.py` (selector), `build.py` (motifs), `check.py` (refusal
codes). No precis-side change.

## Rim standard (proposed 2026-09-27, for the spec's §10/§7)

Reto, 2026-09-27: "Would we benefit from standardizing edges (straight
carbon bonds at 90 deg from edge or something)?" — yes, and it is mostly
declaring what the spec already practises. The catalogue above already
established that seam type and rim edge-word are one decision, so a
library of parts needs a **typed** rim, not a free one.

- **Two rim types, not one.** "Bonds at 90° from the edge" is the zigzag
  rim (α = 90°); armchair is α = 60°. They are the only pure rims the
  lattice offers and they are not interchangeable: 120° three-sheet sp²
  seams need zigzag, 90° four-sheet sp³ seams need armchair. A generic
  framework needs both.
- **Rim type = (edge-word, N).** Two rims join iff both match, up to phase
  k. Mixed edge-words (chiral tube ends, `z5·a3·z2`) are bespoke
  interfaces; the standard library restricts itself to `(n,0)`, `(n,n)`,
  lattice-aligned sheet cuts and the `(6k,0)` lids — all pure.
- **Preferred numbers: multiples of 6 for zigzag.** Already implicit in
  `cap(6k,0)`, washer rule k ≥ r+3, "Δn ≥ 12 only". Declaring the series
  means every cap, washer, tube and lid interoperates by construction.
- **One adapter type.** Zigzag ↔ armchair is a 30° lattice rotation across
  the boundary — a graphene grain boundary, a line of 5-7 pairs, zero net
  curvature. It is the periodic `57` glyph and the only adapter the
  two-type standard needs.

Why it matters beyond hexfold: the rim type is the sp² instance of the
**port type** that makes se generic across lattices (see
`diamondoid-pattern-language.md` — a `(hkl)` facet with termination is
the sp³ instance). `options(handle, wish)` (spec §25.3) should search a
typed space, so this lands before it.
