# 0043 — The `structure` kind: an atomistic cell + bond-graph IR the LLM can *read*

- **Status**: accepted (2026-06-28) · **v1 implemented** (`structure` kind live — `src/precis/handlers/structure.py`; the materials sibling of
  [ADR 0041](./0041-cad-kind-analytic-ir.md) and
  [ADR 0042](./0042-pcb-kind-netlist-placement-ir.md); same philosophy — own
  a legible IR, rent the heavy kernel only at export — applied to a **general
  atomistic structure**: a cell + atoms + a bond graph the LLM reads as
  *structure*, not pixels. DFT is the headline consumer; molecules
  (`pbc=[F,F,F]`) and other backends fall out of the same IR.)
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0041 — The `cad` kind: analytic-IR solids](./0041-cad-kind-analytic-ir.md)
    — the **keystone source**: give the LLM *eyes* into 3D without pixels, via
    a graph it queries with a **probe ladder** + **persisted observers**. 0043
    retargets "material vs void along a ray" to "atoms / bonds / coordination
    over a periodic cell."
  - [ADR 0042 — The `pcb` kind](./0042-pcb-kind-netlist-placement-ir.md) — the
    **direct structural twin**: a netlist is a graph stored in **dedicated
    relational tables + one card chunk**, with a **measure** family carrying
    graded design intent and a `fixed` mark for what the optimizer may not
    move. 0043 lands on the *same* storage shape and the *same* measure model;
    a bond graph is as relational as a netlist.
  - [ADR 0033 — Drafts as editable chunk-native documents](./0033-draft-chunks-editable-document.md)
    — only the **soft-delete *semantics*** (a `deleted_at` column on the
    `struct_*` tables); the graph is **not** chunk-native (§4, as 0042 decided).
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) — the design
    ref's 2-char code is **`st`** (STructure; verified free in
    `handle_registry.py`, register at implementation).
  - **The `precis-dft` spec** (`~/.claude/plans/we-have-a-cluster-hidden-bird.md`)
    — leaned on **lightly**: this ADR is *only* the legible atom-organizer IR
    and its feedback surface. The campaign coordinator, the physics-defaults
    schema, the failure-recovery ladder, ML-uncertainty, embedding spaces, and
    the reaction/Pourbaix/volcano analyses all stay in that spec. The one hard
    overlap — the relaxer — is resolved here (§9): **we rent it.**

## Context

We want a way to **arrange atoms for a DFT model**: define a periodic cell,
fill it with atoms and bonds, relax, inspect, repeat. The hard question is the
one 0041/0042 already answered for solids and circuits — **how to give an LLM
*eyes* into a 3D atomic structure without making it stare at pixels.**

The new insight that supersedes the `precis-dft` spec's *spatial-reasoning*
section (its `view:ascii_top` / `view:ascii_side` grids): **visual cognition is
not an LLM strength.** A top-down ASCII render of a slab is a savanna-ape
projection — it asks the model to do the one thing it is worst at (reconstruct
3D from a flattened raster) and spends tokens doing it badly. **Graph
manipulation, with good feedback, is what the LLM is good at.** A crystal is
*already* a graph: atoms are nodes, bonds (with a periodic-image label) are
edges, symmetry collapses many atoms into a few orbits. So the precis-shaped
answer is the same as for papers, solids, and boards: **structure the model can
query** — a symmetry-reduced TOC, targeted atom/bond lookups, and *probes* that
report coordination, distances, strain, and rule violations **as numbers** —
never a rendered crystal, never a live DFT run, to *inspect*. The heavy
machinery (relaxation, SCF, properties) happens only at **compute/export**.

### One wording correction up front — how a cell actually tiles

The request described "the cell is reflected/tiled infinitely … mirror xyz yes
/no, then tile or keep on mirroring." The industry convention is narrower and
cleaner, and getting it right is load-bearing:

- The cell is a **unit cell**, defined by three **lattice vectors `a, b, c`**
  (a 3×3 matrix; equivalently lengths `a,b,c` + angles `α,β,γ`). It may be
  triclinic — no orthogonality assumed.
- A crystal tiles by **periodic boundary conditions (PBC)** — **pure
  translation** by integer combinations of the lattice vectors. **There is no
  "mirror-tile" mode.** Reflection and glide are *space-group symmetry
  operations internal to one cell*, never the repeat rule. Translation is the
  tiling; mirror is not.
- Periodicity is **per-axis** (`pbc=[bool,bool,bool]`, the ASE convention):
  - **bulk** `[T,T,T]`;
  - **slab** `[T,T,F]` — periodic in the surface plane, with a **vacuum gap**
    (≥15 Å, §3) along the non-periodic axis so the surface does not see its own
    image;
  - **wire** `[T,F,F]`, **molecule** `[F,F,F]`.
- What the request *probably* wants from "mirror" is one of two **one-shot
  build ops**, not a tiling mode: (a) a **symmetric slab** — a mirror plane at
  the slab mid-plane so both surfaces are identical (cancels the dipole, lets
  you freeze the centre); or (b) reduction to the **asymmetric unit** for
  display. Both are ops (`symmetrize`, §6), applied once. Likewise a
  **supercell** (`repeats=[n,m,k]`) is an op that *bakes* `n×m×k` translations
  into a bigger explicit cell — distinct from the infinite PBC tiling.

So: **tiling primitive = translation (PBC flags + lattice). Mirror/supercell =
ops.** This is the standard CIF/POSCAR/ASE model and what every DFT engine
expects.

### This is a general atomistic builder; the repeating box is just one mode

The periodic cell is *handy for DFT*, but nothing here is DFT-specific — the
kind is named `structure`, not `dft`, on purpose. **Turn PBC off (`[F,F,F]`)
and a big-enough box, and the exact same IR is a general molecule / cluster
builder**: atoms + a bond graph + measures, with no tiling at all. The bond
graph that is a *scaffold* for a crystal is the *whole point* for a molecule
(organic chemistry, ligands, adsorbate fragments are graph-native), and the
build ops degrade gracefully — `add_chain`/`attach` build a molecule by valence
exactly as they decorate a surface; `bonds_in_sphere` reads a functional group;
`fill` is just unused. So **DFT is one consumer, not the framework**:

- the **legible IR + probes + ops are core** (pure, numpy-only);
- **DFT (GPAW) is one backend**; an **ML / molecular-mechanics relaxer** is
  another; **cheminformatics** (RDKit: SMILES/InChI canonicalisation, valence
  models) is a third — all plugins behind the same `struct_*` tables.

The molecule case even gets *better* file round-trips than the crystal case:
**MOL/SDF and PDB carry bonds + orders natively** (§13), so the bond graph —
which CIF/POSCAR must drop — survives losslessly. We design the IR to serve both
from day one; the periodic machinery (MIC, image offsets, symmetry orbits)
simply no-ops when `pbc` is all-false.

**SMILES is not only an I/O format.** The *string* is I/O + a canonical identity
(dedup / search key, §13). But the molecular graph it encodes unlocks **SMARTS
substructure matching** as a first-class **query/navigation primitive**:
`find(smarts='C(=O)[OH]')` selects every carboxyl, `focus(on='[c]1ccccc1')`
embodies a benzene ring (§6.6). So SMILES = I/O + identity; **SMARTS = a graph
query** — the cheminformatics-plugin lens on molecule mode, and a genuine reason
the format earns its keep beyond import/export.

## Decision

### 1. Keystone — own the cell + atoms + bond-graph IR; rent the relaxer and the DFT engine

The IR — **a periodic cell** (lattice + per-axis PBC) **filled with atoms**
(element, position, constraint, magmom, hybridization) **and an explicit bond
graph** (edges with bond order and a periodic-image offset) — is the source of
truth and the thing the LLM authors, reads, and probes. **The relaxer (ASE +
an ML potential), the DFT engine (GPAW), and the file writers (CIF/POSCAR/…)
are backends**, not the evaluator and not the store. What we own and the LLM
sees: *which atoms, bonded how, where, with what coordination and strain.* What
we rent: *turning the arrangement into a relaxed geometry and into energies.*

The cost we accept: a small **PBC-aware graph + geometry evaluator** of our own
(coordination, neighbour shells, distances/angles under the minimum-image
convention, strain). The discipline that bounds it: **report only what we can
compute exactly from positions + cell + graph, or label as an estimate** — the
same contract-as-exclusion-line move 0041/0042 use.

### 2. Two layers — the graph (intent) vs the geometry (evaluated)

Mirroring 0041's split of the analytic graph from the evaluated solid, and
0042's logical/physical split:

- **Graph layer (intent, the LLM authors it):** atoms + bonds + declared
  hybridization/bond-order + constraints + cell. This is what the structure
  *is* — it round-trips, it is what the LLM edits, and it is the LLM's strong
  suit (a labelled graph).
- **Geometry layer (evaluated, derived):** relaxed Cartesian/fractional
  positions, per-atom force, per-atom local strain/stress, **derived
  coordination** (who is actually within bonding distance after relax),
  symmetry/Wyckoff orbits, special sites. Re-derived after every edit and after
  every relax; never authored directly.

The separation is load-bearing: the LLM gets the **connectivity** right (a
graph edit) before the **geometry** is good (a relax), and the prober can flag
when the relaxed geometry *contradicts* the declared graph (a declared sp²
carbon that relaxed to four neighbours → a warning, §8).

### 3. Invariants

- **Units: Å for length, eV for energy, e for charge, μ_B for moment** — the
  DFT/ASE/VASP lingua franca. Lattice and positions in Å; fractional
  coordinates (0–1 within the cell) are the *default reading* form (§11),
  Cartesian available.
- **Positions are stored fractional, evaluated either way.** Fractional
  survives a lattice edit (strain, supercell) without rescaling every atom, and
  reads more legibly ("¼ of the way along `c`").
