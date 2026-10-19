---
status: draft
title: Kinetics v2 — rejection-free KMC core, absorbing-chain timescale handling, one free-energy ledger with detailed balance by construction, and a split input/output partial-pressure environment block
pillar: 3d-design
prio: high
model: opus
---

# Kinetics v2 for reaction pathways

Replace the single-site linear chain with one ledger, a site-balance MKM and a rejection-free KMC, all driven by a split input/output partial-pressure environment block.

Owner: catalysis-selectivity (thread file: Horizon 5a, next to
`electrochem-kinetics-beyond-che.md`). Engine work lands in catpath
(`~/catpath`, GPL-3) through chemistry's wheel-bump path; precis owns the
environment block, the job, the views and the quest scalars.
Survey evidence: `perplexity-research:498166` (Novotny lineage), `:498168`
(rare events), `:498172` (BEP/scaling/BEEF), `:498185` (SSA variants and
codes), `:498187` (thermodynamic consistency), `:498244` (gas boundary
conditions). Every claim below that matters is pinned to a paper handle.

## Motivation / why

Today's kinetics (`autocatpath/kinetics.py::solve`, run by
`precis_pathway.runner.run_kinetics_subprocess` after every aggregate, 900 s
cap) is a **linear master equation over one site**: each network state is
one whole-cell configuration, a site is in exactly one state, and
`rate_matrix` is a generator matrix whose steady state is solved exactly by
`_linear_steady`. That is the zero-interaction, single-site Markov chain. It
gets detailed balance right (`_step_pair`, `_gas_pair`, the `K_CAP` common
rescale, the structural check at the end of `build_transitions`) and it has
DRC/TRC/Monte-Carlo sensitivity. What it cannot do, with the code anchor:

| Gap | Where | Consequence |
|---|---|---|
| No site balance. Dissociation needs no vacancy, N+N coupling needs no second N; adsorption into state `a` is `k_ads·θ_a`, never `k_ads·θ_*`. | `rate_matrix` (one column per cell state) | "Coverage effect" (`coverage_effect = tof/tof_span`) measures the resting-state distribution inside one cell, not blocking or ordering. Selectivity at high coverage is unphysical. |
| No lateral interactions, no spatial correlation. | `coverage.py` docstring ("No lateral-interaction fitting"); kinetics has no hook | Fork margins at θ > 0.25 ML are extrapolations. |
| Steps without an NEB are **excluded** (infinite barrier) and only bracketed by the optimistic bound. The BEP tier (`bep.py`, with `band_eV`) is computed but never fed to kinetics. | `build_transitions` ("excluded -- never assumed barrierless"), `excluded_step_bracket` | Any non-verify run reports `tof_bracket.agree=False` and the quest drops the TOF (`_dispatch_common._kinetics_scalars`). |
| Adsorbate entropy TS = 0 (Nørskov convention), gas entropy from the table; TS carries the reactant's correction. | `thermo.py` ADSORBATES, module docstring | Adsorption K over-stabilises adsorbates by T·S_ads ≈ 0.2–0.35 eV at 298 K for NH₃/NO (Campbell–Sellers pa1169: S_ads ≈ 0.70·S_gas − 3.3R). Desorption prefactors are off by 10³–10⁵. |
| Unlisted gas → 1 bar with a warning; no input/output split; no conversion or approach-to-equilibrium. | `build_transitions` `p_of`, `ConditionsConfig.pressure` | A product never fed is priced at 1 bar, over-counts readsorption, and silently suppresses its own net production (the docstring says so). |
| Sweeps run over elements only. | `sweep.py` | No apparent orders, no pressure/T maps, no Q/K. |
| Monte-Carlo sensitivity draws every barrier independently. | `monte_carlo` | Destroys the correlated-error cancellation BEEF-style propagation relies on (Medford pa656; Wang pa498267). Bands are too wide for relative ranking, too narrow for absolute TOF. |
| No U, σ or pH in rates; CHE lives in `electrochem.py` post-processing. | `electrochem-kinetics-beyond-che.md` | Kinetic U/pH window undefined. |
| precis `view='kinetics'` is per-edge Eyring + τ, no coverages. | `toon_views.kinetics_text` ("Honest v1 only") | Dossiers cannot cite a coverage or a DRC. |

