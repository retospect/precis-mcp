---
id: precis-pathway-help
title: precis — reaction pathways (autocatpath)
summary: explore a catalyst reaction network — intermediates, barriers, honest uncertainty — and argue with it as data, not a diagram
answers:
  - how do I sanity-check a catalyst reaction network before spending compute on it?
  - how do I run a pathway and get its barriers/energies?
  - how do I find the rate-limiting step in a reaction pathway?
  - what's the CHE electrochemistry potential lever?
  - how do I compare two candidate pathways to see which is better?
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

Heavy backends run on the GPU nodes (the seed fan-out spreads across them); a real run takes minutes. Re-running an
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

## The potential lever (CHE electrochemistry)

NOx reduction is *electro*catalysis: applied potential `U` (V vs **RHE**) is a
real design lever. Under the computational hydrogen electrode, `U` enters only
through the `+H*` reservoir (each supplied H is H⁺ + e⁻), so a state's energy
shifts by `n_H·eU`. All of it is **post-processing over energies already
computed** — no new relax/NEB, closed-form optima, no search.

- `meta.graph` nodes carry `n_H`; `meta.results` carries flat scalars:
  `U_L` (limiting potential, over **every** electrochemical step reachable
  from the root — both branches must turn over), `U_opt` + `span_at_Uopt`
  (span minimized over U; span = worst over *required* leaves — target **and**
  each parked branch's sink — of the easiest route to each),
  `span_at_UL`, `span_target_at_Uopt` (target-path-only diagnostic),
  `P_side` (probability of leaving the target path at its forks, branch
  fractions ∝ `exp(−ΔEa/kT)`; `null` = insufficient data — a fork with any
  missing or untrustworthy competitor barrier is never scored), and `T`
  (default **298.15 K**).
- engine >= 0.6.0 also carries `meta.results.score` (the four-axis
  scorecard) — `selectivity_margin_eV` > 0 means side products are
  kinetically disfavored at the worst branch point, `trap.margin_eV` > 0
  means the worst off-route state can't outcompete the best route's span
  — alongside the raw `results.traps` (per-state cheapest escape vs the
  best route's span; `trap: true` = a self-poisoning resting state) and
  `results.poisons`
  (per-species clean-slab `E_ads` vs the substrate's; verdict
  `blocks`/`competitive`/`weak` — the site-competition screen, config
  `poisons: ["CO"]`).
- engine >= 0.6.0 adds `results.score` — the four-axis scorecard
  (activity span + worst-case selectivity/poison/trap margins, each with
  its ranked breakdown); its `limiting_factor` + one-line `worst_problem`
  ride onto quest candidates as naming context ("what do I fix first"),
  never as measures.
- The explorer (`/refs/pathway/{id}`) re-renders the diagram at any `U`
  client-side (slider, `→ U_L` / `→ U_opt` snaps), shows RHE **and** SHE
  (`U_SHE = U_RHE − 0.0592·pH` at 298.15 K; PCET steps are pH-independent on
  RHE), and labels guarded fork percentages.
- Quest ranking: the scalars (plus derived `U_L_abs`) are harvested onto
  candidates under the same trust gate as `barrier`. Declare them in
  `meta.rubric_objectives` (Pareto), or combine via `meta.rubric_composite`
  (`{key, weights: {measure: weight}}`, e.g. span/|U_L|/P_side). Weights are
  **human-set per quest** — the agent may not tune its own objective; a
  candidate missing any weighted component simply isn't scored on the
  composite.

## Fidelity tiers (screening → neb → verify)

`meta.results` also carries `screening: true` (relax-only run — **no
barriers**, spans are thermodynamic) and `template: parked|coadsorbed`.
Pathways and candidates carry `meta.tier`; a verify (coadsorbed) pathway
`refines`-links its parked sibling. A candidate's canonical `barrier` comes
from the highest-fidelity trusted run; a superseded parked value is kept as
`barrier_screen` — the screen→verify delta is the parking-approximation
error, calibration data, never deleted. Graduation requires a trusted
verify-tier barrier on tier-ladder quests.

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
- `view='warnings'` — where to distrust the numbers (non-converged NEB, bad
  geometry); a ~0 eV barrier carries no auto-flag and is usually a
  broken/degenerate NEB, not a record — see `precis-quest-help`'s trust
  section for the full auto-flag list and the low-vs-high read asymmetry.
- `view='methods'` — the citable methods paragraph; `view='config'` — the snapshot.

## See the reaction — the interactive web explorer

`/refs/pathway/{id}` (web, not an MCP verb) renders a clickable energy
diagram — one coloured profile per root→leaf path, target path first,
shared prefixes aligned (mirrors catpath's `viz.draw_profile`), TS humps,
Ea labels, ±1σ bands, low-confidence marked — plus a per-state 3D cell
viewer stepping through the linked `structure` refs in the same path order.
The states panel is grouped by branch (one section per path, tinted like
its profile; branch sections say where they diverge), and supply-edge rows
annotate reservoir traffic — `+H* from reservoir`, and where a dissociation
byproduct goes: `O* parked — continues in → H2O`. A preflight warning that
names a state (`INFEASIBLE`, `wrong-site`, `detached` = red; `RESEATED ok`
= amber) badges that state's row and reddens its diagram level, so a
quarantined number is visible where you'd read it. A provenance strip links
the candidate structure, owning quest, dossier, logbook, and the run jobs
that produced the pathway (the per-seed jobs carry a `run_log` chunk — the
compute's captured stdout/stderr tail — and the strip notes which node it
`ran on`); a candidate
stepper walks sibling pathways for the same substrate→target reaction
(ranked by `rate_Ea`), carrying the selected state across so you can park
on one step and compare candidates.
When the run solved microkinetics (`results.kinetics`), a Kinetics card
renders the catpath report's panel: the fixed-rule verdict, TOF /
5–95 % band / span-limit table, excluded-step bracket guard, a collapsed
"kinetic equations solved" section (master equation, per-kind rate-constant
forms, this run's ODE system with the numbered rate constants), X_RC/X_TRC
bars, steady-state coverages, and the solve warnings; a `kinetics_error`
shows as a did-not-run note instead.
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

## Avoid the traps that waste a run

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