- **Place-outside-wraps-inside (the request's "place outside the box").** Any
  position the LLM gives outside the cell — fractional outside `[0,1)` or
  Cartesian beyond a lattice vector — is **wrapped modulo the lattice** into the
  canonical cell, and the wrap that was applied is **recorded as the bond's
  image offset** (§4.1). So you may "place a partner outside the box" at its
  natural location; it shows up inside, in the right place, and the bond
  auto-carries the correct `to_jimage`. This is the ergonomic way to build
  across a wall — you never compute an offset by hand.
- **PBC + minimum-image convention (MIC)** govern every distance, angle, and
  neighbour query: the distance between two atoms is to the *nearest periodic
  image*. This is the load-bearing tunable's analog — get MIC wrong and every
  bond length is wrong near a face.
- **Charge: declared is intent, computed is feedback** (the bonds split, §8,
  applied to charge). The LLM may declare a per-atom **oxidation state**
  (`Pd⁰`, `O²⁻`) and a **net cell charge** (for a charged-cell or
  electrochemical run) — *intent*, used for valence sanity, NELECT, and
  magmom/occupation seeding. The **partial charge in the current state**
  (the request's "charges of each atom") is **derived** from the DFT density
  (Bader by default; Hirshfeld/Mulliken/DDEC6 opt-in) and reported per atom
  like force and strain. Net charge defaults to 0 (neutral cell + compensating
  background when nonzero, the spec's `fixed_charge` occupation).
- **Slab conventions** (when the annotator detects a slab): vacuum ≥ 15 Å,
  ≥ 4 layers, `+z` surface normal by default — warned, not enforced (cited from
  the `precis-dft` spec; this ADR only *surfaces* the warnings as probes).
- **Elements: any** (Z = 1…118 via ASE); v1 *validates* against the spec's
  initial set (Pd, Cu, C, H) only for the **relaxer** (which ML potential
  covers them), not for the IR — the IR holds any element from day one.

### 4. The node model — relational tables, *not* chunks (converging with 0041 Amdt 1 / 0042 §4)

Same call as 0042, for the same reasons: **nobody semantic-searches for
`aPd123`**, so the graph earns no embedding; and a bond graph is data we
**query with joins under FK integrity** (a dangling bond endpoint is a *bug* an
FK prevents for free), not JSONB we re-parse on every probe. So the **only**
corpus artifact is the design itself — a `refs` row (`kind='structure'`)
carrying **exactly one embedded `card_combined` chunk** ("Pd(111) 3×3×4 slab,
36 atoms, OH adsorbate on an fcc hollow") — so the *structure* is findable by
intent and gets an `st` handle, soft-delete, and links (`derived-from` a paper,
`produced-by` a todo, `relaxed-by` a job). Everything below is **rows**,
soft-deletable via `deleted_at` (the 0033 *semantics* without the chunk
*mechanism*).

The node concepts:

- **`cell`** — the design ref's `meta`: the **3×3 lattice matrix**, the
  **per-axis `pbc`** triple, the detected **system type** (bulk/slab/wire/mol),
  vacuum gap, **net charge** (default 0), and the **space group + Wyckoff**
  (derived by spglib, cached).
- **`atom`** — one `struct_atoms` row: an **id `aPd123`** (`a<El><n>`, per-design
  unique, stable across edits — like a pcb refdes), **element**, **fractional
  position**, a **`fixed` constraint** (none / `fixed-xy` / `fixed-z` /
  `fixed-all`, used *sparingly* — typically the bottom slab layers), a
  **magmom seed**, a **declared hybridization** (`sp3`/`sp2`/`sp`/`metal`/
  `none`) and an optional **oxidation state** the LLM sets as intent, plus the
  **derived partial charge + force** written back by the relaxer/single-point
  (§8).
- **`bond`** — an edge in the graph, **possibly N-ary.** The common case is a
  **pairwise** bond — one `struct_bonds` row, `(atom_i, atom_j)`, a **bond
  order** (1 / 1.5 / 2 / 3 / aromatic / metallic), and the crucial
  **periodic-image offset `[nx,ny,nz]`** (§4.1). But a **delocalized /
  coordination bond is a hyperedge over *many* atoms** — an aromatic ring (6 C),
  an η5-Cp (5 C), a 3-center-2-electron bond (3), a metal-π interaction — and is
  stored as a `struct_bonds` row + a **`struct_bond_atoms` membership table**
  (one row per atom-in-bond, the exact pcb-net hyperedge pattern, §12), with a
  `kind` (`aromatic`/`eta-n`/`3c2e`/…). A delocalized bond **"includes" all the
  atoms it spans** — which is precisely what makes it a *point of view* the
  cursor can embody (§6.6). A bond is the editable IR; its *strength/strain* is
  **derived** (§8), not authored. Every bond carries a **`provenance`** flag —
  `declared` (LLM intent) · `inferred` (auto-detected from geometry on ingest) ·
  `dft` (from a COHP/Bader single-point): the LLM **always sees the best image of
  reality we can muster** (bonds are auto-detected, never withheld), with the
  *inferred* ones **marked** rather than hidden (Open-Q 2). **`bond_order` is a
  single continuum** — vdW (≈0) → single → double → triple → aromatic → metallic
  — declared coarsely and refined to a specific value by DFT.
- **`measure`** — the 0042 measure family, retargeted: a **named metric over
  the graph + geometry, with a direction of goodness** (`min`/`max`/`target`/
  `keep-above`/`keep-below`), an optional **goal**, a **strength** (`hard` =
  a constraint the relaxer/DRC must hold · `soft` = a term a constrained
  optimizer pushes on · `gauge` = report-only tape), and a **reason**. It is
  **persisted and re-evaluated** — reports its value + verdict after every
  relax and "tries again." This *is* 0041's persisted observer / 0042's
  measure, generalized to atoms: a bond-length target, an adsorbate-height
  band, a min-separation between two species, a coordination-number target,
  a max-residual-force gauge (§8.3).
- **`site`** (derived, not authored) — named **special sites** for navigation:
  surface `top`/`bridge`/`fcc`/`hcp` hollows and `octahedral`/`tetrahedral`
  interstitials (from the spec's annotator), exposed as datums the ops and
  probes reference by name (`add_atom site=fcc_hollow_4`).

**Addressing** (the 0042 scheme): the **design** gets a `st…` handle. Sub-rows
are addressed by their **natural identity scoped to the design**: an atom is
`st7#aPd123`, a bond `st7#aPd123~aCu44[1,0,0]`, a site `st7@fcc_hollow_4`. The
atom id and bond pair *are* the identity a chemist uses; we deliberately do
**not** mint a per-row 0036 handle (0036 could resolve one to the tables later;
see Open questions).

#### 4.1 Bonds across the cell wall — the image offset, precisely

A bond can connect an atom to a **periodic image** of another atom (or of
itself). The edge carries an integer triple `[nx,ny,nz]` — the **lattice
translation of the cell containing the partner** (pymatgen calls this
`to_jimage`; it is the standard "labelled quotient graph" edge label for a
periodic net). `aPd123 ~ aCu44[1,0,0]` reads: *aPd123 bonds to the copy of
aCu44 one cell over in `+a`*. `[0,0,0]` is the in-cell bond.

This directly answers the request's corner question. The triple disambiguates
*exactly which* image, so an atom in a corner facing several walls is never
ambiguous:

- each **nonzero component = crossing that pair of opposing faces**; one
  nonzero = a **face** neighbour (`[1,0,0]`), two = an **edge** neighbour
  (`[0,1,1]`), three = a **corner** neighbour (`[1,1,1]`).
- **There is no artificial prohibition on the corner image.** The request's
  intuition ("the 000 cube can't talk to the 111 cube directly") is *almost*
  right but for the wrong reason: corner bonds are **rare, not forbidden** —
  the **minimum-image convention naturally selects the nearest image**, and the
  nearest image of a near-neighbour is almost always a face or edge image. A
  `[1,1,1]` bond is legal whenever a real atom actually sits within the bonding
  cutoff there; MIC just means it seldom does. So we keep the full integer
  triple (legal, exact, unambiguous) and let **default bond detection use MIC**
  (which yields face/edge bonds for typical lattices), with explicit offsets
  for anything the LLM declares by hand.
- **Self-image bonds** (`aPd123 ~ aPd123[1,0,0]`) are how a 1-atom-thick chain
  or a metallic wire expresses its periodic continuity — fully supported.

### 5. The ops — the LLM edits the graph, the framework re-derives

The agent emits typed ops via `edit(kind='structure', id=st, ops=[…])`
(0041/0042's op model; *not* coordinate-poking, *not* code-gen). The framework
validates against the cell, applies, schedules re-derivation (coordination,
symmetry, sites, measures), and returns the structure with `derived_pending`.
The request's "insert sheet/cube/line, relax, inspect, repeat" is the op
catalog:

```yaml
# cell / tiling
set_cell:      {a: 8.4, b: 8.4, c: 24.0, alpha: 90, beta: 90, gamma: 120, pbc: [T,T,F]}
supercell:     {repeats: [3,3,1]}                    # bake n×m×k translations
symmetrize:    {mode: 'slab_mirror'}                 # one-shot mirror build op (§wording)
strain:        {component: 'biaxial', magnitude: 0.02}

# atoms — single and bulk inserts (the "insert sheet/cube/line")
add_atom:      {element: 'O', site: 'fcc_hollow_4'}  # by named site (datum)
add_atom:      {element: 'H', frac: [0.33, 0.33, 0.55]}
add_atom:      {element: 'H', frac: [1.10, 0.33, 0.55]}   # outside [0,1) → wraps in (§3), bond auto-imaged
add_layer:     {element: 'Pd', plane: 'a-b', n: [3,3], stacking: 'fcc', z: 'top'}   # a sheet
add_block:     {element: 'Cu', repeats: [2,2,2], lattice: 'fcc', a: 3.61}           # a cube
add_chain:     {element: 'C', from: 'aC1', to: 'aC1[1,0,0]', n: 4, order: 'aromatic'}  # a line
fill:          {element: 'Pd', lattice: 'fcc', a: 3.89, region: 'fc<0.5'}  # flood-and-carve (§5b)
attach:        {to: 'aC1', element: 'H', geometry: 'from_hybridization'}   # grow-from-anchor (§5b)
set_element:   {atom: 'aPd14', element: 'Ni'}
vacancy:       {atom: 'aPd20'}
displace:      {atom: 'aH7', vector: [0,0,0.1]}

# bonds (the graph the LLM is good at)
add_bond:      {i: 'aC1', j: 'aC2', order: 2}
add_bond:      {i: 'aC1', j: 'aC4', order: 1, image: [1,0,0]}   # across the +a wall
remove_bond:   {bond: 'aC1~aC2'}
set_order:     {bond: 'aC1~aC2', order: 'aromatic'}

# constraints, charge, magnetism (used sparingly — declared intent)
constrain:     {layers: [3,4], kind: 'fixed-all'}    # freeze bottom slab layers
constrain:     {atoms: ['aPd1','aPd2'], kind: 'fixed-z'}
set_magmom:    {atom: 'aNi5', magmom: 2.0}
set_oxidation: {atom: 'aO3', state: -2}              # intent (valence sanity / NELECT seed)
set_net_charge:{value: -1}                           # cell-level; charged-cell DFT
```

Bulk inserts that fan out (`add_block`, `fill`, `supercell`) carry a **hard
`max_atoms`** cap (default 512, like 0041's `max_siblings`) so a runaway tiling
cannot explode the cell unbounded; **`log()` the cap** if it bites.

### 5b. Two ways to build — flood-and-carve vs grow-from-anchor

The request named both top-down and bottom-up construction; the op catalog
supports each, and they compose:

- **Flood-and-carve (top-down).** `set_cell` with a vacuum gap → `fill` the
  non-vacuum region with a lattice of an element ("half vacuum, flood the rest
  with Pd") → `relax` → then `vacancy` / `set_element` / `add_atom` to carve and
  decorate. `fill` takes a lattice (`fcc`/`bcc`/`hcp`/`diamond`) + spacing + a
  **region predicate** (`fc<0.5`, a sphere, a half-space) and snaps atoms onto
  the lattice inside it — the fast way to a bulk or slab starting point.
- **Grow-from-anchor (bottom-up, "from first principles").** The request's
  *"Pd has these orbitals, so given the first anchor, atoms must be at these
  locations and angles."* `attach {to: anchor, element, geometry}` places new
  atoms by **coordination geometry**, not coordinates: the framework reads the
  anchor's **hybridization → VSEPR template** for covalent atoms (sp³ →
  109.5° tetrahedral, sp² → 120° trigonal, sp → 180°) or its **lattice
  template** for metals (fcc → the 12 nearest-neighbour directions at the
  lattice spacing), generates the candidate positions at the correct bond
  length + angle, and the LLM picks or fills them. This is the genuinely
  LLM-shaped builder: **the model reasons about coordination and valence (a
  graph fact); the framework computes the Cartesian positions.** `attach`
  auto-bonds the new atom to its anchor (and auto-images across a wall, §3).

Both feed the same `struct_atoms`/`struct_bonds` tables; a structure can be flooded,
relaxed, then grown — there is no mode switch, only ops.

### 5c. The Edit contract — validator gate + result envelope

Every `edit` (the Edit category of the surface) passes a **validator gate before
it commits**, and returns a **structured envelope** (folding considerata §22-B/C
into the op contract, not just a note):

- **Gate (microseconds, before any state change or compute):** reject
  sub-covalent bond lengths, over-valence, hard-sphere atomic overlap, broken
  declared periodic bonds (the §6.4 DRC rules, run as a *gate* on the mutation).
  A rejection names the **rule + offending value + a `suggested_fix`** in the same
  op vocabulary ("Pd–H 0.6 Å < 1.85 Å covalent sum; did you mean frac 0.5 not
  0.05?"). And before a relax, an **MLIP pre-relax guardrail** (barely budges =
  plausible; flies apart = refuse the DFT spend).
- **Envelope:** success → `{ok, ref, warnings[]}`; failure → `{error:{code,
  message, suggested_fix}}`. **Warnings never block** ("succeeded, but you may not
  have meant it" — atom outside the cell, bond > 2× covalent sum) — the LLM sees
  them in the same result, no extra call. On success the touched refs + the new
  design `version` come back so the model never guesses what its edit did.

### 6. The eyes — a probe ladder, graph-first (the real 0041/0042 parallel)

Not "render the crystal and interpret pixels," but **probing mechanisms that
report, as numbers, what is bonded to what, what is near what, and how strained
it is** — directly over the graph + geometry. Increasingly soft, exactly
0041's ladder retargeted.

**§6 is the *read/navigate/output* side of the surface** — three of the seven
operation categories (the others: **Create**=`put`, **Edit**=§5 ops, **Compute**=
relax/neb jobs §9, **Organize**=`link`/`tag`/`delete`/`search`). Within §6:

- **Navigate** — moves the focus/cursor; *has state*, changes nothing structural.
- **Read** — data at the focus; *idempotent*, in-memory (§12 memo), **numbers for the LLM**.
- **Output** — an artifact for handoff (SVG/plot/export); *leaves the system*, **often for a human, not the LLM**.

**The §6 ladder, classified + tiered** (each row tagged `[v1]` = the geometry+
graph+forces construction loop, `[vision]` = needs DFT-fields / the ensemble / MD):

| § | feature | category | tier |
|---|---|---|---|
| 6.1 | graph & coordination probes (toc, atom, neighborhood, bond, walks/rings) | Read | **v1** |
| 6.2 | spatial probes (line·plane·sphere·bonds-through-plane·distance) | Read · *(SVG=Output)* | **v1** *(SVG render = vision/human)* |
| 6.3 | symmetry · sites · find · diff | Read | **v1** |
| 6.4 | DRC-lite (feeds the Edit validator gate, §5) | Read | **v1** |
| 6.5 | void/channel · env-fingerprint · egocentric focus · anomaly · fragment | Read + Navigate | **v1** *(field-aware void + cross-ensemble similarity = vision)* |
| 6.6 | embodiment / point-of-view `(support, reach)` | Navigate + Read | **v1** for atom/bond/fragment/site/void supports · **vision** for field supports |
| 6.7 | lenses (composable TOON columns / overlays) | Read | **v1** geometric/graph lenses · **vision** electronic field lenses (charge/ESP/spin/Fukui) |
| 6.8 | cursors + dashboard | Navigate | **v1** per-structure · **vision** cross-experiment |
| 6.9 | trajectories | Read + Output | **v1** = relax convergence-curve + diff · **vision** = keyframe/per-frame scrubbing (MD/NEB) |

6.6–6.8 are one idea three ways (a thing · a property of it · a set of them);
§7 measures are "embodiment A vs B." Anything tagged **vision** is **not v1** —
see §21 build order; the v1 cut keeps the *geometric* magic (embodiments, lenses,
cursors, fragments, anomaly, void) and defers only what needs an electron-density
field, the ensemble, or MD.

#### 6.1 Graph & coordination probes (exact, the LLM's home turf)

- **`toc` — the symmetry-reduced structure.** The token win: a 36-atom slab is
  **not** 36 rows. spglib collapses it to its **Wyckoff orbits** — "Pd(111),
  4 layers, top layer = 9 equivalent atoms (orbit `4a`), …" — so the LLM reads
  *one row per orbit*, the way 0041 renders a 6-bolt circle as one line. Cell
  card + composition + pbc + space group + orbit table + bond summary, one
  round trip.
- **`atom(id)` — the full config the request asked for.** Element, fractional
  position, layer, site role, **declared vs derived coordination**, neighbour
  list with **bond lengths + orders + image offsets**, bond angles, `fixed`
  state, magmom, **oxidation state (declared) + partial charge (derived)**,
  **per-atom force + local strain** (post-relax), and **displacement since the
  input geometry**.
- **`neighborhood(center, radius)`** — the coordination shell within R,
  MIC-aware, with bonds + angles. The workhorse for "what surrounds this site."
- **`bond(id)`** — the two atoms, length, order, offset, and **strain** (§8).
- **graph walks** — `path(a, b)` through bonds (with image bookkeeping),
  **ring/cycle detection** (essential for sp² carbon — find the 6-rings),
  **connected components** (did an edit fragment the structure?).

#### 6.2 Spatial probes (exact geometry — "like the CAD ray/plane")

The request's "inspect can be similar to CAD (observation ray or plane)":

- **`line(origin, direction, radius)`** — atoms within `radius` of a ray,
  in order along it. The instrument for **channels / columns** (a pore down a
  zeolite, the atom column under an adsorbate). 0041's ray, retargeted from
  material-intervals to atom-hits.
- **`plane(point, normal, thickness)`** — atoms within `thickness` of a plane,
  returned as a **labelled 2D map of in-plane fractional coordinates** — a
  *layer slice*, exact planar geometry, **not a render** (0041's exact section
  SVG, not its forbidden elevation). This is the honest, token-cheap form of
  the spec's banned `ascii_top`: real coordinates + labels, no raster.
- **`bonds_through_plane(point, normal)`** (the request's *"bonds through
  planes"*) — every **bond whose segment crosses the plane**, with its **length,
  order, derived strength, and the angle the bond makes to the plane normal**.
  This is the bond-graph analog of 0041's feature-attributed section: instead of
  *which atoms* sit in a layer, *which bonds stitch one side to the other* — the
  instrument for "what holds these two layers together," cleavage-plane
  reasoning, and finding the bonds an adsorbate makes across the surface plane.
  MIC/image-aware, so a bond crossing the plane *via* a cell wall is counted at
  its real crossing point.
- **`bonds_in_sphere(center, radius)`** (the request's *"bonds through
  spheres"*) — every bond **inside or crossing the sphere**, with **length,
  order, strength, and each end's bond angles** to its neighbours — the local
  bonding environment around a point or site as a graph, not a coordinate dump.
  A sphere centred on an atom is its coordination shell *with the angles*; on a
  site, the bonds an adsorbate would compete with. (`shell(center, r_in,
  r_out)` is the annular form — the second-neighbour shell without the first.)
- **`distance(a,b)` / `angle(a,b,c)` / `dihedral(a,b,c,d)`** — MIC-aware
  measures, the exact tape.

#### 6.3 Navigation & finding

- **`symmetry`** — space group, Wyckoff, equivalence classes, equivalent atoms.
- **`sites`** — named adsorption sites + interstitials (the spec's annotator),
  navigable as datums.
- **`find(predicate)`** — atoms/sites by element, layer, coordination, role,
  or **undercoordination** (`search(kind='structure', id=st, view='find',
  where='undercoordinated')`).
- **`diff(a, b)` / before-vs-after-relax** — per-atom displacement, RMSD, and
  **which bonds broke or formed** during relax (a graph diff — the most
  insightful single view of a relaxation's effect).

#### 6.4 DRC-lite — warnings the LLM sees *before* it spends DFT

A rule-check probe, the 0042 DRC analog: **atom overlaps** (two atoms closer
than a covalent-radius floor), **undercoordinated / dangling** atoms,
**hybridization-vs-geometry contradictions** (declared sp² but four
neighbours), **broken periodic bonds** (a declared image bond whose partner
relaxed out of cutoff), **vacuum too small / slab too thin / no dipole
symmetry**, and **valence sanity**. These are *rules the LLM reads*, surfaced
cheaply, never a render.

#### 6.5 Five more navigation primitives (LLM-shaped)

Beyond the probe ladder, five primitives that make a structure *navigable* the
way the bond graph (not a picture) is navigable:

1. **Void / pore / channel finder (navigate the *negative* space) — field-aware.**
   Most chemistry happens where the atoms *aren't*. The geometric pass
   (Voronoi/Delaunay empty-sphere) returns **voids** (centre, largest inscribed
   radius), marks which **percolate** into **channels**, and reports each
   channel's **bottleneck radius** — "where can an H/Li go?", "is there a
   diffusion path?", "what's the limiting pore?". But "the surface" is a
   **`field` parameter**, not one fixed thing (your question):
   - **`space`** (default) — the **steric** surface. By default vdW/covalent
     radii; optionally the **electron-density isosurface** (the 0.001 a.u.
     contour — the *real* molecular surface, post-DFT) instead of hard radii.
   - **`charge`** — the **electrostatic potential (ESP)**: pockets characterised
     by their **equipotential** sign/magnitude (a negative pocket binds a
     cation, a positive one an anion). "Space based on equipotential" → yes,
     this is exactly the ESP-isosurface mode.
   - **`lipophilic`** — the **hydrophobic/hydrophilic** field (a molecular-
     lipophilicity-potential map): which pockets are greasy vs wet — the
     binding/solvation lens.
   - **`both` / combined** — the *intersection*: a pocket that is **sterically
     open AND electronegative** is a cation site; open-AND-greasy is a
     hydrophobic binding cleft. The killer query is the conjunction.
   So one finder, a field selector, and the results are **filtered + ranked by
   the active lens** (§6.6) — the same pocket read three ways.
2. **Local-environment fingerprints → "find sites like this."** Give every atom
   a canonical **environment signature** (element + sorted neighbour
   elements + distances + angles; SOAP/ACE-lite later). Then dedup the structure
   by *environment*, not just global symmetry — `find(like='aPd14')` returns
   every chemically-equivalent site, `view='environments'` lists the **k
   distinct environments** ("3 distinct Pd environments: terrace, step, kink").
   This collapses a 500-atom structure to a handful of *kinds of place*, which
   is how a chemist actually navigates it.
3. **Egocentric navigation — a stable focus that is an *embodiment*.** The focus
   is **more than an atom id**: it is a **persisted, named reference frame** that
   **tracks an entity, not a coordinate** (a focus on "the OH oxygen" survives
   relax / renumber — a datum, 0041-style). The LLM moves it with relative verbs
   — `up`/`down` a layer, `along` a bond, `to` the next-cell image, `nearest`
   step/defect — keeps a **named bookmark stack** (`@active_site`), and reads
   positions **relative to it** ("1.3 Å above the surface, over fcc_hollow_4")
   rather than raw fractions. And the focus can *be different kinds of thing* —
   an atom, a delocalized bond, a fragment, a field — each with its own sense of
   "what I touch." That generalization is big enough to get its own section: the
   **embodiment / point-of-view model, §6.6.**
4. **Anomaly-first navigation — and it splits into *feature* vs *needs-fixing*.**
   Diff the structure against its **ideal reference** (perfect bulk / the
   dominant environment / the pre-relax geometry) and surface the deviations
   first — but **tag each anomaly by which kind it is** (your question):
   - **`feature`** — the *intended* deviation: the adsorbate, the dopant, the
     vacancy you put there on purpose. This is **the thing we want to make** —
     anomaly-view doubles as "here is my design, highlighted."
   - **`problem`** — the *unwanted* deviation that **still needs fixing**: an
     atom overlap, a dangling/under-coordinated bond, a wild strain, an
     unconverged force, a hybridization-vs-geometry contradiction (§6.4 DRC).
   So `view='anomalies'` answers **both** "what's interesting (my design)?" *and*
   "what's still wrong (my work-remaining)?" — and the **needs-fixing list is the
   project's work surface**: it unifies open DRC violations + failing measures +
   atoms still at a low fidelity rung (§9), i.e. *what's left to do before this
   model is mature.*
5. **Fragment-centric navigation (group before you list).** Auto-partition the
   atoms into **named chemical fragments** by connectivity + chemistry — the
   slab, each adsorbate, each solvent molecule, each ion — and let the LLM
   navigate and *edit by fragment* ("move the OH", "delete the second water",
   "rotate the ligand") rather than atom-by-atom. A 100-atom system becomes
   "slab + 3 fragments"; ops and probes accept a fragment handle wherever they
   accept an atom.

(*A sixth* — **trajectory scrubbing**, navigation across geometry *versions* over
time — grew into its own layer: see **§6.9**.)

### 6.6 Point of view — the cursor as an *embodiment*

The focus (§6.5 #3) generalizes into the most powerful navigation idea here:
**the cursor doesn't just sit somewhere, it *is* something, and it reports what
it touches.** The unifying abstraction is tiny:

> **An embodiment = a `support` (what I am) + a `reach` (how far "touch"
> extends).** Every embodiment answers the *same* three questions —
> `view='pov'` → **`i_am`** · **`i_include`** (my support) · **`i_touch`**
> (atoms / bonds / other embodiments within my reach) — plus the active
> **lenses** (§6.7) evaluated over my support (my charge, my spin, my strain).
> One uniform readout regardless of what you are: **that uniformity is what
> makes it easily accessible.**

The aromatic ring is the proof: a **delocalized bond is an N-ary hyperedge
(§4)**, so focusing on it makes the support = its member atoms — `i_include` is
the 6 carbons, `i_touch` is the ring's substituents + whatever sits on the π
face. "A coordination bond that includes all the atoms it touches" is *literally*
`focus(on=ring) → i_include`. No special case.

**Becoming something is one verb:** `focus(on='ring1')` infers the embodiment
from the target's type (an atom → atom, a hyperedge → its atom set, a fragment →
molecule); `focus(as='field', at=@site, r=4, lens='charge')` for a field POV.
The embodiment is persisted design state, not a per-call argument.

#### What "we" can be — the catalog (all just *(support, reach)*)

A `support` is a set of atoms, a region of space, or a region of a *field* — so
the embodiments fall into families, and the list is open (a new one is "define
its support," the §4/§6 extensibility surface):

- **Atom-backed (support = atoms):** an **atom**; a **pairwise bond** (POV along
  the bond axis); a **delocalized/coordination bond** (ring, η5-Cp, 3c2e — the
  hyperedge); a **functional group** (carbonyl, hydroxyl — selected by SMARTS);
  a **fragment / molecule**; a **fused-ring / conjugated system**; a **layer**;
  a **surface / facet**; a **defect** (vacancy, step, kink, dislocation core,
  grain boundary); a **unit-cell image** (touch = the neighbour images across
  each wall); the **whole structure**.
- **Negative-space-backed (support = empty region):** a **site** (adsorption /
  interstitial — "if I were an adsorbate here, what would I bond to?"); a
  **void / pocket** (§6.5 #1); a **channel / path** (a diffusion route or a
  conjugation path — touch = what lines it).
- **Set / symmetry-backed (support = a class of atoms):** a **Wyckoff orbit**
  (embody all symmetry-equivalent atoms — edit one, edit all); an
  **environment class** (§6.5 #2 — "all the step-edge Pd"); a **coordination
  shell** (first/second neighbours); a **spin sublattice / magnetic domain**
  (touch = the opposite sublattice).
- **Field / electronic-backed (support = a region of a field — needs a model):**
  an **electron cloud / Bader basin** (the "electron" POV — embody a density
  region, touch the atoms it overlaps); an **orbital / lone pair / dangling
  bond** (reactivity — what it can bond to); an **ESP test-charge** or a
  **field-at-a-place** (a lens isosurface, §6.7). Honest boundary: real density/
  orbitals need a DFT single-point (the rented field); pre-DFT these degrade to
  the *declared* lone-pair/bond picture (§8.1).
- **Process-backed (phase 2):** a **vibrational mode** (touch = the atoms that
  move in it); a **reaction-coordinate / NEB image** (touch = the making/
  breaking bonds); a **rolling probe molecule** (be a water, roll over the
  surface, report where it sticks — the dynamic SASA/docking POV).

Two embodiments compose into a relation (the §7 measures are exactly
"embodiment A vs embodiment B": separation, proximity, supply-path). So the POV
model and the measure model are the same machinery seen twice.

### 6.7 Lenses — composable, selectable property overlays

A **lens** is a property the structure can be *read through* — and the request's
"can we switch what's visible at the same time in TOONs?" is answered directly:
**lenses are selectable, composable columns/overlays, several at once.** The
atom-table TOON (§11) takes a **column set** (`columns=['charge','coord',
'strain','spin']`) so the LLM sees only the lenses that matter for the question,
and a field lens (charge/ESP/lipophilicity) drives the void finder (§6.5 #1),
the plane/sphere probes (§6.2), and an embodiment's `i_touch` ranking (§6.6).

The lens library (each a small pure function over geometry / graph / a rented
field; adding one is one entry, the 0041-primitive / 0042-measure extensibility
move):

- **Geometric / steric:** `space` (vdW or density-isosurface surface),
  `accessibility` (per-atom SASA), `volume/void`.
- **Bonding:** `bonds` (order / strength), `coordination` (CN, under/over),
  `aromaticity` / ring membership, `hybridization`.
- **Charge / electronic:** `charge` (partial / oxidation), `equipotential`
  (ESP), `spin` (spin density, ↑/↓), `magnetism` (magmom), `d-band` /
  local electronic descriptor, **`reactivity`** (Fukui f⁺/f⁻/f⁰ — *where it
  reacts*, the chemist's lens).
- **Mechanical:** `strain` (lengthwise + angular), `force` (residual /
  convergence), `displacement` (since input / since last relax).
- **Chemical / property:** `lipophilicity` (hydrophobic ↔ hydrophilic),
  `element`, `layer` / depth.
- **Workflow / meta:** `fixed` (constraint state), `fidelity` (which rung
  relaxed this atom, §9), `anomaly` (feature vs needs-fixing, §6.5 #4).

Some lenses are **scalar-per-atom** (TOON columns); some are **fields**
(isosurfaces — `space`, `equipotential`, `spin`, `lipophilicity`, `reactivity`),
read via the spatial probes (§6.2) or by embodying them (§6.6). The field lenses
that need electrons (`equipotential`, `spin`, `d-band`, `reactivity`) require a
DFT single-point and are blank until one runs (§8.1's intent-vs-derived line).

### 6.8 Many cursors — a named watchlist + a live dashboard

> **Built (2026-07-01).** Cursors + §7 measures are now wired on the
> dormant `struct_measures` table (they were schema-only): the
> `cursor`/`measure`/`unmark`/`remove_measure` ops (`structure/ops.py`),
> a `measures.py` evaluator, versioned persistence in `structure_save`,
> and `get(view='markers')`. Anchors are the stable atom **label** (not a
> `struct_atoms.id`, which the retire-and-reinsert of an edit orphans), so
> a marker survives edits and re-evaluates its value + verdict against the
> current geometry. Surfaced in the web viewer (`/structure/<slug>` — a
> panel + a 3D overlay of measure lines and cursor flags). A named-cursor
> *bookmark stack* + the cross-experiment tier stay vision.

Yes — **not one focus, a *set* of named cursors** (your question): `@active_site`,
`@defect`, `@left_edge`, `@big_ring` (its aromaticity lens on). Each is a
**persisted embodiment (§6.6)** carrying a **`for` / reason** — *why I'm watching
this* ("the site I'm decorating", "the vacancy I'm healing", "the edge that must
stay frozen") — the same intent field the §7 measures carry. So the cursors are
just **observers with a point of view**, re-evaluated after every edit and relax.

Their payoff is a **`view='cursors'` dashboard** — the structure's minimap, the
analog of 0042's "the placement report leads with the fixed set." One row per
cursor: **what it is** (`i_am`), its **key lens values**, **what it now touches**,
its **distance to the other cursors**, and its **purpose**. So you "always see
all the things nearby and know what they're for":

```
# cursors (re-evaluated @ relax v4)
{cursor        is              lens                 touches              nearest cursor     for}
@active_site   site fcc_4      charge −0.3          aO5, 3×Pd            @defect 4.2 Å      decorating with O
@defect        vacancy V1      coord 8 (−1)         5×Pd undercoord      @active_site 4.2   healing — watch CN
@left_edge     layer x<0.1     fixed 📌             9×Pd frozen          @defect 6.1 Å      keep frozen (slab base)
@big_ring      aromatic R1     aromaticity ok       6×C, 2 substituents  @active_site 8.0   must stay planar
```

Because two embodiments compose into a relation (§6.6), the **inter-cursor
distances are live measures** — so "the adsorbate just drifted within 2 Å of the
defect" surfaces *on the dashboard*, no manual probing. Cursors are stored as
observer rows (the §7 measure family in its `gauge` form, §12), so they cost no
new machinery — they *are* persisted measures that happen to have a viewpoint.

### 6.9 Trajectories — navigating frames over time

A relax, an NEB, or an MD run returns **many frames** (the request) — and the LLM
wants to *navigate time*, not scroll 200 geometries. So a **trajectory is
navigable exactly like a structure is**: the whole of §6 (probes, lenses,
embodiments, cursors) gains a **time axis**, plus two things that keep it sparse.

- **A trajectory is an ordered set of geometry versions** (frames) — the engine's
  relax `.traj` / NEB path / MD run, stored as the per-frame positions over the
  one graph (the §12 geometry-version memo, indexed by frame). Bonds/coordination
  are re-derived per frame; the §6.3 diff applied frame-to-frame is the engine.
- **Keyframes — "the interesting space in time" (don't read every frame).** An
  event detector auto-surfaces the few frames that *matter*: a **bond breaks or
  forms** (graph-topology change, frame-to-frame diff), an **energy extremum**
  (the NEB crest *is* the TS), a **max-force spike**, a **cursor's measure
  crossing a threshold**. `get(view='keyframes')` returns ~5 frames, each with
  *why it's interesting* ("frame 47: N–O bond broke; frame 88: barrier crest") —
  the trajectory's TOC.
- **Time series — any measure or lens over the run = a plot (the request's
  "distinct bond strength changes over time").** `get(view='timeseries',
  series=['bond_strength(@N,aO1)','charge(@N)','dist(@defect,Cu)'], over=traj)`
  → one column per series per frame, **as TOON** (with min/max/inflection flagged)
  for the LLM, and an **overlaid SVG plot** for the human (§14). Overlaying
  series is how the *why* shows: the bond-strength drop and the charge spike line
  up at frame 47 → "that's the bond breaking."
- **Watch the reaction through the cursors (the request).** The §6.8 cursor
  dashboard gains a **frame slider**: each cursor's lens values + `i_touch`
  become a time series, so you *play* the reaction and watch `@N` shed an oxygen
  and gain a hydrogen, `@defect`↔Cu distance breathe, the active site's charge
  flow — the embodiments animate over the trajectory.
- **Scrub + embody a frame.** `focus(frame=47)` or `focus(at='event:bond_break')`
  pins the cursor at an interesting frame and embodies it (a frame is just a
  geometry version), so all the §6.6/§6.7 *why* tooling (TS embodiment, breaking-
  bond strain, charge lens) applies *at that instant*.

Boundary: the **frame data is rented** (the engine's trajectory); 0043 owns the
**navigation, the keyframe/event detection, the time-series extraction, and the
plots** — numbers/TOON for the LLM, SVG for the human only. (MD trajectories are
phase 2; relax + NEB scrubbing, keyframes, and time series are v1 — they fall
straight out of the per-frame geometry the relax/NEB rungs already produce.)

### 7. Measures — graded design intent, persisted (the 0042 carry-over)

The "good feedback tools" the request is really after. A **measure** (§4) is a
named metric with a direction of goodness, re-evaluated after every relax and
drawn (for the human SVG) as an annotated tape. Each is a **small pure
function**; adding one is a single library entry. The v1 library:

- **`bond_length`** (`target`) — keep a bond at its equilibrium length; reports
  current length + strain.
- **`coordination`** (`target`) — keep an atom's neighbour count at N (a
  surface atom should keep its 9, an adsorbate its 1–3).
- **`separation`** (`max`) / **`proximity`** (`min`) — keep two atoms/species
  far/near (e.g. two adsorbates apart; an H near its host site).
- **`adsorbate_height`** (`target` / band) — the adsorbate's height above the
  surface plane — the single most-watched relaxation quantity.
- **`layer_spacing`** (`target`) — interlayer distance (surface relaxation /
  reconstruction signal).
- **`planarity`** (`keep-below`) — out-of-plane deviation of a set (is the
  graphene still flat?).
- **`max_force`** (`gauge`, `keep-below`) — the **relaxation-convergence tape**:
  max residual force on any non-fixed atom (the loop's stop signal).
- **`bond_strain`** (`gauge`) — *the request's "bond stress," in two
  components* (§8): **lengthwise strain** `(length − l₀)/l₀` (stretch /
  compression) **and angular strain** — the deviation of the bonds *at* an atom
  from its ideal geometry (sp³ from 109.5°, sp² from 120°, an fcc site from its
  lattice angles). Both reported per bond / per atom; angular strain is what
  catches a "right length, wrong shape" arrangement a lengthwise tape misses.
- **`bond_angle`** (`target`) — a specific angle `(a,b,c)` held at a value.
- **`charge`** (`gauge` / `target`) — an atom's **derived partial charge**
  (Bader) — watch a redox centre's charge state across edits/relaxes.
- **`charge_neutrality`** (`target 0` / `keep-below`) — Σ partial charges vs the
  declared net cell charge (a sanity tape: a large residual flags a bad density
  or a wrong NELECT).
- **`local_strain`** (`gauge`) — per-atom deviation from the reference geometry.
- **`vacuum_gap`** (`keep-above`) — the slab's vacuum, against the 15 Å floor.

Every measure has a **strength**: `gauge` shows the tape; `soft` is a term a
constrained relax pushes on (e.g. ASE's `FixBondLength` / a harmonic restraint);
`hard` is a constraint the relax must hold (a frozen bond, a fixed atom). So
"this O–H stays at 0.98 Å" can start as a gauge, become a soft restraint during
a tricky relax, and end as a hard constraint — without changing shape.

### 8. Intent vs derived — bonds, strain, and charge

The same split governs bonds, strain, and charge: **the LLM authors intent; the
backend returns the measured truth as feedback.**

- **The bond graph is authored intent** (order + image offset + hybridization).
  DFT itself does *not* consume bonds — it works on positions + cell — so the
  graph is an **authoring + inspection** layer, not a physics input. Its value
  is exactly the request's insight: *the LLM reasons about a graph far better
  than about coordinates.*
- **Bond strength is derived after relax — but "bond stress" is a category
  error, flag it (considerata §22-A).** DFT **stress** is a 3×3 *cell* tensor
  (d E / d cell-metric); it does **not** decompose onto pairs of atoms, so there
  is no quantum per-bond stress. What we report, cheapest first: (a) **geometric
  *heuristics*, labelled as such** — **lengthwise** strain `(length − l₀)/l₀` and
  **angular** strain (angles at an atom vs its ideal sp³/sp²/sp/lattice geometry):
  the everyday "is this bond stretched?" tapes, *not* QM observables; (b) the
  **true per-atom observable — the Hellmann–Feynman force** (and its projection
  along a bond axis), which *every* engine outputs; (c) **quantum bond order**
  (opt-in single-point) — Mayer / COHP / Bader, the honest electronic strength.
  v1 ships (a) + (b); (c) is a `derive_*` job in the spec's domain.
- **Scalar vs vector vs tensor — what a bond legitimately carries.** The IR's
  **bond order is a *scalar*** and that is the right thing: it is the LLM-facing
  intent label (vdW→single→double→triple→aromatic→metallic), refined by DFT to a
  Mayer/Bader number. The **per-atom *vector*** is the **force** (the everyday
  observable). The only honest **per-bond *tensor*** is the **force constant /
  Hessian block** — directional bond *stiffness* (restoring force vs stretch vs
  bend), from a vibrational (`gpaw_vib`) calc; advanced, phase-2. **Stress is a
  3×3 *cell* tensor, not a bond property** (the §22-A category error). So: order =
  scalar (intent), force = vector (per-atom truth), stiffness = Hessian tensor
  (opt-in vib), stress = cell tensor (whole box). No "per-bond stress tensor."
- **Charge is the same split** (§3): declared **oxidation state + net cell
  charge** = intent (valence sanity, NELECT, magmom seed); **partial charge** =
  derived (Bader from the DFT density), reported per atom and watchable as a
  `charge` measure (§7). Pre-DFT there is no real charge — only the declared
  oxidation state — so the atom view shows `charge: —` until a single-point runs.
- **Geometry can contradict the declared graph** — and that contradiction is a
  *feature*: the DRC probe (§6.4) flags it ("declared sp² aromatic ring,
  but aC3 relaxed to 4 neighbours / a bond stretched to 1.8× / 30° angular
  strain / valence sum ≠ declared oxidation"), which is precisely the feedback
  that tells the LLM its arrangement was unphysical.
- **The read/write topology of the IR — why it is thin, and what we own.**
  The whole intent-vs-derived split above collapses to three lens kinds,
  distinguished by *who may write them*:
  - **Atoms — conserved identity, flowing position.** The atom's **label is
    a stable handle** (`aPd2` is the same atom before and after any relax —
    the relaxer moves atoms, never creates / destroys / transmutes them);
    its **position** is write-once-then-refined; its **charge / force** are
    read-only, written back by physics. A conservation law bonds lack.
  - **Bonds — the *sole bidirectional* lens.** The LLM both **writes** the
    graph (order + hybridization + image offset = intent) and **reads** it
    back refined (`declared` → `inferred` → `dft`), order flowing as a
    scalar continuum. The edge set itself may be re-perceived — no identity
    conservation, unlike atoms.
  - **Fields — read-only from physics.** Energy, forces, and the spatial
    fields (density / ESP / spin / d-band / reactivity) are *looked
    through*, never authored; they are blank until a backend produces them
    (§8.1).

  This is *why* atoms and bonds get authoring verbs and fields never will,
  and *why the IR is thin*: we own exactly the two writable lenses (atoms,
  bonds) + their labels + the observers over them, and **rent every
  read-only field** from the backend (§8.1) — the same own-the-IR /
  rent-the-kernel line 0041/0042 draw.

#### 8.1 Do we model bonds or electron states? — neither as physics; we rent the physics

The request's instinct — *"put in bonds, relax, and it fixes itself; if we did
something stupid it gets fixed"* — is exactly right, and it resolves the
bonds-vs-electrons question:

- **The ground-truth physics is electron states, and we do not model it — we
  rent it.** DFT solves for the electron density / Kohn–Sham orbitals; it has
  **no concept of a bond** at all. A "bond" is an *interpretation* of the
  density (Bader basins, COHP, an overlap population), computed *after* the
  fact. So we never author electron states and never write an electronic-
  structure solver — that is the rented kernel (§9).
- **We model bonds as the LLM's editable intent-graph — a cognitive scaffold,
  not a physics input.** The relaxer consumes only **positions + cell +
  elements** (and, for DFT, electrons it solves for itself); it ignores our
  bond list. So the workflow is precisely the request's: **put atoms in (with
  bonds + hybridization as the scaffold the LLM reasons over) → relax → the
  physics moves the atoms to a sensible minimum → the *real* bonds, charges,
  and strain are re-derived from the relaxed geometry and handed back as
  feedback.** "It fixes itself."
- **The honest boundary: relax fixes *local* stupidity, not a *wrong topology*.**
  A relax (ML or DFT) descends to the **nearest local minimum** — it cleans up
  bad bond lengths, off angles, slightly-misplaced atoms, light overlaps. It
  will **not** repair a globally-wrong structure (wrong phase, wrong
  connectivity, an atom on the wrong site) — it will just relax *into* a wrong
  minimum. The §6.3 before/after **diff** (which bonds broke/formed, RMSD) and
  the §6.4 **DRC** are exactly what tell the LLM *which kind* of stupid it was:
  "the relax fixed it" vs "the relax converged but the topology is still wrong,
  re-author." When the LLM *wants* to hold a topology through a relax (so it
  *can't* drift), it promotes the relevant bonds/atoms to `soft`/`hard`
  constraints (§7) — but the default is a free, physics-led relax that is
  allowed to fix things.

So: **the bond graph is for the LLM; the electron states are the rented
arbiter.** We give the model the representation it reasons in, and let the
physics be the thing that's right.

### 9. Relax — a fidelity ladder from *fast* to *correct*, all rented

Relaxation is the heavy kernel, so by §1 we **rent it, entirely** — we write no
optimizer and no energy model. But the request's deeper point is the right one:
**there is a continuum of methods from fast-and-approximate to slow-and-correct,
and a project should work in that space** — set up elements, *do stupid bonds*,
and climb to ever-higher fidelity as the model matures. So `relax` is **one verb
with a `fidelity` rung**, the rungs all rented, each warm-starting the next:

| rung | method (rented) | wall time | what it's for |
|---|---|---|---|
| `0 clean` | **pure geometry** (ours): snap bond lengths to covalent radii, angles to VSEPR/lattice | instant | *fix the stupid bonds* before any energy model |
| `1 ff` | classical force field — UFF / GFN-FF | sub-second | rough pre-relax, get atoms off each other |
| `2 xtb` | semi-empirical — GFN2-xTB | seconds | cheap chemistry-aware geometry (great for molecules) |
| `3 ml` | ML potential — MACE-MP-0 / CHGNet | seconds–min | the **interactive default**; near-DFT, near-interactive |
| `4 dft-fast` | GPAW LCAO, loose kpts/grid | minutes | first real DFT check |
| `5 dft-tight` | GPAW PW, converged kpts/cutoff + dispersion | hours | the **publishable** answer |

- **One verb, a rung argument:** `relax(fidelity='ml')` (or `0`–`5`). The
  framework picks the rung's backend if installed, else reports `Unsupported`
  with the next-best available rung — never a crash. Rung 0 is **ours and always
  available** (no energy model — it literally just repairs the geometry the LLM
  declared; this is "do stupid bonds, it fixes itself" at zero cost).
- **Warm-start up the ladder:** each rung seeds the next from the previous
  rung's relaxed geometry (the spec's "ML-pre-relax then DFT" is just rungs
  3→4). Climbing is cheap because you never start cold.
- **The structure records its maturity:** `meta.relaxed_at_fidelity` (the
  highest rung it has converged at) + provenance per rung. So "how mature is
  this model?" is a field, and `find(where='fidelity<4')` lists what still needs
  DFT. This is 0042's "place light, place harder only when it matters," applied
  to physics: **spend the cheap rung while exploring; escalate only the
  candidates that survive.**
- **Why rent every rung:** ASE optimizers (FIRE/(L)BFGS) + UFF/GFN-FF/xTB/
  MACE/CHGNet/GPAW are all mature and someone else's maintenance burden; an
  in-house optimizer is the fragile reinvention 0041 refused for meshing. We own
  rung 0 (trivial geometry) + the *graph + probes + ops*; rungs 1–5 are backends
  behind the same `struct_*` tables.

Every rung is PBC-aware and honours `fixed` atoms + `hard`/`soft` measures as
constraints; each writes relaxed positions + per-atom forces (and, at DFT rungs,
charges/magmoms) back onto `struct_atoms`, bumps the geometry version (the §6.3
diff + `max_force` measure then report), and links `relaxed-by`. Rungs 0–3 can
run **in-process** on a worker that has the backend (the near-interactive loop);
DFT rungs are the spec's `gpaw_*` jobs on the cluster.

### 10. Load & store

- **Store** = the dedicated tables (§12), the design as a `refs` row + one card.
- **Load (ingest)** = read **CIF / POSCAR / extended-XYZ / ASE `atoms.json`**
  via ASE → populate `struct_atoms` (+ derive bonds via a CrystalNN/cutoff pass if
  the format carried none) → one card. A `put`-by-file path and a "from
  Materials Project `material`" path (the spec's `ingest_materials_project`).
- **Commit / freeze** = an immutable, **content-addressed `structure:<sha>`**
  snapshot (the spec's frozen structure) when the LLM is happy — the workbench
  ref stays mutable; the frozen sha is what a `dft_calculation` cites, so a
  result is always pinned to an exact geometry.

### 11. The legible representation — concrete views (TOON, the token budget)

Homogeneous rows come back as **TOON** (header + tab-separated rows), a single
atom/bond as **JSON** (per `precis-toon`). The win over the spec's ASCII grids
is **symmetry + graph, not pixels**:

```
# st7 — Pd(111) slab · 3×3×4 · 36 atoms · pbc[T,T,F] · vac 15.2 Å · SG P3m1 (#156)
#       lattice a=b=8.40 c=24.0 Å  α=β=90 γ=120 · frozen: layers 3–4 (18 atoms)
{orbit  wyck  element  n  layer  z_frac  coord  role        fixed}
o1      3a    Pd       9  1      0.42    9      surface     no
o2      3a    Pd       9  2      0.34    12     subsurface  no
o3      3a    Pd       9  3      0.25    12     bulk        all      📌
o4      3a    Pd       9  4      0.17    9      bottom      all      📌
o5      1b    O        1  ads    0.50    3      adsorbate   no       # on fcc_hollow_4
```

```
# atom st7#aO1 — O, frac(0.333,0.333,0.50) · on site fcc_hollow_4 · coord 3
{neighbor   element  length_Å  order  image      strain}
aPd11       Pd       2.01      1      [0,0,0]    +0.3%
aPd12       Pd       2.02      1      [0,0,0]    +0.4%
aPd17       Pd       2.01      1      [0,1,0]    +0.2%     # bond across the +b wall
force: 0.04 eV/Å   displacement-since-input: 0.21 Å   magmom: 0.0
```

```
# probe plane z=0.42 (top layer) — 9 Pd, in-plane fractional map · area 61.1 Å²
{atom    a_frac  b_frac  coord  nn_dist_Å}
aPd1     0.00    0.00    9      2.78
aPd2     0.33    0.00    9      2.78
…
```

```
# measures (re-evaluated after relax @ v4)
{measure            target   current   verdict}
max_force           ≤0.05    0.04      ok
adsorbate_height    1.3±0.2  1.28      ok
O–Pd bond_length    ~2.00    2.01      ok
Pd top layer coord  9        9         ok
vacuum_gap          ≥15.0    15.2      ok
```

A relax is one round trip: `edit(ops=[add_atom …]) → relax → get(view='toc')`,
then `get(view='diff', from=v3)` to see exactly which atoms moved and which
bonds changed.

### 12. Storage & evaluation — dedicated tables

Grouped by layer; **[v1]** = the construction loop, **[fwd]** = DFT/ensemble/
import. v1 tables carry the forward-compat hooks so later layers slot in without
a rewrite. Reuses the existing precis substrate (`refs`, `chunks`, `links`,
`ref_tags`). The corrections from the schema critique are applied (per-atom
*derived* values are run-scoped not design-scoped; version-stamped soft-delete
for undo; `event_defs`/`event_hits` split; surrogate `id` PK + a `content_sha`
secondary key; `frames` optional vs `traj_ref`; cursor = anchor-FK + derived
members).

**A. Design + graph core [v1]**
```
refs (kind='structure')   -- EXISTING. a DESIGN (editable). handle st<id>.
   meta = {lattice 3x3, pbc[3], system_type, vacuum, net_charge, spacegroup, version}
                          --   `version` = per-design monotonic counter, bumped per mutation
chunks                    -- EXISTING. exactly ONE card_combined per design → search + embed
struct_label_seq  (ref_id→refs, element, next int)   -- mints aPd124…; never reset (no-recycle)

struct_atoms      (id PK, ref_id→refs, label,        -- label 'aPd123', UNIQUE(ref_id,label), design-scoped
                   element, fa, fb, fc,              -- INTENT + current fractional position only
                   fixed smallint, magmom, oxidation,-- declared intent (fixed = per-axis bitmask)
                   hybridization_declared,           -- intent only; displayed value is re-derived
                   added_version int, retired_version int)   -- version-stamped soft-delete (undo, §G)
struct_bonds      (id PK, ref_id→refs, kind,         -- pairwise | aromatic | eta-n | 3c2e
                   bond_order real, provenance,      -- provenance = declared | inferred | dft  (mark inferred)
                   i→struct_atoms, j→struct_atoms, image int[3],   -- pairwise endpoints (NULL for N-ary)
                   constrained bool, added_version int, retired_version int)
struct_bond_atoms (bond_id→struct_bonds, atom_id→struct_atoms, image int[3])  -- N-ary members (ring/η/3c2e)
```

**B. Navigation persistence — measures / observers / cursors [v1, per-structure]**
```
struct_measures   (id PK, ref_id→refs, kind, direction, goal jsonb, strength,  -- hard|soft|gauge
                   operands jsonb, embodiment jsonb,  -- embodiment = (anchor, selector, reach) SPEC; members DERIVED on read
                   anchor_atom_id→struct_atoms NULL,  -- typed anchor for the single-entity case → integrity + reverse lookup + cascade
                   anchor_bond_id→struct_bonds NULL,  --   (a cursor = strength='gauge' + a `for`; an observer = no goal)
                   "for" text, value_derived jsonb, verdict, retired_version int)
```
*Not a member join (stale every relax) and not `links` (atoms aren't refs). A
static pinned set lives in `selector`; cross-experiment cursors [fwd] reuse the
same `embodiment` shape on the exploration todo's `meta`.*

**C. Frozen geometry + runs + trajectory [v1 commit/relax → fwd scale]**
```
structures        (id PK,                            -- SURROGATE pk = the FK target
                   content_sha text UNIQUE WHERE NOT NULL, sha_algo_version,  -- dedup + `structure:<sha>` citation
                   lattice, pbc,
                   atoms jsonb,                      -- FROZEN geometry = [{label?, element, frac}] in fixed order
                                                     --   carries the design LABEL per atom (NULL for imports) so run
                                                     --   outputs map ord↔label↔struct_atoms (query-walk gap #1).
                                                     --   bonds dropped (DFT ignores; re-inferred on fork).
                   natoms, composition, formula, spacegroup, created_at)
                                                     -- content-addressed, NOT a ref (millions at import scale)
runs              (id PK, ref_id→refs NULL,          -- the design that spawned it (NULL for imports). ONE run = a trajectory.
                   design_version int,               -- the design `version` this ran at → STALENESS check (gap #3)
                   input_structure_id→structures, converged_structure_id→structures,
                   rung, params_hash→run_params NULL, conditions_hash→run_conditions NULL,  -- dims [fwd]
                   source, trust, job_id→refs NULL,
                   converged bool, energy, max_force, n_steps,   -- the end-state CONVERGENCE ENVELOPE
                   convergence jsonb,                -- the cheap SCALAR CURVE {energy:[…], fmax:[…]} per step — ALWAYS stored
                   key_derived jsonb, traj_ref text, created_at)  -- traj_ref ALWAYS = the engine .traj blob (debug/restart)
-- For a routine RELAX you keep only: converged structure + the scalar `convergence` curve + the .traj blob.
-- The geometry `frames` below are materialized only for MD/NEB (where the trajectory IS the payload) or lazily on a debug/scrub request.
frames            (run_id→runs, frame_idx,           -- MD/NEB + lazy debug ONLY (not routine relax)
                   positions jsonb | positions_ref,  -- inline (small) vs blob-ref (TB-scale)
                   energy, max_force, PK(run_id,frame_idx))
run_atom_results  (run_id→runs, atom_ord int,        -- per-atom DERIVED outputs, keyed into the run's input structure order
                   force float8[3], charge, magmom, partition text)   -- CONVERGED-frame only; per-frame (run,frame,ord) is [fwd] (gap #5)
event_defs        (id PK, ref_id→refs / family, name '@H_lost',    -- the PORTABLE predicate (travels across forks/runs)
                   predicate jsonb, kind)            -- kind = bond_break|threshold|intervention|…
event_hits        (event_def_id→event_defs, run_id→runs, frame_idx) -- the per-run RESOLUTION (re-set after a new run)
```

**D. Ensemble dimensions [fwd] — the cube's other axes**
```
run_params        (params_hash PK, h_or_ecut, kpts, cell_size, xc, dispersion,
                   smearing, code, code_version, pseudo_set jsonb)   -- provenance; SHA per pseudopotential
run_conditions    (conditions_hash PK, U, pH, T, solvent, field)
```
The **cube** = `runs` (fact) × `structures` (geometry) × `run_params` ×
`run_conditions` × `frames` (time). A *filled cell* = a run. OLAP slice/dice/
nearest/gaps query this; embedding + proposal stay in the spec.

**E. Overlays & aggregates [fwd] — reconcile precis-dft kinds**
```
refs kind='material'           -- curated aggregate (search-by-intent ref); links → canonical structures
refs kind='reaction_network'   -- the OVERLAY (precis-dft's YAML library)
reaction_intermediates (network_id→refs, species, structure_id→structures)  -- networks SHARE intermediates (§18.2)
```

**F. Reused, not new** — fork lineage = `links(rel='derived-from',
meta={ops, family_id})`; `relaxed-by`/`has-requirement`/`touched` = `links`;
op audit = the `derived-from` payload + `ref_events`/`agentlog`; tags = `ref_tags`.

- **Evaluation model — PG is the system-of-record, *not* the per-probe compute
  path.** A single design is **kilobytes** (hundreds of atoms × a few fields), so
  it is **hydrated once into an in-memory working object** (≈ an ASE `Atoms` + the
  bond graph) on first touch via one small `SELECT … WHERE ref_id=?`. Every §6
  probe/derivation (cell-list/MIC, coordination, orbits, embodiments, lenses,
  cursor re-eval) runs **against that object — no DB round-trip** (µs in memory
  vs ~ms per PG call, and a tick does hundreds). PG's jobs are: **durable
  write-through** on mutation, **dedup** (`content_sha`), and the
  **cross-structure / ensemble OLAP** (the cube) — *that* you query in PG, never
  load. The boundary is **scale: one structure → memory; across structures/runs →
  PG.** The cache is keyed by `version` (bump → patch/rehydrate; cold = one small
  re-hydrate).
- **The derived layer is *not* stored** — coordination, symmetry orbits,
  special sites, neighbour shells, strain, measure verdicts are
  **computed-on-read in the in-memory object** (the per-`(ref, version)` memo;
  0042 §12; 0035 in spirit, no chunk cascade). The exception is **expensive
  DFT-derived results** (Bader charge, PDOS — minutes to make): those get a
  **persisted cache** keyed `(run_id, query_hash)` so they survive across
  processes (considerata's `observation` table). Cheap = in-memory memo;
  expensive = persisted cache. What *is* stored is **expensive backend output**:
  relaxed **positions** update `struct_atoms` (the design adopts the relaxed
  geometry), but per-atom **forces / charges / magmoms** are **run-scoped**
  (`run_atom_results`, fix #1) — they belong to the run that measured them, not
  the mutable design row; the atom-config view *joins* the live atom + its latest
  run's outputs.
- **Versioning & undo — two axes (query-walk gap #2).** Every mutation bumps the
  design's `version`; soft-delete is **version-stamped** (`added_version` /
  `retired_version`), so "the design at version N" = `added_version ≤ N AND
  (retired_version IS NULL OR retired_version > N)` — this recovers prior
  **membership** (which atoms/bonds existed). But positions are **in-place**
  updates, so **geometry-undo is a different axis**: to restore a prior *geometry*
  (e.g. after a diverged relax) **reload the relevant frozen `structures`
  snapshot** (a run's input/converged). Structural undo = version-replay;
  geometry restore = structure-reload. Per-atom **derived** values (force/charge)
  are **stale** whenever the current `version` ≠ the producing `runs.design_version`
  — the atom view shows them `—`/stale rather than misattributing them.
- **Indexes** that matter: `struct_bonds(ref_id, i)` + `struct_bonds(ref_id, j)`
  (the two graph traversals), `struct_atoms(ref_id, element)`, `struct_atoms(ref_id,
  label)`, `structures(content_sha)` (dedup), `run_atom_results(run_id)`,
  `event_hits(event_def_id, run_id)`. FKs give integrity (no dangling bond
  endpoint, no cursor anchored on a vanished atom); partial indexes exclude
  retired rows (`retired_version IS NULL`).
- **Neighbour / coordination** is O(atoms) with a **cell-list / AABB
  pre-filter** under MIC (most pairs are far); symmetry is one spglib call,
  cached. Instant at DFT cell sizes (tens–low-hundreds of atoms); **`log()`
  any cap** — "scanned the whole cell" must never silently mean "the first K"
  (0041 §9). **No spatial index** until cells get big.
- **Concurrency** is row-level (`FOR UPDATE` on touched rows), not a chunk-set
  rewrite — clean for two ticks editing one structure (0042 §12).
- **Forward-only migrations** (repo rule): one migration adds the `struct_*`
  tables; `st` is registered for the `structure` ref. **All in-tree, one
  package** (§20): the IR + handler + probes are pure-Python/numpy core; the
  relaxer/ingest/derive workers depend on ASE/spglib/pymatgen/MACE/GPAW and ship
  as **optional extras** of precis-mcp (`[dft]` / `[dft-ml]` / `[dft-gpaw]`),
  import-gated at use — *not* a separate plugin package. The IR/probe loop still
  imports none of the heavy stack; "core vs heavy" is now an **extras boundary,
  not a package boundary.**

### 13. Output files — what we ultimately emit

**Many small writers off one IR** (0042 §13), by what each consumer needs.
The key fact: **most structure formats carry positions + cell but NOT bonds**
(DFT does not use bonds), so the bond graph is **precis-native** and survives
only in the formats that can hold it.

- **Geometry interchange (the geometry layer):**
  - **extended XYZ (`.extxyz`)** — **the precis-native lossless export**:
    ASE-native, carries cell + per-axis pbc + constraints (`fixed`) + arbitrary
    per-atom arrays, so we stash our **atom labels, magmom, hybridization, and
    derived per-atom force** as extra columns. The round-trip format.
  - **CIF** — the crystallographic standard (symmetry, Wyckoff, fractional);
    the publication + Materials-Project-interop form.
  - **VASP POSCAR / CONTCAR** — the DFT-engine lingua franca (and ASE's bridge
    to most other engines).
  - **GPAW / ASE input deck** (a Python script) — the actual compute input,
    carrying the §dft_settings physics defaults from the spec (rented engine).
- **Bond-carrying export (the graph layer, for consumers that want it):**
  - **MOL / SDF and PDB** — for the **molecule mode** (§*general builder*):
    both carry **atoms + bonds + bond orders natively**, so a non-periodic
    structure's graph round-trips *losslessly* (unlike CIF/POSCAR). The natural
    out for ligands / clusters / adsorbate fragments.
  - **SMILES / InChI** — the molecule's **canonical graph identity** (via a
    cheminformatics plugin, RDKit) — a search/dedup key for molecule-mode
    structures, the analog of the crystal's `structure:<sha>`.
  - **LAMMPS data (`.data`)** — the one common *periodic* format that natively
    carries **bonds + bond types** (for MD / reactive-FF consumers); the bond
    graph round-trips here. Optional, behind the relaxer plugin.
  - the **labelled quotient graph** (atoms + `to_jimage`-labelled edges) as our
    own JSON, for graph tools — the exact §4.1 representation.
- **Compute outputs** (`.gpw`, `.traj`, energies, forces, DOS) are the
  **`precis-dft` spec's domain** (NFS artifacts under `jobs/<id>/`) — this ADR
  produces the *input geometry* and ingests the *relaxed geometry*, not the
  energies.

Artifacts land on `PRECIS_CORPUS_DIR`; each writer is a small exporter; the
design loop (author / probe / measure / interactive-relax) has **no external
binary dependency** — only the DFT/LAMMPS *export* gates on its backend.

### 14. Mapping onto existing precis machinery

- **Kinds & codes.** `kind='structure'` is a **refs-backed kind** (handle `st`,
  one card chunk, links, soft-delete) over the `struct_*` graph tables;
  `corpus_role='none'`. Atoms/bonds/sites are addressed by design-scoped natural
  identity (§4), not per-row handles. Aligns with the spec's `structure` /
  `structure_draft` split (the workbench ref is mutable; commit freezes a
  content-addressed `structure:<sha>`).
- **Search.** The single embedded `card_combined` (system + composition +
  adsorbate + intent) makes `search(kind='structure', q='OH on Pd(111)')` work
  on intent, while no atom or bond ever touches the embedder.
- **Workers (in-tree, extras-gated, §20).** A `structure_relax` ML job_type
  (in-process where the potential is installed) + `gpaw_*` on `ssh_node`; an
  ingest worker for CIF/POSCAR/MP; export workers per §13 — all **in `precis`**,
  registered by hard-import like every other built-in, their heavy imports gated
  behind the `[dft*]` extras (no entry-point discovery needed for first-party).
- **Web.** A **layer-slice / section SVG** (exact in-plane fractional geometry +
  measure tapes — the analog of 0041's section SVG, *not* a 3D render) + the
  measures table + probe results as the primary view; an optional vendored
  3D viewer (e.g. 3Dmol.js) for the *human only*. The agent never needs pixels.
- **Agent loop.** `put` → `edit(ops)` → `relax` → probe/measure → `edit` on the
  `job` / `plan_tick` substrate; each structure is a persisted, linked artifact
  (`derived-from` a paper, `produced-by` a todo, `relaxed-by` a job, frozen to
  a `structure:<sha>` a `dft_calculation` cites).
- **Skills.** `precis-structure-help` (verbs + the cell/PBC model + the op
  catalog + the periodic-image offset), `precis-structure-probe-help` (the §6
  probe ladder + the §7 measure library), and a `precis-pbc-help` (the §wording
  conventions — lattice, per-axis PBC, mirror-is-an-op, supercell). Index rows
  in `precis-overview` / `precis-help` / `precis-toolpath-help`.

### 15. Workflow — fork, sequence, resume (lineage + the todo tree)

> **Built (2026-07-01) — the human fork/modify loop, from the web.** A
> `structure_propose` job_type (claude_inproc) turns a natural-language
> instruction into proposed ops **without applying them** — the `claude -p`
> call is given no MCP tools, so it can only return dry-run-validated ops
> JSON, never mutate. `StructureHandler.derive(id, to, ops)` then **forks a
> new slug** with the ops applied, linked `derived-from` the parent (the
> parent is untouched, so `view='diff'` compares them). The web
> "Further instructions" box drives this end to end (`/instruct` →
> `/proposal` poll → `/apply`), with each proposed op hover-highlighting
> its target atom in the 3D cell.

The request names the real loop: **build a base model → fork it → modify →
run a simulation → continue the thread of thought once the result has actually
arrived → maybe fork again, analyzing each variant individually or as a group.**
This is a compute-job *needfully sequenced before* an LLM job, and precis already
has the substrate. The boundary: **0043 owns the *fork lineage* (it is
IR-level); the *sequencing* is the existing todo tree + coordinator
yield/resume; the *campaign* logic is the `precis-dft` spec.**

- **Fork = first-class structure lineage (0043).** Forking a structure is a
  cheap **copy-on-write** of its `struct_*` rows into a new `structure` ref,
  linked **`derived-from`** the parent with the **edit ops** as the link payload
  (the spec's derivation tree, and the 0033 edit model) — git-branch semantics
  for atoms. A batch fork (enumerate dopants, rotate an adsorbate over every
  site) shares a **`family_id`** so siblings are one group. A **`structure_tree`
  view** walks the lineage and **pins each node's latest relax result + maturity
  rung** (§9), so the LLM sees the whole family with results in one hop — the
  navigation analog of §6.5's diff, across forks instead of versions.
- **Sequence = the todo tree + coordinator (existing, not new here).** The
  thread of thought that must pause until a simulation returns is a
  **coordinator job** (or an `LLM:*` `plan_tick`): it mints compute children
  (a `structure_relax`/`gpaw_*` job per fork, parent = a phase todo), then
  **yields with `wake_when: children_done`** and releases its slot. The
  `wake_runner` re-queues it when the relaxes finish; it **resumes with the
  results in hand** — *"continue the thread once the MCP can truly provide the
  info"* is exactly the coordinator's resume. The `child_job_succeeded`
  auto-check (a `relax_converged` evaluator) is the wait-for-condition leaf.
  0043's contribution is only the **hooks**: `relax` is a job_type that links
  **`relaxed-by`** and writes results back so the wake condition can fire; it
  does **not** redefine the coordinator (Phase 0 PR 3 of the spec) or the
  campaign.
- **Fork-and-proceed, with the fidelity ladder as the screen.** The loop is the
  spec's `dft_campaign` phases (`propose → screen → confirm → analyze → decide`)
  and it rides §9: **fork wide, relax all at a *cheap* rung (ml), screen, then
  escalate only the survivors to DFT** — the "place light, escalate survivors"
  discipline (0042 §9) applied to physics. Each escalation is another
  compute-before-LLM hop (yield → DFT children → resume).
- **Analyze individually *or* as a group — two shapes.** Per-variant analysis is
  a **fan-out**: one analysis child per fork, each streaming as its relax
  finishes (no barrier). Group analysis is a **barrier**: one job that needs
  *all* results — a **`compare` / `family` view** that reads the **same measure
  or cursor across every sibling** (adsorbate_height on each, the d-band of
  each → the spec's volcano). So a measure defined on the parent **propagates to
  the forks**, and "analyze the group" is "render that measure across the
  family." (This is the Workflow-tool pipeline-vs-barrier distinction, in the
  job substrate.)
- **The thread survives the pause** because the coordinator's
  `meta.coordinator_state` carries it across the yield and the `job_summary` /
  `agentlog` chunks record the reasoning — so the resumed tick re-enters with
  both its intent *and* the freshly-arrived geometry/energies.

So the whole workflow is: **0043 forks + relaxes + compares; the todo tree
sequences compute-before-thought and resumes; the spec's campaign is the named
coordinator that drives it.** Nothing new in the orchestration layer — 0043 just
makes structures forkable, relaxes them on a rented ladder, and renders a family
with results pinned.

### 16. Tracer use cases (design validation)

Three end-to-end traces, to check the verbs actually *express* the work — and to
surface the ops/measures they require (listed after).

**A. Two SMILES → lowest-energy bound state.** *"Make two molecules, find their
lowest energy state together."*

```
put(kind='structure', name='molA', from_smiles='CCO')      # ethanol  (molecule mode, pbc off, big box)
put(kind='structure', name='molB', from_smiles='O')        # water
edit(name='pair', from=['molA','molB'], box=20,            # combine into one box…
     ops=[dock{mobile:'molB', target:'molA', n_poses:16}]) # …fork 16 relative poses (family_id)
relax(family=<fid>, fidelity='xtb')                        # cheap conformer/pose relax each
get(view='compare', family=<fid>, measure='total_energy')  # rank — the LOWEST is the binding pose
relax(id=<best>, fidelity='ml')                            # escalate the winner
get(id=<best>, view='pov', focus='intermolecular')         # read the H-bond: O–H···O length + angle
```
*Honesty:* "lowest together" is a **search**, not one relax (a relax only finds
the nearest local min) — so it *is* the §15 fork-and-rank loop, not a verb.

**B. Pd surface + random H on top + Cu inside → catalysis at a voltage.**

```
put(kind='structure', name='PdCuH', recipe='fcc111', element='Pd', size=[4,4,4], vacuum=15)
edit(ops=[set_element{atom: find(layer=2, element='Pd')[0], element:'Cu'}])      # subsurface Cu
edit(ops=[add_adsorbate{species:'H', sites:'top', coverage:0.375, random:true, seed:7}])  # "random amount"
relax(fidelity='ml'); relax(fidelity='dft-fast')                                 # settle, then real DFT
# "does it catalyze at a voltage" → the precis-dft spec's electrochemistry, not 0043:
reaction_evaluate(network='her_volmer_heyrovsky', U_RHE:-0.1, pH:0)  # → η, RDS (CHE; needs gpaw_vib chain)
```
*0043's part:* build the doped slab + random H, relax, expose the `charge` /
`equipotential` lens. The **voltage / overpotential is the spec** (CHE,
constant-µ in v2) — 0043 hands it a relaxed geometry.

**C. Move Cu relative to a surface defect → trend.** *"Alter copper location
relative to a surface defect (a few atoms missing)."*

```
edit(id=PdCuH, ops=[vacancy{atoms: find(layer=1, element='Pd', near='@x')[:3]}])  # 3-atom surface vacancy
focus(name='@defect', on=<vacancy region>, for='the vacancy cluster')             # a named cursor
edit(ops=[place_dopant{element:'Cu', relative_to:'@defect', distances:[2,4,6,8]}])# 4 forks (family)
relax(family=<fid>, fidelity='ml')
get(view='compare', family=<fid>, x='dist(@defect, Cu)', measure='eads_H')        # property vs Cu–defect distance
```
*This one exercises the whole design cleanly:* vacancy → cursor-with-`for` →
**egocentric placement relative to a cursor** → batch fork over a parameter →
relax family → **compare with an x-axis that is an inter-cursor distance** (a
measure between two embodiments, §6.6). The parametric study falls right out.

**D. NO₂ on Pd → reduce it.** *"The palladium has an NO₂ in there and we want to
reduce it."* Reduction is a chain of proton-coupled electron transfers
(NO₂* → NO* → N* → NH* → NH₂* → NH₃, or the *HNO₂/*NOH branches) — and each step
is **literal bond surgery on the graph**, which is exactly the bond-as-intent
payoff (§8): the LLM strips an O and adds an H, the relax fixes the geometry.

```
edit(id=PdCuH, ops=[add_adsorbate{species:'NO2', site:'bridge', orientation:'N-down'}])  # N bonds Pd; 2×O up
focus(name='@N', on='aN1', for='the nitrogen being reduced')
# build the reduction ladder as a FORK CHAIN — one graph edit per PCET step:
edit(from=PdCuH, name='step1', ops=[vacancy{atom: <one O>}, add_atom{element:'H', attach:'@N'}])  # NO2*→NO* (+H elsewhere)
edit(from='step1', name='step2', ops=[vacancy{atom: <other O>}])                                  # NO*→N*
edit(from='step2', name='step3', ops=[add_atom{element:'H', attach:'@N'}])                         # N*→NH*
# … NH*→NH2*→NH3*, each a fork derived-from the last (family = the pathway)
relax(family=<fid>, fidelity='ml'); relax(survivors, fidelity='dft-fast')
# free-energy ladder at a potential → the precis-dft spec:
reaction_evaluate(network='no2_reduction', U_RHE:-0.3, pH:7)   # ΔG per step, RDS, η  (CHE; gpaw_vib chain)
# TRANSITION PATHS — barriers between consecutive intermediates (kinetics, not just ΔG):
neb(from='step1', to='step2', fidelity='ml')                  # rent NEB → TS + barrier Ea (ML-NEB cheap → gpaw_neb confirm)
get(view='pathway', family=<fid>)                             # relative energy vs reaction coordinate, ΔG + Ea per step
get(view='pathway', family=<fid>, explain='max')             # drill the worst step → TS embodiment + why
```
*0043's part:* place NO₂, **build each intermediate by graph edits** (the
derivation chain *is* the mechanism), relax, run NEB between neighbours, and
render **energy vs reaction coordinate** across the family. The **ΔG-at-voltage /
overpotential is the spec** (`reaction_evaluate` + `pathway_diagram`, which can
*generate* the intermediate forks from a `reaction_network`). The bond graph
makes "reduce it" read as what it is — *remove an oxygen, add a hydrogen.*

**Reasoning about *what stage is a problem and why* (the request).** The
`pathway` view is a navigation surface, not just a plot: it flags the
**rate-determining step** (largest ΔG, thermodynamic) *and* the **highest barrier**
(largest Ea, kinetic), and `explain='max'` drills into it by **embodying the TS**
(§6.6 — a NEB image is a first-class embodiment) and reading *why* it is high:
the §6.3 **diff** between the two endpoints names the **breaking/forming bond**,
which at the TS shows up as extreme **bond strain** (§8) and a **charge / spin /
d-band** shift on the active atom (the lenses, §6.7). So "step 3 is the problem"
comes with "because the N–O bond is 1.7× stretched and 0.4 e of charge is
transferring to the O as it leaves" — the legible, graph-shaped *why*.

**Gaps these surfaced (folded into the design):**

- **Structure-level scalar measures** — `total_energy`, `binding_energy`,
  `eads` (adsorption energy) are *per-structure*, not per-atom/per-bond; the §7
  measure family gains a **structure-scalar tier** (so `compare` can rank a
  family by energy).
- **`from_smiles` / `recipe` builders** — molecule from SMILES; slab/bulk from a
  named recipe (`fcc111`, …) — ingest-side constructors beside §10's file load.
- **`from=[…]` combine + `add_fragment{near:…}`** — merge structures / drop a
  fragment near a focus (molecule assembly).
- **`dock{n_poses}` / pose + conformer enumeration** — a fork-generating op (or
  a coordinator search); honestly a *search*, per trace A.
- **`add_adsorbate{coverage, random, seed}`** — coverage-fraction placement with
  a recorded seed (reproducible "random amount"; the seed lives in provenance).
- **`place_dopant{relative_to:@cursor, distances:[…]}`** — egocentric,
  cursor-relative batch placement (a direct payoff of §6.6/§6.8).
- **`compare(x=<measure>, measure=<measure>)`** — the parametric/trend view: any
  measure on the x-axis (here an inter-cursor distance), any on the y.
- **Transition paths — `neb` + a `pathway` view.** NEB is the **transition-path
  sibling of `relax`** on the same rented fidelity ladder (§9): **ML-NEB cheap →
  `gpaw_neb` confirm**, finding the TS + barrier between two forks (endpoints).
  0043 owns the **`pathway` view** (relative energy vs reaction coordinate over a
  reaction family — ΔG *and* Ea per step) and the **`explain` drill-down** that
  embodies the TS (§6.6) + reads the breaking bond's strain + the charge/spin/
  d-band lens to answer *why a step is high*. The NEB *compute* and the formal
  `pathway_diagram` artifact are the spec; the **interactive reasoning surface**
  is 0043. (A NEB path is a trajectory → it also feeds §6.5's trajectory
  scrubbing.)
- **Boundary confirmed:** electrochemistry / overpotential / voltage stays in
  the `precis-dft` spec (CHE, `reaction_evaluate`); 0043 supplies the relaxed,
  charged geometry, the `equipotential` lens, and the transition-path *reasoning*
  view.

### 17. Skill directory — the questions the LLM will ask

The agent-facing skills (`src/precis/data/skills/precis-structure-*.md`, served
via `get(kind='skill')`) are organized **by the question the LLM is asking**, not
by subsystem — so the right skill surfaces from a plain-language goal
(`search(kind='skill', q=…)`). A skill needn't be specified in full here; what
matters is the question it must answer.

| skill | the question the LLM asks | covers (§) |
|---|---|---|
| `precis-structure-help` | "How do I build and edit an atomic structure — what are the verbs and the loop?" *(the entry point)* | §1–§5, §11 |
| `precis-pbc-help` | "How does the cell tile? What's periodic, how do bonds cross a wall, how do I make a slab vs a molecule, and why isn't mirror a tiling mode?" | §3, §4.1, wording |
| `precis-structure-build-help` | "How do I make one — from SMILES, a recipe, flood-and-carve, or grow-from-anchor? How do I place an adsorbate / dopant / vacancy / sheet / chain?" | §5, §5b, §16 |
| `precis-structure-navigate-help` | "How do I look around — what's bonded to what, what's near this site, where are the voids/channels, what's anomalous, what's *interesting*?" | §6.1–§6.5 |
| `precis-structure-pov-help` | "How do I *be* an atom / ring / fragment / field and see what I touch? How do I set up named cursors and read the dashboard? Which lenses can I turn on at once?" | §6.6–§6.8 |
| `precis-structure-relax-help` | "How do I relax, and which fidelity rung fits my question (cheap screen vs publishable)? How do I know it converged?" | §9 |
| `precis-structure-measure-help` | "How do I pin a goal/constraint and watch it — bond length, adsorbate height, separation, coordination, charge — and what does hard/soft/gauge mean?" | §7, §8 |
| `precis-structure-charge-help` | "What's the charge on each atom right now? What's the difference between the oxidation state I declare and the partial charge that comes back?" | §3, §8 |
| `precis-structure-trajectory-help` | "A run returned many frames — how do I find the interesting moment, plot a quantity over time, and watch the reaction through my cursors?" | §6.9 |
| `precis-structure-fork-help` | "How do I fork variants, sequence a simulation before continuing my reasoning, and compare a whole family?" | §15 |
| `precis-structure-pathway-help` | "How do I build a reaction's intermediates, find the barriers between them, and reason about which step is the problem and *why*?" | §16 D, §6.9 |
| `precis-structure-export-help` | "What files can I get out, and which ones keep my bond graph?" | §10, §13 |
| `precis-structure-troubleshoot-help` | "My relax didn't converge / atoms overlap / the geometry contradicts my declared bonds — what now?" | §6.4, §8.1 (+ spec retry) |

Index rows go in `precis-overview` / `precis-help` / `precis-toolpath-help`
(the latter gets the canonical call sequences — §16's traces are its seed). The
**chemistry/physics domain skills** (adsorption references, solvation, magnetic
init, reaction networks) stay in the `precis-dft` spec — this directory is the
*IR + navigation* surface only.

### 18. The experiment space — navigating the ensemble

**`[vision — Phase C/D]`** — not v1; lands after import (§19) fills the space. The
v1 construction loop (§21 Phase A) operates on *one* structure at a time.

The first wrinkle: **a set of experiments is one high-dimensional, very sparse
space, and the LLM should navigate it smoothly** — comparing *specific steps* of
*specific runs* so it can reason about the reaction itself. This is the entire §6
navigation model **lifted from one structure to the ensemble**: the same probes /
embodiments / lenses / cursors, with more axes.

**The axes.** A point in the space is one *(experiment, frame, region, lens)*:

1. **space** (3D within a structure) — a region / embodiment (§6.6).
2. **time** (the 4th axis) — a frame in a trajectory (§6.9).
3. **variation / composition** — different atom-sets: dopant identity × location
   (`Cu 3-down-1-over` vs `Cu 2-down-1-over`), coverage, facet. This *is* the
   fork family (§15), now treated as an axis.
4. **sim / DFT parameters** — grid `h` / `ecut`, kpts, cell size, timestep, xc
   functional, dispersion, **tool version**. Provenance (§ physics defaults,
   spec) **promoted to a navigable axis** — and the axis that **guards
   comparison**: you may not compare across incompatible settings (the spec's
   `material_promote` setting-equivalence rule); the axis is what makes that
   checkable rather than a silent error.
5. **conditions** — U, pH, T, solvent, applied field (the electrochemistry axis;
   spec).

**Semantic cursors that resolve *per experiment* (the crux).** A time cursor is
**not** a frame index — it is a **named event** that resolves to *that run's*
frame: "first hydrogen lost," "inner-Helmholtz-layer passed," "Ar added,"
"barrier crest." An event is a predicate over a trajectory (auto-detected by the
§6.9 keyframe machinery — bond-graph change, plane crossing — or an LLM-defined
predicate, or a logged intervention). Because the H is lost at a *different
absolute time* in each run, the event **aligns (time-warps) the trajectories**:
`@H_lost` points at frame 47 in one sim and 61 in another. The same holds on
other axes — `@active_site` is a *role*, not coordinates; `Cu↓3→1` is a *family
label*, not a row id — so a cursor is **semantic on every axis**.

**A cursor is a (possibly partial) coordinate; partial = a slice.** Fully
specified → one *(experiment, frame, region, lens)*. Leave an axis open → a
**selection** across it. The request's query is exactly a partial cursor:

```
compare(
  experiments=['Cu↓3→1','Cu↓2→1', …],   # the variation axis: a set
  at=@H_lost,                            # the time axis: a semantic event (resolves per-exp)
  where=@active_site,                    # the space axis: an embodiment/region
  lens='field')                          # the view: the ESP/charge field there
# → one row per experiment: the field at the active site, at the moment its H left + the diff
```

**Smooth navigation = hold axes fixed, sweep one** — the §6 probe ladder lifted
to parameter space: a **point** (one cell), a **ray** (sweep one axis — step
through events, walk dopant positions, refine `ecut`), a **slice** (sweep two).
"Watch the reaction" is a ray along the time axis with the cursors live;
"compare these 4 at the H-lost step" is a point on time × a set on variation.

**Sparsity is first-class.** The product space is astronomically large and almost
all empty, so it is a **sparse fact table** (one row per *computed-or-imported*
cell + its axis coordinates + result), never a dense grid. The §6.5 negative-
space / anomaly primitives **reappear at the ensemble level**: **voids = unrun
regions** (candidate next experiments — handed to the spec's campaign/proposal),
**nearest-filled-cell = reason by analogy** to existing data (§19), and an
**anomaly = an experiment that breaks a trend** across a parameter sweep. 0043
owns the *navigable representation* (axes + events + cross-experiment cursors +
the slice/sweep/compare verbs); the **embedding space over experiments and the
"what to run next" proposal stay in the `precis-dft` spec** — it decides what to
fill; 0043 lets the LLM *see and compare* what is filled.

#### 18.1 Where it lives, and how the LLM navigates the axis — *not* refs

**`refs` is the wrong home for an experiment** (the request's instinct, correct).
A `refs` row carries the corpus machinery — a card chunk + 1024-dim embedding, a
universal handle, tags, links, soft-delete, the dream/salience rotation — and a
*computed run-point* wants none of it. After §19 there are **millions** of them.
So the experiment space is a **star schema of dedicated tables** (the same
"dedicated-tables-not-corpus" move as §4 atoms and 0042's netlist, taken to its
ensemble conclusion):

```
dft_runs        -- the FACT table: one row per computed-or-imported point
  (run_id, structure_sha, params_hash→dft_params, conditions_hash→dft_conditions,
   source, trust, job_id NULL, energy, max_force, key_derived jsonb,  -- scalars for fast slice/sort
   traj_ref, result_blob_ref, created_at)
dft_params      -- DIM: (params_hash, h/ecut, kpts, cell, xc, dispersion, code_version, …)
dft_conditions  -- DIM: (conditions_hash, U, pH, T, solvent, field)
structure:<sha> -- the geometry DIM: content-addressed, stored ONCE, shared by many runs
dft_frames      -- the TIME axis: frames per run (positions over the trajectory)
dft_events      -- per-run semantic event annotations (frame index per named event, §18) — what cursors resolve against
```

The geometry is content-addressed and **shared** (one `structure:<sha>` backs
every run that used it), the params/conditions are **deduped dimensions** (a hash
each), and the fact row is thin (scalars inline for filtering, the bulk
trajectory/result by reference). **Refs are reserved for the few search-by-intent
objects**: an **editable `structure` design** you're working on (the §4
workbench, with its card), a **`material`** aggregate (the curated catalyst you'd
semantic-search "Pt₃Ni OER"), and a **campaign** — *not* the run-points.

**The LLM navigates the design-space axis with OLAP verbs over the cube** (the §6
probe ladder, lifted to parameters). *("Cube" is the data-warehousing term — an
**N-dimensional sparse hypercube**, not a literal 3D box: the dimensions are the
§18 axes, a **cell** is one coordinate-combination, a **filled cell** holds that
run's results, and it is stored **not** as a dense grid but as the star schema
above — `dft_runs` is one row per filled cell. The cube is the logical view; the
star schema is the bytes, as a pivot-table is to a flat record list.)*

- **slice** — fix all axes but one, sweep it: `energy vs ecut` at a fixed
  structure+conditions → a **convergence curve** (this is how the LLM *checks a
  result is converged* — sweep the sim-param axis and watch it flatten).
- **dice** — a sub-box across several axes: "all Cu-doped Pd(111), RPBE,
  U∈[−0.3,0]".
- **drill / rollup** — material → its constituent runs → their frames → atoms;
  and back up (a volcano *is* a rollup over a family).
- **nearest** — given a desired coordinate, find the nearest **filled** cell —
  *reason by analogy*, ride the giants (§19).
- **gaps** — the §6.5 void primitive at ensemble scale: which nearby cells are
  **empty** → candidate next runs (handed to the spec's proposal).

A §18 cursor is a (partial) coordinate *into this cube*; "navigate the design-
space axis" = move it along `dft_params` / `dft_conditions` / composition while
holding the rest, and the fact table answers each position as a TOON slice. The
**embedding** over runs (similarity, not exact-coordinate) and the **proposal**
that fills gaps remain the spec's; 0043 owns the **cube + the slice/dice/drill/
nearest/gaps verbs.**

**Keeping track of where we are.** "Where am I in the cube" is **session state,
and it has a third home — neither a fact row nor a ref, but the *exploration***
(the §15 todo/campaign that is doing the navigating). Its `meta` carries:

- **`cursor`** — the current (partial) coordinate across all axes (structure /
  frame-or-event / params / conditions / lens / embodiment): the "you are here."
- **`trail`** — the breadcrumb path through the cube (the moves made), so the LLM
  can **backtrack** ("undo the ecut refine, go back to the looser cell") and so a
  resumed tick (§15) re-enters knowing where it left off.
- **`bookmarks`** — the named cursors of §6.8 (`@active_site`, `@H_lost`), now
  spanning the cube, jumped between on the dashboard.

This is the *same* `meta.coordinator_state` that already carries the §15 thread
across a yield — the cube-position **is** part of the thread of thought, so it
persists on the exploration for free, survives the compute pause, and is what the
**§6.8 dashboard renders as "you are here + the nearby filled cells + the gaps."**
A cross-experiment cursor is therefore *not* a `struct_measures` row (those are
keyed to one structure ref); it lives on the exploration todo, which is the one
object that spans the whole space. (Resolves Open-Q 11: the ensemble is a sibling
fact-table surface; positions live on the driving todo; refs stay for
search-by-intent designs/materials/campaigns only.)

#### 18.2 One space for all of chemistry — reactions are overlays, not axes (nor workspaces)

Is CO₂ reduction a different space from NOₓ reduction — another axis, or a
separate workspace? **Neither.** There is **one shared physical space, and the
reaction is an *overlay* over it.** Three layers, kept clean:

- **The physical substrate is universal and unpartitioned.** A `*CO` on Pd(111)
  is the *same physical object* — same `structure:<sha>`, same run — whether you
  reached it doing CO₂RR, CO oxidation, or Fischer–Tropsch. A run in `dft_runs`
  (§18.1) carries **no "reaction" coordinate**; the physics doesn't know what
  you're studying. So reaction-type is **not an axis** on the cube, and the space
  is **never split by application** — it can, in principle, hold all of chemistry.
- **A reaction is an overlay graph that *references* the shared substrate.** The
  `reaction_network` kind (an overlay, already in precis-dft) is a graph of
  intermediates + steps that **points at shared structures** — and the same
  intermediate is reused across networks (`*CO` lives in CO₂RR *and* CO-ox *and*
  FT). So networks **share** intermediates rather than each owning a private copy.
  "Application / domain" (OER, NOₓRR, …) is therefore a **tag / lens**, not a
  coordinate.
- **Workspaces are saved *foci*, not separate stores.** A "workspace" is a saved
  viewport / working-set over the one space — a campaign's scope, a §6.8 bookmark
  set, a `meta.cursor` region (§18.1) — *organizational*, not a data partition.
  You switch what you're *looking at*, never which database you're in.

**Why this is the whole point (the request's instinct).** Because the space is
**not** partitioned by application, **two notions of "near" both enable
cross-domain inspiration**: the exact-coordinate **nearest filled cell** (§18.1)
*and* the **embedding / vector-space neighbour** — a CO₂RR intermediate's nearest
neighbours include NOₓ or hydrogenation intermediates with **similar binding**,
so an idea that works "over there" surfaces "over here." The scientific basis is
real: shared **descriptors + scaling relations** (a `*OH` binding predicts
`*OOH`; similar d-band → similar behaviour) mean chemically-distant reactions sit
*close* in descriptor space. **Partitioning by reaction would destroy exactly the
transfer you want.** (The embedding itself is the spec's `embedding_space`; 0043's
contribution is the decision that keeps the substrate one space so the embedding
*can* span chemistries — and the §18 cursors navigate it either way: by exact
axis, or by similarity.)

### 19. Importing existing databases — riding on the shoulders of giants

**`[vision — Phase C]`** — its own major sub-project (Open-Q 13); not v1.

The second wrinkle: **pre-fill the sparse space from databases that already
exist**, represented in our format. A massive but high-value import — and since
it lands in Postgres, *space is not the constraint* (the IR is rows, not a
corpus; no embedder load, §4).

- **Sources** — Materials Project, the **Open Catalyst** sets (OC20/OC22 — *the*
  catalysis-relevant ones, with relaxation trajectories → they fill the time
  axis too), NOMAD, OQMD, AFLOW, and MD trajectory sets. Each is a per-source
  **importer/adapter** (the spec's `ingest_materials_project`, generalized to a
  family), mapping external records → our `structure` (cell + atoms; bonds
  re-derived, §Open-Q 2) + the run's **result** + its **axis coordinates**
  (composition, dft-params, conditions). Trajectory-bearing sources import as
  multi-frame structures (§6.9), so imported runs are navigable like ours.
- **Provenance & trust are load-bearing, not metadata.** Every imported point
  carries its **source + foreign dft-params**, so it sits at its **true position
  on the sim-parameter axis (§18.4)** — which is exactly what stops a naive
  comparison of an MP-PBE number against your RPBE one: the axis guards it, the
  setting-equivalence rule fires. A `trust` field (`imported` / `confirmed`)
  distinguishes ridden-from-giants data from your own.
- **What it buys the LLM** — it starts in a *partially filled* space: reason by
  **analogy to the nearest existing cell**, avoid recomputing what's known, and
  seed a campaign from real neighbours rather than a cold start. The giants'
  data becomes navigable through the *same* §18 cursors as your own runs.
- **Boundary.** 0043 owns the **importer surface + the unified representation**
  (external → `structure` + result + coordinates + provenance). The **similarity
  embedding** over the imported ensemble, and the **proposal** that picks the
  next cell, are the spec's. Import is **ingest-only** (`supports_put=False` on
  imported records, like `cfp`/`datasheet`), refreshed by a worker, never by a
  hand `put`.

Together these two wrinkles are what let the LLM **reason on the reaction
itself**: a richly pre-filled space (§19) it navigates with semantic, per-
experiment cursors (§18), so it can hold "the moment the hydrogen left, at the
active site, as a field" fixed and ask *what differs across these catalysts there
— and why* (the §6.7 lenses + §16-D breaking-bond diff supply the why).

### 20. Packaging — merge `precis-dft` in-tree; reuse the science, supersede the representation

**Decision (by decree, 2026-06-28): `precis-dft` merges into `precis-mcp` as one
package.** The spec's original choice — a separate `precis-dft` plugin wired
through entry-point groups (its Phase 0 PR1/PR4) — is **reversed.** The ensemble
(§18) + the imported giants (§19) make this fundamentally **one large local
database representation**; splitting the code that owns it across a package
boundary buys little and costs the entry-point plumbing, a second migration
chain, and a version-skew surface. precis-mcp becomes a bigger package; that
tradeoff is **explicitly accepted** ("fine with it at this point"), and the
pure-function discipline below keeps re-extraction cheap if we ever want services
again.

**Consequences of merging:**
- **Heavy deps become optional extras of precis-mcp**, not a separate package:
  `[dft]` (ase/pymatgen/spglib/pyyaml), `[dft-ml]` (torch/mace/chgnet),
  `[dft-gpaw]` (gpaw) — out of `[all]` like the torch/OCCT paths in 0041/0042.
  The IR + probe loop import none of them (§1).
- **One migration chain**: `precis_dft/migrations/0001_dft_kinds.sql` folds into
  precis-mcp's forward-only chain (ADR 0031) as the next ordinal; the `struct_*`
  tables (§12) ship there too.
- **Hard-import registration, not entry-points**: handlers / job_types / workers
  register the way every built-in does (`dispatch.boot`, `cli/worker.py`), so the
  spec's **Phase 0 PR1 (`precis.job_types`) and PR4 (`precis.ref_passes`) are no
  longer prerequisites** for first-party DFT — keep them only if/when a *true*
  third party wants to plug in. **Still genuine precis-mcp prerequisites
  (packaging-independent): PR3 — the `coordinator` executor + `wake_runner`** (the
  yield/resume §15 needs them) and the **`ssh_node` executor** (cluster DFT).
  Merging therefore *unblocks DFT v1 sooner* (drops two of the four blockers).
- **Module home**: `precis_dft/*` → `precis/dft/*` (or `precis/structure*` for
  the IR), keeping the pure-function layering (below) so the "could be a service
  later" door stays open.

**Reuse vs supersede — the "we need pieces of it" call** (against the STATUS.md
inventory; 237 tests passing today):

- **Reuse wholesale (the science/backend — the valuable, hard-won pieces):** the
  annotator's *computational* internals (`structures/{header,sites,symmetry,
  special_sites,warnings}` — spglib / pymatgen / CrystalNN); the **ops** catalog
  + `apply`; the **workbench** (register / fork / commit + content-addressed
  `structure:<sha>`); the **reactions** library (**13 reaction-network YAMLs**) +
  CHE `evaluate` + `volcano` / `pourbaix` / `derive`; the **campaign** state
  machine; the wired **job_type shims** (reaction_evaluate, derive_*, …); the
  **retry `Ladder`/`Rung`** dataclasses (which *are* §9's fidelity ladder —
  adopt them); `view_worker`'s async materialization pattern.
- **Supersede (per this ADR):** **storage** — `meta.poscar` as single source of
  truth + recomputed view-*dicts* → the normalized `struct_atoms` /
  `struct_bonds` / `struct_bond_atoms` tables + one card chunk (§4/§12); the
  **ASCII `renderings.py` (top/side)** → the §6 graph/probe/lens/embodiment
  surface (the headline "no savanna-ape pixels" reversal); the per-structure
  **"views" dict model** → §6/§11. The annotator's *math* survives; its
  *presentation* does not.
- **Net-new here (absent from precis-dft):** the explicit **bond graph as IR**
  (image offsets + N-ary bonds, §4); **embodiments / lenses / cursors** (§6.6–8);
  **trajectory** navigation (§6.9); the **ensemble / experiment-space** model
  (§18) + **DB import** (§19); **molecule mode** (§general builder).
- **Kinds to reconcile:** precis-dft's `material` / `dft_calculation` /
  `reaction_network` / `embedding_space` carry over largely intact;
  `structure` / `structure_draft` get re-based onto the `struct_*` tables;
  `special_site` / `embedding_space` were skeletons — build to §4/§18 (Open-Q 11).

### 21. Build order — construction-first, then DFT, then import, then ensemble

The strategic question (start by *placing atoms in a box and building our own
sim*, or by *importing the existing DBs* first?). `considerata.md` + the
literature it cites resolve it toward **construction-first**: the **validated,
unaddressed gap is the construction surface** (AtomWorld / MatTools show LLMs
fail at exactly the fractional-coordinate / symmetry / periodic-image reasoning
this IR removes); construction is **self-contained, needs no cluster** (MLIP
pre-relax is local and seconds-fast), and is **end-to-end validatable** on a
narrow palette. Import (§19) is "massive and hard" (Q13) and the ensemble (§18)
is the bigger but more speculative bet. This **revises the earlier import-first
lean** (the blind spot `considerata` caught).

- **Phase A — the construction loop (the `considerata` core, no cluster).** The
  `struct_*` representation (§4/§12) + the op catalog (§5, incl. the **slab /
  cluster / adsorbate templates**, §22-J) + the probe-ladder floor (§6.1–6.4) +
  the **validator gate** (§22-B) + **MLIP pre-relax** (rungs 0/3, local) + a
  structured **relax/convergence envelope** (§22-D) + on-demand CIF/POSCAR/XYZ.
  Narrow palette (Pd/Cu/C/H), slab+adsorbate. Ships and validates end-to-end with
  zero cluster/import dependency. **This is v1.**
- **Phase B — DFT finish.** Wire the ASE-Calculator DFT rungs (GPAW default;
  VASP/QE pluggable, §22-H) via the spec's `ssh_node` + coordinator/wake — the
  *real* remaining prerequisites (§20) — completing the fidelity ladder.
- **Phase C — import the giants (§19).** Adapters (MP + OC22) → the **same**
  `struct_*` + `dft_runs` tables. A major sub-project (Q13); now the space is
  pre-filled.
- **Phase D — ensemble navigation (§18).** The cube + semantic cursors +
  slice/dice/compare over the filled space; embedding + proposal (spec).
- **Parallel cheap win:** **molecule mode** rides Phase A (pbc off, MLIP relax,
  MOL/SDF round-trip) with no extra infra.

Both the narrow MCP and the grand ensemble need the **same representation**, so
build it once (Phase A) and ship the self-contained construction loop before
taking on the heavy import and the speculative navigation. *(Import detail: pull
raw records **eagerly** — it's a DB; compute derived nav/events/cursors **on
demand + memoized**; batch-precompute only the cross-cutting embedding.)*

### 22. Cross-check against `considerata.md`

An independent design report (`considerata.md`, in the worktree) was written for
the same problem. It **converges** with 0043 on every load-bearing choice and
**sharpens** several; recorded here so the deltas aren't lost.

**Independently confirms:** Postgres-as-truth + files-on-demand (§10/§13);
`a<El><n>` handles; **image triples, corner offsets allowed, validated at the
distance layer not the address grammar** (§4.1 — same reasoning); **bonds are an
ergonomics layer, never a DFT input** (§8.1); the **tiered rented relaxer**
MLIP→xTB→DFT (§9); **steer the LLM out of raw coordinates** into named-site ops
(§5/§6); **no pixels / no GUI for the LLM** (§6) — it even argues the CAD *ray*
is wrong for a sparse point cloud (a ray misses every atom; use slice +
neighbour-list), trimming our §6.2 ray to a marginal probe.

**Corrections folded in:**
- **A. "Bond stress" is a category error** → §8 patched: stress is a 3×3 *cell*
  tensor, not per-bond; the real per-atom observable is the **Hellmann–Feynman
  force**; lengthwise/angular strain stay as **labelled heuristics**.
- **B. The validator is a pre-compute *gate*, not just a read** — every mutation
  + every pre-DFT step passes a microsecond check (sub-covalent length,
  over-valence, hard-sphere overlap) returning a structured **`suggested_fix`**;
  plus **MLIP-relax-as-guardrail** (barely budges = plausible; flies apart =
  refuse DFT). Elevates §6.4 DRC to a gate; adds the guardrail to §9.
- **C. Result envelope** — `{ok, ref, warnings}` | `{error:{code, message,
  suggested_fix}}`, **warnings ≠ errors** (never block). An interface detail 0043
  under-specified.
- **D. Convergence envelope + the locked-atom trap** — a relax returns
  `converged:bool` + max-force + steps + trajectory + final hash, distinguishing
  *didn't-converge* / *suspicious-minimum* / *large-residual-on-a-locked-atom* —
  the last because **a locked atom has zero force on its constrained axes by
  construction, so the max-force test can mask a stuck geometry as converged**
  (lock sparingly; §7's `max_force` gauge must exclude constrained axes).
- **E. Hybridization is inferred, not stored as input** — reconciled with the
  intent/derived split: *declared* hybridization is **intent only** (seeds the
  grow-from-anchor builder + valence sanity); the *displayed* value is always
  re-derived; mismatch is the §8.1 flag.
- **F. Handle scope** — `considerata` wants a **project-global** atom counter;
  0043 keeps **design-scoped** (`st7#aPd123`), because a global counter is
  meaningless across millions of imported structures (§18/§19); the design prefix
  gives the "unambiguous within the session" feel without a global namespace.
  *(0043's choice stands, for ensemble scale.)*
- **G. Version-as-undo** — the per-`(ref, version)` memo (§12) doubles as a
  lightweight **undo** (recover the cell at version n after a diverged relax).
- **H. Engine-agnostic** — the DFT rung is the **ASE Calculator contract** (GPAW
  default = no licence; VASP the catalysis reference; QE/CP2K pluggable), not
  GPAW-only.
- **I. Right-handed cell** — enforce a positive triple product on cell write
  (reorder vectors if needed); VASP rejects left-handed cells — a silent-failure
  class.
- **J. Templates** — add **`slab`** (Miller hkl + layers + vacuum + fixed bottom
  layers — the catalysis headline), **`cluster`** (sphere), **`replace(atom,
  with='OH')`** (swap an atom for a group), and named-site **`insert_above(site=
  hollow_fcc)`** to the §5 op catalog.

**Build-order tension it resolved:** `considerata` argues the validated,
self-contained, cluster-free gap is **construction**, not navigating existing
DBs — which **revised the build order to construction-first** (§21).

### 23. The dispatch & resurrection model — how a relax (and any delegated work) runs and returns

*(Amendment, 2026-06-29.)* Designing where a heavy relax runs forced a
**general** question: how does *any* unit of delegated work run somewhere on the
cluster and return to whoever asked? The model below is general — it is the
agent-orchestration substrate, and structure relax is merely its first heavy
consumer. It is recorded in 0043 because the need surfaced here; it is a
candidate to graduate into its own ADR (0044) once it has a second consumer.
**Nothing here is deployed**, so it unifies freely with the existing job /
todo / `auto_check` machinery rather than bolting onto it.

#### 23.1 One dispatch, orthogonal knobs

Stop modelling "subagent vs subtask" as two things. There is **one dispatch of a
work-item**, with four orthogonal knobs:

- **what runs** — the *executor class* (§23.2): a model, deterministic code, or a
  wait.
- **where** — capability- and node-routed (`has_gpaw` → spark; an LLM → a litellm
  tier or cloud). Targeting already lives in the right place: `ssh_node`'s
  dispatch picks the node (`PRECIS_DFT_NODE`, spark by default), *not* the
  claim-SQL.
- **priority** — super (jump the queue, run now) … low (drain when idle). `PRIO`
  (1..10) already exists on refs; the job claim must become PRIO-aware (today it
  is `ORDER BY ref_id`), with a super-priority lane for §23.6 sync waits.
- **await mode** — how long the *parent* holds its context open: `sync_agent` or
  `subtask` (§23.6).

The engine under all of it is the existing `dispatch` worker + executors +
`child_job_succeeded`. The knobs are properties of the spawn, not new mechanisms.

#### 23.2 Three executor classes — model, code, wait

What advances a todo is exactly one of:

1. **model** (LLM) — produces its response by reasoning (a `plan_tick`-style
   incarnation; today via `claude_inproc`, tomorrow via a model-agnostic
   `AgentRunner`, §23.10).
2. **code** (deterministic) — produces its response by computation. `struct_relax`
   (ml/gpaw), export, ingest. No model.
3. **wait** — produces *no* work; it resurrects the parent when a condition holds.
   **This already exists** as the `meta.auto_check` evaluators
   (`paper_ingested`, `discord_reply_received`, `time_past`, `tag_present`,
   `child_job_succeeded`).

The keystone unification: **join-on-children and wait-on-a-condition are the same
primitive.** `child_job_succeeded` *is* an `auto_check`. So "wait for the DOI to
arrive and be indexed" (`paper_ingested`), "wait for the user to answer"
(`discord_reply_received` + the `ask-user:`/`asking-reto` tags + the `attention`
view), "wait until 9am" (`time_past`), and "wait for my 50 relaxes"
(`child_job_succeeded`) are all the same: an `auto_check` that, when satisfied,
re-enters the todo into the doable rotation. A human is just a slow external
runner whose reply is the wake-condition.

#### 23.3 `job` vs `todo`

- A **`job`** is the dispatch unit — claimable on any node, runs the executor,
  carries `STATUS:` + lease. It *always* materialises.
- A **`todo`** is the durable, resumable parent — the resurrection anchor. It
  materialises **only** when the work must outlive a single tick (a subtask, or a
  planner that spawns children). A `sync_agent` (§23.6) needs no todo: just a job
  + an `agentlog`.

So `kind='todo'` keeps one meaning — durable, resumable work — and the ephemeral
fast call does not pollute the tree.

#### 23.4 A tick's run log vs its final output

A tick (one incarnation) has two outputs, and conflating them is the classic bug:

- **Run log** — everything the tick *did*: model turns, reasoning, tool calls,
  container stdout. Forensic only: `agentlog` (assembled prompt + `touched`
  chunks) + `job_event` chunks. **Never shown to the parent; never replayed on
  resurrection.** Possibly large, model-specific, maximally tainted (it saw every
  input), GC'd after `PRECIS_AGENTLOG_RETENTION_DAYS`.
- **Final output** — the durable, structured *response*: a verdict (§23.5) + the
  `{status, handle, hint}` envelope + the `resume_brief` + the children minted +
  the records written (`struct_runs`, …). Small, **model-neutral**, propagated
  up, and the *only* thing a resurrection reconstructs from. Persisted as
  `job_summary` + meta + the records.

Rules: the parent sees only final outputs; resurrection reconstructs only from
final outputs (which is what makes it cheap and model-portable); and the final
output is assembled from the agent's **control actions** (spawn/yield/complete +
records written), **not** its prose. The run-log → final-output transform is also
where **declassification** (§23.11) lives — so this split is the governance
boundary too. Every tick emits exactly one final output, however the run ended
(success, yield, exhaustion, failure, or kill → fallback).

#### 23.5 A tick's response — explicit control actions (no parsed verdict)

*(Grounded against code, 2026-06-30: there is no "verdict" return — the runner
captures the tick's stdout as a `job_summary` and maps its exit code to STATUS; it
does not parse the output. A tick sets its own state by **calling MCP verbs during
the run** — the "control actions, not prose" of §23.4, already real in `plan_tick`,
which mints children via `put` and tags its own `STATUS`.)*

We **formalise** that surface as a small set of first-class control actions, each
with required fields — so a weaker/local model can't forget the convention and the
§23.7 capture is deterministic:

| action the tick calls | meaning | what it sets |
|---|---|---|
| `spawn(...)` — the §23.3 call-wrap `put` | delegate work | mints child todo+job; parent yields, waking on the join |
| `yield(resume_brief, wake_on)` | pause until a condition | `meta.resume_brief` + an `auto_check` (children / event / time) |
| `ask_user(question)` | pause for a human | `ask-user:` + a `discord_reply_received` wake |
| `complete(result, handle)` | task done | closes the todo `STATUS:done`, writes the envelope, fires it up to *its* parent |
| (failure) | terminal failure | executor sets `STATUS:failed`; the `child-failed:<id>` bubble surfaces it |

Every non-terminal action is "**yield + an `auto_check` wake-condition**" — join
and wait are one mechanism (§23.2). The §23.7 checkpoint enforces the contract: a
tick that ends without a terminal/yield action gets **nudged once** for it (and the
`resume_brief`) before teardown.

#### 23.6 Await modes — and the timeout→promote rule

Two modes; the difference is whether the parent's context is *held* or
*reconstructed*. **"Super-priority" is prio-ordered claiming, NOT preemption**
(grounded: the claim is `FOR UPDATE … SKIP LOCKED`, a running job is never
interrupted; reuse the existing `refs.prio` 1..10 column as the claim `ORDER BY`).
So sync is "responsive when the resource is free," not "instant."

- **`sync_agent`** — the parent blocks this turn; the result is reinjected inline.
  Fast, high-prio work. With no preemption, on a busy single GPU it *queues*, so
  it is bounded by a **timeout that auto-promotes to a subtask** ("wait up to T;
  else yield and resurrect me with the handle"). A cache hit (§23.16) makes the
  common case genuinely instant.
- **`subtask`** — the parent yields and is resurrected later (§23.7).

**The todo question, resolved — two regimes:**
- *Caller is a durable tick* (the common agent case): the sync work is a **child
  job of the caller's pre-existing todo** — the substrate already auto-parents via
  `PRECIS_CURRENT_TODO`. Sync = the caller's tick holds and polls; timeout→promote
  = the caller simply *yields* and the same child becomes a subtask. **No new
  todo, ever.**
- *Caller is rootless* (interactive / CLI): an **ephemeral job, no resurrection
  contract** (`agentlog` only). On overrun it does **not** auto-promote — it
  returns the handle and "poll `view='runs'`," since there is no durable parent.

The completion handler is **one path**: *child completes → live waiter? deliver
inline : resurrect.* A crash mid-`sync` degrades to resurrection for free. The
child is **oblivious to its parent's mode**. Subtasks nest freely; `sync` nests
but self-limits via timeout→promote.

#### 23.7 Resurrection — same todo, new job per incarnation

The durable identity is the **todo**; each resurrection is a **fresh `job`** under
it (tick J1, J2, …). The sequence of tick-jobs *is* the incarnation history —
each with its own `agentlog`, `job_summary`, cost; the todo carries the rolling
`resume_streak` counter (cap → bubble, the existing `plan_tick` guard). Think:
**todo = the process; each tick-job = one scheduler quantum.**

Continuity is carried by a `meta.resume_brief` — a short, **model-neutral**,
rolling baton (rewritten at each suspend), *plus* reconstruct-from-tree (children
summaries + ancestry via `planner_prompt`). The brief is captured at a **suspend
checkpoint** that fires on every running→suspended transition:

1. **explicit yield** → the brief is a *required field* of the yield action (no
   extra turn; intent is sharpest at the spawn decision).
2. **max-turns exhaustion** → a single budgeted, tool-restricted **epilogue turn**
   ("write your resume note") — the context is still warm, so we can ask one more
   thing before teardown.
3. **hard kill / timeout** → no live turn; fall back to a transcript summary, then
   to tree-only.

Graceful degradation: explicit brief → poked epilogue → summarised → tree-only.
**Forbid transcript replay** (unbounded, stale, kills model-portability) — the
discipline of "commit decisions to the tree + a short note, discard the
chain-of-thought" is what lets a cheap local model pick up a tick.

#### 23.8 Done — declared, not inferred

"Done" is two different things and must not be conflated:

- a **deterministic leaf** is done when its job sets `STATUS:succeeded`
  (mechanical);
- an **LLM parent** is done only when it *declares* `verdict:done` and closes its
  own todo. A tick succeeding ≠ the task done — the existing guard already forbids
  `child_job_succeeded` from auto-closing an `LLM:*` parent. Children landing
  *resurrect* the parent so it can judge "done, or spawn more?" with fresh eyes.

Termination is bounded by: parent-declares-done | budget | `resume_streak` cap |
human-terminate (the same stop-reasons `precis-dft`'s campaign already
enumerates).

#### 23.9 Join & reinjection

*(Grounded against code, 2026-06-30: there are TWO wake systems. The durable
all-of-specific-children **barrier already exists** as the coordinator's
`children_done` `WakeWhen` — event-driven via `wake_runner`, ~2 s, taking explicit
`child_job_ids`. The `auto_check` `child_job_succeeded` evaluator is a different,
weaker thing — `any` (≥1 succeeded), ~60 s polled, for a single-job todo leaf. So
the durable subtask fan-out is **already built**; §23 anchors the barrier on
`children_done` and reserves `auto_check` for the cron-like external waits — DOI
`paper_ingested`, time, tag, human reply — i.e. the §23.2 `wait` class.)*

So **join** is a property of the parent's yield: `all` (barrier, **default**) →
`children_done` over the spawned child ids; `each` → re-arm per terminal child;
`quorum(k)` → resolve at k (a `children_done` variant). A resurrected tick is
handed the **delta** of children terminal (succeeded **or** failed) since its last
run, plus which remain pending — uniform parent code, only the trigger differs;
simultaneous completions **coalesce** into one resurrection.

Reinjection is bounded three ways: only **direct** children's `{status, handle,
hint}` envelopes (grandchildren rolled up a level), never run logs, never raw data
(behind the handle). At fan-out width it degrades to an **aggregate handle +
top-K** from a separate **reduce node** — deterministic by default ("rank by η,
top-K → run-set `rs88`"), a model only when the reduction needs judgment. `all`
costs one tick (the scale default); `each` up to N, so the join is also a budget
lever.

#### 23.10 Model & temperature — resolved per incarnation

Because the brief is model-neutral, **the model is a property of the incarnation,
not the task.** The agent never hardcodes a model; it declares a **role/tier**, and
a router resolves it through a cascade: task-type default → parent advisory hint
(generalising the `LLM:opus|sonnet|haiku` tag from a model to a tier) → hard
constraints (§23.11, budget) → prior-tick **escalate-on-stall / de-escalate-on-
trivial** (a `next_tier` hint riding with the brief). The router (over litellm)
resolves `(model, node, sampling)` and records it on the `agentlog`, so model
choice is auditable per tick — Opus on the one hard planning tick, a local Qwen on
the ten mechanical ones.

**Temperature is a property of the role, not the model**: low for
plan/extract/verify (reliability + cache reuse), high for diverge/propose.
`(tier, temp, N)` is one triple; for a hard fork it becomes a **panel** — N
parallel incarnations at high temp, then vote. The two schema knobs that cover
~90% of routing: a task's **`role`** and a **`local_only`** constraint.

#### 23.11 Sensitivity / `local_only` — a fail-closed governance constraint

`local_only` is data-governance: tainted content must never leave our hardware
(no cloud LLM, no external egress). Unlike tier/temp it is **hard and
fail-closed** — it overrides every preference, and if no local model can satisfy
it, the work *stalls* rather than leaking. It is **content-derived**, not
agent-chosen: data carries a **sensitivity class**; policy maps class →
enforcement (local routing + no egress + a **local embedder**).

The hard part is **taint propagation**, which is structurally the GPL-contagion
problem and is *not* fully solvable (implicit flows). It propagates **down**
(children inherit), **sideways** (the brief, summaries, embeddings inherit), and
**forward** (resurrections inherit), monotonically. v1 is therefore deliberately
coarse and defensive, not clever:

- coarse whole-subtree taint (sound, over-spreads — accepted);
- **contain by decomposition** — keep proprietary work in its own subtree, never
  pull tainted chunks into a clean planning context (isolation, the GPL "separate
  process behind a clean API" analog);
- a **segregated embedding index** for tainted content — else similarity search
  against the shared HNSW is a covert channel (the sharpest, easiest-to-miss
  edge);
- **declassification** only as an explicit, gated, `agentlog`-logged operation;
- fail-closed + audit as the backstop.

Defer fine-grained taint and automatic declassification.

#### 23.12 Structure relax as the first consumer (Slice 1)

Decided: a **thin `struct_relax` job_type in precis-mcp**, reusing only
`precis-dft`'s **pure** helpers — its `gpaw_relax`/`dft_calculation` stay untouched
(the kind-merge is Slice 2):

- **Spawn is cache-first (§23.16):** a request for `(structure_sha, params_hash,
  code_version, model)` first hits the run-cube; an exact hit returns the run
  handle with **zero compute** (a genuinely instant sync call). Only a miss
  dispatches.
- `clean` relax stays **inline** (instant, pure).
- `ml`/`gpaw` relax is a `code`-executor job via `ssh_node` → spark → container.
  Reuse `stage_inputs`/`parse_result` + the in-container runner + image; **adapt
  the argv** to **podman + CDI GPU** (`--device nvidia.com/gpu=all`, not docker
  `--gpus`) and a **deterministic `--name precis-job-<id>`** (grounded: today it's
  an unnamed `docker run --rm --gpus all`).
- **Result sink = the §12 run-cube** (a `runs` row + the convergence curve), *not*
  `dft_calculation`.
- **Write-back is copy-on-write** (§8/§10): freeze the input as `structure:<sha>`,
  write the converged geometry as another immutable snapshot, link via `runs`. The
  mutable design head **floats**; "adopt the relaxed geometry as head" is a
  separate, deferred, explicit fast-forward with an `expected_version` CAS on
  `structure_save` (today it bumps unconditionally) — caught synchronously at
  adopt-time, no messaging system (C10). Because variants are immutable snapshots,
  the write-back race dissolves.
- Result trust is **YAGNI beyond the backend** (A3): record convergence + propagate
  GPAW/ASE **warnings**; errors = failure; keep only near-free numeric sanity (NaN
  energy, post-relax overlap, "converged but forces not below tol" — the §9
  locked-axis trap). Retry is **LLM-owned** (A4): `clean` pre-conditions geometry;
  the parent tick decides retry/escalate, no auto-orchestrator.
- Come-back via the durable barrier: the parent wakes on `children_done`;
  `view='runs'` reads the record; failures bubble.

**Shipped (2026-06-30) — the cache↔relax seam.** The
`struct_relax` job_type (`workers/job_types/struct_relax.py`, `ssh_node` /
`REQUIRES={has_gpaw}`) plus the `StructureHandler` mint path: an energy rung
that misses the §23.16 cache and has no local backend builds a `_NeedsDispatch`
(content address + staged POSCAR + canonical / POSCAR-row orderings) and — given
a parent todo — mints the job (`idem_key=struct_relax:<cache_key>` collapses
duplicate dispatches); without a parent it rejects atomically with the exact
dispatching call. The dispatch stages → runs the container on the node → parses
`result.json` → records the **run-cube** (`structure_record_run` with
`cache_key` / `structure_sha` / `final_geometry` on the row), so the next
identical relax is a zero-compute hit. Two grounded deviations from the text
above: (a) **docker, not podman/CDI** — the deployed spark node runs
`/usr/bin/docker` + NVIDIA Container Toolkit and the `precis-dft:cpu` image was
validated there with `--gpus all`; `PRECIS_DFT_CONTAINER_CMD` flips to podman+CDI
when the node migrates; (b) **the relaxed geometry lives on the run row**, not a
separate CoW snapshot table — the cache is self-contained and adopt-as-head stays
deferred (§8/§10). `struct_relax` is **self-contained** (precis-mcp does not
depend on precis-dft); it mirrors precis-dft's container contract rather than
importing it. *Still open in Slice 1:* the cluster `roles/dft` (install
precis-dft into spark's worker venv + the `PRECIS_DFT_*` env), node-/parent-scoped
claim gates (#3) so spark claims the job and `/shared` paths stay consistent, and
the sweeper per-job timeout (#1) + container-name kill (#6).

#### 23.13 Build slices (what it takes)

Most of the engine exists — including the durable subtask fan-out (coordinator +
`wake_runner`/`children_done`) — so this is wire + extend + unify, with a few
genuine new builds. Ordered so the first slice ships the original goal and proves
the come-back contract end-to-end:

1. **Relax-as-subtask on spark** — cache-first `struct_relax` (§23.12); podman +
   CDI + deterministic `--name`; write the run-cube + CoW snapshot; per-job
   stuck-timeout override on the sweeper (#1); node- + parent-scoped claim gates
   (#3); container-name kill in the sweeper (#6); come-back via `children_done`.
   Cluster: `roles/dft` (GPU image + PAW on NFS + podman/ssh + `has_gpaw`). Needs
   none of slices 3–7. *(Smallest valuable thing; mostly wiring.)*
2. **Structure unification** — one canonical kind (`struct_*`); `precis-dft`
   becomes overlay job_types. Hard blocker before `precis-dft` deploys (the kind
   collision).
3. **Durable-subtask polish** — explicit control actions (§23.5) + `resume_brief`
   + suspend checkpoint + join `each`/`quorum` + envelope standardisation.
4. **Ephemeral fan-out primitive (§23.15)** — the in-process bounded, structured,
   blocking+budgeted map (no per-child job rows) that Tier-2 search needs.
5. **Model router + `local_only`** — role/tier per incarnation (the cube +
   `PRECIS_CURRENT_MODEL` hints exist); then sensitivity (coarse + fail-closed +
   segregated index). Note: `local_only` LLM work is **blocked on #7**.
6. **`sync_agent` hold path** — hold-and-poll + timeout→promote + the rootless
   ephemeral-job affordance.
7. **Model-agnostic `AgentRunner`** — drive litellm/Qwen through the MCP surface +
   the yield/resume/`agentlog` contract. **Shared keystone** of `local_only` LLM
   ticks *and* the Tier-2 fleet (#4 in §23.15) — candidate to pull earlier than
   its number.

The honestly-hard bits: the GPAW container (toolchain pain, but the in-container
runner already exists), `local_only`/IFC (scoped down, not solved), the
`AgentRunner` (a real project), and the kind collision (must resolve before
`precis-dft` ships).

#### 23.14 Backends are submit+poll adapters (ssh_node, SLURM, …)

A future SLURM backend is **an adapter, not a rewrite — if the executor interface
is submit-oriented now.** SLURM is *submit-then-poll* (`sbatch` returns instantly;
the job queues for hours; the outcome comes from `sacct`), so it is **not** an
`ssh_node` sibling (which *blocks* a worker for the whole run). It slots into the
§23.2 `wait` class: a **submit executor** (`sbatch`, record the id, **yield**) + a
**`slurm_done` wait-evaluator** (polls `sacct`). Results via the shared FS (the
existing NFS staging); cancel via `scancel` (the §6 cascade for free); placement =
cluster+partition+gres; runtime = Singularity/Apptainer, not podman.

**Guardrail to lock now:** define the executor as `submit(spec) → handle` + a
backend-specific done-evaluator, with `ssh_node`'s blocking run as the *degenerate
synchronous case*. SLURM also forces the right form of #1 — **sweep by the job's
declared/observed state, never by tag age** (you can't time-out a PENDING SLURM
job by elapsed time).

#### 23.15 Two fan-out substrates — durable vs ephemeral (Tier-2)

There are **two** fan-outs, and conflating them is the mistake the Tier-2 search
workload exposes:

| | **Durable** (already built) | **Ephemeral** (new) |
|---|---|---|
| substrate | coordinator + `wake_runner`/`children_done` | in-process bounded map |
| per child | a `kind='job'` row + claim + boot | a semaphore-gated task, **no row** |
| returns | `job_summary` text | **schema-validated structs** |
| call | async yield/resume | **blocking, budgeted, partial-on-exhaustion** |
| log | per-job `agentlog` | **one roll-up `agentlog`** |
| for | heavy / long / must-survive | cheap stateless N-way (triage / judge / verify) |

The ephemeral primitive *is* the Workflow `parallel`/`pipeline` engine — bounded
concurrency, `schema=`, `budget.remaining()`, `_recoverable_exhaustion` — **exposed
as a runtime primitive a driver agent can call**. Boundary rule: stateless-cheap-
many → ephemeral (no rows); durable / long / per-item-retry → the job substrate.
The two new builds it needs are the **map** (no-row gather) and the
**provider-agnostic runner** (§23.13 #7 — the same `AgentRunner`).

This split is **empirically validated**: the hold-vs-resurrect lit review found
fresh contexts win for *parallel read-heavy* work (= Tier-2 triage/judge/verify)
and warm context wins for *tightly-coupled write* (= the planner) — Mem0 trades
~6 accuracy points for ~90 % fewer tokens / ~91 % lower latency; its "keep durable
structured state out of context, re-retrieve on demand" guidance is exactly the
`{status, handle, hint}` + cube model. So: build both, route by regime.

#### 23.16 The run-cube is the cache — memoise, don't recompute

DFT/MLIP results are (near-)deterministic in `(structure_sha, params_hash,
code_version, model)`, and the §12 cube already keys on exactly those. So **spawn
is cache-first**: look up the cube; an exact hit returns the run handle with zero
compute; a miss dispatches. The §19 import path pre-fills the same cube.

Invalidation is a **non-problem** (A2): the cube is **append-only**. We always
simulate a *locked snapshot*, so a new simulator/model is just a **different cube
cell** — old runs are retained and served. A `(structure+params)` match with an
older `code_version` is a **hit-with-a-staleness-note** ("computed with simulator
vX; current vY"), not an invalidation. Parameter changes (temp, radius, kpts) →
new `params_hash` → a new run.

The cube also powers **one estimator, three uses** (B6/B7): from priors over
similar runs (N atoms × fidelity ≈ X GPU-h) → (a) an **ETA** shown to the LLM, (b)
an **overrun warning** (with the log-tail + log-file mtime surfaced via `job get
status` as a liveness signal), and (c) an **exhaustion gate** on pathological specs
("estimated 3 weeks — confirm or refuse"). Param-sanity is a cost estimate, not a
hard cap.

#### 23.17 Decisions resolved + code-grounding (v2, 2026-06-30)

All eight load-bearing code claims were verified against source (not summaries).
**Holds:** the `auto_check` registry + `evaluate(store, spec, *, ref_id)`
signature; the `child_job_succeeded` guards; the dispatch mint +
`_SELF_RESOLVING_JOB_TYPES` injection-strip; the `agentlog` API
(`open_log`/`finalize_log`/`touch_from_env`/`PRECIS_CURRENT_AGENTLOG`);
`structure_save` (no CAS). **Corrected into the text:** the durable barrier is
`children_done`/`wake_runner`, not the `any` `child_job_succeeded` auto_check (B5,
§23.9); there is no parsed verdict — control is via explicit MCP actions (§23.5);
jobs claim FIFO but `refs.prio` exists to reuse (§23.6); `ssh_node` checks cancel
only pre-run and launches an unnamed `docker` (§23.12 + §6).

**Operator decisions folded in:** explicit control actions (§23.5); prio-ordered,
no preemption (§23.6); thin `struct_relax` reusing pure helpers (§23.12);
cache-first spawn + append-only cube + the estimator (§23.16); result-trust
YAGNI-beyond-backend + LLM-owned retry (§23.12); the two fan-out substrates +
Tier-2 consumer (§23.15); the tree stays hierarchical — diamonds use sibling-scoped
`children_done`, shared results dissolve into cube lookups (C9); adopt-as-head is a
synchronous CAS, deferred, no messaging (C10); per-job stuck-timeout (#1); node- +
parent-scoped claim gates (#3); kill-handle + flag-driven cascade + deterministic
names (#6); the submit+poll adapter guardrail (§23.14).

**Parked / open:** half-completed-tick re-mint idempotency (#10 — `idem_key` is the
lever); cross-tree fairness/quota (#8 — YAGNI); the hold-vs-resurrect *threshold*
(#12 — tune via an eval; the lit review gives the priors, and notes the live A/B of
hold-vs-reset doesn't yet exist in the literature — the one real gap).

## Phasing

- **v1 — the legible IR + the read/write loop, all core (pure, numpy-only) bar
  the rented relaxer:**
  - **Store/graph** — `struct_atoms` / `struct_bonds` (+ `struct_bond_atoms` for
    N-ary/delocalized bonds) / `struct_measures` tables; design as a `refs` row +
    one card chunk (§4, §12). Å/eV/e/μ_B, fractional storage, MIC, per-axis PBC,
    place-outside-wraps-inside (§3). IR holds any element.
  - **Write** — the op catalog (§5): single + bulk inserts (sheet/cube/line,
    `max_atoms` cap), **flood-and-carve + grow-from-anchor** (§5b), bond surgery,
    charge/oxidation/constraint ops, `from_smiles`/`recipe`/combine builders (§16).
  - **Read (§6)** — probes (6.1–6.4); the five nav primitives (6.5, with the
    **field-aware void finder**); the **embodiment/POV** model (6.6) + **lenses**
    (6.7, incl. composable TOON columns) + **cursors + dashboard** (6.8);
    **relax/NEB trajectory** keyframes + time-series + scrubbing (6.9, MD deferred).
  - **Measures (§7)** — per-atom/per-bond/**structure-scalar** (energy/eads) +
    persisted observers; **bond strain = lengthwise + angular** (§8a) +
    force-projected (§8b).
  - **Relax (§9)** — the fidelity ladder rungs **0 (geometry-clean, ours) + 3
    (ML) + 4/5 (GPAW)**; `neb` (ML→GPAW) as relax's transition-path sibling.
  - **Fork/sequence (§15)** — copy-on-write lineage (`derived-from` + ops + a
    `family_id`), `structure_tree` + `compare`/`pathway` views; rides the
    existing todo-tree + coordinator (no new orchestration here).
  - **Files (§10, §13)** — CIF/POSCAR/extXYZ ingest + export; **molecule mode**
    (`pbc=[F,F,F]`) + MOL/SDF/PDB; frozen `structure:<sha>` commit.
  - **Ensemble (§18)** — experiments-as-points (axis coordinates: composition /
    sim-params / conditions), **semantic per-experiment event cursors**, the
    `compare`/slice/sweep verbs over the sparse fact table. (Embedding +
    proposal = spec.)
  - **Import (§19)** — the importer family (Materials Project + Open Catalyst
    first; NOMAD/OQMD/AFLOW follow), external → `structure` + result +
    coordinates + provenance/trust; ingest-only.
- **Phase 2**: fidelity rungs **1/2 (FF/xTB)**; quantum bond order + Bader
  partial charge as a spec `derive_*` (§8c); **field lenses needing electrons**
  (ESP/spin/d-band/Fukui reactivity); SMILES/InChI + **SMARTS** (cheminformatics
  plugin); **symmetry-aware orbit-wise ops**; constrained relax against `soft`
  measures; **MD trajectories** + the rolling-probe/NEB-image embodiments;
  interstitial-annotator depth; LAMMPS-data + quotient-graph export; a 2D
  sketch-to-slab builder; charge methods beyond Bader; embedding-space hooks
  (spec).

## Non-goals

- **3D / pixel rendering** of the crystal in the design loop (the spec's
  `ascii_top`/`ascii_side` are **explicitly dropped** — render elsewhere for a
  human; the agent reads structure). Layer-slice SVGs are exact planar geometry,
  not a render, and stay.
- **Owning a relaxer or an energy model** — rent ASE + MACE/CHGNet/GPAW (§9).
- **DFT physics defaults, the campaign coordinator, failure-recovery,
  reaction/Pourbaix/volcano analyses, embedding spaces, the "what to run next"
  proposal** — all the `precis-dft` spec's domain. 0043 is the atom-organizer IR
  + the *navigable* representation of the ensemble (§18); the spec decides what
  *fills* it.
- **Bonds as a physics input** — bonds are authoring + inspection intent; DFT
  consumes positions + cell (§8).
- **Mirror as a tiling mode** — tiling is translation (PBC); mirror is a
  one-shot build op (§wording).

## Consequences

- **Good**: the LLM gets a *legible, graph-shaped* structure at a fraction of a
  render's tokens, **playing to graph-manipulation, not visual cognition** —
  symmetry-reduced TOC, atom/bond config, exact MIC geometry, the embodiment/POV
  model (be an atom, a ring, a fragment, a field), composable lenses, a named-
  cursor dashboard, and trajectory time-series. **Bonds are intent, the physics
  is rented**: put atoms+bonds in, relax, it fixes itself, and real bonds/
  charges/strain come back as feedback (§8.1). Periodic bonds are exact via the
  image-offset triple (corner case and all); **strain is lengthwise + angular**;
  **charge is declared-vs-derived**. The **fidelity ladder** makes the inner loop
  near-interactive (rung 0/ML) and the confirm real DFT, escalating only
  survivors; **NEB + the `pathway` view** give kinetics and a *why* for the
  bottleneck step. **Fork lineage + the todo-tree coordinator** express the whole
  build→fork→simulate→resume→compare workflow with no new orchestration. It is
  **one IR for crystals *and* molecules** (PBC off), with lossless bond round-
  trips via MOL/SDF. **Dedicated tables** give SQL + FK integrity and **no
  embedder load**; the IR is core/pure, heavy backends are the plugin; reuses
  0041/0042's probe + measure + observer + export machinery wholesale.
- **Cost / risk**: we own a PBC-aware graph + geometry evaluator + the navigation
  surface (bounded by "compute exactly or label an estimate"); the declared graph
  can drift from relaxed geometry (a *feature* via the DRC contradiction probe,
  but a surface to read); a relax only fixes *local* stupidity, not a wrong
  topology (§8.1) — the diff/DRC must tell them apart; symmetry/orbit + the
  environment-fingerprint reductions depend on tolerances (fall back to per-atom
  rows, `log()` it); field lenses + quantum bond order + Bader charge need a DFT
  single-point (blank until then); the relaxer/NEB are plugin-gated, so the core
  IR authors anywhere but only relaxes where a backend is installed; the §6
  navigation surface is broad — a real risk of *scope*, mitigated by everything
  past the probe ladder being pure reads over the same tables.
- **Deferred** (see Phasing phase 2): FF/xTB rungs, quantum bond order, electron-
  needing field lenses, SMARTS, orbit-wise ops, constrained relax, MD
  trajectories, interstitial depth, LAMMPS/quotient-graph export, the sketch
  builder, embedding hooks.

## Open questions

1. **Handle code — RESOLVED: `st` (STructure).** `sx` was an arbitrary
   placeholder; the registry convention (`src/precis/utils/handle_registry.py`)
   is a 2-char **mnemonic** (`paper`→`pa`, `draft`→`dr`, `todo`→`td`, `cad`→`cd`),
   and **`st` is verified free** in both `KIND_CODES` and `CHUNK_CODES`. Register
   `"structure": "st"` at implementation. `structure:<sha>` is a separate
   *citation* key for the frozen geometry, not the `st<id>` ref handle. *(Sibling
   DFT kinds — `material`, `reaction_network` — take their own free mnemonics when
   they land.)*
2. **Default bond detection — RESOLVED.** The LLM **always sees the best image
   of reality we can muster** — so bonds are **auto-detected on ingest** (cutoff
   / CrystalNN, refined by DFT where available), **never withheld for purity**.
   Every bond carries a **provenance flag** (`declared` = LLM intent · `inferred`
   = derived from geometry · `dft` = from COHP/Bader) so inferred edges are
   *marked*, not hidden (§4). **`bond_order` is the universal continuum** — one
   field spans van-der-Waals (≈0/partial) → single → double → triple → aromatic
   → metallic, declared coarsely and **refined to a more specific value by DFT**.
3. **Per-atom vs per-orbit editing — RESOLVED: per-atom.** An edit touches
   **exactly the named atom**; the resulting **broken symmetry is a feature** (a
   single dopant / vacancy / asymmetric adsorbate is the science). `orbit:` is an
   explicit opt-in for the rare symmetric-substitution case only — never the
   default. (Auto-applying would also be an illusion: a DFT relax can break the
   symmetry anyway.)
4. **Measure mini-DSL — RESOLVED: not needed.** "Any adsorbate within 3 Å of any
   `hot` site" is a **spherical probe + class filter** (§6.2/§6.6), not a new
   DSL — compose existing probes/lenses.
5. **Sub-row handles.** Address atoms/bonds only by design-scoped path
   (`st7#aPd123`), or also mint per-row 0036 handles so a `gripe` can point at a
   single atom? Path-scoping is simpler and matches chemical identity (0042 Q6).
   *(Distinct from the long-gap/"cold-storage" question, now §15.1.)*
6. **Relax placement — RESOLVED: run where the backend is.** Interactive rungs
   (0–3) run **in-process on a worker that has the potential** (local on a prod
   node — "ssh" is just the cluster-submit transport for the heavy DFT rungs,
   not a remote hop once the worker is *on* the node); **dev = a container with
   the `[dft*]` extras** (to be added alongside the existing precis containers).
   Extras-gated (§20).
7. **§6 scope + navigation-vs-output — OPEN, needs a pass.** Two things: (a) the
   v1 floor (proposed: probes 6.1–6.4 + the `pov` readout + scalar lenses + a
   single focus; cursors/dashboard + field lenses fast-follow); and (b) a
   **cleaner taxonomy** — some §6 items are *navigation* (move the focus), some
   are *reads* (data at the focus), some are *outputs* (a plot/SVG/export
   artifact). Separating those three is the discussion to have before build.
8. **`compare`/`pathway` — core or campaign?** Placed in 0043 (pure reads over a
   lineage) so the interactive loop can compare/reason without a campaign;
   confirm that's the right side of the 0043↔spec line.
9. **Energy as a measure — RESOLVED: a run-joined, status-tagged value.**
   Structure-scalar quantities (total/binding energy, eads) are **not** computed
   from geometry — they **join the relevant run(s)** (binding energy joins the
   reference runs). You can always *ask*; the answer carries a **status**:
   `uncomputed` (`—`, no run) · `pending` (run in flight) · `<value> eV` (ok) ·
   `diverged` (the genuine NaN — computed-but-meaningless). **Bare NaN is reserved
   for `diverged` only** — "never ran" (`—`) and "ran and failed" (NaN) are
   distinct because they demand opposite next moves (run it vs retry/fix). Maps to
   `runs`: `energy` NULL = uncomputed; value + `converged` = ok; value/NaN +
   `¬converged` = diverged; open job = pending.
10. **Keyframes/events — RESOLVED: durable, documented predicates.** Both
    **auto-generated and LLM-definable/selectable**; a keyframe is a **portable
    predicate** (declarative or *code*) that **persists across cloning/forking**
    and is documented well enough that a **small agent re-resolves it after a new
    run** (re-points to the right frame). Detection of bond break/form uses a
    **threshold + hysteresis**, **case-by-case**, **defined by the LLM after
    inspection + paper review**, **redefinable over a set** (and *between cursors*
    that themselves span sets). The bond-strength-over-time graph is shown as a
    **segmented series** — intervals collapsed at inflections, each `mean ± sd`
    with a trend + event marker (a better form than a per-frame dump), e.g.
    `−5.0…−2.1 ps │ 32±5 eV │ flat` / `−0.3…0.0 ps │ 80±50 eV │ spike ⚠break`.
11. **Ensemble navigation home (§18) — RESOLVED (§18.1).** The experiment space
    is a **sibling star-schema** (`dft_runs` fact + `dft_params`/`dft_conditions`
    dims + shared `structure:<sha>` + `dft_frames`/`dft_events`), **not** refs and
    **not** a `kind='structure'` view; **position state lives on the driving
    todo/campaign's `meta`** (the §15 thread), not on a ref. Refs stay only for
    search-by-intent designs / materials / campaigns. (Remaining sub-question: the
    exact seam to the spec's embedding/proposal over the same fact table.)
12. **Event detection trust — folded into Q10.** Auto for the cheap structural
    ones (bond break/form via threshold+hysteresis, plane crossing); LLM-declared
    (predicate or logged intervention) for the rest; stored as the portable,
    self-documenting predicates of Q10 that re-resolve per run.
13. **Import scope & schema drift (§19) — ACKNOWLEDGED as a major sub-project.**
    "Massive and hard; do it properly." Its own phase (§21): robust per-source
    adapters (MP + OC22 first), provenance/trust, resilience to external
    schema/API drift, and per-source licensing/attribution. Not a side task.

## Known weaknesses, risks & loose ends

*(Distinct from Open questions, which are decisions to make. These are soft spots
and risks to track — the honest self-critique.)*

- **Altitude mismatch — vision vs v1.** The navigation surface (§6.5–6.9), the
  ensemble (§18), and import (§19) are the doc's centre of gravity but are **not
  v1** (§21). Risk: over-building the cathedral before the shed works. Mitigation:
  an explicit *v1 / vision* split, keeping the practical navigation magic in v1
  (the geometry+graph+forces line, scoped pre-leveling) and tagging the rest.
- **The precis-dft port — assessed *decent* (Reto, 2026-06-28).** §20's "reuse
  the science / supersede the representation" looks tractable on a read of the
  package (237 tests; the pure-function `structures` / `annotator` / `ops` /
  `workbench` / `reactions` / `campaign` layers lift cleanly; the real work is
  re-basing storage from `meta.poscar` + view-dicts onto the `struct_*` tables —
  handlers + `view_worker`). Residual, to settle *during* implementation: the
  **`structure` vs `structure_draft`** reconciliation (one kind + a frozen flag,
  vs two kinds). Handle code is **resolved** (`st`, Open-Q 1).
- **Frame/trajectory storage — RESOLVED (§12).** `runs.convergence` (scalar
  curve) is always stored; `traj_ref` (the engine `.traj` blob) is always stored;
  geometry `frames` rows materialize **only for MD/NEB or lazy debug**, not for a
  routine relax; per-atom outputs are run-scoped (`run_atom_results`). The
  glossary/§18.1 conflict is gone. *(Remaining at scale: OLAP partitioning over
  millions of imported runs — a Phase-C/§19 concern, not v1.)*
- **Field-dependent navigation is mechanism-vague *and* DFT-gated.** Field lenses
  / field embodiments (ESP, spin, Fukui) need a stored **3D volumetric grid** —
  storage + a *numeric, non-pixel* LLM representation are unspecified, and they
  are blank through the entire MLIP loop. Post-v1.
- **Ensemble `gaps` needs a candidate model.** Enumerating *absent* cells in a
  huge sparse space isn't free; it leans on the spec's proposal. §18 understates
  this.
- **Semantic event predicates "possibly code."** Language, sandboxing, and robust
  cross-structure re-resolution are undefined; only the threshold+hysteresis
  bond-break case is concrete (§6.9 / Open-Q 12).
- **Cursor-storage — RESOLVED (§12/§18.1).** Per-structure cursors [v1] are
  `struct_measures` rows (anchor FK + embodiment spec, members derived);
  cross-experiment cursors [vision] live on the exploration todo's `meta`, reusing
  the same `embodiment` coordinate shape. No conflict.
- **Physics under-specification.** Inferred bond *order* (vs mere connectivity) is
  unreliable pre-DFT for metals/surfaces; charged periodic cells (jellium /
  Makov–Payne) are glossed under `net_charge`; molecule open-boundary vs
  big-box-with-vacuum is glossed.
- **Validator gate / error envelope — RESOLVED (§5c).** Now part of the Edit
  contract: a pre-commit gate (rule + value + `suggested_fix`) + MLIP guardrail,
  and the `{ok,ref,warnings}` | `{error:{…,suggested_fix}}` envelope.
- **Async boundary — RESOLVED.** Construction-v1 (Phase A) is **synchronous**
  (fast in-process MLIP relax) and does **not** need the coordinator/wake (§15);
  async yield/resume enters only at the DFT phase (B+) — confirming Phase A's "no
  cluster, no Phase-0-PR3 dependency" claim (§21).

## Glossary

The coined vocabulary, grouped. The four the reader most often conflates —
**design / structure / experiment / ensemble** — are defined first.

**The nested scales (smallest to largest):**

- **Cell** — the periodic box: three lattice vectors `a,b,c` + per-axis `pbc`
  flags. The geometric container atoms live in (§3).
- **Design** (`kind='structure'`, handle `st7`) — *an editable structure you are
  working on*: a cell + atoms + bonds in the `struct_*` tables + one card chunk.
  Mutable; the workbench. Forking one makes another (§4, §15).
- **Frame** — **one frozen geometry snapshot** (the atoms at one instant of a
  relax/NEB/MD step). The atomic unit of the time axis (§6.9).
- **Structure (frozen)** (`structure:<sha>`) — a **single, immutable,
  content-addressed** geometry — i.e. *one frame worth naming*: typically the
  **input** geometry and the **converged** geometry of a run are each frozen and
  **shared/cited** (every experiment that used that exact geometry points at the
  same sha). Most intermediate frames are *not* frozen — they live as bulk
  trajectory rows (§10, §18.1).
- **Experiment / run** (a `dft_runs` row; precis-dft's `dft_calculation`) — **not
  one frozen structure but a whole *trajectory of frames*** (the request's
  correction): a (input structure × sim-params × conditions) **evolved** → an
  ordered sequence of snapshots (`dft_frames`) + the result scalars (energy,
  forces, derived) + provenance. The run references its input/converged
  `structure:<sha>` and **owns** the frames between them. It is **one filled cell
  of the cube**; the **time axis indexes frames *within* a run**, not across runs.
  "Experiment" is the colloquial word for a run (§18, §18.1).
- **Ensemble** — **the whole collection of experiments** (yours + the imported
  giants); the populated/filled cells of the cube (§18, §19).

**Space, time, and the cube:**

- **Axis / dimension** — one degree of freedom of an experiment: structure/
  composition · sim-DFT-params · conditions · time (§18).
- **Cube** — the **N-dimensional sparse hypercube** of *all possible*
  experiments (a data-warehousing term, not a literal 3D box). The ensemble = its
  filled cells; stored physically as a **star schema** (`dft_runs` fact table +
  `dft_params`/`dft_conditions` dimension tables), not a dense grid (§18.1).
- **Cell (of the cube)** — one coordinate-combination; a *filled* cell holds a
  run. (Distinct from the *unit cell* above — both are called "cell" by
  convention; context disambiguates.)
- **Frame** — one geometry in a trajectory (a relax/NEB/MD step); the time axis
  (§6.9).
- **Trajectory** — the ordered frames a run produced (§6.9).
- **Workspace** — a **saved focus / working-set** over the one space (a campaign
  scope, a bookmark set, a cursor region) — *organizational, not a data
  partition* (§18.2).

**Identity & addressing:**

- **Handle** — a stable id; the design ref's is `st7` (ADR 0036).
- **Design-scoped handle** — an atom/bond id unique *within its design*, written
  `st7#aPd123` (= atom `aPd123` in design `st7`). **Not globally unique**: `st9`
  may have its own `aPd123`; the `st7#` prefix is the scope. Bonds:
  `st7#aPd123~aCu44[1,0,0]`; sites: `st7@fcc_hollow_4` (§4).
- **Image offset / `to_jimage`** — the integer triple `[nx,ny,nz]` on a bond =
  the lattice translation of the partner's cell; how a bond crosses a wall (§4.1).
- **PBC** — periodic boundary conditions: the cell tiles by **pure translation**
  (not mirror) (§3).
- **MIC** — minimum-image convention: a distance is to the *nearest* periodic
  image (§3).

**Intent vs derived (the recurring split, §8):**

- **Declared** — LLM-authored *intent* (bond order, hybridization, oxidation
  state, net charge). Seeds builders + sanity checks.
- **Inferred** — auto-computed from geometry on ingest (e.g. detected bonds),
  **marked** as such (Open-Q 2).
- **Derived** — read back from a relaxed/computed result (coordination, partial
  charge, force, strain). Blank until a run exists.
- **Bond order** — the universal bond-strength scalar (vdW→single→…→metallic),
  declared coarse, refined by DFT (§8).
- **Strain (lengthwise/angular)** — a **heuristic** distance/angle deviation;
  *not* a QM observable (the real per-atom observable is the HF **force**) (§8/§22-A).

**The editing & relax loop:**

- **Op** — a typed edit (`add_atom`, `vacancy`, `slab`, `attach`, …) the LLM
  emits; the framework applies + re-derives (§5).
- **Fidelity ladder / rung** — the rented relax tiers 0 (geometry-clean) → 1 FF →
  2 xTB → 3 MLIP → 4 DFT-fast → 5 DFT-tight; climb as the model matures (§9).
- **MLIP** — machine-learning interatomic potential (MACE/CHGNet/Orb), the cheap
  near-interactive relax (rung 3); also a pre-DFT guardrail (§9, §22-B).
- **NEB** — nudged elastic band: the transition-path / barrier finder, relax's
  sibling on the ladder (§16-D, §22).
- **Validator gate** — the microsecond pre-compute check (sub-covalent length,
  over-valence, overlap) returning a structured `suggested_fix` (§22-B).
- **Fixed / constraint** — an atom pinned (used sparingly; the locked-atom
  convergence trap, §22-D).

**Reading & navigation (§6):**

- **Probe** — an exact query at the focus (neighbourhood, line, plane,
  bonds-through-plane, distance) (§6.1–6.2).
- **Lens** — a property the structure is read *through* (charge, spin, strain,
  reactivity, …); composable TOON columns / field overlays (§6.7).
- **Measure** — a named metric with a direction-of-goodness + strength (`hard`/
  `soft`/`gauge`), persisted and re-evaluated (§7).
- **Observer** — a persisted saved query (a `gauge` measure) (§7).
- **Embodiment / point-of-view** — the cursor *as* something: a **`support`**
  (what I am) + **`reach`** (how far I touch) → uniform `i_am / i_include /
  i_touch`. Be an atom, a ring, a fragment, a field (§6.6).
- **Cursor** — a persisted, named embodiment with a `for` (purpose); many at once
  on a dashboard; a (partial) coordinate into the cube (§6.8, §18.1).
- **Focus** — the active cursor / "you are here" (§6.5).
- **Keyframe / event** — a portable, documented predicate (e.g. `@H_lost`) that
  resolves to *that run's* frame; aligns trajectories across experiments (§6.9, §18).
- **DRC-lite** — rule-check warnings (overlaps, dangling/under-coordination,
  hybridization-vs-geometry mismatch) (§6.4).

**The collection & overlays:**

- **Fork / lineage / family** — a copy-on-write derived design, `derived-from`
  its parent with the edit ops; a `family_id` groups sibling forks (§15).
- **Reaction network** (`reaction_network`) — an **overlay graph** of
  intermediates + steps that *references shared* structures; not an axis (§18.2).
- **Material** — a curated catalyst aggregate (a search-by-intent ref) (precis-dft).
- **Provenance / trust** — a run's source + foreign sim-params + `imported`/
  `confirmed` flag; what guards cross-setting comparison (§19).

**Boundaries:**

- **precis-mcp / precis-dft** — one merged package now (§20); the IR is core, the
  heavy backends are `[dft*]` extras.
- **ASE Calculator** — the engine-agnostic relax contract (GPAW default; VASP/QE
  pluggable) (§9, §22-H).
- **The spec** — `~/.claude/plans/we-have-a-cluster-hidden-bird.md`: physics
  defaults, campaign, electrochemistry, embedding, proposal — deferred-to, not
  redefined here.
```
