---
status: draft
title: Ringe's constant-charge electrochemistry on an MLIP ladder — σ-linear corrections, a charge-aware fine-tuned MACE, explicit double-layer MD
pillar: 3d-design
prio: normal
model: opus
---

# Ringe's constant-charge electrochemistry on an MLIP ladder

Potential-, pH- and cation-dependent energetics for catpath pathways: `a_i =
∂ΔE_i/∂σ` per state and TS plus one equation of state `σ(U, pH, cation)`,
produced by DFT (tier 0), a σ-conditioned fine-tuned MACE (tier 1) or
explicit double-layer MD (tier 2), handed to the kinetics layer as one
grand potential `Ω(U, pH, cation)`.

Source talk: Stefan Ringe, *Continuum modeling & quantum chem: multi-scale
modeling of electrochemical processes* (Korea University, 2025-10-30),
`youtube:496611`. Research behind this spec: `perplexity-research:498170`
(GC-DFT methods), `:498186` (charge/field-aware MLIPs), `:498238`
(training best practice), `:498284` (Ringe's papers),
`perplexity-reasoning:498297` (cation mechanisms). Papers imported
2026-10-19 are cited by `pa` handle; most are stubs awaiting fetch.

## Motivation / why

catpath's electrochemistry is CHE: `G(U) = G(0) + n_H·eU`, barriers
U-independent (`autocatpath/electrochem.py`, `kinetics.py`). The MLIP
backends (`calculators.py`: emt | mace | chgnet | fairchem | grace) see a
slab in vacuum and have no electronic degree of freedom, so nothing in the
pipeline moves with surface charge, PZC, cation or pH-at-fixed-RHE. Ringe's
talk is the cheapest known route out: with constant capacitance the grand
potential is a parabola in U set by two numbers per state (`U_pzc`, `C`),
constant-charge DFT and constant-potential DFT agree in the thermodynamic
limit, and the whole double-layer model collapses into one equation of state
`σ(U)`. So the electronic physics reduces to **one scalar per state and
step, `a_i = ∂ΔE_i/∂σ`**, which DFT can label at three charges and an MLIP
can learn. The Cu-vs-Au CO volcano (pa498298) is the proof that the term
changes rankings; the NRR revision (pa4102) is the proof it matters for our
reaction class (NO→NH₃ on Pd, qu164903).

This item owns the σ side: generating `a_i` (and `b_i`, `U_pzc`, `C`,
`σ(U, pH, cation)`), replacing DFT with MLIPs tier by tier, and the
contract that hands `ΔG_i(U, pH, cation)` and `E_a(U)` to the kinetics
layer. The rate side (β·U in rates, KMC) is owned by the KINETICS spec (in
flight 2026-10-19, rejection-free KMC with an environment block `{T,
partial pressures in/out, optional U/σ/cation}`) and by
`electrochem-kinetics-beyond-che.md` slice 1; this spec supplies their
energies and never redefines their environment block.

## Ringe's workflow → the ML-accelerated version

| step (talk chunk) | Ringe | here | stays DFT |
|---|---|---|---|
| 1. equation of state `σ(U)` (~18, ~33) | MPB continuum, or experiment | `double_layer.py`: Helmholtz `σ = C_gap (U_SHE − U_pzc)` default; size-modified PB with cation radius + Stern layer (pa498302, pa492259) as the cation lever; tier 2 replaces its parameters by MLIP-MD | — |
| 2. constant-charge energies `ΔE_i(σ)` (~30–31) | 3 implicit-solvent DFT points per state, linear fit | tier 0: 3 GC-DFT points on MLIP-relaxed geometries; tier 1: an MLIP with σ as input predicts `a_i` at every geometry | the 3 label points (tier 0 all states; tier 1 training set + 2 NEB anchors per campaign) |
| 3. PZC per surface (~38) | work function, `U_pzc = −ε_F/e − const` | one DFT single point per clean slab (MLIPs have no Fermi level); tier 2 computes it from the computational SHE (pa498339) | yes, always |
| 4. capacitance vs coverage (~49) | computed; constant to good approximation except O/OH | assume constant per slab, verified once per slab family by a 2×2 vs 4×4 check (pa498306); O/OH-covered terminations get their own `C` | one check per family |
| 5. kinetic model (~34–37) | CatMAP mean-field, BEP barriers, β rationalised from experiment | KINETICS spec consumes `Ω_i(U)`; barriers from NEB under the same σ correction (tier 1) or BEP/β fallbacks labelled as such | 2 NEB anchors per campaign |
| 6. descriptor volcano (~38–41) | `(ΔG_CO, U_pzc)` 2-D volcano, Cu > Au | reproduced as the method's acceptance test (slice 4) | — |
| 7. explicit interface (closing) | MLIP-MD of water/graphene, SFG | tier 2: MLIP-MD of slab/water/cation → PZC, `C_dl`, cation position, `ΔG_coord` | periodic relabel frames |

## Survey — methods, models, codes (handles)

### Grand-canonical / constant-potential electronic structure

| what | ref | takeaway for this item |
|---|---|---|
| CHE | pa81 | the zero-charge, first-order limit (pa498305); keep as the `U`-term, add charging on top |
| constant-charge ↔ constant-potential equivalence | pa498308, pa498309, pa498306 | fixed-charge differences map to grand-energy differences at the mean potential; GC converges faster at finite size, constant charge is EDL-model-insensitive — Ringe's argument for labelling at fixed σ |
| parabolic grand potential, state-specific `(C, U_pzc)` | pa498307, pa498305 | three points per state suffice; reconstructs constant-potential barriers from canonical runs |
| GC-DFT frameworks | pa498303 (GPAW, multicomponent), pa498301 (JDFTx), pa498342 (GPAW solvated jellium, SJM) | SJM is constant-potential + implicit solvent inside the GPAW the `precis-dft` image already runs |
| implicit solvation codes | pa498300 (SCCS/ENVIRON), pa2295 (VASPsol), pa4903 (VASPsol++, Ringe co-author), pa4098 (review) | cavity + countercharge placement are the main error sources (pa498312) |
| size-modified PB, Stern layer | pa498302, pa492259 | the `σ(U, cation)` lever; parameters: ion radius, `C_gap`, concentration |
| charge extrapolation for barriers | pa492260, pa492264, pa492265 | `∂E_a/∂U` = charge transferred IS→TS; the force-based β is per-step from an existing NEB |
| good practice for charged cells | pa172964, pa498332 | report potential scale, σ, countercharge, cell; never leave a charged slab on the uniform background |
| NRR under potential-dependent modelling | pa4102 | the NO/N chemistry anchor: scaling relations revise materially vs CHE |
| Ringe data papers | pa492259 (cations, EES 2019), pa492258 (Au, Nat. Commun. 2020), pa498298 (two-descriptor volcano, 2023), pa498310 (explicit Na⁺ on Cu), pa498311 (unifying cation mechanism), pa492262 (CatmapInterface.jl/LiquidElectrolytes.jl) | acceptance numbers below |
| capacitance vs CO coverage, potential-dependent CO adsorption | pa498314, pa498313 | the cases where constant `C` fails (high CO coverage, O/OH) |

### ML potentials for electrochemical interfaces

| class | models | ref | fit here |
|---|---|---|---|
| global charge equilibration | 4G-HDNNP, ee4G, kQEq, PQEq | pa50963, pa498343 | nonlocal charge transfer; QEq over-polarisation and spurious long-range charge transfer documented (pa498343) — not the first choice |
| latent Ewald / long-range | LES, CACE-LR, universal LES on MACE/UMA/CHGNet (MACELES) | pa498315, pa498316, pa498317 | tier 2: Pt(111)/KF 0.31 meV/atom, 34 meV/Å; drops onto the shipped MACE backend |
| field-response | FIREANN, Allegro-pol, MACE-Field, FIREANN+MLEDR (Au/NaCl, Helmholtz capacitance) | pa498318, pa498319, pa498325, pa498324 | tier 1 candidate: MACE-Field injects a field vector into every MACE layer, starts from pretrained MACE weights |
| grand-canonical / σ-conditioned | GC-ML (Peterson), DP-Nₑ, CP-MACE, CPMPNN, veNNP, RAZOR | pa498320, pa46076, pa498321, pa172963, pa498323, pa173979 | tier 1 candidate: RAZOR = zero-charge descriptors + imposed σ, learned work function + charge-force + quadratic `C` — the exact Ringe parabola as a readout |
| explicit-electrode response | ec-MLP/DPLR (Zhu–Cheng), DPχ | pa498322 | tier 2 reference: bell-shaped `C(U)`, max ≈ 80 μF cm⁻² near PZC for Pt(111)/KF |
| failure modes | Anstine–Isayev, Parker/Csányi Cu–water | pa498344, pa498334 | local models miss fields/screening; short-range foundation models produce false OH/H₃O at Cu–water |

### Foundation models and training practice

| topic | ref | rule adopted |
|---|---|---|
| MACE-MP-0 / MPA / OMat24, UMA, OC20, OC25 | pa5619, pa2364, pa498346, pa4719, pa498326 | universal model = initialiser only; OC25 (7.8 M solid–liquid frames, nine ions, RPBE-D3, dipole-corrected) is the tier-2 initialiser; match its label conventions or treat ours as a separate head |
| fine-tuning | pa498327 (frozen transfer), pa498328 (5–15× force gains; lr 1e-4–1e-2, batch 4–5, E/F ratio 0.5–150) | pick freeze depth on trajectory stability, not validation loss |
| training protocol, UQ, active learning | pa5190, pa498330 (MACE readout ensembles), pa498331 (hyperactive learning), pa498333 (validation reporting) | committee of readouts on a frozen backbone; calibrate disagreement against DFT error on held-out interface frames; bias sampling toward uncertain σ/coverage/TS regions |
| explicit-solvent interface training | pa498329, pa498345 | include bulk water, clean and covered interfaces, adsorption configs and reaction paths; validate water structure and DFT adsorption energies, not force MAE alone |
| OOD and softening | pa498335 | include steps, vacancies, rattled and compressed slab frames |
| PZC / capacitance benchmarks | pa498339 (computational SHE: Pt, Au, Pd, Ag PZCs), pa498340 (Pt(111) PZC 0.22 V, 19 μF cm⁻²), pa498341 (negative Helmholtz component) | tier-2 acceptance numbers |
| cation mechanisms beyond the field | pa498336 (no cation, no CO₂RR), pa498337 (local pH / buffering), pa498338 (field in a microkinetic model), pa498311 | four separable terms: field, explicit coordination `ΔG_coord(M⁺)`, local pH, coverage; tier 0 carries field + local pH, tier 2 adds `ΔG_coord` |

## Architecture — three tiers, one contract

Every tier emits the same record per state and transition state:

```
potential_response:
  version: <int>                 # joins the correction-basis tuple
  source: tier0-gcdft | tier1-mlip | tier2-md | che-only
  E0: eV                         # energy at σ = 0 (vacuum MLIP or DFT-referenced)
  a: eV cm² μC⁻¹                 # ∂E/∂σ   (0 under che-only, flagged)
  b: eV cm⁴ μC⁻²                 # ½∂²E/∂σ² (0 unless fitted; ≥10× smaller than a·σ per pa498298)
  n_e: int                       # CHE electron count relative to root (existing n_H_rel)
  beta: float | null             # TS only: symmetry factor when a_TS is unavailable
  fit: {n_points, residual, sigma_range}
double_layer:                    # one per slab (campaign-level), from double_layer.py
  model: helmholtz | mpb
  U_pzc_SHE: V                   # from one DFT work-function point (tier 0/1) or tier-2 MD
  C_gap: μF cm⁻²                 # default 20; tier 2 measured
  cation: {species, radius_A, c_M}
  sigma(U_RHE, pH): callable     # σ = C (U_RHE − 0.059·pH − U_pzc) for helmholtz; MPB solve otherwise
```

The kinetics layer evaluates one grand potential for states **and** TS:

```
Ω_i(U, pH, cation) = E0_i + G_corr_i + n_e,i·eU_RHE + a_i·σ + b_i·σ²
                                        σ = double_layer.sigma(U_RHE, pH, cation)
E_a,f = Ω_TS − Ω_IS ;  E_a,r = Ω_TS − Ω_FS ;  k_f/k_r = exp(−(Ω_FS − Ω_IS)/kT)
```

Two consequences, both Ringe's: on the RHE scale pH enters **only** through
σ (talk ~45), so `pathway-selectivity-u-ph-window.md`'s three pH routes gain
a fourth, "charging", that is non-zero exactly when `a_i ≠ 0`; and because
`C` is coverage-independent to first order (talk ~49), σ does not depend on
the lattice configuration, so a KMC rate table stays static per `(U, pH,
cation)` — lateral interactions change `E0`, not `a`.

### Tier 0 — σ-linear correction on vacuum MLIP energies

- `a_i` from three GC-DFT single points per state (σ ∈ {−20, 0, +20} μC cm⁻²
  or U ∈ {U_pzc − 0.5, U_pzc, U_pzc + 0.5}) on the MLIP-relaxed geometry;
  `E0` stays the MLIP energy + existing corrections (`corrections.py`), so
  the correction is additive and the vacuum pathway is bit-identical when
  `response: off`.
- TS: `a_TS` from the same three points on the MLIP NEB saddle when a saddle
  exists; else BEP-interpolated `a_TS = (1−ω)a_IS + ω·a_FS` with ω recorded;
  else `beta` from pa492265. The fallback is written into `source`.
- Cost: one DFT single point per (state, σ) — ~15 states × 3 σ = 45 points
  per network plus one work-function point per slab; a 4-layer 3×3 Pd(111)
  + adsorbate RPBE single point is minutes-to-an-hour on a compute-group GPU
  node (measure in slice 1; the chemistry thread's timed MPI GPAW run is the
  baseline). One network ≈ 1–2 node-days.
- Accuracy: Ringe's own level (pa498298: linear term dominant, quadratic
  ≥10× smaller; constant `C` within 0.1 μF cm⁻² at 4×4, pa498306). Fails
  where `C` is not constant (O/OH-rich terminations, high CO coverage —
  pa498314, pa498313) and where the adsorbate's charge sits far from the
  surface (countercharge artefact, pa498312).
