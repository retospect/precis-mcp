---
status: draft
title: Reaction Explorer controls — an environment panel on the Pourbaix slider bank, a visual-filter panel, per-step kinetics on the diagram, and a compute cart for what is not computed yet
pillar: 3d-design
prio: high
---

# Reaction Explorer controls: environment, filters, per-step kinetics, compute cart

Reto, 2026-11-03, from `/refs/pathway/549461` (NO→NH₃ on st549459
`Pd111-Ge-subsurf-1`): replace the one control box above the explorer
with two collapsible boxes — an **environment** box on the left (T,
U vs RHE, pH, pressures, salt; a small clickable Pourbaix map showing the
current point; correlated sliders with hover help) and a **visual filter**
box on the right (hide steps above a barrier threshold, terminate routes
below a turnover threshold, named checkboxes for every annotation layer).
Behind both sits a **compute cart**: whatever the page cannot show from
stored data is a request the user can queue (NEB on chosen steps, a
coverage scan, modes, a kinetics re-solve at other T/p). Three questions,
in Reto's words: which chemistry space to display (and what of it needs
more simulation, and how to ask); how to display what is known; how to
store all of it. Coverage and multi-site chemistry are out: the slab would
have to grow and the compute is not there.

This file is the umbrella: the plan, the facts it rests on, the decisions
Reto takes, and one gripe per shippable slice (filed 2026-11-03, cluster
`pathway-explorer-controls`). Slices ship separately; this file closes
when the last one does.

## What pw549461 actually is (read 2026-11-03, prod)

- **Screening tier**, `template: parked`, seeds `[0, 1]`, `mace:medium`,
  engine 0.45.0 (`view='config'`). Relax-only: **no NEB barrier exists on
  any edge** (`view='analysis'`: every Ea n/a). The "little mountains" are
  not hidden by a regression; they were never measured for this pathway.
  Measured barriers come at the `neb` and `verify` tiers
  (`precis-pathway-help` §Fidelity tiers).
- The engine did fit **BEP estimates** per step (`results.bep`), and the
  kinetics solved on them: `view='kinetics'` reports `bep_flux_share = 1`,
  TOF (mf) 6.9e-53 /s, "poisoned surface: vacancy 1.8e-32, held by N",
  "state not fully settled", gases priced at the 1 bar reference (no
  `environment:` block). So "turnover per transition" exists — v1 chain
  `k_f`, `k_b`, net rate per edge; v2 mean-field DRC/TRC, reversibility
  `z_i`, coverages — but every number on this pathway stands on BEP
  barriers at default conditions.
- `che_affine` + `her` are present: `view='staircase'` gives 9 routes,
  route 1 (NO@N→N+O→…→NH₃) blocked by the U-flat NO@N→N+O rung
  (ΔG0 +0.325 eV), DAG-wide U_L −2.248 V, HER margin per route.
- st549459 has **no coverage scan**, so no resting-state map
  (`view='pourbaix'`: "no anchors"; the staircase reads "starting surface
  unknown").
- Title: the write-time fix (gr549574, r195) is live; the backfill rename
  of existing pathways has not run (comment 8 there), and the readable
  formatter is gr553416 (in flight). Not this item.

## Motivation / why

The explorer is how Reto reads results (catalysis-selectivity thread
context). Today it shows one U slider and a pH input (`#pw-u-lever`,
`#pw-ph-input`, `setU` in `pathway_detail.html.j2`; pH moves only the
SHE readout), no checkbox or toggle of any kind, and nothing on the page
says which of its numbers are measured, estimated, interpolated or
absent. The Pourbaix page's "honest slider" flag (`data-offgrid` on
`#pbx-u`/`#pbx-ph`, `pourbaix/detail.html.j2`) is wired but nothing sets
it, so the off-grid warning has never shown. The environment is spread over four vocabularies (the pathway
config's `electrochemistry` and `conditions` blocks, the kinetics
`environment` block, the quest's `operating_conditions`, the page's
`u`/`ph` query parameters), so "show me this pathway at 350 K, 10 bar,
−0.3 V" has no single place to be asked and no single answer about what
that would cost. The Pourbaix map, the staircase and the explorer are
three pages over the same slab and the same (U, pH) point
(surface-pourbaix-staircase-optimizer §7: one slider bank, honest sliders).

## Design

### A. The three honesty states of a control

Every axis in the environment box renders in exactly one of three states,
and the state is visible on the control:

