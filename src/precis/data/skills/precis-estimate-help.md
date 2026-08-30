---
id: precis-estimate-help
title: precis — the estimate kind (ms chemistry workup)
summary: millisecond semi-empirical chemistry workup — element-descriptor composition tier (electronegativity, covalent radius, magmom, d-electron count, Hammer-Norskov d-band center) plus pairwise alloying heuristics; structure-coupled tier (geometry lint, coordination/strain, symmetry, dedup, own-campaign BEP scaling) with a what-if mutation + a compare view; hypothesis-generating only, never a ruling
answers:
  - how do I quickly check a composition's element descriptors before dispatching a sim?
  - what does the estimate kind's d-band center number mean, and can I cite it as a measured barrier?
  - how do I ask for an alloying/mismatch read between two elements?
  - how do I sanity-check a held structure (floating atoms, symmetry, is it a duplicate) before dispatching a sim?
  - how do I ask "what did this dopant/op DO" to a structure without dispatching a relax?
applies-to: get (kind='estimate')
status: active
---

# precis-estimate-help — the millisecond chemistry-workup panel

`estimate` argues chemistry **fast and cheap**, in order to set up the slow
stuff (MLIP relax, NEB, QE/VASP DFT) — not to replace it. Two fidelity
tiers ship today: **composition** (pure element-property lookup, no
geometry needed) and **structure** (a held `structure` design's own
geometry).

## Composition tier

```python
get(kind="estimate", q="Pd Zr H")
get(kind="estimate", q="PdZrH")     # concatenated formula — same composition
get(kind="estimate", q="Pd, Zr")    # comma-separated — also fine
```

Returns one row per element (Z, group/period, Pauling electronegativity,
covalent radius, ground-state magnetic moment, d-electron count, and the
Hammer–Nørskov d-band center εd where vendored — Ni/Cu/Pd/Ag/Pt/Au only,
never a guessed number for the rest) plus a **pairwise** section when you
give ≥2 elements: electronegativity difference, covalent-radius mismatch
%, and a formulaic qualitative read ("large radius mismatch —
strain-dominated alloying") derived only from those two numbers — never
free-text generation.

An unknown element symbol is refused by name (`get(kind='estimate',
q='Pd Xx')` → error naming `Xx`), not silently dropped.

## Structure tier

```python
get(kind="estimate", id="st245406")                      # workup panel
get(kind="estimate", id="st245406", args={"quest": "qu164903"})  # + dedup/BEP grounded in the quest
get(kind="estimate", id="st245406",
    args={"ops": [{"op": "add_atom_site", "element": "H",
                    "site": {"type": "hollow", "anchors": [...]}}]})  # what-if — a copy, held design untouched
get(kind="estimate", id="st245406", view="compare",
    args={"against": "st237458"})                         # two workups + a numeric delta table
```

`id=` is the structure's universal handle (`st<id>`, not its slug — the
route through `handle_registry`). The panel:

- **Geometry lint** — the fidelity-tier-0 MLIP preflight (floating/detached atoms,
  clashes, vacuum/porosity) re-run over the (possibly what-if'd) geometry.
- **Coordination / strain** — per dopant/adsorbate atom: site classification
  (top/bridge/hollow for a true adsorbate; `—` for a substitutional dopant),
  nearest-neighbour distance vs covalent-radius-sum strain %.
- **Symmetry** — spglib space group + Wyckoff letters (symprec 1e-3);
  degrades to a note (never a crash) if spglib can't resolve the cell.
- **Dedup** — pymatgen `StructureMatcher` against `args={'quest': 'qu<id>'}`'s
  served structures, composition-prefiltered first. A match says **skip
  dispatch**. Omit `quest=` and the row says so explicitly rather than
  silently reading an empty set.
- **BEP** — a line fit (own-campaign trusted barriers only —
  `barrier_trusted is not False`) of barrier vs the vendored Hammer-Nørskov
  εd of the dominant surface metal (composition-weighted mean when
  alloyed). Needs ≥3 trusted points and `args={'quest': ...}`; fewer says
  `insufficient trusted barriers (n=N)`. The prediction pre-registers a
  `lower | on-trend | higher` branch call vs the campaign's own spread —
  the residual afterward is the anomaly detector, not the headline.

`args={'ops': [...]}` (a `structure/ops.py` op list — `add_atom_site` is the
site-symbolic, no-coordinate-guessing form) builds a what-if mutant on a
**copy** before the workup runs; the held design is never touched. A bad
op raises `BadInput` naming the `OpError`. `view='compare'` (with
`args={'against': 'st<...>'}`) runs the primary id's workup (ops apply only
to it) and the `against` structure's plain workup, then a numeric delta
table (strain %, mean coordination, predicted BEP barrier) — the
doped-vs-pristine "what did it DO" argument form.

Results are deterministic and cache-pinned per `(structure content sha,
ops, quest, against, view)` — editing the held design (a new content sha)
or changing `args=` always re-keys; an unchanged call is a free cache hit.

## Epistemic grade — read before citing

**`estimate` rows are hypothesis-generators, inadmissible for rulings.**
The ladder: estimate (ms) → MLIP sim (min) → QE autopsy (h) → literature.
An `[es…]` cite is visibly estimate-branded so a dossier reader never
mistakes a d-band heuristic (or a BEP prediction) for a measured barrier.
Validate the semi-empirical layer against something the campaign already
measured (e.g. does it get the Au-vs-Pt d-band ordering, or the d¹⁰ weak-
interaction pattern, right?) before leaning on it in an argument.
