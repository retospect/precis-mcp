---
id: precis-structure-slab-help
family: science
title: precis — building a metal slab (the slab op, adsorbate sites, IS/FS endpoints)
summary: seed an fcc(111) metal slab with the slab op, place a dopant or adsorbate on a named site (top/bridge/hollow) instead of a guessed z, and branch a label-matched IS/FS endpoint pair an NEB band can interpolate
answers:
  - how do I build a metal slab with the slab op, then put a dopant or adsorbate on it?
  - what does a 3×3 slab supercell mean for coverage?
  - how do I place an adsorbate on a real site (top/bridge/hollow) without guessing a z coordinate?
  - how do I make an IS/FS endpoint pair an NEB band can use?
applies-to: put/edit/get (kind='structure')
status: active
tags: design, verbs
kinds: structure
---

# precis-structure-slab-help — surfaces, adsorbates and endpoints

The rest of the structure kind (ops table, probes, relax ladder, export)
is `precis-structure-help`; imports are `precis-structure-import-help`.

## Build a slab — the `slab` op

`slab` is the bulk template and the usual **first op**: it seeds a whole
fcc(111) metal slab — the same `ase.build.fcc111` call autocatpath uses,
so atom order and geometry are identical and the slab can be injected
straight into a NEB run. It **clears the scene** (atoms, bonds,
measures), sets the cell itself and forces `pbc (true,true,true)` — the
vacuum gap is geometric, not a pbc flag. Omit the top-level `cell`; run
through `edit` it wipes whatever the design already held.

| arg | type | default | meaning |
|-----|------|---------|---------|
| `element` | str | required | slab metal, e.g. `"Cu"` |
| `size` | `[nx,ny,nz]` | required | in-plane repeats × layers |
| `vacuum` | Å | `10.0` | gap above the top layer |
| `fix_layers` | int | `0` | COUNT of bottom layers to freeze — **not** a list of layer indices |
| `a` | Å | ASE reference | lattice constant |

Atoms mint `aCu1…aCu36` in ASE order — on a `[3,3,4]` the **top layer is
the last `nx·ny` labels** (`aCu28…aCu36`), the first `2·nx·ny` the frozen
bottom. One substitution in a `3×3` top layer is 1/9 ML coverage.

### Worked example — an Ni adatom on Cu(111)

```python
put(
    kind="structure",
    id="cu_ni_ad",
    text='{"ops":[{"op":"slab","element":"Cu","size":[3,3,4],"vacuum":10.0,"fix_layers":2},{"op":"add_atom_site","element":"Ni","site":{"type":"top","anchors":["aCu28"]}}]}',
)
```

37 atoms: `aNi1` lands on top of `aCu28` at the default covalent-radius
height (~2.6 Å above the surface). An adatom is `add_atom_site`, never a
hand-computed `frac`.

### Worked example — an Ni single-atom alloy + a bridging O

```python
put(
    kind="structure",
    id="cu_ni_saa_o",
    text='{"ops":[{"op":"slab","element":"Cu","size":[3,3,4],"fix_layers":2},{"op":"set_element","atom":"aCu28","element":"Ni"},{"op":"add_adsorbate","species":"O","site":{"type":"bridge","anchors":["aCu28","aCu29"]}}]}',
)
```

`set_element` transmutes `aCu28` into the Ni dopant **but keeps the
label** — later ops still address it as `aCu28`; there is no `aNi28`. The
O lands over the Ni–Cu bridge (`aCu28`–`aCu29` midpoint + ~1.9 Å default
height): 37 atoms, an O* bridging the dopant. A CH4 spectator is one more
`add_adsorbate` (`species:"CH4"`, `site:{"type":"top",…}`,
`height:2.5`).

### `slab` refusals

- `slab needs 'element' and 'size' as [nx, ny, nz]` — both required.
- `slab 'fix_layers' is an integer COUNT of bottom layers to freeze …` —
  it is not a list of layer indices.
- `slab numeric params must be numbers — got …` — a non-coercible
  `size`/`vacuum`/`fix_layers`/`a`.
- `slab op needs ASE (the [dft] extra)` — the runtime lacks ASE.

The follow-up ops refuse in kind: an unknown `site.type` (only
`top`/`bridge`/`hollow` — no interstitials), the wrong anchor count
(1/2/3 anchors), a repeated or missing anchor label, and
`add_adsorbate 'species' must be one of […]` outside the catalogue.

