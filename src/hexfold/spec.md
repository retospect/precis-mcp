# hexfold — notation and layered design for curved sp² carbon

Format version **0.2** (draft). Supersedes `SPEC.md` 0.1 and the hexfold
`README.md` of the standalone repo (both folded into this file, 2026-09-16),
and absorbs the *Smooth-Space → Carbon Structure Converter* design note.
The format is versioned independently of the library; every file's header
line names the format version, the provenance line names the library that
wrote it.

hexfold describes carbon-like nanostructures — tubes, cones, sheets, caps,
fullerenes, nanobuds, pillars, junctions, seams, tori — as **defect
placement on a hexagonal lattice** and, above that, as **patches of a smooth
surface joined along rims and seams**. A `.hx` file is a string you can
paste into a paper; the atoms are derived from it deterministically.

**Status badges.** Every section is tagged so a reader (human or agent)
knows what exists: `[impl 0.1]` shipped and tested in the standalone
library; `[spec 0.2]` specified here, to build in the 0.2 discrete work;
`[design]` architecture agreed, grammar or numerics not yet built; `[open]`
an unresolved question with a stated tradeoff. Nothing tagged `[design]` is
callable today.

Sources are cited inline as `[Sn]` and listed in §31 with DOI, full
reference and, where the paper is in the precis store, its store id. The id
is for us; the DOI is for readers of the public repo.

---

## Part I — What and why

### 1. The claim `[design]`

A system for designing arbitrarily complicated three-dimensional sp² carbon
— sheets, tubes, junctions, enclosed volumes with holes and channels — where
the designer states intent in language, the smooth surface is the source of
truth for anything authored there, and the atoms are a derived, regenerable
product. The claim being demonstrated is not primarily chemical: it is that
an LLM can drive a geometry kernel of this kind *in the proper way*, stating
what it wants rather than placing rings.

The originating problem is the **three-sheet seam**: an sp² carbon whose
three σ-bonds do not lie in one sheet — one to sheet A, one to B, one to C —
so sheets can be joined along an edge, the way walls meet in a honeycomb.
Topologically it is a triple-junction line as in Plateau borders [S1] and
in the sp² junction variant of carbon honeycomb [S2]. It is the one object
that takes hexfold beyond surfaces (§6.3).

### 2. The layer stack `[design]`

```
LLM / natural language        states intent: what, where, roughly which way
        │
SYMBOLIC layer                parts, rims, chains, integer constraint propagation
        │
SMOOTH layer                  minimal-surface patches, seams, curvature bound,
        │                     lattice direction field      (source of truth when authored here)
DISCRETE layer                ring placement: budget + distribution   ← the 0.1 notation lives here
        │
ATOMS                         coordinates, relaxation, rim termination
```

Each layer is a pure function of the one above. Edits go to the highest
layer the design was authored in; everything below is regenerated (§23).

| layer | status | where |
|---|---|---|
| discrete notation, check, canon, text/JSON | `[impl 0.1]` | `hexfold` |
| stick geometry | `[impl 0.1]` | `hexfold.stick` |
| seams k ≥ 3, registry closure, fit families, sectioned file | `[spec 0.2]` | `hexfold` |
| symbolic chain solver | `[design]` | `hexfold` (integer, numpy-free) |
| smooth layer | `[design]` | `precis_surface` (kernel) + `hexfold.smooth` (carbon binding, parse/emit) |
| physics tiers | exists in precis | `precis.structure` ladder |

### 3. Vocabulary

Existing surface-topology terms are used in code and prose rather than
coined ones: a three-rim part is a *pair of pants* [S3], not a new word.

| term | meaning |
|---|---|
| **patch** | a rim-bounded piece of surface; every primitive is one (§7) |
| **port** | a named attachment site on an instance; two kinds below |
| **rim** | the port kind that is a boundary loop: an ordered cyclic list of rim atoms with an edge-word (§10) |
| **atom port** | the port kind that is one atom with a direction, for bond-mode attachments (§10) |
| **seam** | an identification of k ≥ 2 rims as the same curve; k = 2 is a *fuse*, k ≥ 3 a *junction line* (§11) |
| **sheet** | a connected component of the net under fuse identification; seams of k ≥ 3 do not merge sheets (§6.3) |
| **defect** | a non-hexagonal ring, realised as Volterra surgery (§8); its charge is 6 − n |
| **glyph** | a named multi-defect footprint (`sw`, `57`) whose disks may overlap (§8) |
| **budget** | the net charge a region must carry (integer; §22.1) |
| **placement** | which sites carry it (the search; §22.1) |
| **registry** | the lattice orientation, carried as fuse phases on the discrete side and as a direction field on the smooth side (§12, §20.5) |
| **handle** | a stable reference into surface space: point, frame, patch (§23) |
| **menu** | a macro expanding to solved `bond`/`fuse` lines with a citation (§16) |
| **fidelity** | the tier atoms were produced at: `check` (none) < `stick` < `geo` < `emt` < `ml` (§15) |

### 4. Truth ordering and the sectioned file `[spec 0.2]`

A file has **authored sections** and one optional **generated section**.

- Authored: the header, `lattice:`, the `smooth:` section (§21), instance
  and connect statements (the 0.1 notation), and atom-addressed ops
  (`bond`, holes, `terminate`, §23.2). Canonical form (§14) and the
  content hash cover these only.
- Generated: atoms, bonds, rings, coordinates, the generator name and
  version, the fidelity tier, the platform, and the hash of the authored
  sections it was produced from. It is a **verifiable cache**: a reader
  recomputes the hash and reports `gen.stale` on mismatch; it never
  silently reuses a stale block.

Truth is ordered: `smooth:` (if present) > discrete statements > generated
atoms. The **text form `.hx` carries authored sections only** and stays
pasteable; the **JSON form `.hx.json` carries everything** (§18).

Determinism, stated honestly: atom and bond topology is byte-deterministic.
`stick` coordinates are deterministic on one platform and differ in the
last float bits across platforms, so they are stored rounded to 3 decimals
(as report values already are). Physics-tier coordinates are not
deterministic across versions of the relaxer; tier and version are part of
the cache key.

---

## Part II — The notation (format 0.2)

Sections 5–19 are the 0.1 specification with 0.2 changes marked.

### 5. Lattice `[impl 0.1]`

Axial coordinates `(u, v)` on the honeycomb's triangular Bravais lattice with
basis `a1 = (1, 0)·a`, `a2 = (1/2, √3/2)·a`, `a = √3·σ` (σ = ideal bond length,
default 1.42 Å for C). Each unit cell holds two atoms, sublattice `A` and `B`;
a site is `(u, v, s)`, `s ∈ {A, B}`.

The six primitive lattice directions are `dir 0..5`, counter-clockwise from
`+a1`; bond directions are `dir mod 3`. **All orientations in the discrete
notation are integers** in this set — never angles.

Elements: default `C`; `element=X` sets both sublattices; `elements=(X,Y)`
sets A and B respectively (h-BN, etc). `sigma` (Å), `sigma_CH` (Å, default
1.09), `strain_max` (§15.2) and the ring-ideal angles are lattice
parameters, never constants in code.

Units: the discrete notation and the generated atoms are Å-native, which is
precis's atomistic-enclave convention (the m↔Å crossing happens once at the
envelope boundary). The `smooth:` section and everything in
`precis_surface` use bare SI metres, the same convention as se poses, so a
surface overlays an se scene with no conversion (§24).

### 6. Counting law

#### 6.1 Manifold nets `[impl 0.1]`

For a trivalent net with `Pₙ` rings of size `n` and Euler characteristic χ:

    Σₙ (6 − n)·Pₙ + B = 6χ

where `B` is the boundary term contributed by open rims (§10). Hexagons are
neutral. Closed cage (χ=2): `Σ(6−n)Pₙ = 12`. Torus: 0. Schwarzite: < 0.
The derivation uses trivalence (3V = 2E) and "every edge borders exactly two
faces" (Σ n·Pₙ = 2E) substituted into V − E + F = χ; the same bookkeeping is
the disclination counting of the nanotube-junction literature [S4, S5]. In
direction-field terms it is Poincaré–Hopf for a six-fold field: a pentagon
is a +1/6 singularity, a heptagon −1/6, and the indices sum to χ [S6, S7].

Two boundary terms are carried per rim. `B` (combinatorial) is the
attribution that makes the law hold by construction on the net as built —
an internal consistency value. `B_expected` is what the rim's primitive
says the rim bounds: a sheet outer rim +6 (flat disc), a tube end 0
(cylinder), a cone base 6 − P, a cap rim 0; a hole rim carries the term
of the cell complex that was removed (`−(6χ_S − Σ_S(6−n))`, so −ring size
for a single ring, −6 for a flat-disc opening of any radius). Rims
consumed by a fuse are no longer rims; terminated rims keep their
expectation. `euler.residual` reports
`Σ_faces(6−n) + Σ_open_rims B_expected − 6χ`: non-zero means the net
cannot be flat/cylindrical/conical where its primitives say it is —
that is the curvature budget (a `collar{7×6}` on a sheet opening doubles
the six seam heptagons → −6, over-curved).

**Any ring size is legal.** The only *hard* error is a net declared `closed`
whose count cannot reach `6χ` for any χ ≥ 2 — matter that cannot exist.
Everything else — residuals on open nets, ring sizes outside 4–8, high defect
density — is a *finding*, never a refusal (§13). The constructor additionally
asserts `V − E + F = χ` on every net it builds (an internal consistency check;
its failure is a library bug, code `internal.euler`).

