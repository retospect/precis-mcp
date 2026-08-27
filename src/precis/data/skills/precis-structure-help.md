---
id: precis-structure-help
title: precis — the structure kind (atomistic cells you can read as a graph)
summary: build a periodic cell + bond graph as typed ops, then probe it analytically (neighbours/coordination/line/plane/sphere/path/rings/fragments/diff/pov), relax it on a fidelity ladder (clean→ml→dft), and export POSCAR/extXYZ/CIF — no pixels; the graph + numbers are the interface
answers:
  - how do I build an atomistic cell and bond graph as typed ops?
  - how do I relax a structure through the fidelity ladder (clean to ml to DFT)?
  - how do I probe coordination, rings, or fragments in a structure?
  - how do I import a structure from an external catalyst database?
  - how do I export a structure to POSCAR or CIF?
  - does it matter where in the cell I put a dopant?
  - what does a 3×3 slab supercell mean for coverage?
  - how do I place an adsorbate on a real site (top/bridge/hollow) without guessing a z coordinate?
applies-to: get/search/put/edit/delete (kind='structure')
status: active
---

# precis-structure-help — atoms the LLM can *read*

A `structure` design is a **periodic cell filled with atoms + an explicit
bond graph** (ADR 0043) — the materials sibling of `cad` (0041) /
`pcb` (0042). You author it as **typed ops** and, instead of staring at a
render, **probe it as a graph + numbers**: "what's bonded to this atom?",
"what's under this adsorbate?", "which bonds cross the cleavage plane?",
"did that edit fragment the slab?". Postgres is canonical; the active design
is a small in-memory object, so every probe is exact and instant. Lengths
are **ångström**, positions are **fractional** (cell coordinates).

Seven verbs, no new ones: `put` (create/replace), `edit` (apply ops / relax),
`get` (list / TOC / probe / nav / runs / export), `search` (by **intent**),
`delete` (soft-retire), plus `tag`/`link`.

## Handles & atom labels

