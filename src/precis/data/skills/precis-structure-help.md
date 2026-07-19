---
id: precis-structure-help
title: precis — the structure kind (atomistic cells you can read as a graph)
summary: build a periodic cell + bond graph as typed ops, then probe it analytically (neighbours/coordination/line/plane/sphere/path/rings/fragments/diff/pov), relax it on a fidelity ladder (clean→ml→dft), and export POSCAR/extXYZ/CIF — no pixels; the graph + numbers are the interface
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
put(kind='structure', id='pd111', text='''{
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
}''')
```

- **`cell`** is either `{a,b,c,alpha?,beta?,gamma?,pbc}` (lengths/angles, °)
  **or** `{lattice: [[…],[…],[…]], pbc}` (an explicit 3×3, Å). `pbc` is a
  per-axis `[bool,bool,bool]` — a **slab** is `[true,true,false]` (periodic
  in-plane, vacuum gap along **c**).
- **`description`** makes the design findable by purpose (folded into the
  one search card). Optional, recommended.
- `put` applies the ops eagerly and echoes the TOC, so a bad op surfaces
  immediately. Re-`put`ting a slug **replaces** it (old atoms/bonds
  soft-retired, recoverable). Atom labels are minted in op order.

### The ops (also used by `edit`)

| op | args | effect |
|----|------|--------|
| `set_cell` | `lattice` or `a,b,c,…` + `pbc` | redefine the cell |
| `slab` | `element`, `size:[nx,ny,nz]`, `vacuum?`, `fix_layers?`, `a?` | **bulk template** — build an fcc(111) metal slab; **clears the scene** and sets the cell (ASE-exact atom order, so catpath can inject it). Omit the top-level `cell`. |
| `add_atom` | `element`, `frac:[fa,fb,fc]` | place an atom (wraps into the cell) |
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

## Edit — `edit(id=<slug>, ops=[…])`

```python
edit(kind='structure', id='pd111',
     ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.55]},
          {"op": "add_bond", "i": "aO2", "j": "aPd2"}])
```

Each edit bumps the design version. A graph edit invalidates any prior relax.

## Read the TOC — `get(id=<slug>)`

```python
get(kind='structure')               # list all designs
get(kind='structure', id='pd111')   # the TOC: formula · natoms · pbc · bonds · per-atom rows
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
get(..., view='atom',         args={'atom': 'aPd2'})                  # config + neighbour shell + coordination + fixed
get(..., view='neighborhood', args={'center': 'aPd2', 'radius': 3.0}) # the coordination shell within R Å
get(..., view='bonds')                                               # the whole bond list (order · kind · provenance · image)
get(..., view='find',         args={'element': 'Pd', 'undercoordinated': true})  # select atoms by predicate
get(..., view='validate')                                           # the DRC gate: overlaps + over-valence + fixes
```

### Spatial — the CAD ray / plane, retargeted to atoms (§6.2)

Geometry args are **Cartesian** (Å); accept a list `[x,y,z]` or a
comma-string `"0,0,5"`.

```python
# 1D — a ray: atoms within `radius` of the line, ordered along it (channels, columns)
get(..., view='line', args={'origin': [0,0,0], 'direction': [0,0,1], 'radius': 1.5})

# 2D — a layer slice: atoms within `thickness` of a plane, as labelled in-plane (u,v) coords
get(..., view='plane', args={'point': [0,0,5], 'normal': [0,0,1], 'thickness': 1.0})

# bonds that cross a plane — what stitches two layers (cleavage reasoning), image-aware
get(..., view='bonds_through_plane', args={'point': [0,0,5], 'normal': [0,0,1]})

# bonds inside/crossing a sphere — the local bonding environment around a point
get(..., view='bonds_in_sphere', args={'center': [4.2,4.2,6.0], 'radius': 3.0})
```

### Graph topology & diff (§6.1/§6.3/§6.5)

```python
get(..., view='path',      args={'a': 'aO1', 'b': 'aPd2'})  # shortest bond path (or "disconnected")
get(..., view='rings',     args={'max_size': 8})            # smallest cycles — find sp² 6-rings
get(..., view='fragments')                                 # connected components: "slab + 3 adsorbates"
get(..., view='diff',      args={'other': 'pd111_v0'})     # vs another design: RMSD · per-atom move · bonds/atoms broken/formed
```

`fragments` answers "did this edit break the structure apart?"; `diff` is
the single most insightful view of what a relax (or an edit) actually did.

### Point of view — the embodiment readout (§6.6)

One uniform readout regardless of *what* you focus on — an atom or a
fragment (a ring from `rings`, a molecule from `fragments`):

```python
get(..., view='pov', args={'support': 'aO1', 'reach': 3.0})          # i_am=atom
get(..., view='pov', args={'support': ['aC1','aC2','aC3','aC4','aC5','aC6'], 'reach': 3.0})  # i_am=fragment
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
edit(kind='structure', id='pd111',
     ops=[{"op": "eye", "name": "active_site", "atoms": ["aPd12"],
           "reach": 3.0, "for": "the reactive Pd"}])

# a pinned measurement with a graded goal
edit(kind='structure', id='pd111',
     ops=[{"op": "measure", "kind": "distance", "atoms": ["aH1", "aPd12"],
           "direction": "target", "goal": {"target": 2.4, "tol": 0.1},
           "strength": "soft", "for": "keep the H bound"}])