The state graph is small (pw455722: 50 states, 15 reaction edges), the
slab is fcc(111) in vacuum, barriers are MLIP CI-NEB with pooled spreads
(`uncertainty.Estimate`). That is exactly the regime where a lattice KMC is
cheap and a mean-field site-balance model is a one-second cross-check.

## In scope

1. An **environment block** (T, split partial-pressure table, optional
   U/σ/cation/pH) as the one input object for every kinetic solve, owned
   here, consumed by the sibling electrochemical spec through a provider seam.
2. One **free-energy ledger** per solve: every k_f/k_b pair derives from one
   ΔG and one TS free energy; a thermodynamic-consistency test battery runs
   before any steady state.
3. **Barrier sources** ranked NEB → DFT-refined → BEP (with band), flagged
   per transition, flux-weighted into a trust scalar.
4. A **rejection-free lattice KMC** core on the fcc(111) site graph.
5. **Timescale handling**: paired channel scaling plus a local
   absorbing-chain exit sampler (Novotny MCAMC lineage).
6. A **nonlinear mean-field site-balance MKM** as the cross-check and the
   cheap sweep engine.
7. Outputs: TOF, selectivity, coverages, DRC/TRC, apparent orders, apparent
   Ea, Q/K and per-step reversibility; a partial-pressure sweep job.

## Explicitly NOT in scope

- The σ/field/double-layer *physics* (a_i fits, PZC, cation models, MLIP-MD):
  the sibling electrochemical-interface spec. This spec fixes only the seam
  (§Environment and provider contract) and ships the vacuum provider plus a
  CHE-shift instance as the seam's proof (slice 8).
- Non-(111) facets, multidentate adsorbates occupying > 1 site, off-lattice
  (adaptive) KMC, reactor transport (CSTR/PFR coupling is a named
  follow-on, not v1).
- Replacing the energetic span or the CHE route scalars; they stay and are
  reported next to the kinetic numbers.
- NEB scheduling (`pathway-kinetics-promotion-gate.md` decides where NEB
  runs; `neb-barriers-in-the-catpath-pipeline.md` makes it run).

## Survey

### Methods

