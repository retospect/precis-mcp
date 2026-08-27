---
id: precis-estimate-help
title: precis — the estimate kind (ms chemistry workup)
summary: millisecond semi-empirical chemistry workup — element-descriptor composition tier (electronegativity, covalent radius, magmom, d-electron count, Hammer-Norskov d-band center) plus pairwise alloying heuristics; hypothesis-generating only, never a ruling
answers:
  - how do I quickly check a composition's element descriptors before dispatching a sim?
  - what does the estimate kind's d-band center number mean, and can I cite it as a measured barrier?
  - how do I ask for an alloying/mismatch read between two elements?
applies-to: get (kind='estimate')
status: active
---

# precis-estimate-help — the millisecond chemistry-workup panel

`estimate` argues chemistry **fast and cheap**, in order to set up the slow
stuff (MLIP relax, NEB, QE/VASP DFT) — not to replace it. Slice 1 (today)
ships the **composition tier**: pure element-property lookup, no geometry
needed.

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

## Epistemic grade — read before citing

**`estimate` rows are hypothesis-generators, inadmissible for rulings.**
The ladder: estimate (ms) → MLIP sim (min) → QE autopsy (h) → literature.
An `[es…]` cite is visibly estimate-branded so a dossier reader never
mistakes a d-band heuristic for a measured barrier. Validate the
semi-empirical layer against something the campaign already measured
(e.g. does it get the Au-vs-Pt d-band ordering, or the d¹⁰ weak-
interaction pattern, right?) before leaning on it in an argument.

Results are deterministic and cache-pinned — a fixed composition always
returns the same panel; `mode='refresh'` bypasses the cache after a
code/vendored-table change.

## What's coming (slice 2 — not built yet)

The composition tier is step one of a bigger workup panel. Not yet
available — asking for one of these `view=` values today returns a clean
error naming this list, not silent fallback:

- `view='structure'` — full workup of a held `structure` ref (coordination,
  strain, adsorbate height, d-band via extended-Hückel on a real geometry).
- `view='whatif'` — mutate a structure in-call (`ops=` reusing the quest
  candidate-ops vocabulary) and workup the mutant.
- `view='compare'` — doped-vs-pristine delta, the core argument form
  ("what did Zr *do*?").
- `view='shape' | 'orbitals' | 'spin' | 'kinetics' | 'card'` — depth views
  (coordination-polyhedron naming, HOMO–LUMO/Wiberg bond orders, per-site
  magmoms, BEP-scaling kinetics, an SVG orbital-diagram card).

Design of record: `docs/backlog/estimate-kind-ms-chemistry-workup.md`
(git-only once slice 2 ships).