#### 6.2 Why a k ≥ 3 seam breaks the law `[spec 0.2]`

At a seam where three sheets meet, an edge can border three faces, so
Σ n·Pₙ = 2E is false and the law no longer relates the ring census to
anything geometric. It is not that χ takes a different value; the formula
does not apply.

Concrete model (sp² variant; derived here, **verify by build**): three
graphene half-sheets A, B, C meet at 120° along a line, each ending in a
zigzag rim. A chain of seam atoms sits on the line, one per zigzag period,
each bonded to one rim atom of A, one of B, one of C. Every atom is
trivalent and planar: the seam atom's three bonds lie in the cross-section
plane at 120°; each rim atom keeps its two in-plane bonds plus the bond to
the seam atom, which lies in its own sheet's plane. The seam is unstrained.
The rings through the seam are octagons in three families (AB, BC, CA), and
A's rim-path edge between consecutive rim atoms borders A's interior
hexagon, the AB octagon and the CA octagon. That triple-bordered edge is
the failure. Carbon honeycomb [S2] is the literature object with both sp²
and sp³ junction-line variants; the sp² census here is to be checked
against it.

#### 6.3 Per-sheet rule `[spec 0.2]`

- The Net carries a **sheet partition** of faces and a list of **seams**
  (§11.3). A sheet is a connected component under fuse (k = 2)
  identification; a seam of k ≥ 3 does not merge sheets.
- χ, `internal.euler` and `euler.residual` are evaluated **per sheet**. A
  sheet's rim loop carries its primitive's `B_expected` regardless of how
  many seams the loop passes through: a flat sheet meeting a seam is still
  a flat disc. Seam membership is invisible to the sheet's law.
- **Seam faces belong to no sheet.** They are reported as `seam.rings`
  (§13) and excluded from every census. A seam has no χ of its own; its
  consistency is combinatorial (§11.3).
- A fuse is the k = 2 case: its seam faces are assigned to the merged
  sheet, because the result is again a manifold.

#### 6.4 Completeness and exclusions `[spec 0.2]`

- *Manifolds.* Every compact orientable surface with boundary decomposes
  into pairs of pants, cylinders and discs glued along rims [S3]. hexfold
  has cylinders (`tube`), discs (`sheet`, `cap`, `cone`) and the sphere;
  the pair of pants is `junction(3)` (§28, roadmap 4). Once it lands,
  hexfold with fuse gluing is pants-complete for compact orientable
  surfaces with boundary.
- *Non-orientable* surfaces are excluded by construction: fuse traverses
  the destination rim in reverse (§11.2), which enforces orientability. A
  Möbius strip would need an orientation-reversing fuse; deliberate
  non-goal.
