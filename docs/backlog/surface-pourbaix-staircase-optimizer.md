---
status: ready
title: a surface-Pourbaix validity map and linked reaction staircase drive slab search by where and why each candidate's operating window is lost
pillar: 3d-design
prio: high
owner: catalysis session
---

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
`~/.claude/projects/-Users-reto-precis-mcp/scratch/review-msgs/surface-pourbaix-transfer-prompt.md`;
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
  `coverage.facets` must be empty; a prebuilt slab is refused
  (`failure_class="input"`) because the engine's scan builds its own clean
  slab and ignores the pathway pipeline's prebuilt-slab side channel —
  doped candidates and β-PdH wait on the engine task below.
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
  energy); wiring it to a live pathway ref is the next slice-1 step.

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

**Not yet (slice-1 residue):** the engine task A1 (catpath repo, in
build 2026-10-13; the 2026-10-07 brief was never filed). Mint recipe for
further scans: `put(kind='job', job_type='surface_coverage_scan',
parent_id=<structure id>, params={config: REACTION_CONFIG, point_U_RHE:
-0.3})`, `target_node` unset (the `ssh_node` executor routes it). A1: the coverage scan honours
`cfg._prebuilt_slab` (doped slabs, β-PdH twins; `n_slab` from
`slab.info` so lattice H is not counted), applies the same gas set and H*
shift as the pipeline under `cfg.corrections`, and records a
`corrections` block plus `slab_source: injected|built` in
`coverage.json`.

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

## Follow-ups from wave review (2026-10-13, non-blocking)

- `/pourbaix` quest page builds each candidate's maps twice (`quest_maps`
  then `_candidate_ctx`); pass the precomputed maps through (B2 review).
- Structure `view='staircase'` catches every exception from the pathway
  sibling as "not installed"; narrow it, and fix `Hub.sibling()` so the
  `pw_handler.hub = self.hub` backfill can go (B2 review).
- The JS re-tilt and mismatch logic on `/pourbaix` have no test.
- Failed anchor scans are never re-minted (Part A convention, chosen in B1);
  a retry cap would need its own idem-key generation.

## Explicitly NOT in scope

- Slice 8: ordered, not scheduled; needs its own `ready`.
  (Slices 2–7 are built as of 2026-10-14.)
- The hydride pilot itself (item 23) and any re-run under item 25.