- Justification: it is the talk's own recipe, needs no new MLIP, and is
  the label generator every higher tier reuses.

### Tier 1 — charge-aware fine-tuned MLIP for adsorbates under σ

- Model: fine-tune the shipped MACE backend into a σ-conditioned model.
  Two candidate forms, decided in slice 6 by a 2-week bake-off:
  (a) **RAZOR-form readout** (pa173979): per-structure learned
  `(E0, ∂E/∂q, ½∂²E/∂q²)` with imposed σ — trains on the same three-point
  labels as tier 0, reproduces the parabola by construction;
  (b) **MACE-Field** (pa498325): field vector into every layer; needs a
  σ↔field convention (Helmholtz `E = σ/ε₀ε_H`) and sawtooth-field labels
  (`dft.py::dipole_settings` already sets `tefield`/`dipfield` in QE).
  Default (a): same labels as tier 0, σ is the native variable, TS get
  `a_TS` for free.
- Data: catpath's own relax + NEB trajectories (states, images, rattled
  frames, 3–5 coverages, 2 facets) labelled at 3 σ; initial 500–1000 frames,
  active learning with readout-committee uncertainty (pa498330) biased
  toward high-|σ|, TS and dense-coverage frames (pa498331); stop when
  committee σ-slope disagreement < 0.3×10⁻² eV cm² μC⁻¹ on held-out TS.