- A design has an `st<id>` handle (shown in the TOC).
- Each atom is `a<El><n>` — `aPd1`, `aPd2`, `aO1`, `aH7` — **minted in order
  per element and never recycled** (a vacancy doesn't free its label).
- Design-scoped path: `st7#aPd123`. Atoms in different designs are unrelated
  even with the same label.
- **`set_element` keeps the original label.** Transmuting `aPd28` → Cu leaves
  it named **`aPd28`** (now a Cu atom) — it does **not** become `aCu…`. Refer to
  it by its original label in later ops/eyes/measures; there is no `aCu28`.
- A bond crossing a cell wall carries a **periodic image offset** `[i,j,k]`:
  `aPd1 — aPd2[−1,0,0]` bonds aPd1 to aPd2's image one cell back along **a**.

## Author a design — `put(id=<slug>, text=<JSON>)`

The payload is JSON: a **cell** + a list of **ops**. Atoms wrap into the
cell, so you can place one *outside* `[0,1)` and it folds back in.

```python
put(
    kind="structure",
    id="pd111",
    text="""{
  "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [true, true, false]},
  "description": "Pd(111) 3-layer slab for OH adsorption screening",
  "ops": [
    {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.20]},
    {"op": "add_atom", "element": "Pd", "frac": [0.33, 0.33, 0.30]},
    {"op": "add_atom", "element": "O",  "frac": [0.16, 0.16, 0.45]},
    {"op": "add_atom", "element": "H",  "frac": [0.16, 0.16, 0.52]},
    {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
    {"op": "constrain", "atoms": ["aPd1"], "kind": "fixed-all"}
  ]
}""",
)
```

- **`cell`** is either `{a,b,c,alpha?,beta?,gamma?,pbc}` (lengths/angles, °)
  **or** `{lattice: [[…],[…],[…]], pbc}` (an explicit 3×3, Å). `pbc` is a
  per-axis `[bool,bool,bool]`; a hand-authored vacuum-gap cell conventionally
  sets `[true,true,false]`. The **`slab` op** ignores this — it always emits
  `(true,true,true)` (ASE's fcc111 convention; the vacuum gap is geometric,
  not a pbc flag).
- **A periodic cell tiles — a lone defect has no absolute position.**
  Placements that differ only by a lattice translation (or an in-plane
  rotation/mirror) are the same crystal; only the relative offset between
  species is physical, capped by the cell size. Quest candidate structures
  are stored canonicalized into one reference frame
  (`precis.structure.canonical`).
- **`description`** makes the design findable by purpose (folded into the
  one search card). Optional, recommended.
- `put` applies the ops eagerly and echoes the TOC, so a bad op surfaces
  immediately. Re-`put`ting a slug **replaces** it (old atoms/bonds
  soft-retired, recoverable). Atom labels are minted in op order.

### The ops (also used by `edit`)

| op | args | effect |
|----|------|--------|
| `set_cell` | `lattice` or `a,b,c,…` + `pbc` | redefine the cell |
| `slab` | `element`, `size:[nx,ny,nz]`, `vacuum?`, `fix_layers?`, `a?` | **bulk template** — build an fcc(111) metal slab; **clears the scene** and sets the cell + `pbc (true,true,true)` (ASE-exact atom order, so autocatpath can inject it). Omit the top-level `cell`. `size` is `nx×ny` in-plane repeats × `nz` layers — the cell tiles, so one substitution = 1/(nx·ny) ML coverage. `fix_layers` is an **integer count** of *bottom* layers to freeze (`2` = bottom two layers), **not** a list of layer indices. |
| `add_atom` | `element`, `frac:[fa,fb,fc]` | place an atom at a raw fractional coordinate (wraps into the cell) — an escape hatch; for an adsorbate on an existing site, use `add_atom_site` instead |
| `add_atom_site` | `element`, `site:{type:"top"\|"bridge"\|"hollow", anchors:[atom labels]}`, `height?` | place an atom by **naming** a site (1/2/3 anchors) instead of guessing coordinates — `xy` = the anchors' centroid, `z` = the anchors' top + `height` (Å; default the covalent-radius sum of anchor + placed element) |
| `set_element` | `atom`, `element` | transmute — **keeps the atom's label & position** (see caution below) |
| `vacancy` | `atom` | remove an atom (label not recycled) |
| `displace` | `atom`, `vector:[dx,dy,dz]`, `cartesian?` | nudge (Cartesian Å by default; `cartesian:false` for a fractional delta) |
| `add_bond` | `i`, `j`, `order?`, `image?:[i,j,k]` | declare a bond (intent) |
| `remove_bond` | `i`, `j` | drop a declared bond |
| `constrain` | `atoms:[…]`, `kind` | `fixed-x|y|z|all` — freeze axes (use sparingly) |
| `eye` | `name`, `atoms:[…]`, `reach?`, `for?` | drop/replace a named eye (§6.8) — see Eyes & measures |
| `measure` | `kind`, `atoms:[…]`, `direction?`, `goal?`, `strength?`, `for?` | pin a measurement with an optional graded goal (§7) |
| `unmark` | `name` | retire an eye by name |
| `remove_measure` | `kind`, `atoms:[…]` | retire a measure |
| `relax` | `fidelity?`, `steps?`, `model?` | terminal op — see the ladder below |

**Bonds are intent, not a DFT input.** Declare the bonds you mean; the
geometry gets fixed by `relax`, and DFT consumes positions + cell (bonds are
dropped on export, §8.1). Auto-detected bonds from geometry show up in
probes tagged `inferred` — you always see the best picture of reality.

## Place an adsorbate on a real site, not a guessed z

```python
edit(kind="structure", id="pd111", ops=[
    {"op": "add_atom_site", "element": "H",
     "site": {"type": "hollow", "anchors": ["aPd10", "aPd11", "aPd12"]}},
])
```

`z` comes from the anchors' own geometry, never a remembered constant — a
hand-picked `frac` copied from another cell doesn't transfer. Use raw
`add_atom` only when no anchor exists yet, and then derive `z` from *this*
cell: top-layer `frac z` + bond length / cell height. On the standard
Pd(111) `[3,3,4]`/vacuum-10 cell (height ≈16.74 Å, top layer `frac
z≈0.403`), on-surface hollow `z≈0.46–0.47` for a ~0.9–1.2 Å H–metal bond —
`frac z=0.66` looks plausible but sits H ~4.3 Å above the surface: it either
fails preflight as a floating atom or relaxes in 0 steps to a
subsurface-degenerate energy, not a real adsorbate.

## Edit — `edit(id=<slug>, ops=[…])`

```python
edit(
    kind="structure",
    id="pd111",
    ops=[
        {"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.55]},
        {"op": "add_bond", "i": "aO2", "j": "aPd2"},
    ],
)
```

Each edit bumps the design version. A graph edit invalidates any prior relax.

## Read the TOC — `get(id=<slug>)`

```python
get(kind="structure")  # list all designs
get(
    kind="structure", id="pd111"
)  # the TOC: formula · natoms · pbc · bonds · per-atom rows
```

The TOC is the **one round-trip overview**: cell card, composition, pbc,
bond count, fragment count, the last relax envelope (if any), and one row
per atom (element · fractional position · coordination · fixed). A symmetry-
reduced (Wyckoff-orbit) collapse is a later increment.

## Probe it — `get(view=…, args={…})`

All probes are exact, in-memory reads over the graph + geometry. Distances
and angles are **minimum-image** (MIC) — they see across cell walls.

### Graph & coordination

```python
get(
    ..., view="atom", args={"atom": "aPd2"}
)  # config + neighbour shell + coordination + fixed + per-atom |F| (if a run has forces)
get(
    ..., view="atom", args={"atom": "aPd2", "run": 7}
)  # pin a specific run's per-atom force (else: current-version run, else a cheap EMT estimate)
get(
    ..., view="neighborhood", args={"center": "aPd2", "radius": 3.0}
)  # the coordination shell within R Å
get(..., view="bonds")  # the whole bond list (order · kind · provenance · image)
get(
    ..., view="find", args={"element": "Pd", "undercoordinated": true}
)  # select atoms by predicate
get(
    ..., view="validate"
)  # the DRC gate: overlaps + over-valence + too-long bonds + fixes
```

### Spatial — the CAD ray / plane, retargeted to atoms (§6.2)

Geometry args are **Cartesian** (Å); accept a list `[x,y,z]` or a
comma-string `"0,0,5"`.

```python
# 1D — a ray: atoms within `radius` of the line, ordered along it (channels, columns)
get(..., view="line", args={"origin": [0, 0, 0], "direction": [0, 0, 1], "radius": 1.5})

# 2D — a layer slice: atoms within `thickness` of a plane, as labelled in-plane (u,v) coords
get(..., view="plane", args={"point": [0, 0, 5], "normal": [0, 0, 1], "thickness": 1.0})

# bonds that cross a plane — what stitches two layers (cleavage reasoning), image-aware
get(..., view="bonds_through_plane", args={"point": [0, 0, 5], "normal": [0, 0, 1]})

# bonds inside/crossing a sphere — the local bonding environment around a point
get(..., view="bonds_in_sphere", args={"center": [4.2, 4.2, 6.0], "radius": 3.0})
```

### Graph topology & diff (§6.1/§6.3/§6.5)

```python
get(
    ..., view="path", args={"a": "aO1", "b": "aPd2"}
)  # shortest bond path (or "disconnected")
get(..., view="rings", args={"max_size": 8})  # smallest cycles — find sp² 6-rings
get(..., view="fragments")  # connected components: "slab + 3 adsorbates"
get(
    ..., view="diff", args={"other": "pd111_v0"}
)  # vs another design: RMSD · per-atom move · bonds/atoms broken/formed
```

`fragments` answers "did this edit break the structure apart?"; `diff` is
the single most insightful view of what a relax (or an edit) actually did.

### Point of view — the embodiment readout (§6.6)

One uniform readout regardless of *what* you focus on — an atom or a
fragment (a ring from `rings`, a molecule from `fragments`):

```python
get(..., view="pov", args={"support": "aO1", "reach": 3.0})  # i_am=atom
get(
    ...,
    view="pov",
    args={"support": ["aC1", "aC2", "aC3", "aC4", "aC5", "aC6"], "reach": 3.0},
)  # i_am=fragment
```

Returns **`i_am`** (atom/fragment) · **`i_include`** (the support) ·
**`i_touch`** (everything within reach, nearest-first). `pov` is the
*stateless* readout; an **eye** is the persisted, named form — see below.

## Eyes & measures — persisted, re-evaluated markers (§6.8/§7)

Unlike a `pov` (recomputed each call), an **eye** or **measure** is *saved*
on the design and **re-evaluated after every edit/relax**, so its value +
verdict stay live. Anchors are atom **labels** (stable identity), so a marker
survives an edit.

```python
# a named navigation handle over a support set
edit(
    kind="structure",
    id="pd111",
    ops=[
        {
            "op": "eye",
            "name": "active_site",
            "atoms": ["aPd12"],
            "reach": 3.0,
            "for": "the reactive Pd",
        }
    ],
)

# a pinned measurement with a graded goal
edit(
    kind="structure",
    id="pd111",
    ops=[
        {
            "op": "measure",
            "kind": "distance",
            "atoms": ["aH1", "aPd12"],
            "direction": "target",
            "goal": {"target": 2.4, "tol": 0.1},
            "strength": "soft",
            "for": "keep the H bound",
        }
    ],
)

get(
    kind="structure", id="pd111", view="markers"
)  # all eyes + measures, live value + verdict
```

- **measure `kind`**: `distance` / `bond_length` (2 atoms) · `angle` (3) ·
  `coordination` (1). **`direction`**: `min|max|target`. **`goal`**:
  `{target, tol}` or `{min}` / `{max}`. **`strength`**: `hard|soft|gauge` — a
  `soft` failure is downgraded to a warning; `gauge` is a readout with no
  verdict. Retire with `unmark`/`remove_measure`.
- A marker whose atoms are later removed reads **`dangling`** (legible, not an
  error).

## Lineage — `link` (relate designs)

```python
# a derived design points back to its parent
link(kind="structure", id="pd111_h", target="structure:pd111", rel="derived-from")
```

`derived-from` (⇄ `derived-into`) records that one design came from another —
e.g. an LLM-proposed edit branched to a new slug. Read both directions with
the store's link queries; the web viewer renders the lineage.

**Design provenance — link a design to the paper(s) that motivated it**, with a
one-line rationale, so a material shows *why* it was made:

```python
link(
    kind="structure",
    id="pd111_cu",
    target="paper:yaghi-2023",
    rel="cites",
    note="Cu-doped this facet because Yaghi 2023 showed it lowers the N–O scission barrier",
)
```

The TOC and the web detail page surface these **paper-provenance** links (the
paper, its DOI, and your `note`); re-linking the same edge with a fresh `note`
updates it. Any paper-target link shows up, so you reason from design intent,
not just a citation list. To *find* the papers in the first place, use the
literature view below.

## Relax — the fidelity ladder (`{"op":"relax", …}`)

One verb, a `fidelity` rung from fast-and-rough to slow-and-correct. Run it
as a terminal op in `put`/`edit`. The relaxed geometry is written back onto
the design and **every run is recorded** (see `view='runs'`).

| fidelity | backend | needs | when |
|----------|---------|-------|------|
| `clean` (default) | pure geometry repair (ours) | nothing | fix overlaps / sub-covalent bonds — "make the stupid geometry sane" |
| `emt` | ASE EMT + FIRE (ours, torch-free) | nothing (core dep) | cheap, real-but-approximate energy/forces on the closed fcc-metal set `{Al,Ni,Cu,Pd,Ag,Pt,Au,H,C,N,O}` — never dispatches to the GPU node |
| `ml` | ASE + MACE-MP-0 / CHGNet | `precis-mcp[dft-ml]` | cheap, physical pre-relax before any DFT |
| `ff` · `xtb` · `dft-fast` · `dft-tight` | rented | (later) | progressively more correct |

`emt` (ADR 0053 §8) is ours like `clean` and works out of the box (ASE +
spglib are core deps, not gated behind an extra); an element outside its
closed set raises `Unsupported` with a "use fidelity='ml'" hint rather than
a stray error. No variable-cell mode.

```python
edit(kind="structure", id="pd111", ops=[{"op": "relax", "fidelity": "clean"}])
edit(
    kind="structure", id="pd111", ops=[{"op": "relax", "fidelity": "ml", "steps": 200}]
)
```

`clean` is always available and **has no energy** — asking for its energy
gives a defined "undefined" (shown as `—`), not a fake `0`. `ml` and up
return real energy + forces. A rung whose backend isn't installed **on this
host** doesn't crash — it dispatches to the GPU node as a `struct_relax` job
(see "Energy rungs run on the GPU node" below), never a bare error. `relax`
honours `fixed` constraints — a frozen atom never moves.

```python
get(
    kind="structure", id="pd111", view="runs"
)  # the compute history: fidelity · converged · steps · energy · max_force
```

### Energy rungs run on the GPU node — no todo needed (ADR 0044)

A rung with no local backend (a real `dft`/`ml` relax on a worker without
the kernel) is **derived compute**: `edit`/`put` dispatches a `struct_relax`
job to the GPU node and returns immediately. The job parents on the
**structure itself** — you do *not* need to create a todo first (that
requirement is gone). The relaxed geometry lands in the run-cube on
completion; poll `view='runs'`. An identical relax — same geometry, same
rung — is a **zero-compute cache hit** (returns synchronously, mints no job).

**Pre-flight gate before the GPU spend.** A dispatch first runs the `validate`
gate as a **hard reject**: an overlap, over-valence, or impossibly-long declared
bond raises an error naming the offending atom pair and mints **no** job — fix
the geometry first. It then runs a cheap local `clean` pre-relax (pass
`preflight='emt'` for an EMT pre-relax instead) so a mild clash is repaired
before cloud compute, and re-checks the cache on the cleaned geometry. So the
GPU is the last resort, not the first thing that runs on bad geometry. (A plain
local `clean`/`emt` relax to *fix* a clashing structure is never gated.)

```python
edit(
    kind="structure", id="pd111", ops=[{"op": "relax", "fidelity": "dft"}]
)  # dispatches, then poll view='runs'
```

**Want an intentful task to block on the build?** Pass `requested_by=<todo_id>`
on the relax op. That links the todo `requested`→the job and arms a
`derived_job_succeeded` auto_check, so the todo closes when the relax
converges and gets a `child-failed` bubble if it fails. Two tasks that
request the *same* relax share one job (idempotent on the cache key).

```python
edit(
    kind="structure",
    id="pd111",
    ops=[{"op": "relax", "fidelity": "dft", "requested_by": 4821}],
)
```

## Import from an external catalyst DB (ADR 0053)

`get(kind='structure', args={'source': 'catalysis-hub', ...})` hydrates a
real, DFT-relaxed config from an external library into an ordinary
`structure` design — a "quest worker pokes around and pulls a real
substrate" surface, not a separate API.

```python
get(
    kind="structure",
    args={"surface_composition": "Pd", "facet": "111", "source": "catalysis-hub"},
)  # a filtered fetch — hydrates every match, renders a summary table if >1
get(
    kind="structure",
    args={"config_id": "12345", "source": "catalysis-hub"},
)  # exact config_id: a network-free cache hit if already imported
```

- **First touch hydrates, forever after is a cache hit.** The fetch → adapter
  → `store.structure_import` write path is idempotent on `(dataset,
  config_id)` (`ref_identifiers`); a repeat `config_id=` lookup never refetches.
  A broad `surface_composition=`/`facet=`/`q=` filter (no `config_id=`) always
  refetches — there's no way to know in advance whether new configs match.
- **Only `catalysis-hub` has a fetch layer wired today** (its network
  client, httpx, is a core dep); an unregistered source raises `BadInput`
  naming the known ones. A broken venv missing httpx returns
  `Unsupported` with a reinstall hint, never a crash.
- **The `catalysis-hub` *live* fetch is dark** (verified 2026-07-24): SUNCAT
  gated all public access — the GraphQL API 401s without an `X-API-Key` and
  the old public Postgres password is rotated. The on-demand `source=` path is
  code-complete but needs a SUNCAT credential to reach the network.
- **Keyless ingress — mine a local cathub `.db`.** A cathub `.db` is a
  self-contained, credential-free package (relational reactions + embedded ASE
  structures + citation). `precis.structure.importers.cathub_db.batch_import(
  store, path, surface_contains=['Pd','Cu','Ni'], facet='111',
  product_contains=['NO'])` imports each reaction's product adsorbate config as
  an ordinary `structure` (external run carries the adsorption energy + method
  fingerprint), idempotent on `(dataset, config_id)`, needs only ASE (a core
  dep), no network. This is the "bulk-download-and-mine-local" path.
- **Imported designs are read-only.** They carry `provenance:external` on
  their `struct_runs` row; `edit` refuses ("derive a variant instead") —
  branch off one with `derive(id=<imported-slug>, to=<new-slug>, ops=[...])`.
  `view='runs'` labels each row's provenance + method fingerprint
  (functional/cutoff/spin/dataset_doi/facet/...).
- **Energies are only comparable within one method.** An external run's DFT
  functional/cutoff differs from a computed rung's model — subtracting across
  them is a category error, not a real ΔE, so an energy-delta surface must
  check both runs share a method fingerprint before comparing (geometry/graph
  reads like `diff` stay method-agnostic).

## Find a design — `search`

```python
search(kind="structure", q="OH on Pd(111)")  # by intent (hybrid)
search(kind="structure", q="catalyst surface", mode="semantic")
search(kind="structure", q="palladium", mode="lexical")  # keyword
```

Each design carries **one** embeddable card (title + composition + your
`description`), so search lands on **intent**, not coordinates — and joins
the cross-kind fan-out `search(kind='*', q='…')`. Hits are design-level
(`st<id>`); open one with `get(id='<slug>')`.

### From a structure to the literature — `get(view='literature')`

```python
get(kind="structure", id="pd111_cu", view="literature")
```

Assembles a query from the design itself — its `description` + host metal(s),
adsorbate, and facet read off the composition (every metal of an alloy is
kept) — and runs it against the **paper** corpus, returning the generated
query (so you can refine it) plus the ranked hits. Deterministic: same design
→ same query, no model call. Pair it with a paper-provenance `link` (above) to
record which of the hits actually motivated the design.

## Export — `get(view='poscar'|'extxyz'|'cif')`

The output side; bonds are dropped (DFT consumes positions + cell).

```python
get(
    kind="structure", id="pd111", view="poscar"
)  # VASP POSCAR (pure; Selective dynamics iff any atom is fixed)
get(
    kind="structure", id="pd111", view="extxyz"
)  # extended XYZ (pure; carries cell + pbc + our labels — lossless round-trip)
get(kind="structure", id="pd111", view="cif")  # CIF via ASE (core dep)
```

POSCAR and extXYZ are pure (zero deps); CIF goes through ASE, a core dep —
no extra needed.

## Delete

```python
delete(
    kind="structure", id="pd111"
)  # soft-retire the whole design (atoms/bonds retired, recoverable)
```

## Scope (v1)

Cell (lengths/angles or explicit lattice) + per-axis PBC; atoms (any
element) with fractional positions, `fixed` constraints, declared
magmom/oxidation; a bond graph (order + provenance + periodic image).
Ops: set_cell / add_atom / set_element / vacancy / displace / add_bond /
remove_bond / constrain / relax. Probes: atom / neighborhood / bonds / find
/ validate. Nav: line / plane / bonds_through_plane / bonds_in_sphere / path
/ rings / fragments / diff / pov. Relax: `clean` (pure) + `emt` (torch-free,
closed element set) + `ml` (MLIP-gated). Compute runs recorded with
convergence curves. Export: POSCAR / extXYZ / CIF. **External-DB import**
(ADR 0053): `catalysis-hub` on-demand hydrate is code-complete but dark (live
API now credential-gated); the keyless path is `cathub_db.batch_import` over a
local cathub `.db`. Open bulk-source adapters (OC20/AQCat25) are follow-ups. **Deferred (vision):**
Wyckoff-orbit TOC, named adsorption sites, bulk-insert ops (add_layer /
fill / add_chain), persisted named eyes + bookmark stack, electronic-field
lenses (charge / ESP / spin / Fukui), voids/channels, MD/NEB trajectories
with per-frame geometry, the cross-experiment ensemble cube, GPAW/DFT as a
cluster job.
```