- *Seams* (k ≥ 3) take the class from surfaces to 2-complexes whose
  singular set is a union of curves — closed loops (a sheet with a pill
  above and a bump below meeting along the pill's foot ring) and open
  segments whose ends turn into free or fused rims (the flange, §26).
  Kinks in a seam curve are corners in each sheet's rim — edge-word turns
  on the lattice, piecewise-smooth rims in the smooth layer (Schwarz's
  patches are spanned on skew polygons [S8]) — and are allowed.
- *Excluded in 0.2:* **seam vertices**, points where seam curves meet
  (Plateau's second law: four lines at the tetrahedral angle [S1]); an
  atom there needs four sheets, i.e. sp³, and belongs to the sp³ backlog
  item (§28). **Creases** — two sheets meeting at an angle along a line
  with no third sheet — are not seams at all: a fuse merges them into one
  sheet, so a crease is curvature of one surface and is refused when
  sharper than the bound (§20.4). A "pyramidal join of four planes" is
  therefore a `cone(P)` with creased edges plus a seam vertex at the apex,
  and is out until sp³.

So the honest description of hexfold 0.2 is: **branched surfaces with a
one-dimensional singular locus, built from pants, tubes, discs and k-fold
seams.**

### 7. Primitives `[impl 0.1]` (+ `junction` `[spec 0.2]`)

| primitive | parameters | notes |
|---|---|---|
| `sheet(W, H, rim=…)` | extent in lattice cells or Å; rim edge-word (§10) | flat, χ contribution via rim |
| `tube(n, m, len=L\|fit, hand=+\|−)` | roll-up `(n,m)`, `0 ≤ m ≤ n`; length in unit cells or `fit` | rotational symmetry order `g = gcd(n,m)`; `hand` only meaningful for `0<m<n`; radius `R = a√(n²+nm+m²)/2π` [S9] |
| `cone(P)` | `P ∈ 1..5` pentagons at apex | derived opening angle `sin(θ/2) = 1 − P/6` [S10]; `P=6` is a cap, `P=0` a disc |
| `cap(n, m)` | C60 hemisphere cut perpendicular to a face axis, port `in` | v0.1: `cap(5,5)` only (30 atoms, 10 dangling, 6 pentagons; C5 axis). Any other `(n,m)` is `build.kind` — a C3-axis hemisphere is not a cap (its seam is `{5:3,7:6}`, leaving the end over-curved). 0.2 roadmap: flat-lid family (§28) |
| `fullerene(N, iso=k)` | closed cage, 12 pentagons | v1: `C60` only [S11]; general Goldberg later |
| `junction(k)` `[spec 0.2]` | sphere with k tube holes | χ = 2 − k ⇒ 6(k−2) heptagons (or half as many octagons); k = 3 is the pair of pants [S3]; C3-symmetric Y-junctions per CoNTub v2 [S5] |

Every primitive is a **decorated lattice patch**: a region of the flat lattice
plus a cut prescription (§8). Tubes/cones/sheets differ only in defect content
and identification of boundary.

### 8. Defects and surgery `[impl 0.1]`

A defect is `(ring size r, site (u,v,s), dir d)`. Semantics are **Volterra
surgery** on the flat lattice: a pentagon excises a 60° wedge at the site and
re-glues; a heptagon inserts one. Squares/octagons are two wedges. The
surviving vertices and edges *are* the bond graph — there is no fill or search
step.

**Validity = cut disks are disjoint.** Each surgery owns a disk on the lattice
(the wedge plus one hexagon corona). Disjoint disks always compose; this is
checkable in integer arithmetic before any atom exists. For pentagons the
disjoint-disk rule is the isolated-pentagon rule [S12]. Overlapping disks are
legal only through a named **glyph**:

| glyph | meaning | parameters |
|---|---|---|
| `sw` | Stone–Wales 5-7-7-5 (90° bond rotation) | site, `dir` of the rotated bond |
| `57` | pentagon–heptagon pair (elbow / dislocation core) | site, `dir` from 5 to 7 |

Glyphs are fixed footprints; their internal cut arrangement is part of this
spec, not user-tunable. A defect set that neither has disjoint disks nor is a
glyph is finding `cut.overlap` (ERROR) naming the two defects.

Symmetric shorthand: `{7×6 @fit}` = six heptagons, sixfold, positions solved
(§12). Orbit sums: `{5@(u,v,A)·k3 + 7@(u',v',B)·k3}` = each site replicated
under the `C_k` rotation of the host. A collar of order `k` requires
`k | gcd(n,m)` on a tube (`port.symmetry`, ERROR).

### 9. Atom identity `[impl 0.1]`

Every atom has a stable **path ID**:

    <instance>/(u,v,s)          native lattice site of instance
    <instance>/d<i>/(u,v,s)     atom created by defect i's inserted wedge
    <instance>/H/<path>         termination atom on a dangling atom (§12)
    <seam>/s<i>                 seam atom i of a k ≥ 3 seam            [spec 0.2]
    <fragment>.<label>          atom inside a foreign fragment (§11.4)

Surgery deletes or adds sites; it never renumbers. The sublattice letter is
part of the ID, so sublattice parity of any bond is a string comparison.
Stable IDs are what let atom-addressed ops survive regeneration (§23.2).

Inserted-wedge frame: for defect `i` at site `p` with direction `d`, the wedge's
own `(u,v)` coordinates are measured from `p` with `+a1` along `dir d` and the
wedge opening counter-clockwise from it.

A flat integer **ordinal** is also emitted in the JSON form: atoms sorted by
`(instance, path)` lexicographically, 0-based. Consumers wanting arrays use the
ordinal; the path↔ordinal map is always written alongside.

### 10. Ports: rims and atom ports `[impl 0.1]` (+ atom ports `[spec 0.2]`)

`port` is the general term. Two kinds:

**Rim.** An **ordered cyclic list of rim atoms** with an edge-word and an
outward normal (`dir`, or `axis` for tube ends). Edge-word alphabet:

    z   zigzag step      a   armchair step      <r>   non-hexagon rim ring of size r

Written run-length: `z5·a3·z2`. Canonical form is the lexicographically minimal
rotation, traversed counter-clockwise as seen along the surface's *inward*
normal (Booth's algorithm; O(n)).

Boundary term `B` in §6: each rim contributes its combinatorial
attribution (consistency value); the JSON form carries `B` per rim plus
`B_expected`, the term the rim's primitive says it bounds. Consumers
do not need to compute either.

Module-level ports are **always relative**: a module states its out-port's
transform from its in-port, never a global frame. Registry frames propagate
from the single `origin` instance; for a chiral tube the out-port rotates by
`len × twist-per-cell`, derived from `(n,m)`.

**Atom port** `[spec 0.2]`: one atom with a direction, declared
`port <name> = <inst>/(u,v,s) [dir d]`. It is what a bond-mode attachment
(the `[2+2]` and `9-6` menus) or an se `bond` connect binds. A rotational
degree of freedom around a single bond is not hexfold's concern: it is an
se `bond` connect carrying a revolute joint on that axis.

Ports on `se`-style consumers that bind one atom get a rim's atom 0; the
whole ring is in the JSON (`GeneratedPort.atoms`).

### 11. Attachments

#### 11.1 `bond` `[impl 0.1]`

    bond  A -- B [order=1]        add a covalent edge; both sides keep all atoms

`bond` hosts that gain a fourth bond are recorded `hyb=sp3` in the JSON
(derived, never authored); a fifth is `valence.over` (ERROR). Every
authored `bond` emits `annot.sublattice` INFO (`data.parity = same|cross`)
when both endpoints are lattice sites.

#### 11.2 `fuse` — a seam of multiplicity two `[impl 0.1]`

    fuse  P --> Q  k=<phase>      delete nothing further; glue two rims rim-to-rim

`fuse` pairs the two rims' **dangling atoms** (the degree-2 atoms of each
rim walk, in cyclic order — `rim.dangling` in the JSON): `P.dangling[i]` is
bonded to `Q.dangling[(k-i) mod N]` for all `i`, the destination rim
traversed in reverse because the two rims face each other. The rim
edge-words are not a hard constraint: after fusing, the seam faces are
enumerated and reported as `seam.rings` INFO (`data.rings = {size:
count}`; was `fuse.seam` in 0.1); a seam ring outside 4..8 additionally
reports `ring.size.unusual`. Fused rims are consumed. `k ∈ 0..N−1` is the
rotational phase (360·k/N) — the only "angle" in the discrete notation, an
integer. Equal dangling counts are required (`port.mismatch`, ERROR).
`--fuse k=-->` is sugar for a two-rim `seam` statement (§11.3).

Euler bookkeeping for `bond`-joined surfaces: the counting law is reported
per surface component evaluated **before** the authored attachment bonds
(`euler.chi` findings carry `where=<instances>`); the fused/bonded whole
keeps its own `euler.chi` as well.

#### 11.3 `seam` — multiplicity k ≥ 3 `[spec 0.2]`

    seam <name>: A.r1 == B.r1 == C.r1 [k=<phase>] [atoms=sp2]

Identifies k rims as one curve. Semantics: a chain of seam atoms
(`<name>/s<i>`, one per rim period) each bonded to the i-th dangling atom of
every participating rim, registered by the phase. Requirements: equal
dangling counts and compatible edge-words along the loop for all k rims
(`port.mismatch`, ERROR, generalised to k); the seam is a closed loop or an
open segment whose ends are free or fused rims (§6.4). `atoms=sp2` is the
only variant in 0.2; `sp3` is reserved (§28). Seam faces are enumerated
and reported as `seam.rings` and excluded from every sheet census (§6.3).
Acceptance example: a sheet with a pill above and a bump below meeting
along the pill's foot ring — a closed, in-plane-curved triple seam.

#### 11.4 Holes and openings `[impl 0.1]`

`- pentagon@<site>` / `- hexagon@<site>` removes the ring of that
size incident on the site — the one whose canonical vertex tuple is
lex-min, or, when a `:d` suffix is given, the ring whose centre lies in
lattice direction `d` from the site (the preferred, authored form — the
emitter writes it). A hole mints a rim named `hole` (or `hole<i>`) whose
`atoms` is the rim walk and `dangling` its bondable atoms.

`- path3@<site>:d` removes a connected path of three atoms: `site`, its
neighbour along `d`, and the zigzag-straight continuation. Removing a
connected set `S` with `e_S` internal edges leaves `3|S| − 2e_S` dangling
atoms, so a 3-path gives 5 and no degree-1 leftovers.

`- hex(r)@<site>:d` removes the hexagonal cluster of rings within
ring-adjacency radius `r` of the selected hexagon; dangling `6(r+1)`:
hex(0) = 6, hex(1) = 12, hex(2) = 18. This is the sheet opening for landing
a tube post: hex(0) seats a `(6,0)` end, hex(1) a `(12,0)` or `(6,6)` end.

Thin hosts are a hard error, not a residual: if a hole's rim walk
revisits a vertex under the tube identification the opening spans the
circumference, and if the new rim shares a vertex with a pre-existing
rim the opening clipped a tube end — both are `cut.overlap` (ERROR).
The same applies to collar disks whose wedge sectors self-identify under
the wrap.

Foreign fragments:

    frag glucose = smiles("OC[C@H]1... [*:1] ... [*:2]")

are **opaque references** with declared attachment points (SMILES's own
`[*:n]` markers). A reader must parse the file without realizing the fragment;
realization (rdkit) is optional. Attachment atoms are `glucose.1`, `glucose.2`.

### 12. Solving

#### 12.1 `fit` `[impl 0.1]`, families `[spec 0.2]`

`fit` marks an integer parameter the compiler solves. 0.1 solves: tube
`len` (footprint solve, below), fuse phase `k` (smallest maximum seam-ring
size, tie-break lowest k) and collar site positions (`{r×k @fit}` —
maximal, symmetric, cut-disjoint placement). Tie-break is fixed: **lowest
lattice index wins** (`(u,v)` lexicographic, then `A<B`, then `dir`).
Unsolvable → `fit.unsolvable` (ERROR) with the nearest commensurate values in
`data`.

0.2 changes, so the bent collar (§22.2) is a drop-in later:

- **Generators return a family, not a value.** `fit` enumerates every
  solution up to a cap of 64, ranks by the cost below, applies the first,
  and reports the rest as `fit.alternatives` (INFO, `data.alternatives =
  [{value, cost}]`). The `options` verb (§25.3) is this family surfaced as
  ranked candidates.
- **Cost, in order:** (1) distance to the smooth target, *only when a
  `smooth:` section is present* — this is what makes a collar on a bumped
  sheet put heptagons on the outside of the bend; (2) smallest maximum
  seam ring; (3) smallest `|euler.residual|`; (4) lowest lattice index.
  Hand-authored files without a smooth section therefore keep their 0.1
  results.
- **`fit` on more parameters:** `tube(fit in {(5,5),(6,6)}, len=fit)`,
  `cap(fit)`; a domain is a set literal or `fit` alone (the primitive's
  whole catalogue, §26). Chains of parts with `fit` members are solved by
  propagation from the pinned ends (§22.3); because the constraints are
  integer equalities, propagation flows both directions.

Collar semantics (0.1): `fuse{r×k @fit}` on a connect places `k` `r`-ring
defects around the destination hole at the smallest radius whose
wedge-sector cut disks are pairwise disjoint and disjoint from the hole's
own disk. On a tube the `C_k` orbit is realised by circumferential
translation of `|C_h|/k`, which requires `k | gcd(n,m)` — otherwise
`port.symmetry` (ERROR). On a sheet the orbit is the `C_k` rotation about
the hole centre for `k ∈ {1,2,3,6}`. Solved sites are recorded in
`connects[i].expanded.defects`. A collar that solves far from its hole
emits `collar.far` (WARN).

`len=fit` is a **footprint solve**: the smallest integer `len` for which
every attachment on the tube is free of `cut.overlap` and rim clips; the
compiler iterates `len` from 1 upward (cap 64).

#### 12.2 Registry `[impl 0.1]`, closure `[spec 0.2]`

Registry rule: **tube `len` is quantised in translation periods `T`, and
`T` is a lattice vector — after any integer number of cells the lattice
is in register by construction.** Sub-period lengths are not expressible.

0.2 scopes this claim: it holds for a part graph that is a **tree**. Around
any **cycle** in the part graph — a torus of elbows, two legs from one
sheet landing on a second sheet, an axle pinned at both ends — the frame
maps must compose to the identity, and the last fuse cannot pick its phase
freely.

- `registry:` lines on acyclic graphs emit `registry.redundant` (INFO,
  "in register by construction").
- **`registry.closure`** (WARN by default, ERROR under STRICT): for every
  cycle of a cycle basis of the part graph, compose the per-part frame
  maps — tube: identity mod `C_g` after whole periods; fuse: rotation by
  `k` steps of its rim's `N`; symmetric collar: identity; bent collar: its
  rim-loop twist (§29 Q1) — and report the residual as an integer number
  of steps of the cycle's common symmetry (`lcm` of the rims' `N`). For a
  cycle of equal-`N` rims it is `Σ kᵢ mod N`. Non-zero means the cycle
  closes only through strain or an undeclared defect pair; the author
  spends it explicitly (a collar, a `57` glyph) or accepts the WARN. The
  direction field (§20.5) is the smooth-side generalisation; it is not
  needed for this check.

`grain` remains a reserved parsed keyword, a no-op.

#### 12.3 `terminate` `[impl 0.1]`

`terminate: <inst>.<port|*> = <element>` passivates dangling rim atoms:
one termination atom per dangling atom, bond order 1, atom path
`inst/H/<dangling atom path>`, `hyb="s"`, C–H rest length `sigma_CH`.
Terminated rims are consumed; the rim still counts as a boundary for
valence and the counting law.

### 13. Diagnostics `[impl 0.1]` (+ 0.2 codes)

`check(spec) → Report`. Never raises for structural problems (only
`ParseError` for unreadable text). A `Finding` is
`(code, severity, message, where, span, data, fix)`; **codes are the API,
messages are not.** Findings are sorted `(severity desc, span, code, where)`;
`to_dict()` uses sorted keys. Severity: `INFO < WARN < ERROR`. `Report.ok` ⇔
no ERROR. No `__bool__`.

| code | default | meaning |
|---|---|---|
| `euler.closed_unreachable` | ERROR | declared closed, count cannot reach 6χ |
| `euler.residual` | WARN when ≠0, INFO when 0 | per sheet (0.2): `Σ(6−n) + Σ B_expected − 6χ` (`data.residual`, `data.sheet`) |
| `euler.chi` | INFO | χ and genus per sheet |
| `internal.euler` | ERROR | `V−E+F ≠ χ` per sheet, or combinatorial `B` inconsistent (library bug) |
| `valence.over` / `valence.under` | ERROR / WARN | atom with >3 (or 4 after `bond`) / <3 bonds not on a rim |
| `cut.overlap` | ERROR | two surgeries' disks intersect and are not a glyph; also an opening/collar disk that spans the tube circumference or clips an existing rim |
| `ring.size.unusual` | WARN | ring outside 4..8 |
| `port.mismatch` | ERROR | seam: dangling count or edge-word incompatible across its k rims |
| `port.symmetry` | ERROR | collar order does not divide gcd(n,m) |
| `collar.far` | WARN | solved collar sits > `2·r_hole + 3` cells from the hole centre |
| `fit.unsolvable` | ERROR | no commensurate value; nearest in `data` |
| `fit.alternatives` `[spec 0.2]` | INFO | the ranked remainder of a `fit` family (`data.alternatives`) |
| `registry.redundant` | INFO | acyclic: in register by construction |
| `registry.closure` `[spec 0.2]` | WARN (STRICT: ERROR) | cycle residual in symmetry steps (`data.cycle`, `data.residual`) |
| `seam.rings` `[spec 0.2]` | INFO | ring census along a seam (was `fuse.seam`); `data.rings`, `data.k` |
| `op.dangling` `[spec 0.2]` | ERROR | an atom-addressed op names an atom that no longer exists after regeneration |
| `gen.stale` `[spec 0.2]` | WARN | generated section's hash ≠ hash of the authored sections |
| `frag.unrealized` | INFO | fragment referenced but not built (no rdkit) |
| `geom.summary` | INFO | rms/max bond-length and angle deviation (geometry tier) |
| `geom.bond.long` / `geom.bond.short` | WARN | bond deviates from σ beyond threshold |
| `geom.angle.dev` | WARN | vertex angle deviates from its ideal — ring ideal for sp², 109.47° for sp³ (0.2 fix; 0.1 used the ring ideal at sp³ atoms too) |
| `geom.join.angle` | INFO | fuse join angle ψ (derived) |
| `geom.join.curvature` | INFO | fuse join curvature sign pattern → Model I / II |
| `strain.over` `[spec 0.2]` | ERROR | physics tier: per-atom strain energy above `strain_max` (§15.2) |
| `smooth.bend` `[design]` | ERROR | principal curvature above the bound on a sheet interior (§20.4) |
| `smooth.singularity_spacing` `[design]` | ERROR | two field singularities closer than two ring-steps (§20.4) |
| `smooth.field` `[design]` | ERROR | direction field admits no solution for the stated boundary conditions (§20.5) |
| `smooth.embedding` `[design]` | WARN | self-intersection detected at the stated tier (§27) |
| `annot.sublattice` | INFO | per attachment bond: same/cross sublattice |
| `annot.host_sublattices` | INFO | per-menu summary of host-sublattice counts |
| `terminate.done` | INFO | port passivated: `data` counts per element |

Profiles are **passed, not set** (no module-global state):
`Profile(promote=frozenset, demote=frozenset, ignore=frozenset,
bond_tol_A=0.10, angle_tol_deg=10.0, strain_max_eV=…)`. `Profile.DEFAULT`,
`Profile.STRICT` (promotes `euler.residual`, `ring.size.unusual`,
`registry.closure`, `geom.*` WARNs to ERROR).

Geometry-tier findings (`geom.*`) exist only with `check(spec, geometry=True)`
and describe the **stick model** (§15), not matter.

### 14. Canonical form `[impl 0.1]`

Two files describe the same structure iff their canonical JSON is byte-equal.
Canonical form covers the **authored sections only** (0.2).

1. Origin: the author-declared `origin` instance (v1 does not auto-select).
2. Within the origin instance, remove translation and rotation freedom by
   enumerating every frame `(defect site as (0,0), rotation r ∈ C_g)` — at
   most `6·D` candidates for `D` defects (fewer on a tube: only `C_gcd(n,m)`
   rotations and axial translation). Re-express all defects, sort, serialize;
   **pick the lexicographically smallest.** Ties are true symmetries; the
   first in enumeration order wins.
3. **Mirrors are excluded.** Chirality is a real distinction. An
   enantiomer-invariant hash is a separate, opt-in function.
4. Edge-words: lex-min rotation (§10). Menus (§16) are stored *expanded* in
   the canonical JSON, with the macro line retained as `source`.
5. Authored JSON: sorted keys, no floats in the discrete sections
   (integers and strings only). The `smooth:` section's floats (metres) are
   serialised as shortest round-trip decimal strings.
6. The **content hash** is sha256 of the canonical authored JSON; it keys
   the generated section (§18) and the catalogue (§26).

### 15. Derived geometry and the fidelity ladder

#### 15.1 `stick` `[impl 0.1]`

`stick(net) → coords` (Å, float64): spectral seed (three adjacency
eigenvectors below the Perron one, signs fixed by largest-component-positive)
rescaled to σ, then a spring model — bond springs to σ, angle springs to the
ring ideal (108° / 120° / 128.57° / …; 109.5° at sp³ in 0.2), soft
non-bonded repulsion — relaxed in fixed iteration order with a fixed step
count. Labelled `fidelity="stick"` in its provenance. Deterministic on one
platform; report values rounded to 3 decimals so cross-platform reports
agree. **A preview, not physics**; consumers with a physics ladder treat it
as a seed.

Primitives with a known closed-form embedding provide the relax seed:
fullerene tables; tubes from the analytic cylinder roll-up; cones from the
flat wedge-excised disc lifted by a fixed apex bump; sheets from the flat
embedding plus a fixed perturbation; spectral otherwise. The chosen seed is
recorded as `seed=closed-form|cylinder|cone|flat-perturbed|spectral`.

#### 15.2 Tiers and the feasibility threshold `[spec 0.2]`

`fidelity ∈ {check, stick, geo, emt, ml}`. hexfold owns `check` and
`stick`; `geo`/`emt`/`ml` are precis's `structure` relaxation ladder and
run as background jobs. `generate` (§25.1) defaults to `stick` below 5000
atoms and `check` above.

Feasibility at a physics tier: `strain.over` when any atom's strain energy
above pristine graphene at the same tier exceeds `strain_max` (eV/atom).
`lattice: strain_max=<value>` overrides the Profile default per design.
**The default value is `[open]`** (§29 Q5): a literature lookup, recorded
here with its source once made. `stick` never evaluates it; `stick`'s own
tolerances (`bond_tol_A`, `angle_tol_deg`) are preview thresholds.

### 16. Menus (macros) `[impl 0.1]`

Named junctions expand to `bond`/`fuse` lines with solved atom IDs. Each carries
a citation and a synthesis-accessibility tag.

| menu | expands to | cite | accessibility |
|---|---|---|---|
| `[2+2]` | 2 `bond` lines: the C60 6-6 bond with lex-min atom pair to the host bond from `site` along `dir` | [S13] | CVD-observed |
| `[4+4]` | 4 `bond` lines | — | theoretical |
| `9-6` | C54 = C60 `- hexagon`; six `bond` lines onto the **intact** host in the C3 registration whose seam is `{9:3, 6:3}` | [S14] | DFT |
| `8-7` | same, in the registration whose seam is `{8:3, 7:3}` | [S14] | DFT |
| `DA-neck(len)` | C60 `- pentagon` → `tube(5,0,len)` → host `- path3` opening, neck fuse `k` fitted over 0..4 by smallest max seam ring | [S15] | TEM-consistent |
| `DB-neck(len)` | C60 `- hexagon` → `tube(6,0,len)` → host `- hexagon`, both fuses `k=0` | [S15] | TEM-consistent |
| `collar{r×k @fit}` | k defects of ring size r in a `C_k` orbit | — | — |

A sheet↔tube seam needs no collar: `hex(0)` ↔ a `(6,0)` end already fuses
to `{7:6}`, and adding `collar{7×6}` doubles the heptagons
(`euler.residual` −6). Collars are for tube-to-tube diameter changes and
asymmetric necks.

**Verified seam results (0.1, `seam.rings`).** 9-6 and 8-7 are the two
C3-symmetric registrations of C54's six dangling atoms onto the atoms
surrounding one host hexagon, per Zhu & Su [S14] (the host CNT stays intact; six `bond`
lines are generated, host atoms become sp³). Enumerating all injective
orbit-to-orbit registrations and computing the seam rings yields, on both
armchair `(10,10)` and zigzag `(17,0)` hosts, exactly the two named
classes `{9:3, 6:3}` and `{8:3, 7:3}`. `DA-neck`: bud-side seam `{7:5}` for
every `k`, host seam on `(10,10)` `{6:1, 7:2, 8:2}` — six units of negative
curvature, what P5 = 11 needs for χ = 0; verified single component, χ = 0,
residual 0. `DB-neck`: P5 = 9 after the hexagon excision, bud seam
`{6:3, 7:3}`, host seam the native `{7:6}`; P7 = 9 balances P5 = 9; verified
on `(10,10)`. Both necks are tighter than the default curvature bound
(§20.4) and need a per-design override when driven from the smooth layer.

Menus are recipes, not the catalogue: the catalogue is the cache of solved
results (§26).

### 17. Text form `[impl 0.1]` (+ sections `[spec 0.2]`)

```
hexfold 0.2
prov: lib=hexfold@0.2.0 design=<content hash of the smooth authoring, if any>

lattice: element=C sigma=1.42 strain_max=0.30

smooth:                                   # [design] — see §21; parsed and round-tripped opaquely in 0.2
  patch substrate: sheet rim=substrate.rim
  frame substrate.rim: at 0 0 0 axis 0 0 1 ref 1 0 0
  bound: curvature 3.52e8

origin substrate
substrate: sheet(20,20) - hex(0)@(9,9,A):0
post: tube(6,0,len=4)

substrate.hole --fuse k=0--> post.in

registry: post.in == post.out
terminate: substrate.rim = H
terminate: post.out = H
```

Grammar (informal): one statement per line; `#` comments; instance lines
`name: primitive(args) [- hole]* [xN]`; connect lines `src --verb[{menu}] [k=]--> dst`;
`seam`, `port`, `origin`, `registry`, `terminate`, `frag`, `bond`, `lattice`,
`prov`, `smooth:` keywords. Whitespace-insensitive except line breaks; the
`smooth:` block is delimited by its keyword and the next non-indented
statement, its lines are line-prefixed keywords like every other statement
(indentation is cosmetic). Unicode `×` and ASCII `x` are both accepted for
repeat/collar counts; the emitter writes `×`. **The text form never carries
the generated section.**

### 18. JSON form `[impl 0.1]` (+ `generated` `[spec 0.2]`)

```
{ "hexfold": "0.2",
  "prov": {...},
  "lattice": {"element": ["C","C"], "sigma_A": "1.42", "sigma_CH_A": "1.09", "strain_max_eV": "0.30"},
  "smooth": {...},                       # §21, present iff authored
  "instances": {name: {"kind":..., "params":..., "defects":[...], "source": "…"}},
  "connects": [...],                     # expanded, sorted; seams included with "k_rims"
  "ops": [...],                          # atom-addressed authored ops, in order (§23.2)
  "ports":  {name: {"kind": "rim"|"atom", "atoms":[ord...], "word":..., "normal":..., "B": int, "B_expected": int}},
  "sheets": {sheet_id: [face...]},       # 0.2
  "seams":  [{"name":..., "rims":[...], "k": int, "atoms":[ord...]}],
  "regions": {instance: [ord...]},
  "registry": [[a, b]],
  "terminate": [[glob, element]],
  "hash": "<sha256 of the canonical authored JSON>",
  "report":  Report.to_dict(),
  "generated": {                         # optional; verifiable cache
     "of": "<hash>", "generator": "hexfold@0.2.0", "fidelity": "stick",
     "platform": "...", "relaxer": null,
     "atoms": [{"path":..., "ord": i, "element":..., "hyb":..., "instance":..., "xyz_A": ["1.234","0.000","-0.710"], "surface": {"patch":..., "u":..., "v":...}}],
     "bonds": [[ord_i, ord_j, order]],
     "rings": [[ord...]]
  }
}
```

Sorted keys throughout; numeric strings keep the authored sections
float-free; generated coordinates are 3-decimal strings. `surface` on an
atom is present iff a `smooth:` section drove generation (§24.1).

### 19. Conformance `[impl 0.1]` (revised)

A conforming implementation: (a) parses §17 and emits §18; (b) produces
byte-identical §18 authored sections and topology for the same input on
repeated runs (CI: run twice, diff); (c) round-trips text → JSON → text →
JSON to identical authored JSON; (d) implements the codes in §13 with the
stated defaults; (e) writes coordinates **only** in the `smooth:` section
(frames, metres) and the `generated` section (atoms, Å) — never in a
discrete statement; (f) accepts and round-trips a `smooth:` section it
cannot solve; (g) reports `gen.stale` rather than reusing a mismatched
generated section.

---

## Part III — The layers above the notation `[design]`

### 20. The smooth layer

#### 20.1 Why solve here first

Solve in a functional representation; discretise to atoms last. Constraints
become analytic rather than combinatorial: rim matching is matching
boundary data; the curvature budget that would be counted in pentagons is
an integral. The result is a smooth target surface; ring placement becomes
*approximate this known surface* — a fitting problem rather than a search.
The catch: a minimal surface is a soap film, and real graphene is an
elastic sheet with bending stiffness, so the minimal surface is a
**scaffold, not the answer**.

#### 20.2 Finite, not necessarily periodic

Nothing forces periodicity; that is a property of the Schwarz surfaces,
not of minimal surfaces in general. These objects have rims, and a rim is
a legitimate boundary condition — Plateau's problem, a finite surface on a
finite wire loop [S8, S16]. A lens, a capped tube, a branched object with
ten terminated rims are all finite and well-posed. **Every open rim is a
prescribed curve (Dirichlet), never a free boundary**: a film with a free
edge contracts and the patch is underdetermined. A terminated rim takes its
curve from the discrete edge-word plus its frame. Periodicity is opt-in
(§28, roadmap 8).

#### 20.3 Seams as first-class

A k ≥ 3 seam is a singular curve, excluded by classical minimal-surface
theory (a minimal surface is a manifold). The relevant branch is minimal
surfaces with triple junctions — soap-film clusters under Plateau's laws,
proved by Taylor [S1]: three films meet along a line at 120°; those lines
meet four at a time at the tetrahedral angle. The first law is exactly the
sp² condition; the seam edge is therefore **free** — the collars either
side, where each sheet bends to reach it, are what costs curvature. Seams
are constraint objects in the representation (*these k boundaries are the
same curve, meeting at 120°*); a smooth patch with one sheet on a boundary
is the special case. The second law (seam vertices) is out of scope (§6.4).

#### 20.4 The curvature bound — a solver constraint, not a rescale

Work in σ-scaled units inside the solver; the surface is born at the right
size, which is both correctness (anything produced is realisable) and
pruning (high-curvature configurations are where generators are slowest).
The interface stays in metres (§5). "Sharper than the bound" is two rules:

- **Bending bound.** On every sheet interior, both principal curvatures
  satisfy |κ| ≤ κ_max, evaluated on the σ-resolution mesh (edge length
  never below σ, so nothing finer exists to measure; discrete curvature
  operators per [S17]). Per mesh edge of length h this is a dihedral limit
  θ ≤ h·κ_max. A crease is pure bending with zero Gauss curvature; this is
  the rule that rejects it: `smooth.bend`. **Default κ_max = 1/(2σ) =
  1/2.84 Å**: permits (5,5) (R = 3.39 Å), C60 (R ≈ 3.55 Å [S11]) and (6,6);
  rejects (4,4), (5,0) and (6,0), so the DA/DB necks need a per-design
  override (`bound: curvature <value in 1/m>`). The smallest observed tubes
  are far tighter (≈ 3 Å diameter inside multiwalls [S18]), so the default
  is conservative by intent. Cores of direction-field singularities, one
  ring-step radius, are exempt: a pentagon is a legal 60° concentration.
- **Spacing bound.** Field singularities at least two ring-steps apart —
  the isolated-pentagon rule [S12] and hexfold's disjoint cut-disk rule
  (§8) restated on the smooth side: `smooth.singularity_spacing`. Rejects a
  120° apex of two adjacent pentagons; allows `cone(2)` with a hexagon
  between.
- **Seams are exempt** from both: the dihedral there is Plateau's 120°
  between different sheets, and rim and seam curves may have corners.

#### 20.5 Registry as a direction field

Requirement: anchor lattice orientation in world space (*on planes A and B
one bond points toward +x*). Once both ends of a tube are pinned, its
chirality, length and end orientations are not independent — a
commensurability condition with a discrete solution family, the smooth
generalisation of `registry.closure` (§12.2).

Registry is carried **in** smooth space as a six-fold direction field on
the surface (an N-RoSy field [S19]); a world-frame constraint is a boundary
condition on that field. Singularities of a six-fold field have index
±1/6 and sum to χ (Poincaré–Hopf [S6]): **the pentagons and heptagons are
the field's singularities**, and the counting law is the index theorem —
ring bookkeeping and the registry field are one object viewed two ways.
What the field does *not* give for free is atom positions: a field is a
direction, not a lattice at fixed spacing, and turning it into one is the
integrability problem of hex remeshing [S20, S21]. The field delivers the
budget and coarse positions; hexfold's integer surgery delivers exact
topology; a fit step sits between (§22). `smooth.field` reports a field
with no solution for its boundary conditions.

#### 20.6 Numerical method — both, asymmetrically

- **Weierstrass–Enneper** [S22]: exact, provably minimal, conformal
  parameterisation for free (the direction field becomes a scalar angle;
  curvature integrals are exact). But recovering the holomorphic data from
  a prescribed rim is an inverse problem solved only for special polygons
  [S8], it is manifold-only (no seams), and it produces only minimal
  surfaces.
- **Discrete mesh**: area minimisation on a triangle mesh [S23]; film
  clusters as in Surface Evolver [S24], where minimising area satisfies
  Plateau's laws by itself, so seams come out naturally; the curvature
  bound, self-intersection energy (§27) and the direction field are
  solved on the same mesh; a Willmore bending term later moves it from
  soap film to elastic sheet. Cost: approximate, mesh-dependent, no proof
  of minimality, slower.
- **Decision:** the discrete mesh is the engine. Closed forms (catenoid for
  a tube, Schwarz P and D fundamental patches, fullerene tables) are the
  seed and the test oracle — the role `stick` already gives closed-form
  seeds. Weierstrass never becomes the general solver.

### 21. The `smooth:` section — proposed grammar `[design]`

Line-prefixed keywords, one statement per line, metres, no indentation
semantics. Parsers accept and round-trip it opaquely before any solver
exists (§19 f). Names match discrete instances: each `patch` is realised by
the instance of the same name; a discrete instance with `fit` parameters
is solved against its patch.

```
smooth:
  patch <inst>: sheet  rim=<inst>.<rim> [curve=rect(<w>,<h>) | curve=<pts>]
  patch <inst>: tube   rims=<inst>.in,<inst>.out [radius=<m> | fit]
  patch <inst>: cap    rim=<inst>.in
  patch <inst>: pants  rims=<a>,<b>,<c>
  frame <inst>.<rim>: at <x> <y> <z> axis <ax> <ay> <az> ref <rx> <ry> <rz>   # 6 dof, metres
  seam <name>: <a>.<rim> == <b>.<rim> [== <c>.<rim>] [angle=120]
  field: <inst>.<rim> bond -> <ux> <uy> <uz>          # direction-field boundary condition
  bound: curvature <kappa_max in 1/m>                  # default 1/(2σ)
  fit: <inst>.<param> [in {<v1>,<v2>,...}]             # don't-care with optional domain
  handle <name>: <inst> <u> <v>                        # named surface point (§23.1)
```

A `frame` line is a **rim frame**: position, axis normal to the loop, and
a reference direction fixing rotation — six degrees of freedom, and the
object both kernels understand (§24.2). Frames are defined here, in smooth
space, never derived from realised atoms, so regeneration does not drift
an anchor.

### 22. The discrete layer: budget and placement

#### 22.1 Two steps, always separated

**Budget:** integrate the required angular defect over the region bounded
by the seam, subtract what the background surface supplies; the result is
a net *pentagons minus heptagons* count — a scalar, non-negotiable, in
60° quanta (charge 6 − n per ring, §6.1). **Distribution:** walk the loop
and allocate that budget where local curvature mismatch is worst. These
stay architecturally separate so the hard search (distribution) can be
swapped without touching the budget.

#### 22.2 Flat seams vs bent seams

*Flat* (a surface of revolution): the collar is a `C_k` orbit, each row the
previous row rotated — `collar{r×k @fit}` today. *Bent* (the seam meets a
sheet that is already bent, e.g. the tilted pill or the sheet-pill-bump
seam): symmetry is gone, required charge varies around the loop —
qualitatively more heptagons on the outside of the bend, more pentagons on
the inside — and generation becomes search: discrete relaxation seeded from
the symmetric collar, moving charges to reduce local angle error. A smooth
target turns the search into fitting, which is easier, not solved. §12.1's
family interface and the budget/placement split are what make the bent
collar a drop-in; until it exists the solver **refuses a bent case with
`fit.unsolvable`** rather than producing something quietly wrong.

#### 22.3 Composition: chains of constraints

The LLM composes constraints, never geometry. An utterance like *a split
ring in a sheet, a lens below, spacers, a capped (n,m) tube on top* is an
ordered chain of parts sharing an axis, each with a spec or a don't-care;
every adjacency is an interface constraint (*this part's top rim equals the
next part's bottom rim*). Pin either end and propagation resolves the
middle; spacers are the slack. The tube contributes two integer parameters
(whole periods of length; rotation steps of `C_g`); collars carry the real
freedom, characterised by the twist they inject (§29 Q1). Matching is then
arithmetic over a small set, the same shape of problem as a smooth
objective sampled at a lattice of achievable points — so the annealing
machinery elsewhere in precis is reused, not the preferred-number well
shape (there is no continuum to nudge here).

### 23. Editing model

#### 23.1 Regenerate, never patch

The highest authored layer is the only thing edited; every layer below is
rebuilt from scratch on each change, so atoms are a pure function of the
authored state and there is no second representation to keep in sync.
Realisation **never amends** authored state; its only channel upward is
diagnostic — the `Report`. Two requirements: determinism (§4), and
**surface-space addressing**: a handle is a point on a patch (`handle`
line, §21) plus a frame; *nearest atom to here* is resolved at realisation
time, never stored. Handles live in smooth space, so they survive edits by
construction.

Naming a doomed atom (*centre the tube on this atom*, where the atom is
about to be removed) is fine provided the reference resolves against the
state **before** the edit: convert *centred on atom X* to a surface point
plus local frame immediately, then let the atom vanish. Relative moves
(*left, up*) act in that frame. Click-to-branch is the same: resolve the
click to a surface point and frame, insert a three-rim junction there,
grow a leg with the requested chirality or a default; the leg is then a
part like any other.

#### 23.2 Atom-level ops — the escape hatch, reserved now

Working from the manifold is the rule. hexfold already has atom-addressed
authored statements (`bond`, holes, `terminate`, `port`) on stable path IDs
(§9), so the atom-level escape hatch costs nothing to keep open: **atom
ops are authored statements replayed after generation, never in-place
edits of the generated section.** Atoms = ops(discretise(smooth)) stays a
pure function. An op whose target no longer exists after regeneration is
`op.dangling` (ERROR), never silent.

### 24. Coordinate mapping and the se interchange

#### 24.1 Both directions, cheaply

Every realised atom **carries its surface coordinates** (`surface: {patch,
u, v}`, §18) alongside its Cartesian position — a lookup, not a
computation; store them at realisation time, never recover them later.
Space → surface is a closest-point query over a spatial index of the
realised mesh. This mapping is what lets the LLM point at things, lets
decoration be specified in surface terms, and lets the mechanical side name
a mounting spot without knowing about atoms.

#### 24.2 Relation to se (the solid / SDF kernel)

Different mathematical objects, deliberately not one kernel: se is solid
modelling (signed distance fields, volumes, booleans); the surface kernel
is a 2-manifold with a singular locus, defined variationally. They share
the layer above — parts, interfaces, handles, constraint propagation, the
MCP surface — and the same units and frame conventions (metres, se pose
semantics), so a surface overlays an se scene trivially.

- **Rim frame = se datum.** The rim frame (§21) is the one object both
  sides understand: on the surface side the patch's boundary condition, on
  the se side a mounting interface with a contract like any joint.
  Anchoring is *this rim frame coincides with that datum on that block*.
  It is the datum concept of the se datum/measure work, not a second one.
- **Surface → solid** is easy and live: realise, thicken by a van der
  Waals radius, hand se an envelope plus named interfaces; recomputable on
  every change. **Solid → surface** is limited by construction: only rim
  frames and poses cross back, as boundary conditions; a general solid has
  no surface description.
- **Storage:** a surface design is a **`surface` binding kind** on an se
  block alongside `cad | structure | component | part` — not a new precis
  kind. The realised atoms bind as `structure` at the fidelity tier they
  were produced at.
- **Packages:** `src/precis_surface/` is the lattice-agnostic kernel
  (patches, rims, seams, curvature bound in caller units, direction fields
  with prescribed singularities, embedding checks, mesh realisation) —
  useful for any surface-shaped thing, not only carbon. `hexfold.smooth`
  is the carbon binding: the hex lattice's singularity charges, σ, the
  discretiser that emits the discrete section from a solved surface, and
  the `smooth:` parser/emitter. Dependency direction: `precis_surface →
  hexfold`; **`hexfold` never imports `precis*`** (enforced by an
  import-walk test; also the MIT licence boundary, §30).

### 25. The MCP surface

#### 25.1 Tiers per call

Neither "always to relaxed atoms" nor "author then dump":

- **Always:** symbolic propagation, discretise, `check` (and the cached
  smooth solve when a `smooth:` section exists). Sub-second; this is where
  feasibility lives — budget, `cut.overlap`, `port.mismatch`,
  `registry.closure`, `smooth.*`. Target 50–200 ms per interaction with
  cached smooth solves.
- **On request or below 5000 atoms:** `stick` and the `geom.*` findings.
- **Background job:** physics tiers; the generated section is updated on
  completion.

`generate` takes `fidelity` in place of 0.1's `dry_run` (`dry_run` ≡
`fidelity='check'`).

#### 25.2 Division of labour

The LLM states an underdetermined wish (*at this handle, a stick pointing
generally that way*); the system enumerates what is realisable nearby —
direction plus location narrow the axis, local sheet geometry constrains
which rims attach, the integer budget kills most of the rest — and returns
a handful of valid options, somewhat sorted, bounded. The wish may carry a
median, a lower or upper limit, a finished spec, or an explicit don't-care.
The model **queries the catalogue rather than recalling it** (§26). The
question kinds: creation, resolution (*which of the three atoms in that
ring?*), feasibility (*the budget doesn't close — grow the cap, shift the
seam, or accept strain?*), inspection. Feasibility is where the solver's
integer refusal becomes a design conversation.

#### 25.3 Verbs against se's existing surface

| §12.5 verb | lands as |
|---|---|
| `realise(scope, fidelity)` | existing `generate` op, `generator='hexfold'`, `fidelity` param; `violations` = the Report in the echo |
| `place_seam`, `attach_chain`, `grow_leg`, `terminate` | lines in the `.hx` authored text — the notation *is* the LLM-facing language; the skill carries the grammar |
| `add_port`, `connect`, `set_binding` | existing se ops; rims are emitted as ports by the generator |
| `inspect(handle)` | `view='block'` plus the check report |
| `check_registry` | the `registry.closure` finding, returned by every check |
| `resolve(click \| atom \| point)` | the se cross-scale pick → hierarchical reference work (`se-pick-hierarchy`); not duplicated here |
| `nearest_atom`, `surface_coords` | **new** `view='surface'` (small) |
| `catalogue(kind, filters)` | **new** `view='catalogue'` over the cache (small) |
| `move(handle, dir, dist)` | **new** op `move_handle`, relative moves in the rim frame (small) |
| `options(handle, wish)` | **new handler** — the one genuinely new verb: `fit.alternatives` surfaced as ranked candidates |

### 26. Catalogue and cache

**Generate on demand; no precomputed catalogue.** A catalogue written in
advance is guesswork; the cache warms itself in the first sessions, stays
honest because every entry came from the generator that would produce it
fresh, and its entries make better test fixtures than a guessed list. One
retained affordance: a force-populate flag for benchmark runs.

- **Keys:** the content hash (§14) of the canonical authored sections plus
  generator version and fidelity tier — the tuple that determines the
  geometry, never a name. Canonicalise before hashing (`0 ≤ m ≤ n` already
  folds `(n,m)/(m,n)`). Human-readable names are aliases to hashes.
- **Store:** a two-method protocol, `CatalogueStore.get(key)` /
  `.put(key, entry)`. **DB first**: table `hexfold_cache(key,
  format_version, generator, fidelity, authored_json, generated_json,
  created_at)`, one migration; a minted se block's `topology` references
  the key rather than duplicating the generated block; entries persist
  across builds. A file backend under `hexfold/catalogue/` arrives with the
  pip re-export behind the same protocol, and shipping precomputed entries
  is then a dump of the table.
- **Adapters** between two rims are largely deterministic (a discrete
  curvature problem, so the generator enumerates candidates); the
  non-deterministic part is *which* candidate, which is ranking, not
  construction. `[open]` — not fully verified.

The flange trick is a catalogue-free example of the model's uniformity: a
closed seam ring with three sheets where two close as a lens and the third
is terminated immediately — a structurally sealed lens with a continuous
chemical belt for decoration. Not a new primitive; a three-rim seam with
one branch terminated. Decoration itself rides on the rim: the rim object
carries termination chemistry alongside its indices, so the interface
constraint that matches geometry also checks chemical compatibility.

### 27. Self-intersection

Cheapest to most rigorous: broad-phase BVH over patches (very fast, gross
intersections); continuous collision detection during relaxation
(moderate; catches tunnelling that discrete checks miss); tangent-point
repulsive energy [S25] (expensive; an infinite barrier that never produces
a bad state); checking the smooth surface pre-discretisation (cheap; an
embedded minimal surface's close approximation usually is too).
**Decision: cheap checks in the search loop; repulsive energy only on
finalists.** Repulsive surfaces are usable at tens of thousands of vertices
but the fractional Sobolev inner product is dense and needs a
preconditioner, which justifies keeping it out of the inner loop.
**Implement from the published papers [S25, S26], not from the reference
source, and cite the work** — algorithms are free to reimplement, and
papers-only keeps provenance simple for a project that may become
patent-adjacent (not legal advice). High-genus objects — reticulum-like
walls with channels — are the same rim algebra with a bigger budget; the
hard part is precisely this global embedding constraint, which the local
algebra cannot see.

---

## Part IV — Roadmap, open questions, decisions, sources

### 28. Roadmap and build order

Each step is a spec that must `check` clean and round-trip through the
precis generator on the dev DB. Small scale first.

1. **Fold-in** (this repo): `src/hexfold/` + `tests/hexfold/` + root
   `hexfold/` export seed; cherry-pick the `feat/hexfold-integration`
   generator and skill; import-boundary test; MIT marked.
2. **0.2 discrete work**: `port`/`rim` vocabulary and `seam.rings`;
   `seam` k ≥ 3 with per-sheet χ (§6.3, §11.3), acceptance = the
   sheet-pill-bump closed seam; `registry.closure`; `fit` families and
   `fit.alternatives`; sectioned file with the generated block and
   `gen.stale`; `op.dangling`; sp³ ideal angle in `geom.angle.dev`.
3. hexgen roadmap, in order: **sheet + light bud, capped (5,5) + bud**
   (finish the dev-DB dogfood); **`cap(n,m)` flat-lid family** (six
   pentagons in a ring; the box lid; unblocks the pill); **`opening(port=)`**
   (solve a host hole from the target rim; a C5 rim on the C6 lattice meets
   only through an asymmetric seam → the **tilted pill**, `geom.join.angle`);
   **`junction(3)`** = opening + fuse (tee); **elbow + closure → genus-1
   torus** (one 5-7 pair per elbow; the first real `registry.closure`
   test; ~C240 from (5,5)/(6,6), N = 5–6 elbows); **genus-N torus**
   (2(N−1) junctions + 3(N−1) tubes); **`junction(k)`** (k = 4 tetrahedral
   D-node, k = 6 octahedral P-node — Mackay–Terrones C216 [S27] is one such
   node per cell); **periodic cell** (translation-tagged fuses, χ on the
   quotient; P = pcu, D = dia, G = srs nets, schwarzites as tubes along a
   periodic skeletal net + junctions [S27, S28, S29]); **box test piece**
   (~4 nm pillbox: (18,0) liner, (5,5) axle, capped crossbars, ~5k atoms;
   axle ⇄ liner as separate blocks with a revolute joint).
4. **`precis_surface` stage 1**: symbolic chain solver with a **stub
   geometry backend** — every part reports rim indices and a rough length;
   the whole chain solves. This alone proves the interface claim, before
   the chemistry is good.
5. Straight tubes; symmetric collars; caps from the cache; then the
   **discrete-mesh smooth solve** (§20.6), the curvature bound, seams as
   film clusters.
6. **Direction field** (§20.5) and the bent collar (§22.2).
7. **sp³ seam line and seam vertices** — backlog item `hexfold-sp3-seam`,
   blocked by 2; can join three or four sheets at an atom.

Out of scope until the above holds: general Goldberg fullerenes, surface
tiling from a target SDF (the 30 nm oval box), rdkit realisation of
`frag`, a ring-port column in `se_ports`. Each swap above is local; the
chain solver never changes.

### 29. Open questions `[open]`

| # | question | tradeoff | severity |
|---|---|---|---|
| 1 | **Bent-collar twist.** Comparing lattice frames along two paths on a curved lattice differs by the enclosed defect charge (60° quanta), so twist is path-dependent by an integer — well defined once measured along one fixed loop, the rim. Price: two collars with equal rim twist can differ in interior arrangement — the placement freedom already split from the budget. | Expected to resolve as *yes, on the rim loop*; verify by building. | high — verify early |
| 2 | **Field ≡ ring placement.** Half is a theorem (indices sum to χ [S6]); the other half — positions — is hex-remeshing integrability [S20, S21], which treats bond length as a soft target we must then snap. | Adopting the field buys the commensurability check before discretising and commits to a remeshing-style step. | high — verify before building on it |
| 3 | **Bent-collar search.** Placing k charges on a loop of N sites with disjoint disks is ~C(N,k) with cheap integer pruning — milliseconds at collar scale even exhaustive; wide asymmetric seams are the risk. | Greedy fit against the smooth target with local repair; exhaustive under a size cap; `fit.unsolvable` above. Instrument first. | high |
| 4 | **Embedding at high genus.** Mitigated by §27, not closed; channels collapsing during physics relaxation are caught only by the expensive tier. | CCD cost vs missed tunnelling. | medium |
| 5 | **`strain_max` default.** Mechanism decided (§15.2); the number is a literature lookup, per-design override. | — | medium — lookup |
| 6 | **Seam census vs literature.** The sp² octagon census (§6.2) is derived here; check against the carbon honeycomb sp² junction [S2]. | — | medium — build |
| 7 | CoNTub licensing, if source were reused. Moot under §27's papers-only rule. | — | low |

Closed: addressing across edits (handles in smooth space, §23); scale
selection (a constraint, §20.4); embedding detection method (§27);
per-sheet χ with seams invisible to each sheet's law (§6.3); bent seams in
scope, seam vertices out (§6.4); registry closure as integer arithmetic
(§12.2); fit cost ordering (§12.1); atom-level ops as replayed statements
(§23.2); numerical method (§20.6); cache backend (§26).

### 30. Decisions log (merged; do not re-ask)

- One canonical representation — the seam — plus a skill layer; no second
  parallel framing. Everything is a rim-bounded patch; a seam is where rims
  meet; a fuse is a seam of multiplicity two.
- Seams (k ≥ 3) are first-class, sp² first; per-sheet χ; seam faces in no
  census; seam vertices and sp³ deferred to the sp³ item.
- Work in smooth space when authored there; discretise last; truth is
  ordered smooth > discrete > generated; regenerate never patch; realisation
  reports, never amends.
- Curvature bound is a solver constraint (bending + spacing), default
  1/(2σ), per-design override; smooth section and `precis_surface` in
  metres; discrete/generated atoms in Å (precis atomistic enclave).
- Registry travels as fuse phases (discrete) and as a direction field
  (smooth); `registry.redundant` scoped to trees; `registry.closure` on
  cycles.
- The LLM composes constraints, never geometry; the notation is the
  LLM-facing language; `options` is the one new verb.
- Generate on demand and cache; DB first behind a two-method protocol;
  cache = library = test fixtures.
- Budget separate from placement; generators return families with costs.
- Discrete mesh is the smooth engine; closed forms are seeds and oracles.
- Adopt existing topology vocabulary; implement from papers; cite prior
  art with DOI.
- Coordinates never in a discrete statement; `stick` is a preview seed;
  precis relaxes.
- One connected net → one `structure` / one se block; bond-mode fringe may
  be its own block; non-bonded placement (axle in liner) is se's job.
- Neck for nanobuds = short `(5,0)`/`(6,0)` tube fused both ends, not a
  cone; 9-6 / 8-7 are bond-mode C54 attachments, not fuse phases.
- `Port.b` vs `Port.b_expected`; `euler.residual` uses `B_expected`.
- Angles are outputs: tilt = asymmetric seam, twist = phase `k`; join angle
  ψ reported, not solvable (deferred).
- Citation key for the viewer is `se_blocks.uid`; labels are display only.
- Counting is a diagnostic, not a gate; lattice-path atom IDs with
  sublattice; `bond`/`fuse`/`seam` as the attachment verbs, menus as
  macros; sextant integer orientations; canonicalisation by lex-min over
  ≤ 6·D frames, mirrors excluded; author-declared origin.
- Monorepo for now: `src/hexfold/` MIT-marked, imports no `precis*`;
  `precis_surface` is a precis package; re-export later is packaging, not
  refactoring.

### 31. Sources

`pa…` are precis store ids (for us); the DOI is for readers. Every DOI
below was verified against Crossref on 2026-09-16, and every paper with a
DOI is in the store (imported the same day; books carry ISBNs only).

- **[S1]** J. E. Taylor, "The structure of singularities in soap-bubble-like and soap-film-like minimal surfaces," *Annals of Mathematics* 103 (1976) 489–539. doi:10.2307/1970949. **`pa343396`**
- **[S2]** N. V. Krainyukova, E. N. Zubarev, "Carbon honeycomb high capacity storage for gaseous and liquid species," *Phys. Rev. Lett.* 116 (2016) 055501. doi:10.1103/PhysRevLett.116.055501. **`pa343395`** (cited in store paper `pa159855`).
- **[S3]** B. Farb, D. Margalit, *A Primer on Mapping Class Groups*, Princeton University Press (2012), ch. 8 (pants decompositions). ISBN 978-0-691-14794-9.
- **[S4]** S. Melchor, J. A. Dobado, "CoNTub: an algorithm for connecting two arbitrary carbon nanotubes," *J. Chem. Inf. Comput. Sci.* 44 (2004) 1639–1646. doi:10.1021/ci049857w. **`pa343397`** (cited in `pa910`).
- **[S5]** S. Melchor, F. J. Martin-Martinez, J. A. Dobado, "CoNTub v2.0 — algorithms for constructing C3-symmetric models of three-nanotube junctions," *J. Chem. Inf. Model.* 51 (2011). doi:10.1021/ci200056p. **`pa910`**.
- **[S6]** J. W. Milnor, *Topology from the Differentiable Viewpoint*, Princeton University Press (1965/1997), §6 (Poincaré–Hopf). ISBN 0-691-04833-9.
- **[S7]** L. A. Chernozatonskii, S. V. Lisenkov, "Classification of three terminal nanotube junctions," *Fullerenes, Nanotubes, Carbon Nanostruct.* 12 (2004) 105–109 (Crossref issued date 2005). doi:10.1081/fst-120027141. **`pa343398`** (cited in `pa910`).
- **[S8]** H. A. Schwarz, *Gesammelte Mathematische Abhandlungen*, Springer (1890), vol. 1 (minimal surfaces spanning skew quadrilaterals; the reflection principle).
- **[S9]** R. Saito, G. Dresselhaus, M. S. Dresselhaus, *Physical Properties of Carbon Nanotubes*, Imperial College Press (1998), ch. 3 (chiral vector, diameter). ISBN 1-86094-093-5.
- **[S10]** A. Krishnan, E. Dujardin, M. M. J. Treacy, J. Hugdahl, S. Lynum, T. W. Ebbesen, "Graphitic cones and the nucleation of curved carbon surfaces," *Nature* 388 (1997) 451–454. doi:10.1038/41284. **`pa207748`**
- **[S11]** K. Hedberg, L. Hedberg, D. S. Bethune, C. A. Brown, H. C. Dorn, R. D. Johnson, M. de Vries, "Bond lengths in free molecules of buckminsterfullerene, C60, from gas-phase electron diffraction," *Science* 254 (1991) 410–412. doi:10.1126/science.254.5030.410. **`pa343399`**
- **[S12]** H. W. Kroto, "The stability of the fullerenes Cn, with n = 24, 28, 32, 36, 50, 60 and 70," *Nature* 329 (1987) 529–531 (isolated-pentagon rule). doi:10.1038/329529a0. **`pa343400`**
- **[S13]** A. G. Nasibulin et al., "A novel hybrid carbon material," *Nature Nanotechnology* 2 (2007) 156–161. doi:10.1038/nnano.2007.37. **`pa2069`**.
- **[S14]** X. Zhu, H. Su, "Magnetism in hybrid carbon nanostructures: nanobuds," *Phys. Rev. B* 79 (2009) 165401. doi:10.1103/PhysRevB.79.165401. **`pa543`**. Defines the 9-6 and 8-7 cases and the formation energy `E(nanobud) − E(C54) − E(CNT)`. (Earlier hexfold docs and the branch skill misattribute this to "Wang & Li 2009", a name from a Perplexity survey; there is no such primary.)
- **[S15]** D. Baowan, B. J. Cox, J. M. Hill, "Discrete and continuous approximations for nanobuds," *Fullerenes, Nanotubes and Carbon Nanostructures* 18 (2010). doi:10.1080/15363830903586625. **`pa679`**. The DA/DB neck models.
- **[S16]** J. Plateau, *Statique expérimentale et théorique des liquides soumis aux seules forces moléculaires*, Gauthier-Villars (1873).
- **[S17]** M. Meyer, M. Desbrun, P. Schröder, A. H. Barr, "Discrete differential-geometry operators for triangulated 2-manifolds," in *Visualization and Mathematics III*, Springer (2003) 35–57. doi:10.1007/978-3-662-05105-4_2. **`pa343401`**
- **[S18]** X. Zhao, Y. Liu, S. Inoue, T. Suzuki, R. O. Jones, Y. Ando, "Smallest carbon nanotube is 3 Å in diameter," *Phys. Rev. Lett.* 92 (2004) 125502. doi:10.1103/PhysRevLett.92.125502. **`pa399`**
- **[S19]** N. Ray, B. Vallet, W. C. Li, B. Lévy, "N-symmetry direction field design," *ACM Trans. Graph.* 27 (2008) 10. doi:10.1145/1356682.1356683. **`pa343402`**
- **[S20]** F. Knöppel, K. Crane, U. Pinkall, P. Schröder, "Globally optimal direction fields," *ACM Trans. Graph.* 32 (2013) 59. doi:10.1145/2461912.2462005. **`pa343403`**
- **[S21]** D. Bommes, H. Zimmer, L. Kobbelt, "Mixed-integer quadrangulation," *ACM Trans. Graph.* 28 (2009) 77. doi:10.1145/1531326.1531383. **`pa343412`**. Also K. Crane, M. Desbrun, P. Schröder, "Trivial connections on discrete surfaces," *Computer Graphics Forum* 29 (2010) 1525–1533. doi:10.1111/j.1467-8659.2010.01761.x. **`pa343404`**
- **[S22]** R. Osserman, *A Survey of Minimal Surfaces*, Dover (1986), §8 (Weierstrass–Enneper representation). ISBN 0-486-64998-9.
- **[S23]** U. Pinkall, K. Polthier, "Computing discrete minimal surfaces and their conjugates," *Experimental Mathematics* 2 (1993) 15–36. doi:10.1080/10586458.1993.10504266. **`pa343405`**
- **[S24]** K. A. Brakke, "The Surface Evolver," *Experimental Mathematics* 1 (1992) 141–165. doi:10.1080/10586458.1992.10504253. **`pa343406`**
- **[S25]** C. Yu, C. Brakensiek, H. Schumacher, K. Crane, "Repulsive surfaces," *ACM Trans. Graph.* 40 (2021) 268. doi:10.1145/3478513.3480521. **`pa343407`**
- **[S26]** C. Yu, H. Schumacher, K. Crane, "Repulsive curves," *ACM Trans. Graph.* 40 (2021) 10. doi:10.1145/3439429. **`pa343408`**
- **[S27]** A. L. Mackay, H. Terrones, "Diamond from graphite," *Nature* 352 (1991) 762. doi:10.1038/352762a0. **`pa343409`** (cited in `pa181896`).
- **[S28]** T. Lenosky, X. Gonze, M. Teter, V. Elser, "Energetics of negatively curved graphitic carbon," *Nature* 355 (1992) 333–335. doi:10.1038/355333a0. **`pa343410`**
- **[S29]** V. R. Coluci, D. S. Galvão, A. Jorio, "Geometric and electronic structure of carbon nanotube networks: 'super'-carbon nanotubes," *Nanotechnology* 17 (2006) 617–621. doi:10.1088/0957-4484/17/3/001. **`pa343411`**. Related: H. Terrones, M. Terrones, "Curved nanostructured materials," *New J. Phys.* 5 (2003) 126. doi:10.1088/1367-2630/5/1/126; D. C. Miller, M. Terrones, H. Terrones, "Mechanical properties of hypothetical graphene foams: giant schwarzites," *Carbon* 96 (2016) 1191–1199. doi:10.1016/j.carbon.2015.10.040 (both cited in `pa181896`).

Source needed: the term **"bond surplus"** (charge e − 6 per ring) as a
named formulation. The counting itself is cited to Euler and [S4, S5];
the term's origin is unknown to us and is not claimed.