- Cost: 1500–5000 labelled single points (2–8 node-weeks of DFT), fine-tune
  hours on one GB10 node; inference ≈ vacuum MACE cost ×1.2.
- Accuracy target: `a_i` MAE < 0.5×10⁻² eV cm² μC⁻¹ vs tier-0 fits on
  held-out states; barrier-vs-σ slope within 0.1 eV/V of a charge-
  extrapolated DFT NEB on two steps.
- Justification: replaces per-state DFT with per-campaign DFT; gives
  potential-dependent barriers along the whole NEB; the σ-parabola is the
  model's readout, so extrapolation is bounded by the same physics as tier 0.

### Tier 2 — explicit water/cation double-layer MLIP-MD

- Model: universal-LES-augmented MACE (pa498317) or CACE-LR (pa498316),
  initialised from OC25 (pa498326), fine-tuned on slab + 3–4 water layers +
  K⁺/Cs⁺ + countercharge AIMD frames; constant-potential sampling via
  Siepmann–Sprik electrode charges (pa498322) or finite-field (pa498324).
- Outputs: `U_pzc` (computational SHE, pa498339), `C_dl(U)`, cation
  position and `ΔG_coord(M⁺)` per adsorbate (pa498336, pa498311), the
  Helmholtz-layer structure that replaces the MPB parameters (ion radius,
  Stern gap) with computed ones. Feeds tier 0/1 as a better `double_layer`
  record; adds the explicit-coordination term the field cannot carry.
