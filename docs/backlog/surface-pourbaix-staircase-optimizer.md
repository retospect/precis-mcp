---
status: ready
title: a surface-Pourbaix validity map and linked reaction staircase drive slab search by where and why each candidate's operating window is lost
pillar: 3d-design
prio: high
owner: catalysis session
---

# Surface-Pourbaix validity map + reaction staircase as a catalyst optimizer

**Lifted 2026-10-07** (Reto, via chat-interface): ready for slices 1–2,
owner = the catalysis session, after the catpath lock (0.24.0 since
2026-10-07); see
§Rulings below. Filed 2026-10-02 as file-only. The full design is Reto's transfer prompt,
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
7. Reset contour: proxy 1 → BEP → active-learned NEBs.
8. End to end on Fe-doped Pd, for the paper.

## Open questions (OPEN in the prompt, §10)

1. Activity vs stability: how "is it active" folds into the window score.
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

## Rulings 2026-10-07 (Reto, via chat-interface)

- "File only" LIFTED. Slices 1–2 are `ready`; owner = the catalysis
  session; started after the catpath lock. Anchors compute on 0.24.0
  (Reto's ruling 2026-10-07): engine token recorded per anchor.
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
- q2/q3 (reference consistency, oxide corrections): still being explained
  to Reto; no ruling yet.

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
  doped candidates and β-PdH wait on the catpath brief below.
- Footing per anchor set (acceptance "one footing"): `meta.anchor_key`
  (`runner.coverage_anchor_key`: config minus `mlip` + engine version),
  `meta.model`, `meta.engine_version`, `meta.corrections` (what the scan
  itself records, never the config's block: on 0.24.0 `coverage.scan`
  applies neither the gas set nor the H* shift — they run only in
  `pipeline._corrections_plan` at aggregation (catpath session,
  2026-10-07) — so a 0.24.0 anchor is uncorrected and keyed `None` until
  catpath's brief lands; the pathway's `results.json` on 0.24.0 sits on
  the corrected gauge, so the θ→0 comparison must account for that).
  The lock is 0.24.0 since 2026-10-07, so every anchor minted from here
  carries `engine_version` 0.24.0; nothing precis-side corrects it.
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

**Not yet (slice-1 residue):** minting the first prod scan on clean
Pd(111) with the qu164903 base config (needs the GPU node; mint via
`put(kind='job', job_type='surface_coverage_scan', params={config:
REACTION_CONFIG, target_node: <gpu node>, point_U_RHE: -0.3})`), the live
θ→0 comparison against pw455722's nodes, and the catpath brief (filed by
the catpath session as its `docs/backlog/coverage-scan-prebuilt-slab-
corrections.md`, draft, 2026-10-07): the coverage scan honours
`cfg._prebuilt_slab` (doped slabs, β-PdH twins; `n_slab` from
`slab.info` so lattice H is not counted), applies the same gas set and H*
shift as the pipeline under `cfg.corrections`, and records a
`corrections` block plus `slab_source: injected|built` in
`coverage.json`.

## Explicitly NOT in scope

- Slices 3–8: ordered, not scheduled; each needs its own `ready`.
- The hydride pilot itself (item 23) and any re-run under item 25.