| state | meaning | examples |
|---|---|---|
| **closed form** | re-renders now from stored energies | U_RHE on the profile (`n_H·eU`, `reaction_graph.at_potential`) and on the staircase (`ΔG0 + n_e·U`); pH as the SHE label and, once `pathway-selectivity-u-ph-window` ships, the protonation and HER terms; a gas pressure on adsorption/desorption steps (`±kT·ln(p/p°)` per gas molecule exchanged) |
| **stored / interpolated** | a stored solve covers this point or brackets it, within the cutoffs below | T and p_total inside a stored `kinetics_sweep` box (46 quest pathways carry one since gr543723 comment 42; this pathway does not); interpolation is Arrhenius in 1/T and log-linear in p between grid points, and the control says "interpolated between 350 K and 400 K" |
| **not computed** | greyed, with a "low confidence — request computation at this point" button that mints the cart item with the environment filled in (Reto 2026-11-03, decision 3) | T outside the box or on a pathway without modes; any kinetic number at U ≠ run U (kinetics has no U dependence — CHE is thermodynamic post-processing, `electrochem-kinetics-beyond-che.md`, kinetics-engine-v2 slice 8); the resting state without a coverage scan |

Cutoffs (decision 3, "within reasonable range; pick a cutoff"): a kinetic
number is *interpolated* only between two adjacent grid points of a
stored sweep (the quest default box is 8×8 over 350–900 K and 0.1–100
bar, so at most ~80 K and ~2.7× in p between neighbours); outside the box,
or with no sweep, it is *not computed*. The thermodynamic profile re-prices
closed-form in p (the ledger's own `kT·ln p` term, exact in the model)
and in T when modes are stored; without modes, ±10 K of the run T is
shown as approximate and beyond that the state is *not computed* with a
`measure_modes` item. U is exact in the model at every value.

This is the "what we can show vs what needs more simulation" partition,
made visible per control instead of argued per page.

### B. Left box — environment, with a Pourbaix tab and a Pareto tab

The box is a tab strip over one slider state (Reto 2026-11-03):
**Pourbaix** (the controls and mini map below), **Kinetic map**, **Rung
lines**, **Sensitivity** (the three in §I) and **Pareto** (§H: every
simulation on this slab as a selectable point). Every tab marks the
current point and a click in any map moves it.

- **Axes:** T (K); U vs RHE with the SHE reading beside it; pH;
  `p_total` and the per-gas inputs/outputs of the kinetics v2 D1 block;
  dissolved-ion concentration (`ion_conc_M`, today an input only to the
  bulk Pourbaix verdict and the spectator reservoirs
  `surface_pourbaix.Reservoir`; the control says so — no cation or
  double-layer term exists in this model).
- **Snaps**, as buttons on the U slider: `→ U_L`, `→ U_opt` (exist), the
  quest operating point (`operating_conditions`, −0.3 V / pH 7 on
  qu164903), the staircase window centre, and the sweep optimum (T,
  p_total) when stored.
- **Mini Pourbaix map:** the structure's resting-state map
  (`/pourbaix/<st>`, `quest.pourbaix_map.maps_for_structure`) drawn
  small, the current (U, pH) marked, click sets the point
  (`pointFromEvent`/`setPoint` already do this on the full page); the
  termination at the point and the coads-mismatch verdict
  (`staircase_states.point_states`, served by `/pourbaix/<st>/states`)
  printed under it. Today the map SVG exists only inlined in the full
  page (`pourbaix_svg.map_svg`, 760×460 with a fixed legend); the embed
  needs a map endpoint (SVG at a requested size, or the regions JSON the
  page already islands as `script.pbx-ejson` plus a client draw). No
  scan → the stub plus a cart item "surface_coverage_scan on st549459
  (~1 GPU-min, mace:medium)". The map, the staircase panel and the
  explorer share one slider state; moving any one moves all (SPSO §7).
  The link from a pathway to its map is
  `/pourbaix/st<meta.candidate_ref>?pathway=pw<id>`; note `/pourbaix`
  picks its pathway by link and ready status (`staircase.ranked_pathways`)
  while `window_objectives._candidate_pathway_ref_id` picks by
  `candidate_ref` — the embed must pin the viewed pathway explicitly.
- **Hover help** on every control: what the axis is, what it moves in
  this model, what it does not (pH on the RHE scale moves no PCET rung).
- **pH ships with the box** (decision 5). The page already has a pH
  input, but it changes only the SHE readout. The three routes by which
  pH moves the answer — fixed-SHE operation, product protonation
  (NH₃/NH₄⁺ pKa 9.25, NH₂OH pKa ~6, on the desorption rung), the HER
  competitor — are specified in `pathway-selectivity-u-ph-window.md`;
  their post-processing mechanism is built with slice 6 and applies to
  any pathway. Using the resulting window to *rank* qu164903 stays under
  that item's hold (a window over an incomplete network is the wrong
  window).