- Cost: 1000–3000 AIMD frames of 150–200 atoms (1–3 h each) ≈ node-months
  unless OC25 pretraining cuts it to 300–500; MD production cheap.
- Accuracy anchors: PZC of Pt(111)/Au(111) within 0.15 V of experiment;
  Pt(111) `C_dl` ≈ 19–20 μF cm⁻² (pa498340); bell-shaped `C(U)`
  (pa498322); no spurious OH/H₃O populations over 1 ns (pa498334).
- Justification: only this tier can say whether a cation effect in
  NO→NH₃ is field (tier 0 carries it) or coordination (it does not). Gated:
  runs only if tier 0 shows cation-sensitive forks in the quest's window.

## Data and training pipeline

- **Label engine.** GPAW-SJM (pa498342) inside the existing `precis-dft`
  image: constant-potential, implicit solvent, RPBE, spin-polarised — the
  same functional the image's relax rung uses. New subcommand
  `precis-dft-run gcdft-point` (POSCAR + `params.json{U|sigma, solvent,
  functional}` → `result.json{E, sigma, U, workfunction, grand_potential}`).
  Alternative: QE + ENVIRON constant-charge through catpath's `dft.py`
  (`ENGINES` qe/vasp/emt) with Gauthier mapping (pa498306). Decision: open
  question 1.
