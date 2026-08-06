---
id: precis-pathway-help
title: precis — reaction pathways (autocatpath)
summary: explore a catalyst reaction network — intermediates, barriers, honest uncertainty — and argue with it as data, not a diagram
applies-to: get/put (kind='pathway')
status: active
---

# precis-pathway-help — reaction pathways (autocatpath)

A `pathway` is a **reaction-network exploration** on a catalyst surface. Give it
an environment (a metal surface), a substrate, and a target; it builds the
network of intermediates, relaxes each, finds transition-state barriers
(climbing-image NEB), and reports energies with **pooled uncertainty** — spread
across seeds and models, low-confidence results *flagged*, never faked into
false precision. Slug-addressed; the body is a citable methods paragraph.

The point is **not the picture**. Every number here is a fact you can contest,
cite, or act on. Read the data, argue with it, change something, run again.

## Frame a run without spending compute / sanity-check the network first / preview intermediates

Building the network is cheap (rule-based, no ML). Do it *before* you pay for
relax/NEB, so you can object to a bad network before it costs anything.

```python
put(
    kind="pathway",
    id="no-nh3-pd",
    mode="preview",
    text="""
substrate: "NO"       # quote it — bare NO is YAML false
target: "NH3"
network: branching
slab: {element: Pd}
""",
)
# → the intermediates + elementary steps, no energies. Argue with it.
```

If an intermediate looks unphysical or a step is wrong, edit the config and
re-preview. Nothing is computed until you drop `mode='preview'`.

## Run the pathway / compute the barriers / get the energies

```python
put(kind="pathway", id="no-nh3-pd", text="""...same config...""")
```

Heavy backends run on the GPU node; a real run takes minutes. Re-running an
unchanged config is a cache hit (free); editing it recomputes only what changed.

## Read the objective / what's the rate-limiting step / how good is this path

```python
get(kind="pathway", id="no-nh3-pd", view="analysis")
```

- **rate-limiting** — the highest single barrier on the path (the step to fix).
- **energetic span (SPAN)** — the whole-path apparent barrier: the biggest climb
  from any intermediate to any *later* transition state. Often the *truer*
  objective — it can exceed every single step when a deep well sits before a
  high TS. Lower SPAN = better path.
- the barriers ranked, and selectivity vs competing branches.

## Compare candidates / rank levers / which surface is best

```python
get(kind="pathway", id="no-nh3-pd", view="compare")
```

Compares this pathway against every computed sibling for the same
substrate→target, as one table: **candidates are rows** (sorted best-first by
`RATE`), the **reaction coordinate is the columns** (state energies + `‡`
barriers). Scan a `‡` column to see which candidate lowers that step; read a row
for one candidate's whole landscape.

## Other reads

- `view='intermediates'` / `view='steps'` — the states / elementary steps as tables.
- `view='warnings'` — where to distrust the numbers (non-converged NEB, bad geometry).
- `view='methods'` — the citable methods paragraph; `view='config'` — the snapshot.

## See the reaction — the interactive web explorer

`/refs/pathway/{id}` (web, not an MCP verb) renders a clickable energy
diagram — one coloured profile per root→leaf path, target path first,
shared prefixes aligned (mirrors catpath's `viz.draw_profile`), TS humps,
Ea labels, ±1σ bands, low-confidence marked — plus a per-state 3D cell
viewer stepping through the linked `structure` refs in the same path order.
Clicking an atom lists its element-grouped relationships and bonds ranked
by Pauling bond order `s = exp((R0−d)/0.37)` (MIC distances; same panel on
`/structure/{slug}`). The selection follows the active state: stepping or
clicking to another state re-reads the same atom/bond against that state's
geometry — a bond broken there is recomputed (length + `s`) rather than
dropped. When
the pathway carries `refs.meta.measures` (`[{name, op, atoms, element?}]`),
each state's measures overlay: `min_distance` (a labeled atom → nearest atom
of an `element`) is identity-safe across states by construction; a plain
`distance`/`angle` anchored on an atom whose element repeats in the slab
renders flagged "unverified across states" (label order isn't guaranteed
stable state-to-state). `refs.meta.measures` has no writer yet — no
`put`/`edit` verb sets it; today it's a manually-authored JSONB field, not
something a call from here produces.

## Moves worth having — a menu, not a recipe

Compose these as the situation calls for. They're how a careful chemist works,
not a fixed pipeline — pick what fits.

- **Argue before you compute.** Preview, contest an intermediate or step, edit,
  then run. Cheap doubt beats expensive certainty.
- **Doubt the gate.** If the rate-limiting (or span-setting) step is `conf=low`,
  its spread is too wide to act on — re-run that step at a higher fidelity
  (EMT → MACE → DFT) before trusting the number.
- **Ground the lever in evidence.** Before proposing a change — a dopant, a
  poison like trace S, a different facet, a reagent swap — search the corpus for
  what's known (`search(kind='paper', queries=[...])`) and cite it. Grounded
  fuzzing beats blind fuzzing.
- **Rank on the objective, not the eye.** Use `compare` / `SPAN`, not a glance.
- **Optimize the path, not just a step.** Sometimes the best move lowers `SPAN`
  by filling a deep well, not by shaving the tallest barrier.

## Levers — what you can change between runs

Today's config knobs are the search space you explore:

- `slab.element` — the catalyst surface metal.
- `network` — `branching` (a DAG with competing routes), `oxidation`, or `auto`.
- `reagents` — which adatoms (O\*, H\*) are available to the steps.
- `mlip.backend` — the fidelity: `emt` (free, qualitative — a smoke test only),
  `mace` / `fairchem` (real ML potentials, GPU), and higher DFT rungs.
- `search.seeds` — more seeds → a real mean ± spread (use ≥3 for confidence).

More levers (dopants, facets, pH/potential) arrive as autocatpath grows; the calls
above don't change — only what you can put in the config.

## Gotchas

- **Quote chemical labels.** `substrate: NO` parses as `false` in YAML — write `"NO"`.
- **EMT is not physics.** It exercises the pipeline; barriers are qualitative.
  Trust MACE / FAIRChem / DFT for numbers you'll act on.
- **Compute is expensive; preview is free.** Preview and argue first.
- **Uncertainty is a signal, not noise.** A `low` flag says *escalate*, not *ignore*.

## See also

- `precis-search-help` — grounding levers in the paper corpus.
- `precis-tasks-help` / `precis-decomposition-help` — running a standing
  optimization campaign on the todo tree.
- `precis-structure-help` — the atomistic structures behind each intermediate.