## Place an adsorbate on a real site, not a guessed z

```python
edit(
    kind="structure",
    id="pd111",
    ops=[
        {
            "op": "add_atom_site",
            "element": "H",
            "site": {"type": "hollow", "anchors": ["aPd10", "aPd11", "aPd12"]},
        },
    ],
)
```

`z` comes from the anchors' own geometry, never a remembered constant — a
`frac` copied from another cell doesn't transfer. Use raw `add_atom` only
when no anchor exists yet; on the standard Pd(111) `[3,3,4]`/vacuum-10
cell, on-surface hollow is `z≈0.46` (a ~1 Å H–metal bond) — `frac z=0.66`
looks plausible but floats H ~4.3 Å off the surface: preflight fails or
the relax finishes in 0 steps at a subsurface-degenerate energy.

## Reaction endpoints — a label-matched IS/FS pair

An NEB band interpolates between endpoints **by atom label** — a final
state re-`put` from scratch gets fresh labels and can never pair. Branch
the FS off the IS instead (`args={'derive_from': …}` on `put`), so labels,
elements, cell and the fixed set carry over:

```python
# IS: Cu(111) [3,3,4], bottom two layers frozen; O* on a bridge site,
# CH4 physisorbed on a neighbouring top site.
put(kind="structure", id="cu_is", text='{"ops":[
  {"op":"slab","element":"Cu","size":[3,3,4],"fix_layers":2},
  {"op":"add_adsorbate","species":"O","site":{"type":"bridge","anchors":["aCu28","aCu29"]},"height":1.9},
  {"op":"add_adsorbate","species":"CH4","site":{"type":"top","anchors":["aCu31"]},"height":2.5}]}')

# FS: CH4 + O* → CH3* + OH* — one transfer of an H from C onto O, then
# the remaining CH3 fragment moved to a fresh top site.
put(kind="structure", id="cu_fs", args={
  "derive_from": "cu_is",
  "ops": [
    {"op":"transfer","from":"aC1","to":"aO1"},
    {"op":"move_group","atom":"aC1","site":{"type":"top","anchors":["aCu34"]},"height":2.4},
  ]})
```

The derive ack reports the pair verdict: `labels preserved …
NEB-pairable` when the atom set survives, `atom set changed (+a/-r): not
NEB-pairable` when an op added or removed atoms. `transfer`'s `from:`
picks the declared-bonded atom nearest `to` (an equidistant tie refuses
naming both — pick with `atom:`); `move_group` carries the whole
declared-bond fragment rigidly and refuses when it holds a fixed or slab
atom (slab = the largest connected metal cluster — a dopant inside it
counts as slab). Site labels come from the design TOC. Both land
starting-guess geometry — `relax` each endpoint at `fidelity:"ml"` first.

Then check the pair (read-only, no compute):

```python
get(kind="structure", id="cu_is", view="pair", args={"other": "cu_fs"})
```

One row per gate — same atoms/cell/fixed set · slab anchored (≥1 fixed
slab atom) · validator-clean · a converged `ml` run at one model on
*both* current versions · distinct basins (max adsorbate move ≥ 0.3 Å)
· ≤2 bond changes · no >5 Å drag — each `fail` row's `next` is the
working fix; `warn` never blocks. `relaxed` fails until both endpoints
have relaxed at `ml`.

Clean pair → `put(mode="neb", args={"final": <slug>})` queues a
`struct_neb` job (`ml` rung, deduped); `view="neb"` reads it.
`view="neb" args={"run": N}` renders one run's gate: the `trust:` table
is one row per check with `measured` (the values the check read), `tol`
(the stamped tolerance, else the check's rule), `where` (which image,
seed or endpoint failed) and `note`; a `## seeds` table prices each
init seed's barrier next to the pooled spread vs the multistart
tolerance. An `untrusted` verdict names the refusing check with its
measured value against its bound — a real disagreement reads different
from tolerance noise. The run's wall-clock deadline is checked only
between calculator calls, so one long or hung call can overrun by its
own length plus the lease's +1 h margin. A hung call ends as a
lease-expiry zombie — dead job, still-`running` row — that
`struct_neb_reap_dead` clears the next time the edge is minted or
harvested.