- **Cells.** 4-layer slabs, bottom 2 fixed, ≥15 Å vacuum with the dipole
  correction (pa498332); 3×3 for energetics, 4×4 for the constant-`C`
  check; counter-charge inside the solvent region (never the uniform
  background, pa172964). Record potential scale, σ, countercharge model and
  cell with every label.
- **Storage.** Each labelled point is a `struct_runs` row (fidelity
  `gcdft`, `params = {sigma|U, solvent, functional, code_version}`) — the
  same run-cube `struct_relax` writes, so a repeated label is a cache hit.
  A dataset is the content hash of its row set; a trained potential is a
  job artifact keyed `(dataset_hash, base_model, recipe_version)` with a
  card on the owning quest (`meta.potentials[]`). The `(backend, model)`
  key joins `pathway-correction-basis.md`'s `model_keys`, and
  `potential_response.version` joins the tuple, so margins with and without
  σ terms never compare. First-class potential ref: open question 4
  (shared with `ephemeral-potentials-for-catpath.md`).
- **Active-learning loop (tier 1).** `mlip_finetune` job: sample frames
  from catpath runs on the quest's candidates → committee uncertainty →
  select top-k by σ-slope disagreement → `gcdft_point` labels → refit →
  stamp the new potential on the quest. Gates: no frame enters training
  with |F| > 20 eV/Å or a broken slab; no potential ships without the
  held-out `a_i` MAE and two NEB anchors in its card.
- **Versioning.** Potentials are immutable; a new fit is a new key. A
  candidate's stored pathway records which potential and
  `potential_response.version` produced it; restale rules follow
  `frontier.same_network_basis` with the extended tuple.

## Validation and acceptance