- Every change round-trips through the URL (gr486822 pattern) so a view
  is a link.

### C. Right box — visual filters

- **Barrier threshold** (eV slider): hide a step whose forward barrier
  exceeds E_max. Reference ticks on the slider are the thermal
  equivalents at the current T: `E = kT·ln(ν·τ)` with ν = k_B T/h, for
  τ = 1 ms, 1 s, 1 h (0.59, 0.77, 0.98 eV at 298 K). Default τ = 1 s.
  Measured NEB barriers count; BEP estimates count only when the "use
  BEP estimates" layer is on; a step with neither is never hidden by
  this filter (it is marked "no barrier known" instead).
- **Turnover threshold** (log slider, /s/site): terminate a route where
  a step's net flux at the stored kinetics environment falls below the
  threshold. Default 1e-10 /s/site. The number is the v2 solve's
  per-step rate where present, else the v1 chain's `net_rate`; the panel
  names which. Off the stored environment the filter says "flux at
  T = 298 K, 1 bar; re-solve to update" (cart).
- **Traversal:** both filters walk the DAG from the root; a node stays
  visible while some uncut route reaches it; a cut edge gets a glyph
  (`barrier 1.4 eV > 0.77 eV` / `no turnover: 1e-100 /s`) with hover
  text, and the route picker marks a cut route "cut at NO@N→N+O" (the
  same mark gr553482 asked for routes cut by an unpriced step).
- **Annotation layers**, one named checkbox each with hover help, state
  in the URL: H* supply legs (the `shoulder` of `pwFoldPath`, drawn as
  `line.pw-shoulder-drop`; off = fold each H* adsorption + hydrogenation
  into one PCET rung, the `staircase_masks.pcet_route` convention);
  `+H⁺+e⁻` feed arrows (`g.pw-hydro-arrow`, `pwLinkAnnotations`); energy
  numbers with ±σ (today the number is printed only when `energy_std` >
  1e-6, else it lives in the tooltip); desorption labels (`−H₂O ↑`,
  `pwFormatAnnotations`; H₂ is not in `PW_MOLECULES`, so `−H₂ ↑` needs
  the `~h2` landing or `link.removed`); Ea labels on transition lines
  (shown only when barrier > 1e-3); measured TS humps (`humpPoints`);
  BEP-estimated humps (dashed, with band; slice 1); fork percentages
  (`computeForkProbabilities`, today appended to the Ea label only);
  low-confidence hatching (today node-level red only; edge
  `low_confidence` is unstyled on curves); the states table.
  Everything above lives in `renderDiagram` and the node-tested pure
  block `// pw-annot:begin … :end` (`tests/precis_web/test_pathway_detail.py`
  lifts it); the toggles are inputs to those functions, not a second
  renderer.

### D. Per-step kinetics on the diagram

Hover on a transition line shows, at the stored environment: barrier
source (`neb` / `bep` with band / none), Ea forward and reverse, `k_f`,
`k_b`, net flux, reversibility `z_i`, `X_RC`; and names the solve the
numbers come from (v2 mean-field or v1 chain — gr549748). Each of those
quantities is also a **viewing checkbox** in the right box (Reto
2026-11-03): on, it is printed as a label on the transition state;
default on for the source, off for the rest, under one "kinetics on
transitions" master toggle, state in the URL like every other layer. What exists: a hump is drawn only when the graph
link's `barrier` is finite (`stepGeom`), a null barrier is a dashed
"Ea n/a" line, and the diagram payload (`routes/refs.py::
_pathway_graph_payload`) carries no barrier source, no `bep` fit and no
trust field; per-edge rates exist only in the kinetics blob
(`pathway_kinetics.kinetics_payload`, `transitions[] = {name, from, to,
kind, dG_eV, barrier_eV, k_f, k_b, net_rate}`, v1 only), with nothing
joining a transition name to a graph link. Slices 1 and 3 add the join
(edge key ↔ transition name; `flux_share.py` already strips the
`~history` suffix) and the per-edge fields to the payload. Where a T sweep is stored, a small Arrhenius inset
(ln k vs 1/T from the sweep's per-point rates; the fitted apparent
prefactor and Ea; ±10 K read-off). Desorption edges show the product's
selectivity share, which answers "how is the water formed / how likely".
A short "how rates are estimated" note (Eyring on `G_TS − G_IS` and
`G_TS − G_FS` from one ledger, BEP as the fallback, sticking and the
entropy model) sits under the panel.