| Method | Papers | What it gives | Cost / limit | Verdict here |
|---|---|---|---|---|
| BKL n-fold way / rejection-free KMC | Bortz–Kalos–Lebowitz pa498245; Novotny 1995 pa498246; Novotny 1999 pa498248 | Every step changes the state; exact CTMC; class-grouped O(1)+O(D) selection when the rate catalogue is finite | Class count explodes with lateral interactions | **Core.** Fixed-reservoir rates are time-independent, so VSSM/BKL beats FRM. |
| Gillespie direct / FRM / next-reaction (Gibson–Bruck) | Gillespie 1977 pa498268; Gibson–Bruck pa498269; Lukkien 1998 pa498273 | Same CTMC; NRM O(log M) with a dependency graph; FRM handles time-dependent rates | NRM bookkeeping loses to optimised direct for small M | FRM only if the reservoir becomes time-dependent (reactor coupling). |
| Composition–rejection selection | Slepoy–Thompson–Plimpton pa498272 | Expected O(1) selection over rates spanning decades | Bins churn when rates move between classes | **Within-class selector** once lateral interactions (slice 7) break equal-rate classes. |
| τ-leaping | Gillespie 2001 pa498270 | Many firings per step | Approximate; negative populations; fails on stiff catalytic adlayers | Rejected. |
| MCAMC / absorbing Markov chains | Novotny PRL 1995 pa498247; Novotny CPC 2002 pa498252; tutorial 10.1142/9789812811578_0003 | Exact joint (exit time, exit state) from a transient set via (−Q)⁻¹; level 1 = n-fold way, higher levels skip whole basins | Transient set must be closed; matrix cost grows with set size | **Timescale tier 2** (local, enumerated basins). |
| Projective dynamics | Kolesik–Novotny–Rikvold pa498253; Biswas–Novotny pa498254 | Map a many-state process onto a 1-D chain conserving mean first-passage time; lifetimes over 50 decades | An analysis tool, not a propagator | Used for the MFPT cross-check of the single-site chain, not in the core. |
| Rejection-free for continuous DOF | Muñoz–Novotny–Mitchell pa498255 | n-fold way beyond finite spin classes | — | Not needed (lattice model). |
| Parallel discrete-event KMC (conservative PDES) | Korniss et al. pa498249, pa498250, pa498251 | Causality-preserving spatial decomposition; scalable with sparse long-range sync | Overhead for lattices < 10⁴ sites | Not v1; replicas on separate cores give the statistics we need. |
| Superbasin acceleration (AS-KMC) | Chatterjee–Voter pa498256 | History-based rate lowering with an error control δ | Configuration-level history is expensive for catalytic lattices | Superseded by channel scaling + local AMC. |
| Reaction-channel rate scaling | Dybeck–Plaisance–Neurock pa498257 | Per-channel quasi-equilibrium detection, both directions scaled by one factor, K preserved | Heuristic, no a-priori bound; convergence by tightening δ | **Timescale tier 1.** |
| Mean-rate / first-passage superbasin, local superbasin KMC | Puchala–Falk–Garikipati pa498258; Fichthorn–Lin pa498259; Swinburne et al. pa498260 | Exact (FPT) or mean-preserving (MRM) exits; comparison of all three | MRM loses exit-time/exit-state correlation | FPT form adopted for tier 2. |
| TPS / FFS / hyperdynamics / ParRep | Dellago pa498262; Allen pa498261; Voter pa498263 | Rates for genuinely rare events; FFS needs no reversibility | Path-ensemble machinery; for lattice KMC the rare events are already enumerated | Not adopted; FFS noted for a future off-lattice tier. |
| BEP / TS scaling | Michaelides pa498264; Bligaard pa58779; Wang pa34945; Abild-Pedersen pa34732 | Ea = α·ΔE + β per class, MAE 0.1–0.3 eV | 0.2 eV ≈ 3 decades in k at 298 K | **Barrier fallback**, with band, flagged (already in `bep.py`). |
| Energetic span | Kozuch–Shaik pa498265, pa498288 (concentrations); Cohen–Vlachos pa498266, pa498289 (heterogeneous MESA, parallel paths) | TDI/TDTS, concentration-corrected TOF | Linear cycle, no site balance, no branching | Kept as the zero-coverage limit; MESA informs the reporting. |
| BEEF-style correlated UQ | Medford pa656; Wang 2019 pa498267 | One functional member per full pipeline run; ranking probabilities | Ensemble not available from MLIPs; emulate with pooled model spread | **Correlated draw design** replaces independent per-step draws. |
| Thermodynamic consistency | Mhadeshwar–Wang–Vlachos pa498280; Andersen–Panosetti–Reuter pa498279; Reuter 2004 pa498283; Nolen 2024 pa498291 | k⁺/k⁻ = K per step; Wegscheider Bᵀ ln K = 0; ω proximity factor for TS under interactions | — | **Enforced by construction + test battery.** |
| Adsorbate partition functions | Campbell 2016 pa1308; Sprowl–Campbell pa2203; Campbell–Sellers pa1169; Bajpai pa498292 | 2D gas / lattice gas / hindered translator; θ/(1−θ) activities | No universal choice | Hindered-translator default, same model on both sides of every step. |
| Gas boundary conditions, De Donder, DRC | Holstein–Boudart pa498285; Dumesic pa498282; Campbell 2001 pa498287, 2009 pa478851, 2017 pa51354; Razdan–Lin–Bhan pa498286 | μ(T,p) per gas, r⁺/r⁻ = exp(A/RT), reversibility z_i, net vs unidirectional rates | ln r_net singular near Q = K | **Split table; report Q/K, z_i, r⁺ and r_net.** |

### Codes

| Code | Model | Selection | Lateral interactions | Licence / fit |
|---|---|---|---|---|
| Zacros (Stamatakis–Vlachos pa498275, pa498290; Nielsen pa498281) | Graph-theoretical on-lattice KMC | FRM, priority queue, graph matching | Cluster expansion, long-range, ω proximity factor | Academic licence, Fortran binary — cannot be pinned in the agent image. Benchmark only (validation case C is a Zacros paper). |
| kmos/kmcos (Hoffmann–Matera–Reuter pa498276; guide pa498279) | Lattice KMC, generated Fortran | VSSM, O(log R) class pick + O(D) local update | `local_smart` catalogue, `otf` runtime rates | GPL-3; needs f2py build per model — too heavy per candidate. Design reference for the core. |
| MonteCoffee (Jørgensen–Grönbeck pa498277) | Programmable Python KMC | FRM | User rate functions | GPL; pure-Python speed; design reference for the rate-function hook. |
| CatMAP (Medford pa1560) | Descriptor mean-field MKM | Newton on steady state | Mean-field self/cross matrix | GPL-3; parity export already planned in `electrochem-kinetics-beyond-che.md` slice 3. |
| MKMCXX | Mean-field MKM, stiff ODE | — | Mean-field lateral potentials | Closed binaries; not adoptable. |
| CatKit | Structure/site enumeration | — | — | Not a kinetics code. |
| Pineda–Stamatakis review pa498278 | — | — | — | The timescale-disparity problem is the named open challenge; our tier 1+2 answer it for this envelope. |