| anchor | number | ref |
|---|---|---|
| `a_σ(*CO₂)` on Ag facets | 2.77–4.11 ×10⁻² eV cm² μC⁻¹; `*CO` on Cu ≈ 0 (−0.38, 0.03, −0.05 ×10⁻²) | pa492259 |
| `*OCCO` on Cu(111)/(100) | 2.64 / 3.93 ×10⁻² eV cm² μC⁻¹ | pa492259 |
| cation trend | Li⁺→Cs⁺: 1–2 orders higher CO current on Ag, C₂ on Cu; `*CO₂` stabilised from ≈ −20 μC cm⁻² | pa492259 |
| Au CO₂RR regimes | `U_pzc` = 0.16 V SHE, `C_gap` = 20 μF cm⁻²; Tafel 42 mV dec⁻¹ (PCET) and 101 mV dec⁻¹ (CO₂ adsorption); CO rate pH-independent on SHE over pH 3–6.8 | pa492258 |
| descriptor volcano | `*CO₂` σ-sensitivity 3–10× `*COOH`/`*CO`; `(ΔG_CO, U_pzc)` R² ≥ 0.9 vs ΔG_CO alone poor; **Cu 3–4 orders above Au** for CO at −1.0 V SHE, pH 6.8; Au above Cu with σ terms off | pa498298 |
| constant `C` | CO₂/Pt(111) ≈ 0.19 eV/V; `C` 12.6 → 10.1 μF cm⁻² at 2×2 but Δ ≈ 0.1 at 4×4 | pa498306 |
| PZC / `C_dl` (tier 2) | Pt(111) 0.22 V, 19 μF cm⁻²; Pt/Au/Pd/Ag PZC ordering | pa498340, pa498339 |
| NRR direction | potential-dependent modelling revises the N-scaling relations vs CHE (direction and rough magnitude) | pa4102 |

Acceptance for the item as a whole: slices 1–5 below green, the Cu-vs-Au
ordering reproduced with σ terms on and inverted with them off, and
qu164903's `U_sel` window recomputed with the charging route, each fork
labelled with its `source`.

## Thermodynamic consistency

- One `Ω` per state and TS; forward and reverse barriers are differences
  of that `Ω` (never clipped separately; `max(barrier, dG, 0)` in
  `kinetics.py` applies to the forward value and the reverse follows as
  `E_a,f − ΔΩ`).
- `a_TS` fallback is the ω-interpolation, which preserves
  `E_a,f − E_a,r = ΔΩ` identically; a `beta` fallback must be applied as a
  TS shift only, with the reverse derived.
- Gas references carry no σ term; desorption closures price through the
  same ledger as today, so Wegscheider closure on every null cycle holds
  with the gas reservoirs included (test: null-space product of `K_r` = 1
  at any `(U, pH, cation)`).
- The pH-via-σ route must leave a pure-PCET fork pH-invariant when
  `a_side = a_on` (test), and move it when they differ (test with Ringe's
  `*CO₂` vs `*COOH` sensitivities).
- `b_i` is kept only when the three-point fit's residual drops by a
  recorded factor; otherwise `b = 0`, flagged.

## Interfaces

- **autocatpath (engine, separate ship path):** `electrochem.py` gains
  `PotentialResponse` + `omega_at(U, pH, cation)` (pure functions, like
  `AffineU`); new `double_layer.py` (Helmholtz + MPB solve, cation radius
  table with provenance); `config.py` conditions block gains `response:
  off|tier0|tier1`, `sigma`, `cation`, `pzc`, `capacitance`;
  `calculators.py` gains backend `mace-sigma` (σ passed per call via
  `atoms.info["sigma"]`, cached like the others); `kinetics.py` consumes
  `Ω` for states and TS; `response.VERSION` for the basis stamp;
  `methods.md`/`VALIDATION.md` paragraphs. Replaces catpath's
  `beyond-che-potential-response.md` BEAST-table tier (same correction
  form, computed instead of tabulated).
- **precis glue (`src/precis_pathway/`, `src/precis/workers/job_types/`):**
  job types `gcdft_point` (container rung in the `precis-dft` image,
  writes `struct_runs`), `mlip_finetune` (GPU node, artifact + quest card),
  `dl_md` (tier 2); the `autocatpath_seed` params carry the quest's
  `double_layer` record and potential key; `get(kind='pathway',
  view=…, args={'U', 'pH', 'cation'})` extends the existing `args={'U'}`
  lever (`analysis.at_potential`); quest `meta.operating_conditions`
  (from `pourbaix-quest-gate.md`) gains `cation` and `electrolyte`; the
  explorer gets a cation selector beside the U slider and the pH input
  moves the charging route.