### E. Compute cart

A per-pathway request list. Each item is typed and maps to a door that
exists:

| item | door | measured cost |
|---|---|---|
| NEB on chosen steps | today the whole candidate: `put(kind='structure', mode='rerun', args={'tier': 'neb'})`; per-edge NEB is neb-structure-builder slice 3 (unbuilt) | verify seed ~5 GPU-h; neb tier less; promotion gate wants barriers only on decisive edges |
| coverage scan for the slab | `surface_coverage_scan` (precis-pathway-help §Scan a candidate's coverage anchors) | ~55 s GPU (jo482106) |
| normal modes (T sweeps need them) | `put(kind='pathway', mode='measure_modes')` | 40–66 s |
| kinetics at another environment / a T–p sweep | `put(kind='pathway', mode='recompute_kinetics', args={environment, objective, sweep})` | 10–25 min CPU (jo548252/3) |
| more seeds / higher model | `mode='rerun'` with `seeds` / `tier` | per seed |

Who mints (decision 2): **the user, or the quest.** A user mints any
item from the page or the MCP door. The quest mints from the per-slab
environment optimizer (`slab-environment-optimizer.md`): when the best
setup it finds lies where stored data ends — outside a sweep box, on a
resting state with no scan, on a decisive edge without a barrier — the
optimizer's `env_next` becomes cart items, minted under the quest's caps
and tote like any other dispatch. A held item (`fidelity_restale_cap =
0`, item 25 "no re-runs") is **queued, not dropped**: it shows as held
with the hold named and runs at the lift (my default; Reto did not rule
on it).

Rules: every item carries its idem key (a repeat collapses onto the
running job); owned GPU
counts as $0 cash and the bound is wall time (Reto 2026-10-29), so the
cart shows GPU-minutes and CPU-minutes, and a cash estimate only for
paid lanes; the $25 rule applies per request. Minting is the same code
path as the MCP doors (`put(kind='pathway', mode='request',
args={'items': [...]})`), the page's button calls it. Progress: the
jobs are linked to the pathway and the explorer shows each item's state
next to the control that asked for it; a landed result flips the
control's honesty state. Storage (decision 4, mine): one `todo` per
cart ("compute request: pw549461") linked to the pathway, job refs as
its children, nothing in `refs.meta`.

### F. One environment record

One `Environment` shape — the kinetics v2 D1 block (T, p_total, gases
inputs/outputs/inerts, sticking) plus `electrochemical {U_vs_RHE, pH,
ion_conc_M}` — is what the page's URL encodes, what `view='staircase'`
and `view='analysis'` take as `args`, what `recompute_kinetics` is given,
and what every stored result is keyed on (`environment_basis`). Today
the pathway page and the Pourbaix page both read `u`/`ph` but with
different ranges (−1.5…0.5 vs the map's window), defaults (bounded
U_opt or −0.5 and pH 7 vs the operating point or 0) and omission rules,
and `/pourbaix/<st>/states` spells them `U`/`pH`; the record fixes one
encoding for all three. The page
reads stored results through that key: exact match, bracketed
(interpolate), or none (cart). Legacy pathways with no block project to
the reference environment, as the harvest already does
(`precis.quest.environment_basis`).

### G. Operating-point optimum for one slab

Owned by `slab-environment-optimizer.md` (filed 2026-11-03 from Reto's
decision 2): the quest finds each slab's best setup over U, pH, T,
pressures and salt against three goals — the target reaction most
favourable at every step, thermodynamically and kinetically; side
reactions minimised; rate maximised — and names the next point to
compute. What exists today is separable: the (U, pH) window and Shapley
attribution on the staircase (`window_objectives.window_measures`), and
the (T, p_total, composition) optimum of the kinetics sweep with a rate
floor (`subject_to`, gr543723). The explorer's part: the snaps in the
environment box (`→ env_best`, `→ env_next`) and the Pareto tab below.

### I. Pourbaix-adjacent views for navigating the environment (Reto's ask, 2026-11-03)

The Pourbaix map answers "which surface exists at (U, pH)". Three more
views answer the other navigation questions, each from data that exists
or is closed-form, each a tab with the point marked and clickable:

| tab | question | axes | data today | render today |
|---|---|---|---|---|
| **Kinetic map** | what holds the surface and how fast it turns over at (T, p) | T × p_total (or any two sweep axes) | the stored sweep rows: per point TOF, selectivity, coverages, `drc_top`, orders (kinetics v2 D7; 46 quest pathways) | none — the panel reads `kinetics_sweep` only for the target name (`pathway_kinetics.py`); `view='kinetics-sweep'` is text |
| **Rung lines** | which rung limits at which U, and where each one crosses zero | U (one line per rung: ΔG0 + n_e·U; HER rungs dashed) | `che_affine` and `her` on every engine ≥ 0.25 pathway, via `staircase_routes.routes_for_pathway` | none in the explorer; the staircase table re-tilts on `/pourbaix` |
| **Sensitivity** | which knob to turn, and which way | a tornado: ∂ln TOF/∂ln p_i and ∂ln S/∂ln p_i per gas, apparent Ea, n_e of the limiting rung, ∂ΔG_lim/∂T from modes | `orders` and `ea_app` on the v2 record (MF stencils; `ea_app` failed on pw549461), n_e exact; selectivity stencils are the optimizer item's addition | none |

Two layers, not tabs: on the Kinetic map, the **MARI regions** (the
most abundant intermediate per point — the kinetic twin of the resting
termination; the sweep's coverages give it) and the dominant `drc_top`
step; on the Pourbaix map, the **limiting-rung colouring** (which rung
closes the window at each point, from `point_states` over the grid; the
Shapley field has no render in `precis_web`, SPSO slice 8 deliverable b)
and the **bulk diagram at the ion concentration** — the one view where
the salt slider visibly does something today (the dissolution
boundaries move by `0.059/n·log c`; the bulk verdict overlay already
reads `bulk.ion_conc_M`). A pathway without a sweep shows the Kinetic
map stub and the cart item; Rung lines and Sensitivity need no new
compute.

### H. Pareto tab — every simulation on this slab

The left box's second tab (Reto 2026-11-03): all simulations run on this
candidate as points in objective space, selectable. Points are (pathway
× environment point): every pathway on the candidate (`get(kind=
'structure', view='pathways')`, gr540871 — tiers, seeds, engine
versions), each at its run point, at every stored sweep grid point, and
at every `recompute_kinetics` result; coverage scans appear as the
resting-state mask. Axes: the optimizer's objectives (worst rung margin,
selectivity, HER margin, log TOF) plus wall time and engine version, with
the hierarchical picker gr553414 asks for on the quest plot; the same
plot code (`quest/frontier.py::plot_axes_for`,
`templates/refs/quest_detail.html.j2`) scoped to one slab. Untrusted
points hollow, with the trust note on hover; the slab's own frontier
drawn. Click → the explorer loads that pathway at that environment (the
URL encoding of §F). The quest-level Pareto stays where it is; this tab
is the slab's.

## Slices (one gripe each; the gripe id is the ship record)

1. gr559345 — BEP-estimated barriers drawn as their own layer; tier
   banner (small).
2. gr559346 — named annotation toggles with hover help, URL-persisted
   (medium).
3. gr559348 — per-step kinetics on hover/labels; Arrhenius inset;
   methods note (medium).
4. gr559349 — visual-filter box: barrier and turnover sliders, DAG
   traversal, cut glyphs, route-picker marks (large).
5. gr559350 — one environment record + URL encoding + basis lookup
   (medium, precis-side; prerequisite of 6 and 7).
6. gr559351 — environment box with the mini map on the shared slider
   state (large).
7. gr559352 — compute cart: `mode='request'`, hold and cost rules, page
   button, progress (large).
8. gr559353 — the optimizer's snaps in the box; the optimizer itself is
   `slab-environment-optimizer.md`.
9. gr559428 — Pareto tab per slab (§H; large, after 5).
10. gr559429 — Kinetic map, Rung lines and Sensitivity tabs plus the
    MARI and limiting-rung layers (§I; medium, after 5; the Kinetic map
    only where a sweep is stored).

Filed beside them: gr559354 (catpath: transient θ(t) from an initial
coverage, decision 6) and gr559355 (`view='kinetics'` prints T = 300 K
beside a 298.15 K environment). The title is gr549574 (backfill pending)
and gr553416.

Order: 1–3 first (stored data only; immediate value on every pathway),
then 4, then 5 → 6 → 7, then 8. The chemistry thread's rank rule puts UI
last; this item is the exception the catalysis thread already states (the
diagram is how results are read), and 1–4 touch no engine.

## Explicitly NOT in scope

- Coverage effects, lateral interactions, multi-site chemistry, bigger
  slabs (kinetics-engine-v2 slice 7; Reto: not yet).
- Kinetics as a function of U (electrochem-kinetics-beyond-che,
  kinetics v2 slice 8). This item only displays the gap honestly.
- Merging the two JS renderers (pathway-presentation-shared-module says
  frozen); the explorer stays the precis page's inline script. New
  payload logic goes into Python builders the offline report could share.
- The title and the readable catalyst name (gr549574, gr553416).
- NEB per edge (neb-structure-builder slice 3) and the promotion gate's
  decisive-edge selection; the cart calls what exists.

## Acceptance criteria

- On pw549461 the page says, above the diagram, that the barriers are
  BEP estimates at screening tier, and draws them as dashed humps with
  bands when the layer is on; every annotation layer has a named
  checkbox whose hover text names what it draws; the URL reproduces the
  view.
- With the barrier slider at 0.77 eV (τ = 1 s, 298 K) and BEP humps on,
  every step above it is cut with a glyph; moving T to 400 K moves the
  tick and the cut set.
- With the turnover slider at 1e-10 /s/site, route 1 of pw549461 is cut
  at the first step whose stored net flux is below it, the glyph names
  the flux, and the route picker shows the cut.
- The environment box shows T, U (RHE and SHE), pH, p and ion
  concentration, each in one of the three honesty states; on st549459
  the mini map is the "not computed" stub with a cart item; on st203306
  (which has a map) the map draws and a click moves the staircase and
  the explorer together.
- A cart with one coverage scan and one `recompute_kinetics` on
  pw549461 mints two jobs with idem keys, shows them as queued on the
  page, and a NEB item on a qu164903 candidate under
  `fidelity_restale_cap = 0` renders held with the hold named.
- `get(kind='pathway', id=…, view='staircase', args={'environment':
  {...}})` and the page's URL agree on one encoding.

## Target + blast radius

`src/precis_web/templates/refs/pathway_detail.html.j2` (the inline
explorer), `src/precis_web/routes/refs.py` (the pathway route and its
payload builders), `src/precis_web/pathway_kinetics.py` and
`static/pathway-kinetics.js` (per-step rates, sweep rows),
`src/precis_web/routes/pourbaix.py` and `pourbaix_svg.py` (the mini map
endpoint), `src/precis_pathway/handler.py` (views' `args`, the new
`request` mode), `src/precis_pathway/staircase_routes.py`,
`staircase_states.py`, `src/precis/utils/reaction_graph.py` (pressure
lever), a new `precis_pathway/environment.py`, the cart's todo/job
writes. No engine change in slices 1–7. Two node-tested pure blocks
constrain the JS edits: `// pw-annot:begin … :end` in the pathway
template and `// pbx-pure:begin … :end` in the Pourbaix template
(`tests/precis_web/test_pathway_detail.py`, `test_pourbaix.py`); the
offline autocatpath report is not touched (its script is a different
renderer; only `pathway_kinetics.py` and `pathway-kinetics.js` are
vendored from it and carry a parity test).

## Open questions / decisions log

Ruled by Reto 2026-11-03 (chat), recorded here:

1. **Defaults — decided.** τ = 1 s for the barrier equivalent, 1e-10
   /s/site for the turnover floor.
2. **Who mints — decided.** The user, or the quest (through the per-slab
   environment optimizer, under its caps). Held items queue and run at
   the lift: my default, not ruled; say so if wrong.
3. **Interpolation — decided.** Allowed within the cutoffs in §A; past
   them the control reads "low confidence" and offers the request
   button that mints the computation at that point.
4. **Cart storage — decided (mine).** One `todo` per cart with job
   children; nothing in `refs.meta`.
5. **pH — decided.** Ships with the box (§B); the existing pH input is
   an SHE label only.
6. **Transient kinetics — explained, stays an engine item.** Today the
   solver reports only the settled steady state. "Conditions from
   initial conditions, how it changes over time" is the time evolution
   from a clean or stated surface: which species takes the surface
   first, how long until steady state, whether a poison accumulates on
   the way. gr559354 against catpath; nothing for Reto to do.

Open:

7. **Pareto tab data volume.** A slab with 8 pathways × 64 sweep points
   is ~500 points; cap per tab or page the sweep points?
8. **Kinetics-on-transition labels** default set: source on, the rest
   off (proposed).