## Design

### D1. Environment block (precis owns it; the engine consumes it verbatim)

One object, every solve, every sweep point, every provider:

```yaml
environment:
  T: 298.15                         # K
  gases:
    inputs:  {NO: 0.10, H2: 1.00}   # feed partial pressures, bar
    outputs: {NH3: 1.0e-3, H2O: 1.0e-3, N2: 1.0e-4, N2O: 1.0e-5, NH2OH: 1.0e-6}
    inerts:  {}                     # e.g. {Ar: 0.9}; priced nowhere, counted in total
    unlisted: refuse                # refuse | reference  (reference = 1 bar + warning, today's behaviour)
  sticking: {NO: 1.0, H2: 0.1}      # s0 per impinging molecule; default 1.0, stated in output
  electrochemical: null             # optional; schema owned by the electrochem spec, keys reserved:
                                    #   {U_vs_RHE, pH, sigma_uC_cm2, U_pzc, cation, provider}
```

Rules:
- `inputs ∪ outputs` must cover every gas the network can consume or
  release (`ledger.GAS_MOLECULES` ∩ network ledgers ∪ cycle-closure gases
  found in `kinetics.solve`). With `unlisted: refuse` a missing gas is a
  `ValueError` naming the gas and the key to add; `reference` keeps today's
  fallback for legacy configs. Default `refuse`.
- A product pressure of 0 is refused (μ → −∞); the far-from-equilibrium
  preset is `outputs: far` = 10⁻⁶ bar for every product, recorded as a
  preset, never as a measurement.
- `total_bar` is derived and reported; the sweep (D7) holds it fixed by
  default.
- The block hashes into the correction basis
  (`pathway-correction-basis.md`): two solves compare only on equal
  `(environment_schema=1, T, inputs, outputs, inerts, sticking,
  electrochemical.provider+params)`.
- Replaces `ConditionsConfig.pressures` and `KineticsConfig.sticking` in
  the engine via a reconciling adapter (`from_dict` keeps the old keys
  readable; new runs write the block).

### D2. Provider contract (the seam the electrochem spec plugs into)

```python
class EnergyProvider(Protocol):
    id: str                                       # stamped into the correction basis
    def gas_mu(self, gas: str, env: Environment) -> float          # μ(T, p) incl. ledger refs, eV
    def state_g(self, state: str, env: Environment) -> Estimate    # G(state) − G(root), ledger-priced
    def ts_g(self, step: StepKey, env: Environment) -> TSEstimate  # G_TS − G(root): .mean .std .source .band .provenance
```

- `ts_g` returns the **transition-state free energy**, never a forward or a
  reverse barrier. The rate builder is the only place rates are formed:
  `k_f = κ·(kT/h)·exp(−(G_TS − G_IS)/kT)`, `k_b = κ·(kT/h)·exp(−(G_TS − G_FS)/kT)`,
  with `G_TS ← max(G_TS, G_IS, G_FS)` applied **once** before both; so
  `k_f/k_b = exp(−ΔG/kT)` identically (the current `_step_pair` contract,
  promoted to the only path).