- **KINETICS spec:** its environment block's optional `U/σ/cation` is read
  as `(U_RHE, cation)` → `σ` via `double_layer`; it receives
  `Ω_i` and `E_a,f/r` per step from `omega_at`, nothing else. If the block
  supplies σ directly, `double_layer` is bypassed and `source` says so.
- **GPU placement:** DFT labels and MLIP fine-tuning on the compute-group
  GPU nodes (GB10 class, 128 GB unified memory) via `ssh_node` under the
  capacity-1 `gpu` slot; the LLM-serving node stays out of the lane
  (`host_lane_exclusive_groups`). GPAW GPU support is partial — the label
  rung may run CPU-MPI on the same node; measure in slice 1.

## Slices (independently shippable)

1. **`gcdft_point` rung + three-point fit.** Image subcommand, job type,
   run-cube write, `PotentialResponse` fit. Test: on CO₂/Au(111) the fit
   reproduces `a_σ` within 30 % of pa492259 and the 2×2 vs 4×4 `C` check
   passes; a repeated label is a cache hit; timing recorded on the quest.
2. **`double_layer.py`.** Helmholtz + MPB with cation radii; `U_pzc` from
   the work-function point. Test: σ(U) for Li⁺…Cs⁺ orders as pa492259
   Fig. (smaller hydrated cation → more negative σ at fixed U); Au Tafel
   slopes 42/101 mV dec⁻¹ reproduced from pa492258's parameters.
3. **Tier-0 corrections in catpath + precis lever.** `response: tier0`,
   `Ω` for states and TS, fallbacks recorded, `args={'U','pH','cation'}`,
   basis stamp. Test: detailed-balance and Wegscheider tests at three
   `(U, pH, cation)` points; `response: off` bit-identical to today;
   PCET-only fork pH-invariant iff `a_side = a_on`.
4. **Cu-vs-Au volcano.** Tier 0 end to end on CO₂→CO for Au, Ag, Cu
   (111)/(100). Test: Cu 3–4 orders above Au at −1.0 V SHE, pH 6.8 with
   σ on; Au above Cu with σ off; `(ΔG_CO, U_pzc)` regression R² ≥ 0.9.
5. **NO→NH₃/Pd(111) tier-0 campaign.** `a_i` for the ammonia-network
   intermediates and the decisive-step TS; `U_sel` window with the
   charging route; report which forks flip, compared with pa4102's
   direction. Test: every decisive edge carries a `source`; the quest's
   results table shows the window with and without σ.
6. **Tier-1 `mace-sigma`.** Bake-off (RAZOR-form vs MACE-Field), active-
   learning loop, `mlip_finetune` job, backend in `calculators.py`. Test:
   held-out `a_i` MAE < 0.5×10⁻² eV cm² μC⁻¹; barrier-vs-σ slope within
   0.1 eV/V of two charge-extrapolated DFT NEBs; potential card complete.
7. **Tier-2 explicit double layer.** `dl_md` job, OC25-initialised LES
   model, Pd(111)/water/K⁺. Test: Pt(111) PZC within 0.15 V and `C_dl`
   within 5 μF cm⁻² of pa498340; computed `double_layer` record replaces
   the MPB one on the quest; `ΔG_coord(K⁺)` reported per intermediate.
8. **Surfaces.** Explorer cation selector, results-table columns
   (`a_i`, `source`, window with/without σ), conditions-effects report
   caveat block names the tier. Test: web and TOON views agree; a pre-
   response pathway refuses the cation lever with a next step.

## Risks

1. Countercharge/cavity artefacts in implicit-solvent labels (pa498312)
   bias `a_i` for adsorbates whose charge sits in the solvent. Mitigation:
   constant-charge labels (EDL-insensitive to first order), two EDL models
   on the slice-1 fixture, flag states whose two models disagree > 30 %.
