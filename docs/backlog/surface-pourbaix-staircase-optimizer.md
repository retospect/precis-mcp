---
status: in-progress
title: a surface-Pourbaix validity map and linked reaction staircase drive slab search by where and why each candidate's operating window is lost
pillar: 3d-design
prio: high
owner: catalysis session
---

Status 2026-10-17: slices 1-7 shipped; slice 8 (Fe-doped Pd end to end) has a DRAFT contract below (not `ready`; vetting and Reto's §Slice 8 open questions pending). Reto decides: the Nov-2026 paper-date collision under qu459585.

# Surface-Pourbaix validity map + reaction staircase as a catalyst optimizer

**Lifted 2026-10-07** (Reto, via chat-interface): ready for slices 1–2,
owner = the catalysis session; see §Rulings below. **Build plan approved
2026-10-13** (coordinator thread memory me481988). **Wave A landed
2026-10-13**: engine tasks A1/A2 (catpath v0.25.0), slice 2 (A3) and slices
4+5 as the pure library `src/precis_pathway/staircase_masks.py` (A4) in main
01b36dba7, A5 = the slice-1 proof below. **Wave B (quest wiring)**: B1 anchors rung (`meta.surface_anchors`),
slice 3 staircase panel (B2) and slice 6 window/Shapley/HER objectives
(B3) all landed 2026-10-13 — wave B complete. **Wave C: slice 7 built
2026-10-14** (Reto: "continue with wave C") — contract below.
Filed 2026-10-02 as file-only. The full design is Reto's transfer prompt,
`~/.claude/projects/-Users-reto-precis-mcp/scratch/review-msgs/surface-pourbaix-transfer-prompt.md`; <!-- home-path: allow — names a scratch/review file in the old Mac project dir -->
this item is its index. Its **DECIDED** points are settled and are not
re-litigated here; its **OPEN** points are this item's open questions.

**Base, which this extends:**
- Part A, the bulk Pourbaix verdict job (`precis_dft/pourbaix_bulk.py`;
  its module docstring is the spec);
- Part B, the quest gate (`pourbaix-quest-gate.md`).

A and B answer "does the bulk survive at (U, pH)". This item adds the
*surface* phase diagram, the reaction staircase on whichever surface
exists, and attribution that tells the slab search *why* a candidate loses
its window.

## Motivation / why

The catalyst quests rank slabs at one condition. Reto's design adds an
environment space (U, pH, additives, T) that is cheap to move in once a
slab's anchor energies exist. In it:
- the surface Pourbaix diagram becomes a **validity map**: which slab
  model to trust where;
- each rung of the reaction staircase carves away operating window;
- Shapley attribution over those rungs, computed as a field over condition
  space, gives the optimizer a diagnosis ("bottlenecked on desorption,
  here"), not a scalar.

## Design summary (DECIDED in the prompt; § refers to it)

- **§3 Two-timescale nested search.**
  - Outer: slab space. Discrete, DFT-priced, surrogate-guided, with part
    of the budget spent where the surrogate is uncertain.
  - Inner: environment space. A continuous CHE sweep from the anchors,
    free.
  - Each slab is scored at its own best environment. A robustness score
    (the size of the stable *and* active window) is a separate Pareto
    objective.
- **§4 Axes.** Count independent degrees of freedom (about 5–8); about 2–3
  of them are expensive. Each additive is classified as spectator (cheap,
  inner) or adsorber (outer, fresh DFT), as a *region* on the map.
  Dissolved metal is an input (open system) or an output (closed) per
  campaign. Each subsurface-H state is a new slab.
- **§5 Three uncertainties, never blended:**
  - surrogate: compute this slab;
  - propagated thermodynamic: add an anchor; **build first**;
  - model-form: go get an experiment.

  The spread across functionals is a reliability band.
- **§6 Validity map** plus the **guaranteed-reset hysteresis contour**,
  estimated in order: thermodynamic overdrive → BEP from sparse NEBs →
  active-learned NEBs → full NEB. The DFT band and the reset band compose
  **in series, pessimistically**.
- **§7 Linked panels** (Pourbaix and staircase) on one slider bank, with
  honest sliders that flag off-grid moves. The operating window is the
  resting-state map ∩ the per-intermediate masks. Each rung is a toggle
  that projects its forbidden region.
- **§8 Shapley attribution** always, never leave-one-out (constraints
  overlap):
  - computed as a field over condition space;
  - reduced by sum of squares, inverse participation ratio and ridge
    churn, never a plain sum;
  - two levers, conditions (cheap) exhausted before slab proposals
    (expensive).

## Slices (the prompt's §11 build order)

1. Inner CHE sweep from existing anchors, plus propagated thermodynamic
   error bands (uncertainty source 2).
2. Resting-state surface map with the validity-map regions.
3. Staircase panel linked to the map (map → staircase).
4. Per-intermediate masks and rung toggles (staircase → map).
5. Shapley at a point → the Shapley field → its reductions (sum of
   squares, IPR, ridge churn).
6. Robustness score plus the Shapley vector fed to the outer Pareto front,
   with active-learning anchor placement.
7. Reset contour: proxy 1 → BEP → active-learned NEBs. **Built
   2026-10-14** (wave C): overdrive + the BEP/NEB margin ladder,
   pessimistic series composition with the DFT band, reset-window
   measures and the /pourbaix layer — contract below. Remaining on this
   rung: real `bep` exports from catpath (spso-c1) and active-learned
   NEB placement.
8. End to end on Fe-doped Pd, for the paper.

## Open questions (OPEN in the prompt, §10)

1. Activity vs stability: how "is it active" folds into the window score
   (resolved 2026-10-13, §Rulings below).
2. **Reference consistency:** every anchor on one footing (functional,
   corrections, H-electrode and gas references). Already measured here:
   MACE-MP-0 misprices N-containing gases (NO→NH₃ U_eq +0.13 V) and H* on
   Pd by about 0.25 eV; the per-molecule and Pd H* corrections
   (catalysis-selectivity-19) are briefed to catpath. This map needs the
   same corrected references.
3. Oxide DFT error and whether to trust empirical oxide corrections.
4. The cheapest validating experiment (likely one predicted boundary
   checked against a CV).
5. A cheap signal that a surface species is missing.
6. **Subsurface H: discrete slabs or a continuous occupancy axis?** The
   discrete route is what `pd-hydride-substrate.md` does: β-PdH twins as
   new slabs, consistent with catalysis-selectivity-16 and -18. Its stage
   0 showed MACE-MP-0 has no α/β gap at that sampling and overbinds H, so
   an occupancy axis from MACE would be untrustworthy too.
   **PBE H-flight check, done 2026-10-03** (catalysis-selectivity-26/-27,
   design note §22f). Next to a subsurface dopant (Ta), MACE moves H from
   the subsurface onto the surface with no barrier. PBE single points on
   MACE's geometries agree:
   - at the midpoint and the end, both next to Ta and on undoped β-PdH;
   - the Ta effect at the end is −0.29 eV in both methods;
   - undoped, MACE makes the move 0.07 eV more downhill than PBE.

   So MACE may build H-loaded surface states for the sign of
   subsurface↔surface H, and the depleted-shell construction rule stands.
   H coverage energies at a given U are not covered by this check; they
   keep the PBE spot-check rung.
7. Wulff construction: which facets, weighted by area under (U, pH).
8. How cross-functional spread enters the diagram and the score.

Also: the prompt says (§1) slab generation and an ML Pareto search exist.
Here those are the quest machinery (`catalyst-discovery-quest.md`, the
tier ladder) and catpath. The validity map's "which slab model" output
needs a quest-side representation that does not exist yet.

## Cross-links

- `encapsulated-metal-candidate-space.md`: the sibling that supplies the
  outer-loop candidates (carbon-encapsulated F1–F4). It adds a carbon/cap
  Pourbaix layer and host/containment constraints to this item's mask. It
  is blocked by this item's first slices.
- `pd-hydride-substrate.md`: subsurface-H states as new slabs (open
  question 6).
- catalysis-selectivity-19 gas and H* corrections
  (`scratch/catsel-catpath-brief-corrections.md`): open question 2.
- `catalyst-discovery-quest.md`: the outer Pareto loop this feeds.
- `neb-barriers-in-the-catpath-pipeline.md`: the reset-contour NEBs (§6.2
  steps 2–4). The prompt's **NEBscape audit** belongs there: compare our
  NEB setup with NEBscape on minima-hopping IS/FS generation,
  permutation-reduced atom mapping, FS→IS symmetry alignment, μ/τ
  reaction-distance ranking, and the fidelity criteria.
- `pathway-selectivity-u-ph-window.md`: the window objective. This item's
  per-intermediate masks generalise it.

## Paper (§9)

A proof of concept on Fe-doped Pd. Target ~**Nov 2026**. It is a quest
output, not a pillar: it belongs under **qu459585** (one preprint a month).
The organizer adds it to the paper/conference list. What is novel is the
synthesis: the validity map, the pessimistic reset contour, and the
Shapley field.

## Rulings 2026-10-05 (Reto; recovered from the retired Codex task branch)

Recorded here 2026-10-07 because the Codex catalysis window's checkpoint
held them and main did not. They stand; the 2026-10-07 rulings below
refine, not replace, them.

- **Q10, demonstration system:** Pd(111) from qu164903. The
  best-characterised stored anchor set is pristine slab `st203306`
  (`Pd111-pristine-baseline`) with pathway `pa246095` (50 anchor-key
  mappings to 48 structures `st246666`–`st246713`; aliases `*~n2o`/`N2O`
  and `*~nh2oh`/`NH2OH` share handles). Selecting it authorises no run
  and no production mutation.
- **Q2, canonical footing (resolved):** use exactly the model, correction
  set, CHE H-electrode and gas references the selected slab's stored
  energies already carry; state each in the map's provenance; add no
  corrections in slice 1. Mixed footings are reported and partitioned,
  never blended into one map.
- **Q8/Q9, uncertainty (resolved):** three named components, kept apart
  and never collapsed into one number: run-to-run spread, within-method
  correlated error across anchors, cross-functional (model-form) spread.
  Joint draws over all anchors (shared seed and functional per draw),
  per-(U, pH) phase probabilities, boundary coordinates at the 16th/50th/
  84th percentiles; unknown covariance → show the independent and the
  fully-correlated bound, both labelled; missing uncertainty inputs →
  the layer is marked incomplete, no invented sigmas. One map per
  functional; between-functional spread is a separate reliability layer,
  never a pooled Gaussian. Preferred within-functional source = the
  BEEF-vdW ensemble (none stored; td470248, approved 2026-10-07, is that
  follow-up).
- **Replicate inventory (read-only, 2026-10-05):** every one of
  `pa246095`'s 50 node energies is `n=1`, seed 0, MACE `mace:medium`; no
  DFT run, no BEEF ensemble, no per-node run handle (the only linked
  `struct_runs` row is the MACE relax of `st203306`). The stored
  energy-reference field names the gas ledger and `G(T,p)` tier but not
  the exact H-electrode, gas-reference records or a correction-set id, so
  that set is provenance-incomplete for a CHE map: report it as such,
  never infer a footing.
- **Consequence for the built slice 1:** anchors come from a fresh
  `surface_coverage_scan` whose footing is recorded per anchor
  (`anchor_key`, `engine_version`, `corrections`) instead of from
  `pa246095`'s node energies; the θ→0 comparison to the pathway is the
  bridge back to the stored set. Open prompt questions after these
  rulings: Q1, Q3–Q7 (q6/q7 ruled 2026-10-07 below).

## Rulings 2026-10-07 (Reto, via chat-interface)

- "File only" LIFTED. Slices 1–2 are `ready`; owner = the catalysis
  session; started after the catpath lock. Anchors compute on the
  locked engine (0.24.0 ruled 2026-10-07; floor is 0.24.1 since the
  headless-matplotlib fix, tag v0.24.1): engine token recorded per anchor.
  The engine changes (A1: prebuilt slab + corrections + shared lattice
  derivation in the coverage scan; A2: per-edge/per-route affine CHE
  export `che_affine` and an HER side branch `network.her`, kept out of
  the headline U_L) are **autocatpath 0.25.0, cut 2026-10-13** (tag
  v0.25.0, f88e45f); precis pins it from the same day.
- Sequencing: first build = anchor inventory + inner CHE sweep with error
  bands (slice 1). The hydride pilot (catalysis-selectivity item 23, still
  HELD) hangs off slice 2.
- v1: q6 = discrete slabs (settled); q7 = (111) only.
- Slice 6 needs a quest-side representation of "which slab model to
  trust" — add it to that slice's scope.
- Open task for the owner before slice 1: do O, OH and H coverage anchors
  on Pd(111) and β-PdH already exist from the qu164903 runs, or must they
  be computed? Report the answer to Reto.
- `PRECIS_MP_API_KEY` is in the overlay; the map must not block on the
  Materials Project import either way.
- q2/q3 (reference consistency, oxide corrections): ruled later; see
  2026-10-05 (q2) and 2026-10-12 (q3).

## Rulings 2026-10-12 (Reto, chat)

- **q3, oxide corrections: ruled.** Apply the Materials-Project-style
  per-element oxide corrections to the *bulk* oxide references only (Part
  A already goes through pymatgen); leave surface O*/OH*/H* anchors
  uncorrected and say so per anchor; draw the oxide phase boundaries as a
  labelled uncertain layer (a few tenths of a volt). For the NO→NH₃
  window this matters little: the operating point is cathodic, so the
  resting-state question is H coverage versus β-PdH, not oxide.
- **q2 stands as ruled 2026-10-05** (one footing per map, mixed footings
  partitioned, never blended); nothing further to decide for v1.
- **The layered diagram must be visible in the viewer.** Reto: "each
  reaction contributes a masked area (where it can operate) and we overlay
  those." The overlay is already the design (§7 above, prompt §6 "compute
  the resting-state map first, then overlay the per-intermediate
  constraints as a mask"), but no rendering surface was specified and
  `precis_web` has no Pourbaix page. Slices 2–4 therefore each ship a
  render: slice 2 the resting-state map as a page under the quest and the
  slab (`/pourbaix/<handle>`) plus `view='pourbaix'` on the quest and
  structure kinds for agents; slice 3 the staircase panel beside it on the
  shared slider bank; slice 4 one translucent mask per intermediate
  (toggle per rung) overlaid on the map, the intersection drawn as the
  operating window. Same object on MCP and web (roadmap §Surfaces). Reto
  offered the fuller reminder prompt if the owner needs it.
  The slice 4 render is built (gr489471):
  `precis_pathway.staircase_states.point_states`, `/pourbaix/<st>/states`,
  the map picker and overlays, and `view='staircase'` with `U`/`pH`/`point`.
  **Left:** the prod check once the st203306 affine HER run lands. A
  point inside the window should show every rung viable; a point in
  another termination region should show every rung off-map, with that
  termination named.

## Slice 1 contract (built 2026-10-07; the prompt's §3 inner loop + §5 source 2)

**Anchor inventory (prod, read-only, 2026-10-07):** no coverage anchors
exist as refs — 376 structures on qu164903, 0 β-PdH/hydride structures
anywhere, 0 explicit O*/OH* coverage structures, no coverage/anchor meta
keys. The 257 harvested candidates embed one pathway each whose nodes hold
single-adsorbate energies (one per supercell, n=1, 0.22.0 engine, no
corrections): an implicit θ→0 point per species, not a coverage series.
So anchors are computed, not read.

**What shipped:**
- `surface_coverage_scan` job_type (`src/precis_pathway/coverage_job.py`):
  one `(slab config, MLIP model)` run of catpath's `coverage` scan in a
  killable child (`runner.run_coverage_scan_subprocess`, the seed child's
  `python -m precis_pathway.runner` entrypoint with `mode: coverage`).
  Defaults: adsorbates H, O, OH at 0.25/0.5/0.75/1.0 ML; (111) only (q7);
  `coverage.facets` must be empty. A prebuilt slab was refused at
  first-landing because the engine's scan built its own clean slab;
  since autocatpath 0.25.0 `slab_extxyz` (or `structure_ref`, resolved
  server-side) injects the candidate's slab, so doped slabs and β-PdH
  twins scan on their own geometry (`coverage_job.py` module docstring).
- Footing per anchor set (acceptance "one footing"): `meta.anchor_key`
  (`runner.coverage_anchor_key`: config minus `mlip` + engine version),
  `meta.model`, `meta.engine_version`, `meta.corrections` (what the scan
  itself records, never the config's block: on 0.24.0 `coverage.scan`
  applies neither the gas set nor the H* shift — they run only in
  `pipeline._corrections_plan` at aggregation (catpath session,
  2026-10-07) — so a 0.24.0 anchor is uncorrected and keyed `None` until
  the engine task below lands (0.25.0); the pathway's `results.json` on 0.24.0 sits on
  the corrected gauge, so the θ→0 comparison must account for that).
  Anchors minted from 2026-10-13 carry `engine_version` 0.24.1 (the
  floor); 0.24.x anchors are uncorrected either way, and nothing
  precis-side corrects them.
- `precis_pathway.surface_pourbaix` (pure, engine-free): γ_A(n; U) =
  γ_A(n; 0) − n·ν_A·U/area with ν_A = 2n_O + 4n_C − n_H on the RHE scale
  (pH only tilts the SHE view by 0.059 V/pH); the resting envelope on a U
  grid; every boundary as the closed-form crossing of two affine lines;
  **two bands per boundary, never summed**: `band_propagated` (source 2:
  per-anchor bars — the run's own `search.energy_thresh` on both relaxes
  plus any stated reservoir-pricing bar — pushed through the arithmetic,
  σ_U* = √(σ_i²+σ_j²)/|s_i−s_j|; remedy "add an anchor", so
  `needs_anchor` ranks boundaries by it) and `band_model_form` (source 3:
  the same boundary's spread across the models that computed it, from
  two models up). The job pools every succeeded scan on its `anchor_key`
  (newest per model) and stamps the sweep as `meta.che_sweep` + a
  `job_summary` naming the resting termination at `point_U_RHE`.
- `compare_lowest_theta` is the θ→0 acceptance check (per-adsorbate
  formation energy at the lowest coverage vs a pathway's single-adsorbate
  energy); wired to the quest path as `surface_anchors._theta0_check`.
Hand-minted scans get no such stamp: call it per subject.

**Slice-1 proof, 2026-10-13 (jo482106, castor, 0.24.1, mace:medium, ~55 s):**
at −0.30 V_RHE clean Pd(111) rests as H* at 9/9 ML; the one boundary in
−1…+0.5 V is H*(9/9)→H*(7/9) at +0.44 V (propagated ±0.05 V; no
model-form band, one model); O*/OH* never win. θ→0 acceptance against
pw455722 (a 0.22.0, uncorrected pathway, so no H* gauge offset is
expected): Δ(anchor 2/9 ML − pathway single-adsorbate) = H −0.08, O −0.06,
OH −0.08/+0.29 eV (the pathway's own OH* seed spread is 0.375 eV). Passed
where the pathway is self-consistent. Surfaced: the scan relaxes its own
lattice (a = 3.958 Å) while the pipeline slab sits at 3.914 Å for the same
config (task A1 addendum: one lattice code path); the default coverage
grid is not cell-aware (gr482107); pathway structure refs are keyed on
the slab-blind `content_key`, so same-key pathways overwrite each other's
`pw-*` structures (gr482121, root-caused; quest ranking unaffected).
**Caveat on the comparison:** pw455722 is candidate 209903
(Pd111-Sn-subsurf-1, slab Pd35Sn1), not clean Pd(111): the seed partials
carry the doped slab even though the pathway's `meta.config` looks clean.
The agreement therefore bounds the single-subsurface-Sn effect plus
lateral/lattice terms at ≲0.1 eV; a strictly clean θ→0 check needs a
clean 0.24.1 pathway on st203306 (pa246095's footing is incomplete).

**Engine task A1 (shipped, catpath 0.25.0):** the coverage scan honours
the injected slab (doped slabs, β-PdH twins; `n_slab` from `slab.info` so
lattice H is not counted), applies the same gas set and H* shift as the
pipeline under `cfg.corrections`, and records a `corrections` block plus
`slab_source: injected|built` in `coverage.json`. Mint recipe for further
scans: `put(kind='job', job_type='surface_coverage_scan',
parent_id=<structure id>, params={config: REACTION_CONFIG, point_U_RHE:
-0.3, structure_ref: <structure id>})`, `target_node` unset (the
`ssh_node` executor routes it); omit `structure_ref` for a clean slab.

## Carried from qu164903-campaign (the old `autocatpath_coverage` spec)

Slice 2's qu164903 asks, beyond the render ruling above:
- the pathways' U_L and U_opt drawn as vertical lines on the map, with a
  verdict "at U_L the slab is H*(2/3 ML), the pathway assumed clean" when
  the winner at U_L differs from the pathway's `meta.params.coads`;
- `view='pourbaix'` on a structure lists terminations and the winner at
  U ∈ {0, U_L, U_opt}; the quest results table gains a `winner@U_L` column;
- provenance footer: model, T, pressures, thermo tier, and the engine-v1
  limit (single-species terminations, no lateral interactions);
- test: a pH change moves axis labels only, ΔG lines unchanged on the RHE
  scale.
- the structure page as hub with URL-addressable tabs
  (`?tab=structure|pathway|pourbaix|runs`; qu164903-campaign items 4/6,
  folded here by Reto 2026-10-21): pareto click → `?tab=pathway` with the
  best trusted pathway's profile inline; Pourbaix tab = (b)+(c) when
  coverage data exists, else a "not computed — N relaxations" stub. Needs
  the pathway route's payload builders factored out of the inline-JS
  profile viewer.

## Sign convention (fixed 2026-10-13, coordinator; binds A2 export and A4 masks)

Staircase rungs use the engine's CHE form: ΔG(U) = ΔG₀ + n_e·eU with
**n_e = proton-electron pairs consumed** by the step (positive for a
reduction, negative for an oxidation, 0 for a chemical or desorption
step), so a reduction rung gets easier at more negative U_RHE and
U_L = −max(ΔG₀/n_e). The surface sweep keeps its own ν = electrons
*released* (γ(U) = γ(0) − ν·U/area); the two are documented per module,
never mixed in one contract.

## Rulings 2026-10-13 (Reto: "that's good")

- **HER is a mask rung and its own Pareto objective**, not a scalar penalty
  on the window score.
- **Open question 1 (activity vs stability), resolved:** rung masks carry a
  tolerance ε on ΔG; a TOF floor is a separate objective, not folded into
  the window.

## Ruling 2026-10-15 (gr489164 defect 1)

- The mask reads each route in the one-rung CHE convention: an H*
  adsorption rung and the hydrogenation that consumes it are one PCET rung
  (`staircase_masks.pcet_route`) for viability, the window, Shapley and
  U_L; the reset contour and the rendered staircase keep the engine's
  split. Chemical (n_e = 0) rungs default to ε = 0.3 eV
  (`DEFAULT_EPS_EV`; Reto: "I like the 0.3 eV thing"), electrochemical
  rungs to 0; `window_objectives.eps_eV` still overrides.

## Rulings 2026-10-16 (Reto, morning queue item 9)

Morning-queue record: "RULED 2026-10-16 (1b labels only; 2b catpath
ledger NH4+ → PR+bump+wheel; 3a retry cap 2 fresh seeds)".

1. **Desorption label (b):** only a route-terminal edge into a `~`
   product is labelled desorption; a mid-route `*~x` node is not. Labels
   only — no energetics, ranking or masking change
   (`staircase_routes._step_kind`; `pcet_route` still never folds a step
   into a `~` landing).
2. **NH3/NH2OH re-reference to NH4+(aq) at pH < 9.25 (b):** engine-side
   in the catpath ledger, then a version bump and wheel. Engine-side,
   catpath PR pending.
3. **Failed anchor scans (a):** retry with its own idem-key generation,
   capped at 2; Reto: "change seeds each time". A retry follows a scan
   *failure* only, never a success with bad numbers.

## Spectator axes (ruled 2026-10-15)

Three tiers, cheapest first.

1. **Spectator axes** (p_NO, chloride as a spectator, dissolved metal) —
   *built 2026-10-15 (p_NO, 68a9901df).* Cheap: each shifts one species' chemical potential
   linearly, and the map and staircase recompute for free. Slice 2 builds
   the generic axis (`surface_pourbaix.Reservoir`: a species, the element
   it sets, an activity) with p_NO as the first instance; every further
   spectator is a config line. One slice. Mixed NO*+H* phases come from
   catpath's `coverage.mixtures` (gr489475), never from summed
   single-species γ. precis requests the feed + H* grid and parses
   `facets[].mixtures[]` into per-species-count anchors; the request is
   gated on the engine having the field. Left: the catpath push and
   version bump, then a real-MLIP NO+H scan on an n_top ≥ 8 slab.
2. **Temperature** — *ordered, not scheduled.* More than a slider: the
   anchors need vibrational and entropy terms per species, which the
   engine doesn't store, so an engine addition comes first. One slice,
   later.
3. **Adsorbers** (chloride that binds Pd, subsurface H) — *ordered, not
   scheduled.* New slabs in the outer DFT-priced loop, not sliders:
   slice-8-class work, plus the open subsurface-H question.

- **Open, for the pillar review:** paper-date collision. This item's
  Fe-doped Pd PoC (~Nov 2026) and the catpath trust-demo paper
  (`threads/chemistry.md`) both sit under qu459585's one-paper-a-month.
  Unresolved.

## Slice 7 contract (the §6.2 guaranteed-reset hysteresis contour)

Built 2026-10-14 as wave C (Reto: "continue with wave C"). This is the
slice's `ready` record plus its I/O contract.

**Inputs** — per-edge affine CHE from `results.json`:
`che_affine.per_route[].steps[] = {edge, delta_g0, n_e}` (the signed
convention above: ΔG(U) = ΔG0 + n_e·U, n_e = pairs consumed, pH-flat on
RHE) and `nodes[].std` → the rung's propagated σ. Per-edge kinetics when
the pathway provides them: `edges[<name>].barrier` (a measured NEB
Estimate) and `edges[<name>].bep = {alpha, beta[, n_fit, rmse]}` — the
catpath spso-c1 export, E_a = alpha·ΔE + beta fitted on that run's sparse
NEBs. Edge names join che_affine `steps[].edge` to `edges[].name`.

**Estimator ladder** (§6.2's order) — each edge gets a *required
overdrive margin* m_e with the rung's label kept per edge:

- `overdrive` — m_e = η (default 0.2 eV, "several hundred meV";
  parameter, not measured).
- `bep` — m_e = (β − floor)/α, clipped ≥ 0: guaranteed where ΔG ≤ −m_e,
  i.e. where the predicted forward barrier hits `floor` (default 0, "no
  barrier left"). α ≤ 0 can't extrapolate → `overdrive`, noted degenerate.
- `neb` — m_e = the measured barrier itself: guaranteed where the driving
  force exceeds the recorded barrier (a lone barrier can't be re-tilted
  without α; it tightens or loosens flat η honestly).

A `bep` block wins over `barrier`, which wins over `overdrive`. Nothing
invented: an edge with no kinetics record stays `overdrive`.

**Outputs** — per grid point on the slice-2 (U_RHE, pH) grid: `reset_ok`
(every edge of the cycle guaranteed: slack_e = −ΔG_e − m_e − σ_e ≥ 0 — a
cycle must close to reset), `margin` (min_e slack_e, the limiting edge's
pessimistic slack), `limiting` (the argmin edge's name), and the *band*:
`viable & ¬reset_ok`, the hysteretic zone §6.2 says not to rely on.

**Series composition, pessimistic** (§6.3): σ_e — the propagated DFT band
on ΔG, hypot of the endpoint stds — is added to m_e before the margin is
applied. The reset requirement launches from the far edge of the DFT
band, never the nominal line; the two bands are never averaged.

**Feeding the window and the frontier.** `window := window ∩ reset_ok`
— the guaranteed window is stamped *beside* the thermodynamic one:
`reset_window_area`/`reset_window_fraction` are new declared measure keys
(`window_area_VpH` keeps its meaning; the layers compose, they don't
blend). On `/pourbaix` the hysteretic band draws as its own layer,
separate from the propagated/model-form bands.

## Rulings 2026-10-29 (Reto, review queue items -28, -29, -30)

- **Item 23 stays held until slice 8 has run** (-28 option 1): the Pd-hydride
  NEB pilot waits for a resting state read off the optimizer on a real
  candidate, not for the slices merely existing.
- **$25 rule on owned GPU lanes (-29.1):** owned hardware counts as $0 cash;
  the bound stated per re-run is wall time (≤1.5 GPU-h per seed, 5400 s
  cap; median 66 min measured on worker 8.37.x).
- **Restale paused on qu164903 until item 25 lifts (-29.2):** the item-25
  hold was prose only; the restale pass (gr510764, on prod) is the path
  that could re-run stale candidates under it.
- **Slice 8 (-30):** footing = `mace:medium` plus one spin-polarised DFT
  check on an Fe anchor (a separate item, not in this slice); scans
  hand-minted, no demo quest; subject set = st202238 (A) and st463208 (B)
  against baseline st203306. The November paper collision was ruled on
  2026-10-30: td459589 (the validation paper) holds November; this paper
  (td462597) has no month yet.

## Slice 8 contract (DRAFT v4 2026-10-30, design review folded in, second-vet fixes applied; not `ready` until a third look; footing, path and subjects ruled 2026-10-29)

The paper's proof of concept (§Paper): the optimizer run end to end on
Fe-doped Pd(111) reads a candidate's window and says why it wins or loses
it. Slices 1–7 supply the stages; slice 8 adds a small library (stage 4) and
a fixed protocol, one footing, and its deliverables.

**Two parts, one contract.** **Part A** is repo-only and gate-checkable: the
code and unit tests listed under stage 4 "New code" (plus the one-argument
change to `_theta0_check` in stage 1). **Part B** is the prod run, stages
0–6, and the paper table; it needs prod compute and Reto's stage-0 decision.
B's stage 4 needs A's functions; B's stage 0 can start while A is built.

**Demonstration set** (qu164903 (NO→NH₃ selectivity) structures, (111) only, q7):

| role | structure | note |
|---|---|---|
| baseline | st203306 `Pd111-pristine-baseline` | clean Pd(111); jo496906–jo496909 are on engine 0.31.0, so re-mint on the slice-8 footing |
| subject A | st202238 `Pd111-Fe-sub-surf-1` | one Fe in the first subsurface layer |
| subject B | st463208 `Pd111-Fe-subsurf-1-H-subsurf-1` | A plus one subsurface H, held fixed (q6 discrete slabs) |

Prod read 2026-10-26: A and B have no succeeded `surface_coverage_scan`; B
serves qu164903 and has 8 pathways (pw463210…pw517889), unchecked for
`che_affine`.

**Footing (one per map, q2).** One model, one engine pin
(`_autocatpath_pinned_version`), corrections as the engine records them.
`mace:medium` (ruled 2026-10-29): every stored qu164903 number is on it and
a scan takes about a minute (jo482106). Stated in the paper's first
paragraph. Backed by one spin-polarised DFT point on an Fe anchor (its own
item, not this slice; see deliverable (a) for how it enters the table).

**Path (decided).** Scans are hand-minted by the slice-1 recipe on the three
structures; no demo quest, and qu164903's `meta.surface_anchors` stays off
(flipping it mints a scan for every live neb/front candidate, not three).
Hand-minting skips the quest path's harvest, so the builder replaces each
piece explicitly: retry by hand per the 2026-10-16 ruling (cap 2, fresh
seed per try, after a failure only); `compare_lowest_theta` called per
subject; the resting termination read from the scan's `job_summary`. The
alternative, a dedicated demo quest with `reaction_config`,
`operating_conditions` (−0.3 V, pH 7) and `surface_anchors`, would give
harvest, retry and `theta0_check` for free but is a prod quest creation;
Reto ruled hand-minted on 2026-10-29.

**Stages.**

0. **Audit** (read-only; before any scan is minted). For each of baseline, A
   and B, list the pathways of that slab: no MCP affordance does this
   (`get(kind='pathway')` lists recent pathways only), so use a read-only
   `scripts/prod-psql` select over `refs` with `kind='pathway'` and
   `meta->>'candidate_ref'` = the structure id, and file the missing
   affordance as a gripe. A pathway qualifies only if all hold: (a) its slab
   is the stated one: `candidate_ref` equals the structure id and
   `structures_for_pathway` confirms it (gr482121; `meta.config` can look
   clean while the seed partials carry a doped slab, see slice 1); (b)
   `status` is ready; (c) `routes_for_pathway(store, id).missing` is empty,
   i.e. `meta.results` carries `che_affine` and `her` (without `her`, the HER
   margin is silently unset and stages 3–4 cannot produce it); (d) succeeded
   seed partials exist (else the lowest-θ check says "pathway has no
   succeeded seed partials"). Also check that `mace:medium` has a correction
   set in the engine (`build_map` flags an uncorrected footing); if it has
   none, every lowest-θ comparison reads "footing differs" and the flag is
   void. Record the chosen pathway id per surface, or "none". If any surface
   has none, stop and bring it to Reto before stage 1: stage 3 would be a
   `blocked` table under item 25 (the catalysis-selectivity thread's re-run
   hold), and he decides whether to spend scans on it. Nothing is minted at
   this stage.
1. **Anchors** (slice 1). `surface_coverage_scan` on baseline, A, B, minted
   with qu164903's `meta.reaction_config`, pH 7 (the config's `substrate`
   sets the NO reservoir and the default mixture anchors), `structure_ref`,
   `point_U_RHE=-0.3`, H/O/OH at 0.25–1.0 ML. First check
   each structure passes `structure_slab_extxyz` (frozen layers, (111)
   cell). Acceptance: three `meta.anchor_key` sets on one engine and model,
   `corrections` recorded; **lowest-θ agreement** against each subject's own
   pathway single-adsorbate energies (`compare_lowest_theta` compares at the
   lowest computed coverage, 2/9 ML on a 9-site cell, not at θ→0, so lateral
   interaction is inside the gap). The reference comes from
   `surface_anchors._theta0_reference(partials, low_confidence, mu, c_ads)`
   with `partials` from `_seed_partials(store, pathway_id)` on the stage-0
   pathway, and `mu`, `c_ads` from the scan's `meta.coverage`. The existing
   wrapper `_theta0_check` calls `_candidate_pathway_ref_id` (newest, no
   filter), so Part A adds a `pathway_id=None` argument to it and the
   builder passes the stage-0 id. Pre-registered as a flag, not a gate:
   |Δ| ≤ 0.1 eV (the Sn-slab agreement, jo482106, was ≤0.1 eV, one
   comparison); a larger gap is reported in the table, not hidden, and does
   not stop the run. The pathway reference must sit on the same corrections
   footing as the scan: the builder compares `correction_footing` of the
   scan's `corrections` with the pathway's `results.corrections` (shapes
   checked when written), and a mismatch reads "footing differs", not
   scored. The baseline H* row may fail honestly: MACE misprices H* on Pd by
   about 0.25 eV (open question 2).
   For B, the subsurface H must stay below layer 1 on every B anchor
   configuration, not only the clean relax: criterion, fixed before the
   first scan, z(H) below the mean z of layer 1 by at least 0.3 Å in the
   final geometry of each anchor, with z(H) − mean layer-1 z recorded in
   every cell. The aim is to catch H that escaped to the surface, not to
   police the site: a tetrahedral subsurface H in Pd sits only about
   0.5–0.6 Å below layer 1, so a larger margin would false-flag a legitimate
   site. **This slice makes no engine or runner change, and the scan does not
   return final geometry** (read on jo482106: each point in `meta.coverage`
   carries only `n, gamma, theta, energy, detached, converged`; the scan runs
   inside external autocatpath). So B's check reads `blocked: no final
   geometry`, B's table column is worded "B as scanned, subsurface-H
   position not verified", and no claim about B rests on the H staying put.
   The criterion above applies once an engine item (gripe filed with this
   contract: return final structures from the coverage scan) delivers
   geometry; slice 8 does not wait on it. MACE flies subsurface H to the
   surface next to Ta (pd-hydride-substrate.md §22f); Fe is untested. A B
   anchor that later fails is reported "H migrated, not a B surface" and
   drops out of B's map; if the resting termination at the operating point
   is among those dropped, B drops out.
2. **Map** (slice 2). Built with `pourbaix_map.scan_jobs` then
   `maps_for_structure`; pick the map by `anchor_key` and the engine pin, not
   `maps[0]` (the newest): re-minting the baseline beside the
   jo496906–jo496909 scans on 0.31.0 can yield several footings. Resting
   state at −0.3 V_RHE, pH 7 and the termination
   map per surface with `band_propagated` (a per-boundary voltage bar). The
   sweep covers H/O/OH coverage only: it has no β-PdH phase and does not
   price H absorption. Acceptance: the resting termination at the operating
   point named per surface, and every `che_sweep.boundaries` crossing in
   −0.4…−0.2 V listed with its band. B holds its subsurface H fixed; the A
   vs B thermodynamic preference is not mapped and not claimed.
3. **Staircase and masks** (slices 3, 4). Precondition per surface: one
   pathway id, `ready`, `meta.results` carrying `che_affine` (engine
   ≥ 0.25.0), footing recorded. A stored pathway meeting it needs no run;
   the version is not required to equal the pin. Use the id chosen in
   stage 0 and cite it: `_candidate_pathway_ref_id` takes the newest pathway
   by candidate ref with no `ready` or `che_affine` filter, while
   `view='staircase'` and the web page take `ranked_pathways`, so the
   table and figures must sit on the same explicitly chosen pathway, and the
   builder calls `routes_for_pathway` on it directly. Slab identity was
   checked in stage 0 (gr482121: pathway structure refs collide on the
   slab-blind `content_key`). Stage 0 stops the run when a surface has no
   qualifying pathway, so a `blocked: no che_affine pathway` cell appears
   only if Reto lets the run go on under item 25 (producing a pathway is a
   re-run and waits for the lift). Acceptance: route
   NO→NH₃ staircase with U_L, limiting rung, and operating window
   (ε = 0.3 eV chemical / 0 electrochemical, HER as a mask rung).
4. **Objectives and attribution** (slices 5, 6, plus one new function).
   `window_area_VpH`, HER margin, and the per-rung Shapley vector per
   surface. The builder calls the pieces directly, not
   `stamp_window_measures` (hand-minted scans have no quest): `sr =
   routes_for_pathway(...)` on the stage-0 pathway gives the routes and the
   HER rung (`sr.her`); `window_measures(resting_map, routes, her, ...)`
   gives the window scalars; `shapley_field(resting, route, her=sr.her,
   her_margin=0.0).global_values` gives the per-rung vector
   (`window_measures` stamps only `shapley_sumsq_raw`, `shapley_ipr`,
   `ridge_churn`). **Pin the route.** `window_measures` evaluates each
   route's one-rung PCET form (`sr.pcet_routes`) and keeps the best-windowed
   one without returning which, so the Shapley route could differ from the
   one behind the window, and between corners. At corner 0 the builder calls
   `window_measures` once per `sr.pcet_routes[i]` (as
   `pcet_routes=[that route]`, `her=sr.her`), pins the `i` with the largest
   `window_area_VpH` (ties: lowest index), and uses that route for
   `shapley_field` and for every corner. `eps` and `her_margin` are the
   defaults (`Rung.eps` 0.3 eV chemical / 0 electrochemical; `her_margin_eV`
   0): hand-minted scans have no quest window meta. `window_measures` takes
   no `seed`; `shapley_field` does, and it matters only above 16 active
   rungs.

   **Sensitivity probe on the attribution.** Rung ΔG and σ come from the
   pathway and do not move; what the anchors' bars (`Anchor.sigma_gamma`)
   can move is the `wanted` region. A uniform ±σ shift of every `gamma0`
   mostly cancels at adsorbate-vs-adsorbate boundaries (a boundary depends on
   a `gamma0` difference) and moves only the clean-vs-adsorbate boundary in
   full, so it would read the top rung as stable when a mixed-sign shift
   would flip it. The probe therefore uses **opposed corners**: anchors whose
   key is the wanted termination move by `+σ_i` and all other anchors by
   `−σ_i` (corner `+1`, wanted loses, since the lowest `gamma` wins), and the
   reverse (corner `−1`); corner `0` is the unshifted case. When the wanted
   termination is the clean surface it has no anchor and must lose at corner
   `+1`, so every anchor moves by `−σ_i` there (and by `+σ_i` at corner
   `−1`); that is the clean-vs-adsorbate boundary, which moves in full. Per
   corner, the builder runs the pipeline end to end:

   Corner `0` is the stored sweep, with no `resweep`; the shifted corners run
   steps 1–2.

   1. `perturb_anchors(anchors, wanted_key, sign)` → shifted anchors.
   2. `resweep(che_sweep, anchors)` → a copy of the stored `che_sweep` with
      `anchors`, `envelope` and `boundaries` recomputed on the sweep's own U
      grid (`U_RHE_window`, `grid`, stored by `sweep`; `envelope(...)`,
      `boundaries(...)` over the shifted anchors); `needs_anchor` is left as
      stored and unused by the probe. It refuses (raises) when the sweep has
      more than one model (`n_models > 1`): `boundaries` takes per-model
      anchors, which pooled anchors cannot rebuild; with one model it passes
      `{model: shifted}`.
   3. `build_map` on a copy of the scan job carrying that sweep (regions are
      read from the envelope).
   4. `wanted_key` stays the one read from the corner-0 map at the operating
      point; it is not recomputed per corner, or the probe would follow the
      shift.
   5. `window_objectives.resting_map_for(sm, m, wanted_key)` → `RestingMap`
      (the existing private `_resting_map_for`, renamed; its one caller in the
      same file is updated).
   6. `window_measures(...)` with the pinned route as `pcet_routes`, and
      `shapley_field(resting, pinned route, her=sr.her).global_values`, with
      one fixed `seed` for all three corners.

   **New code (Part A).** Placement: `Anchor.from_dict`, `perturb_anchors`,
   `resweep` in `src/precis_pathway/surface_pourbaix.py`; the pure verdict
   function `attribution_verdict` in `src/precis_pathway/staircase_masks.py`;
   the six-step per-corner orchestration as `attribution_probe(...)` in
   `src/precis/quest/window_objectives.py` (it returns the three corners'
   window scalars, φ vectors, the pinned route index and the verdict); the
   `resting_map_for` rename there; the `pathway_id=None` argument on
   `surface_anchors._theta0_check`.
   - `from_dict` rebuilds what `as_dict` drops or transforms: `counts` (dict →
     tuple of `(species, count)` pairs) and `reservoirs`: take
     `che_sweep["reservoirs"]` (stored as `Reservoir.as_dict()` rows, parsed
     by `reservoirs_from`, whose extra keys it ignores) filtered to the rows
     whose `element` is a key of `che_sweep["reservoir_shift_eV"][model]`,
     because `Anchor.reservoirs` is the applied subset only and passing a
     reservoir the scan could not price changes `nu` and `slope`. `slope` and
     `nu` are derived properties and are not read from the dict.
   - `perturb_anchors(anchors, wanted_key, sign)` returns
     `dataclasses.replace(a, gamma0=a.gamma0 + s·sign·a.sigma_gamma)` with
     `s = +1` for an anchor with `(a.adsorbate, a.n) == tuple(wanted_key)` and
     `s = −1` otherwise; `sign = 0` returns the anchors unchanged. The match
     is on `(adsorbate, n)` because that is what the map's regions key on
     (`_regions` takes `list(term_key)` with the total count `n`); matching
     `Anchor.key` would never hit a mixture, whose key is `(label, counts)`,
     and would give it the wrong sign. A single-species anchor and a mixture
     sharing a label and `n` are not distinguished by the map either
     (`_key_order` in `surface_pourbaix.py` notes the collision), so both take
     the wanted sign and the table says so when it happens.
   - `attribution_verdict(phi_by_corner, ...)`: the verdict rule below as a
     pure function over the three φ vectors, with the degenerate outputs it
     defines: a single rung → `no runner-up` (no verdict claimed); corner-0 φ
     all zero → `no lost area: attribution undefined`; a tie for top at corner
     0 → ambiguous; the wanted region empty at either shifted corner →
     ambiguous, checked first (all-zero φ would otherwise reach the overlap
     rule by accident).
   - Unit tests: (i) round trip: `from_dict(a.as_dict(), reservoirs)` equals
     `a` on `gamma0`, `slope`, `nu`, `counts` and `sigma_gamma`, on a fixture
     with one reservoir the scan could not price; `resweep` at sign 0
     reproduces a stored sweep's `envelope` and `boundaries` to a stated
     tolerance (1e-9, or the stored rounding if coarser), not exactly (this
     is the test that `slope` and `nu` survived); (ii) shift: wanted +σ /
     others −σ, sign 0 identity, clean-wanted moves every anchor, a mixture
     anchor takes the sign its `(adsorbate, n)` gives; (iii) the failure case
     the opposed corners exist for: two adsorbate anchors bounding the wanted
     region, where a uniform shift leaves their mutual boundary where it was
     and the opposed shift moves it; (iv) `attribution_verdict` on the
     degenerate inputs above, on an overlap-but-no-flip case and on a real
     flip; (v) `resweep` raises on two models.

   **Verdict rule.** For each rung r, let `R_r = [min_c φ_c[r], max_c φ_c[r]]`
   over the three corners (φ = the Shapley lost-area vector), `r*` the top
   rung at corner 0 and `r2` the runner-up. The attribution is reported
   **ambiguous** if `min R_{r*} ≤ max R_{r2}` (the ranges overlap; this also
   covers the top rung changing in any corner), or if the wanted region is
   empty at either corner. Otherwise it is "not ambiguous under this probe";
   the word "robust" is not used. The overlap rule is conservative (it can
   flag a case where `r*` beats `r2` in every corner but the two co-move), so
   the table also reports the per-corner order of `r*` and `r2`, which tells a
   real top-rung flip from overlap alone. Each corner's `window_area_VpH` is
   reported next to the corner-0 value. The probe is two corners of a 2^n space of
   per-anchor shifts and covers the propagated bars only, not the model-form
   band (one model, no ensemble): a sensitivity probe, not a confidence
   interval. The BEEF-vdW ensemble (td470248, estimate-only) is listed under
   claims not made.

   Per subject, one sentence names the top rung and its Shapley-area change
   against the baseline, matched by intermediate name; rungs on only one
   route are listed unmatched. The sum of Shapley values equalling the lost
   area is an invariant of `shapley_field` (efficient by construction): a
   regression check, not evidence.
5. **Reset contour** (slice 7). No catpath `bep` export exists (spso-c1), so
   every edge is `overdrive` or `neb`; the paper says so. Acceptance:
   `reset_window_area` per surface with the limiting edge named; no
   BEP-grade claim.
6. **Deliverables.** (a) A comparison table, stored as a section of the
   paper's draft: baseline vs A vs B on resting state at the operating
   point, `window_area_VpH`, `reset_window_area`, U_L, limiting rung, top
   Shapley rung and its ambiguity verdict (opposed-corner probe), every cell
   either a value with its job/pathway id or `blocked: <reason>` (the stage-0
   stop gate, not this criterion, is what keeps the table from being
   all-blocked), plus a
   provenance row "spin check: pending spin-polarised-fe-dft-check" for the one
   spin-polarised Fe DFT point. The row names the anchor checked (A's
   resting termination at the operating point, its lowest-`gamma` Fe-slab
   anchor at −0.3 V) and the delta that voids the claim: the spin effect on
   `gamma0`, `Δγ0_spin` = spin-polarised minus spin-paired at fixed geometry
   and functional (the item's definition), exceeding that anchor's own
   `sigma_gamma`, in which case the Fe columns are withdrawn and the table is
   labelled MLIP-only. This isolates magnetism; a DFT-vs-MACE `gamma0` gap
   mixes MLIP error with spin, so it is reported beside the verdict as
   information only and does not void anything. One point bounds the MLIP's
   magnetism error at that site only; the row says so. When the item lands,
   "pending" becomes its result.
   **Caption.** The table's caption itself (not only a footnote) states
   "B as scanned, subsurface-H position not verified" and the footing
   (`mace:medium`, one engine pin, corrections as recorded). (b) Figures from the
   `/pourbaix/<handle>` layers that exist: resting map with bands, staircase,
   mask overlay, reset band. The Shapley field has no render in `precis_web`;
   it ships as the table's numbers, and a render is a separate slice if the
   paper wants one. (c) Provenance per figure and cell: footing, engine,
   model, corrections, job and pathway ids. (d) Claims not made: no
   experimental activity; no kinetics beyond the ladder's labels; nothing
   outside (111); no Fe magnetism or spin effect beyond the one checked point
   (above); no A-vs-B stability; no model-form band (one model; the BEEF-vdW
   ensemble, td470248, is estimate-only and the §5 model-form band is never
   blended with the propagated band).

**Failure handling.** A scan that fails after its 2 retries: that surface's
map is `blocked: scan failed`, the others continue. Mixed footings across a
surface's anchors: reported and partitioned, never blended (q2). A surface
with a blocked stage drops that cell, not the table.

**Holds.** Stage 1 is new measurement, not a re-run, so item 25 does not hold
it. Stage 3 may be a re-run (above). Subject B is a coverage-scan surface;
no NEB runs on it. Item 23 (pd-hydride-substrate.md) holds the Ta/242611
pilot; ruled 2026-10-29 (-28): it stays held until slice 8 has run, so this
slice is the condition, and nothing here depends on item 23.

**Risks (unmeasured).** Fe is magnetic and `mace:medium` is not spin-aware;
the Ta spin check (design note §22i) does not transfer. A spin-polarised DFT
point on one Fe anchor is ruled (2026-10-29) but is its own item, not part of
this slice: `spin-polarised-fe-dft-check.md` (filed 2026-10-30; it still
needs a single-point mode, per-species magmoms and a measured wall time).
The void condition is the item's: the spin effect on `gamma0`, not a
DFT-vs-MACE gap (coordinator, 2026-10-30).
One Fe geometry says nothing about Fe–Pd alloys in general.

**Open.** The paper's month: November went to td459589 (Reto, 2026-10-30;
the validation paper, draft:validation-paper, cases A–D), so this paper
(td462597) has none yet; it does not block the build. Footing, path and subject set are ruled (§Rulings
2026-10-29). Design review: approve-with-changes 2026-10-30, changes folded
in, stage 4 re-looked and approved. Second vet 2026-10-30: NOT READY with 7
fixes, applied in v4 (B's H check demoted to `blocked: no final geometry`
because the engine returns none; wanted-key match on `(adsorbate, n)`;
reservoir source named; Shapley route pinned; stage 0 query and conditions
named; theta0 reference path named; orchestration and verdict placed and
tested). No third look on v4 is needed (coordinator,
2026-10-30). Part A is built and landed (code verdict: land, 2026-10-30):
`Anchor.from_dict` (with `applied_reservoirs`), `perturb_anchors`,
`resweep`, `attribution_verdict`, `attribution_probe`, the
`resting_map_for` rename and the `_theta0_check` argument. Built choices
beyond the text above: `attribution_probe(job, sr, u_rhe, *, seed)` takes
one `scan_jobs` row and no pH (the sweep is pH-flat on the RHE scale);
HER is a baseline mask, not a Shapley rung; the probe passes `sr.routes[i]`
beside `sr.pcet_routes[i]` to `window_measures`. Filed with this contract: gr540871 (no MCP listing of a slab's
pathways) and gr540872 (the engine returns no final structures from the
coverage scan).

## Explicitly NOT in scope

- Slice 8: contract v4 drafted (§Slice 8 contract); not scheduled until
  `ready`. Update this bullet when the status flips.
  (Slices 2–7 are built as of 2026-10-14.)
- The hydride pilot itself (item 23) and any re-run under item 25.