- Adsorption: `k_ads = hertz_knudsen(p_i, m_i, T, A_site, s0)`,
  `k_des = k_ads / K`, `K = exp(−(state_g(b) − state_g(a))/kT)` where the
  gas μ already carries `kT ln(p_i/p°)` (today's `_gas_pair`, unchanged).
- Composition: `VacuumProvider` (nodes/edges/`thermo.Table`, today's
  numbers) → `ShiftProvider(base, shifts)` applies per-species
  `ΔG_i(env) = ΔG_i(0) + a_i·σ(env)` to **states** and moves the TS by
  `(1−ω)·δG_IS + ω·δG_FS` (ω = BEP slope of the step's class from
  `bep.py`, default 0.5), so both rates still derive from one shifted ΔG.
  The electrochem spec supplies `a_i`, `σ(U, U_pzc, cation)` and the work
  function descriptor; this spec only promises the slot and the invariant.
- Every provider output carries `provenance` (`neb | dft | bep_fit |
  bep_prior_shifted | bep_literature | bound`) and `band`.

### D3. One ledger, detailed balance and thermodynamic consistency by construction

Rates are never stored; they are rebuilt from `(state_g, ts_g, gas_mu)`
for the current environment. Before any steady state the battery runs
(all tests are pure functions over the transition list; failures are
`AssertionError`s with the offending step or cycle named):

| Test | Check | Catches |
|---|---|---|
| Elementary ratio | `ln(k_f/k_b) + ΔG/kT < 1e−8` per transition (exists today, kept) | prefactor or clipping asymmetry |
| Barrier identity | `Ea_f − Ea_b = ΔG` after floors and shifts | independent clipping, TS shifted on one side |
| Wegscheider / null-space | stoichiometric matrix N over surface species + gases; B = null(N); `‖Bᵀ ln K‖ < 1e−8`; cycles with non-zero gas stoichiometry must sum to the overall gas-phase ΔG | inconsistent references, path-dependent ledgers (extends `compute_ledgers`'s path check to rates) |
| Kolmogorov loop (lattice) | random configurations, random closed event loops: `Π k_cw = Π k_ccw` | event catalogue errors, stale neighbour rates (slice 7) |
| Equilibrium flux | set `outputs` so that Q = K for the overall reaction; every `|r⁺ − r⁻| < tol`, net production ≈ 0 | hidden circulating fluxes |
| Standard-state invariance | change p° and the site-density convention; physical rates invariant | missing concentration factors |
| Entropy production | `Σ (r⁺ − r⁻) ln(r⁺/r⁻) ≥ 0` at every solved point | sign errors |
| Temperature consistency | `K(T)` from `k_f/k_b` vs `exp(−ΔG(T)/kT)` at 3 T | entropy terms applied on one side |

Entropies: `thermo.ThermoConfig.adsorbate_mode: ts0 | harmonic | hindered`
(default stays `ts0` until Reto rules — see open question 4). `hindered`
= Sprowl–Campbell (pa2203) for the two frustrated translations and the
azimuthal rotation, harmonic for the rest, with `freq_floor` as today;
the TS inherits its reactant's correction minus the reaction coordinate
(unchanged convention, stated in output). A mode change bumps
`corrections.VERSION` so margins never compare across it.

### D4. Barrier sources and scaling

Per transition, in order: NEB `Estimate` (trust-gated by `trust.py`;
blocked records are not barriers) → DFT-refined saddle (`refine.py`,
std = 0, stated) → BEP from `bep.py` with `band_eV` as a Gaussian σ,
`source` carried → thermodynamic bound `max(0, ΔG)` only inside the
bracket guard, never as a rate. Excluded steps disappear: every step gets
a rate and a provenance. Outputs add `bep_flux_share` = fraction of the
target's net production that passes through a non-NEB transition at
steady state; `_kinetics_scalars` marks `kinetics_trusted=False` with note
`bep_flux_share > 0.5` (threshold a quest knob). Fast-process rescaling
stays what `_capped` does: one common factor per pair, applied only to
pairs the quasi-equilibrium detector (D5) names; K is untouched.

Uncertainty: replace independent per-step draws with correlated draws —
one global shift per MLIP model (the pooled model-form spread), one
residual per BEP class, one per-step NEB spread — and report the TOF band,
the controlling-step frequency and the **rank probability** against the
quest's reference candidate (Medford pa656). Sampling budget unchanged
(`kinetics.n_samples`).

### D5. Rejection-free KMC core (engine `autocatpath/kmc.py`)

- Lattice: the fcc(111) site graph from `structures.discover_sites`
  (top/bridge/fcc/hcp classes, periodic L×L cells, default 24×24), one
  adsorbate per site, species = the network's fragments; the network's
  states are decomposed into their fragments on neighbouring sites by the
  same placement `coverage.py` uses.
- Events: adsorption (one or two empty sites, Hertz–Knudsen), desorption,
  the network's reaction edges as site-pair events, diffusion hops with a
  BEP-class barrier (`isomerization`), cycle closures from `kinetics.solve`.
- Selection: VSSM — events grouped by elementary class, class picked by
  O(log R) binary search on cumulative class rates, member uniform within
  an equal-rate class, composition–rejection within a class whose rates
  differ (slice 7 onward). Local O(D) update of the enabled-event lists
  after each event (kmos `local_smart` shape, numpy + optional numba).
- Statistics: N independent replicas (seeded), block averaging after a
  burn-in detected by a stationarity test on the production counter;
  TOF with a 90 % CI; coverages per species per site class; pair
  correlation of the two most abundant species.
- Why not kmos/Zacros: Zacros is not pinnable (licence, binary); kmos
  compiles Fortran per model, which is a build step per candidate; both
  are the validation references instead. The core is ~800 lines; the
  physics (rates, ledger, BEP, trust) already exists in catpath.

### D6. Timescale handling

Tier 1 — channel scaling (Dybeck pa498257): per reversible channel,
after `n_min` executions, `|n⁺ − n⁻|/(n⁺ + n⁻) ≤ δ` marks it
quasi-equilibrated; both directions are divided by the same factor `f`
while it stays so; any non-equilibrated event resets the channel.
Convergence protocol is part of the output: the solve runs at
`(δ, f)` and `(δ/2, f/2)`; `tof_scaling_drift = |Δlog₁₀ TOF|`; drift >
0.3 marks the KMC TOF untrusted. No a-priori bound exists; the drift is
the bound we report.

Tier 2 — local absorbing chain (Novotny MCAMC pa498247, FPT form
pa498258/pa498259): when the walker revisits a configuration set within
`m` events, enumerate the transient set S by breadth-first expansion over
events whose rate exceeds `γ·k_slowest_exit` (depth-capped at d), build
Q (S×S) and R (S×exits), sample the exit time from `exp(Qt)` and the
exit state from `R(−Q)⁻¹p₀` **jointly**, jump. Exact for a closed S; the
truncation error is the probability mass of fast events found at the
boundary of S at depth d, measured and reported as `amc_leak`; the solve
refuses to accelerate (falls back to tier 1) when `amc_leak > 1e−3`.
Tier 2 is what handles H adsorption/desorption at 1 bar H₂ and H hopping
among equivalent hollows without touching their rates.

The single-site chain keeps its exact linear solve and additionally
reports the root→product mean first-passage time from the fundamental
matrix (projective-dynamics identity pa498254): `1/τ_MFPT` vs `tof` is a
free self-check of the chain.

### D7. Mean-field site-balance MKM and the sweep (engine `autocatpath/mkm.py`)

- Species coverages θ_i per adsorbate fragment and θ_* vacancies per site
  class; rate laws with vacancy factors (dissociative adsorption ∝ θ_*²,
  pair reactions ∝ θ_iθ_j·z/2); lattice-gas activities θ/(1−θ) where the
  partition-function mode demands them (Campbell pa1308); Newton with an
  analytic Jacobian from a pseudo-transient (BDF) warm start; the same
  battery (D3) runs on its rate constants.
- Outputs: TOF_mf, coverages, DRC and TRC (the existing finite-difference
  code shape, forward and reverse scaled together), apparent orders
  `n_i = ∂ln r/∂ln p_i` on a 5-point log stencil for every gas in
  inputs **and** outputs, apparent Ea from a 3-point T stencil, Q/K and
  per-step reversibility `z_i = r⁻/r⁺`.
- Three numbers always together: `tof_span_limit` (today), `tof_chain`
  (today's linear solve), `tof_mf`, and `tof_kmc` with CI when the KMC
  ran. `|log₁₀(tof_kmc/tof_mf)|` is reported as the correlation effect.
- Sweep: `kinetics.sweep: {design: grid | lhs, axes: {NO: [1e-3, 1], H2:
  [0.1, 10], NH3: [1e-6, 1e-2], T: [298, 400]}, log: true, n: 32,
  fixed_total: true}`. Grid for ≤ 2 axes, Latin hypercube (seeded) above.
  Per point: MF solve (< 1 s) with orders, DRC, Q/K; KMC on the `kmc_points`
  subset (default: the corners and the centre, 4 replicas). Products with
  a non-zero order anywhere in the sweep are listed as inhibitors.
  Output: `kinetics_sweep.json` (rows = points), plus the 2-D map of the
  dominant DRC step and of selectivity over any two axes.

### D8. Outputs (engine `kinetics.json` v2; precis scalars)

`tof_kmc{median,p5,p95,n_events,replicas}`, `tof_mf`, `tof_chain`,
`tof_span_limit`, `selectivity{gas: share}`, `coverages{site_class:
{species: θ}}`, `drc{X_RC}`, `trc{X_TRC}`, `orders{gas: n}`, `ea_app`,
`q_over_k`, `reversibility{step: z}`, `bep_flux_share`,
`tof_scaling_drift`, `amc_leak`, `environment` (verbatim), `provider`,
`warnings`. precis `_kinetics_scalars` lifts `tof` (= `tof_kmc` when
present, else `tof_mf`), `log_tof` band, `kinetics_method`,
`bep_flux_share`, `order_<gas>`, `q_over_k`, `drc_top`, with the trust
gate extended by `bep_flux_share`, `tof_scaling_drift` and `amc_leak`.
`toon_views.kinetics_text` renders coverages, DRC and orders from the
solve instead of the per-edge Eyring table.

### D9. Interfaces: engine vs glue, job type, where it runs

| Piece | Lives in | Runs where |
|---|---|---|
| `environment.py`, `provider.py`, `mkm.py`, `kmc.py`, `consistency.py`, tests | catpath (wheel bump through chemistry) | — |
| environment block in `put(kind='pathway')` params and `_prep`; config snapshot | `precis_pathway/runner.py`, `types.py` | — |
| `autocatpath_kinetics` job (new): inputs = pathway handle + environment (+ sweep); reads `results_json`, writes `meta.kinetics_v2` and a `kinetics_sweep` artifact | `precis_pathway/kinetics_job.py` (new), registered via `precis.job_types` | CPU worker pool, never the GPU seed lane; 1 core per replica, wall 1 h default, own `PRECIS_AUTOCATPATH_KINETICS_SECONDS` |
| aggregate keeps the cheap MF+chain solve in-process (replaces today's call, same 900 s cap); KMC never runs inside the aggregate | `aggregate_job.py` | as today |
| quest harvest: new scalars as `meta.rubric_objectives` candidates; promotion gate unchanged | `precis.quest.compute` | — |
| views: `view='kinetics'` (solve), `view='kinetics-sweep'` (TOON rows) | `handler.py`, `toon_views.py` | — |

The KMC is CPU-only (numpy, optional numba); nothing here needs the MLIP.
A full sweep (32 MF points + 5 KMC points × 4 replicas × 10⁷ events) is
about 1 CPU-hour; a single KMC point about 1–5 min.

## Slices

1. **Environment block + split pressure table (engine + glue).** `refuse`
   default, presets, Q/K and reversibility in today's chain solve, basis
   hash. Acceptance: a config lacking a product gas the network releases
   refuses naming the gas; pw455722 replay at `outputs: far` vs today's 1
   bar changes `selectivity` and both are stamped with different bases;
   at `outputs` solved for Q = K every `|r⁺ − r⁻| < 1e−6·k_scale`.
2. **Ledger + provider seam + consistency battery.** `VacuumProvider`, the
   rate builder as the only rate path, the eight tests, `adsorbate_mode`
   knob (default unchanged). Acceptance: battery green on the NO→NH₃
   network; a fixture with an independently clipped reverse barrier fails
   the barrier-identity and Wegscheider tests with the step named; mode
   `hindered` shifts NH₃ desorption K by the Campbell–Sellers amount within
   0.1 eV and bumps `corrections.VERSION`.
3. **BEP as a rated source + correlated uncertainty.** Excluded steps
   vanish; `bep_flux_share`; correlated draws; rank probability.
   Acceptance: a screening-tier run now reports a TOF with
   `kinetics_trusted=False, note='bep_flux_share=…'`; on a verify run
   `bep_flux_share = 0` and the TOF equals slice-2's; the correlated band
   on pw455722 is narrower than today's independent band for the
   candidate-vs-reference ratio and wider for the absolute TOF.
4. **Mean-field site-balance MKM + orders + sweep job.** Acceptance:
   non-interacting single-site limit reproduces `tof_chain` to 1 %;
   `Σ X_RC = 1 ± 0.05`; orders for every input and output gas; the
   `autocatpath_kinetics` job runs a 2-axis grid on a fixture pathway on a
   CPU worker and `view='kinetics-sweep'` renders it.
5. **Rejection-free KMC core.** Acceptance: chemistry off reproduces the
   Langmuir isotherm (θ vs p at 3 T) within CI; non-interacting single-site
   fixture reproduces `tof_chain` within CI; validation case C
   (Piccinin–Stamatakis CO oxidation on Pd(111), `tests/validation/`)
   within 0.5 log₁₀ of the published TOF at its conditions; equilibrium
   flux test passes on the lattice.
6. **Timescale tiers.** Acceptance: 1 bar H₂ with a 0.5 eV cycle step —
   events per unit physical time drop ≥ 100× with TOF inside the
   unaccelerated CI and `tof_scaling_drift < 0.3`; AMC exit-time and
   exit-state distributions on a 3-state toy match brute-force KMC
   (KS test p > 0.05); `amc_leak` reported and the fallback triggers on a
   fixture with an open basin.
7. **Lateral interactions (pair cluster expansion + ω).** Optional.
   Acceptance: Kolmogorov loop test and cluster finite-difference test
   green on random configurations; O/Pt(111) ordering fixture reproduces
   the p(2×2) phase at the published coverage.
8. **Electrochemical provider pointer (seam proof only).** A `ShiftProvider`
   instance with CHE `−n_H·eU` on states and ω-split TS as the minimal
   electrochemical provider; `environment.electrochemical.provider='che'`.
   Acceptance: battery green at U = −0.2 V; `k_f/k_b` of every H-supply
   step equals `exp(−ΔG(U)/kT)`; the sibling electrochemical-interface spec
   plugs `a_i·σ` and the work-function descriptor into this seam without
   touching the rate builder. Everything beyond this line is that spec's.

Order: 1 → 2 → 3 (one build each, engine + glue), 4 (one build), 5–6
(two builds), 7–8 (one build each). 1–4 are useful without any KMC.

## Target + blast radius

catpath `kinetics.py`, `thermo.py`, `config.py` (conditions/kinetics
blocks), new `environment.py`/`provider.py`/`mkm.py`/`kmc.py`/
`consistency.py`, `tests/validation/` case C; precis `precis_pathway/
runner.py` (`_prep`, `run_kinetics`), `_dispatch_common._kinetics_scalars`,
`aggregate_job.py` kinetics tail, `toon_views.kinetics_text`, `handler.py`
views, a new job type, `precis.quest.compute` harvest keys,
`pathway-correction-basis.md` (environment joins the basis tuple).
Every existing TOF-bearing measure re-stamps its basis on slice 1; no
existing margin changes value until slice 2's `adsorbate_mode` is switched.

## Open questions / decisions log

1. Implement the KMC core in catpath (recommended, ~800 lines, CPU, no
   build step) or depend on kmos (GPL-3, f2py build per model)?
2. numba in the agent image, or numpy-only with a 10× slower core?
3. DRC from the MF model (cheap, recommended) with KMC only for TOF and
   coverages, or KMC-DRC via common random numbers (≈ 2·N_steps extra runs
   per point)?
4. `adsorbate_mode` default: keep `ts0` (every existing margin stays
   comparable) or switch to `hindered` now and re-stamp the basis for the
   whole qu164903 frontier?
5. Default product pressures when a quest states none: `far` (10⁻⁶ bar)
   or refuse at the quest level and require the seed to state them?
6. Does `autocatpath_kinetics` join the tier ladder (verify only) or run
   as its own rung the quest can request per candidate?
7. Zacros benchmark reproduction without Zacros: accept the published
   numbers of validation case C as the reference, or ask an academic
   licence holder for one reference trajectory?
8. Lateral interactions (slice 7) now or after the first paper?
9. The σ-shift extension (Ringe, youtube `NCvmskkpvws` chunks 18, 30–33,
   38, 49): strongly affected species OH and CO₂, weakly H and CO; the
   coordinator wants it optional. Confirm the sibling spec owns `a_i`
   values and the PZC/cation model, this spec only the seam.