2. Universal-MLIP softening and OOD surfaces (pa498335) plus Cu–water
   pathologies (pa498334) in tier 2. Mitigation: long-range term mandatory,
   species-population and dipole monitors in `dl_md`, relabel on drift.
3. Functional mismatch: labels RPBE (image), MACE pretraining PBE (MPtrj).
   Mitigation: fine-tune resets the energy zero; `corrections.py` keys stay
   per `(backend, model)`; never mix label sets across functionals.
4. Barrier response extrapolation: `a_TS` learned from few saddles.
   Mitigation: two DFT NEB anchors per campaign are part of the acceptance.
5. Constant-`C` assumption fails on O/OH-rich and high-CO terminations
   (pa498314). Mitigation: per-termination `C` from the coverage scan's
   resting state; flag when the 2×2/4×4 check fails.
6. Engine/glue split cost: each slice needs a catpath release + pin bump
   (`catpath-wheel-version-reuse.md`). Mitigation: slices 1–2 are
   pure-function modules shippable behind `response: off`.
7. MLIPs have no Fermi level: one DFT point per slab is the floor cost for
   `U_pzc`; a quest proposing many slabs pays it each time. Mitigation:
   work-function point is a cached run-cube row; tier 2 later.
8. Scope: tier 2 is a research programme. Mitigation: gated on slice 5
   showing cation-sensitive decisive forks.

## Open questions for Reto

1. Label engine: GPAW-SJM in the existing `precis-dft` image (recommended)
   vs QE + ENVIRON through catpath's `dft.py` vs JDFTx (catpath's
   `jdftx-gcdft-backend.md`)?
2. Validate the method on CO₂RR (Au/Ag/Cu, Ringe's numbers, slice 4) before
   NO→NH₃ — about 1–2 node-weeks on a chemistry outside the quest?
3. Cation set: K⁺ only (KHCO₃/KOH cells) or the Li⁺…Cs⁺ sweep for slice 2?
4. Where a trained potential lives: job artifact + quest card (proposed) or
   a first-class ref (shared decision with `ephemeral-potentials-for-catpath.md`)?
5. Tier-2 go/no-go rule as written (gated on slice 5), or run Pd(111)/water
   now to anchor `U_pzc` for the whole Pd campaign?
6. Research budget: the session's Perplexity hourly cap ($20) was hit during
   this survey; raise it for research sessions?
7. CatMAP parity (GPL) stays with `electrochem-kinetics-beyond-che.md`
   slice 3 — keep it as the cross-check for slice 4?

## Rough compute cost

| tier | DFT | GPU/MLIP | wall (one campaign) |
|---|---|---|---|
| 0 | ~45 single points + 1 work-function point per slab; +2 NEB anchors | none beyond today | 1–2 node-days |
| 1 | 1500–5000 single points (active learning) | fine-tune hours; inference ×1.2 | 2–8 node-weeks, then minutes per candidate |
| 2 | 300–3000 AIMD frames (150–200 atoms) | LES-MACE fine-tune, ns MD | node-weeks to node-months |

## Target + blast radius

autocatpath `electrochem.py`, new `double_layer.py`, `config.py`,
`calculators.py`, `kinetics.py`, `corrections.py`; precis
`src/precis_pathway/` (seed params, views, basis), `src/precis/workers/job_types/`
(three new job types), `docker/precis-dft` (one subcommand), quest
`meta.operating_conditions`, explorer template, `pathway-correction-basis.md`'s
tuple. No migration unless a first-class potential ref is chosen (open
question 4).

## Related

`electrochem-kinetics-beyond-che.md` (rate side; its slice 2 is superseded
by this item's tier 0), the KINETICS spec (consumer),
`pathway-selectivity-u-ph-window.md` (charging route),
`surface-pourbaix-staircase-optimizer.md` (per-termination `C`, resting
state), `ephemeral-potentials-for-catpath.md` (potential storage),
`neb-barriers-in-the-catpath-pipeline.md` (NEB anchors),
`catalyst-discovery-quest.md` (backend per quest), catpath
`beyond-che-potential-response.md` and `jdftx-gcdft-backend.md`.