get(kind='structure', id='pd111', view='markers')   # all eyes + measures, live value + verdict
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
link(kind='structure', id='pd111_h', target='structure:pd111', rel='derived-from')
```

`derived-from` (⇄ `derived-into`) records that one design came from another —
e.g. an LLM-proposed edit branched to a new slug. Read both directions with
the store's link queries; the web viewer renders the lineage.

## Relax — the fidelity ladder (`{"op":"relax", …}`)

One verb, a `fidelity` rung from fast-and-rough to slow-and-correct. Run it
as a terminal op in `put`/`edit`. The relaxed geometry is written back onto
the design and **every run is recorded** (see `view='runs'`).

| fidelity | backend | needs | when |
|----------|---------|-------|------|
| `clean` (default) | pure geometry repair (ours) | nothing | fix overlaps / sub-covalent bonds — "make the stupid geometry sane" |
| `ml` | ASE + MACE-MP-0 / CHGNet | `precis-mcp[dft-ml]` | cheap, physical pre-relax before any DFT |
| `ff` · `xtb` · `dft-fast` · `dft-tight` | rented | (later) | progressively more correct |

```python
edit(kind='structure', id='pd111', ops=[{"op": "relax", "fidelity": "clean"}])
edit(kind='structure', id='pd111', ops=[{"op": "relax", "fidelity": "ml", "steps": 200}])
```

`clean` is always available and **has no energy** — asking for its energy
gives a defined "undefined" (shown as `—`), not a fake `0`. `ml` and up
return real energy + forces. A rung whose backend isn't installed **on this
host** doesn't crash — it dispatches to the GPU node as a `struct_relax` job
(see "Energy rungs run on the GPU node" below), never a bare error. `relax`
honours `fixed` constraints — a frozen atom never moves.

```python
get(kind='structure', id='pd111', view='runs')   # the compute history: fidelity · converged · steps · energy · max_force
```

### Energy rungs run on the GPU node — no todo needed (ADR 0044)

A rung with no local backend (a real `dft`/`ml` relax on a worker without
the kernel) is **derived compute**: `edit`/`put` dispatches a `struct_relax`
job to the GPU node and returns immediately. The job parents on the
**structure itself** — you do *not* need to create a todo first (that
requirement is gone). The relaxed geometry lands in the run-cube on
completion; poll `view='runs'`. An identical relax — same geometry, same
rung — is a **zero-compute cache hit** (returns synchronously, mints no job).

```python
edit(kind='structure', id='pd111', ops=[{"op": "relax", "fidelity": "dft"}])  # dispatches, then poll view='runs'
```

**Want an intentful task to block on the build?** Pass `requested_by=<todo_id>`
on the relax op. That links the todo `requested`→the job and arms a
`derived_job_succeeded` auto_check, so the todo closes when the relax
converges and gets a `child-failed` bubble if it fails. Two tasks that
request the *same* relax share one job (idempotent on the cache key).

```python
edit(kind='structure', id='pd111',
     ops=[{"op": "relax", "fidelity": "dft", "requested_by": 4821}])
```

## Find a design — `search`

```python
search(kind='structure', q='OH on Pd(111)')                 # by intent (hybrid)
search(kind='structure', q='catalyst surface', mode='semantic')
search(kind='structure', q='palladium', mode='lexical')     # keyword
```

Each design carries **one** embeddable card (title + composition + your
`description`), so search lands on **intent**, not coordinates — and joins
the cross-kind fan-out `search(kind='*', q='…')`. Hits are design-level
(`st<id>`); open one with `get(id='<slug>')`.

## Export — `get(view='poscar'|'extxyz'|'cif')`

The output side; bonds are dropped (DFT consumes positions + cell).

```python
get(kind='structure', id='pd111', view='poscar')   # VASP POSCAR (pure; Selective dynamics iff any atom is fixed)
get(kind='structure', id='pd111', view='extxyz')   # extended XYZ (pure; carries cell + pbc + our labels — lossless round-trip)
get(kind='structure', id='pd111', view='cif')      # CIF via ASE — needs precis-mcp[dft]
```

POSCAR and extXYZ are pure (zero deps); CIF needs the `[dft]` extra. A
missing extra returns Unsupported with the install hint.

## Delete

```python
delete(kind='structure', id='pd111')   # soft-retire the whole design (atoms/bonds retired, recoverable)
```

## Scope (v1)

Cell (lengths/angles or explicit lattice) + per-axis PBC; atoms (any
element) with fractional positions, `fixed` constraints, declared
magmom/oxidation; a bond graph (order + provenance + periodic image).
Ops: set_cell / add_atom / set_element / vacancy / displace / add_bond /
remove_bond / constrain / relax. Probes: atom / neighborhood / bonds / find
/ validate. Nav: line / plane / bonds_through_plane / bonds_in_sphere / path
/ rings / fragments / diff / pov. Relax: `clean` (pure) + `ml` (MLIP-gated).
Compute runs recorded with convergence curves. Export: POSCAR / extXYZ /
CIF. **Deferred (vision):** Wyckoff-orbit TOC, named adsorption sites,
bulk-insert ops (add_layer / fill / add_chain), persisted named eyes +
bookmark stack, electronic-field lenses (charge / ESP / spin / Fukui),
voids/channels, MD/NEB trajectories with per-frame geometry, the
cross-experiment ensemble cube, external-DB import, GPAW/DFT as a cluster
job.
```
