---
status: draft
title: Per-slab environment optimizer — the quest finds each slab's best setup over U, pH, T, pressures and salt against three goals, and names the next point to compute
pillar: 3d-design
prio: high
---

# Per-slab environment optimizer

Reto, 2026-11-03 (chat, deciding who may mint from the compute cart of
`pathway-explorer-controls.md`): the user, or the quest. "The quest can
make a call to find the best setup for any material, getting differential
pressures, temperatures, salt concentration, pH and so on
multi-optimized. I expect the quest to spend a multiple of time per slab
now." Goals, his words: (a) make the desired reaction the most
energetically and kinetically favourable at each step; (b) minimise side
reactions; (c) maximise reaction speed. Some parameters can be wiggled
(we get a derivative, a slope says which way is strictly better); for
others downhill is not defined; an optimizer should help with both.

This is the inner loop of the surface-Pourbaix optimizer's §3 nested
search (`surface-pourbaix-staircase-optimizer.md`: outer = slab space,
DFT-priced; inner = environment space, cheap once the anchors exist),
widened from (U, pH) to the whole environment and run by the quest per
slab. It feeds the explorer's environment box (snaps) and Pareto tab.

## What exists (read 2026-11-03)

| piece | where | status |
|---|---|---|
| (U, pH) window per route, ε per rung, HER as a mask rung, Shapley attribution as a field, window area and its reductions | `precis/quest/window_objectives.py` (`window_measures`, `shapley_field`), `precis_pathway/staircase_masks.py` | built (SPSO slices 4–6); needs a coverage scan for the resting state, else the pathway's own energetics alone |
| (T, p_total, composition) sweep with an objective (`maximise`, `subject_to` rate floor) and a Nelder–Mead optimum on the mean-field surface | kinetics v2 D7, `kinetics.sweep`; `optimum` on the record; `view='kinetics-sweep'` | built; 46 qu164903 pathways carry an optimum (gr543723 comment 42) |
| derivatives from the mean-field solve: apparent orders `∂ln r/∂ln p_i` (5-point log stencil, every input and output gas), apparent Ea (3-point T stencil) | v2 record `orders`, `ea_app` | built; `ea_app` fails where the state is unresolved (pw549461) |
| per-rung slope in U: `∂ΔG/∂U = n_e` exactly | `che_affine` | built |
| quest-level Pareto over slabs, rubric objectives, rubric-first-two plot axes | `precis/quest/frontier.py` | built |
| Optuna acquisition for the *outer* loop (`suggest_next(quest)`, MOTPE/NSGA-II, no persistent study) | `catalyst-discovery-quest.md` slice 7 | unbuilt |
| kinetics as a function of U | `electrochem-kinetics-beyond-che.md`; kinetics v2 slice 8 (CHE shift provider) | unbuilt — kinetic objectives do not move with U today |
| salt / cation effect on energetics | `electrochem-mlip-ladder.md` | unbuilt — `ion_conc_M` enters only the bulk Pourbaix verdict and the spectator reservoirs |

## Design

### Objectives and masks (Reto's a, b, c; SPSO's rulings)

Per environment point `e = (U_RHE, pH, T, p_i…, ion_conc_M)` on one slab
and its pathway:

| goal | scalar | source |
|---|---|---|
| (a) favourable at every step | `margin_thermo(e)` = min over target-route rungs of −ΔG(e) (one-rung PCET form, ε as ruled: 0.3 eV chemical, 0 electrochemical); `margin_kin(e)` = worst fork margin on barriers where measured, BEP where not and then flagged | staircase masks; `results.score.selectivity_margin_eV`; `pathway-selectivity-u-ph-window` |
| (b) side reactions | `selectivity_target(e)` (share of the target's key element leaving as target); `her_margin(e)` | v2 record; `her_stats` |
| (c) speed | `log10 TOF(e)` with its trust (`bep_flux_share`, settled, poisoned, KMC drift) | v2 record |
| mask | the resting termination at (U, pH) is the surface the pathway assumed (coads-mismatch), the bulk verdict at `ion_conc_M` is not dissolved, the reset contour holds | `point_states`, `pourbaix_bulk`, `reset_contour` |

SPSO rulings carried over: HER is a mask rung and its own objective; a
TOF floor is a constraint, never folded into the window; masks are
intersected, objectives are never summed. So the output is a **Pareto
set of environment points**, not one number; "best setup" is the
rubric's pick on that set (human-set weights, as for slabs).

### The scalar the optimizer climbs (Reto, 2026-11-03: "do we have a clear cost function?")

**Today: no.** At the run point the engine gives `results.score`, four
worst-case axes (activity span, selectivity margin, poison margin, trap
margin) with a `limiting_factor`; across slabs the quest ranks on
Pareto objectives or a human-weighted `rubric_composite`. Neither is a
function of the environment. Proposed, and consistent with the rulings
(masks intersect, objectives are never summed — a minimum is not a
sum), the **bottleneck form**:

```
J(e) = min{ m_th(e), m_kin(e), m_HER(e), s(e), r(e) }   if M(e) = 1, else −∞
e*   = argmax_e J(e)
```

every term in eV at the environment point `e = (U, pH, T, p_i, c_ion)`:

| term | formula | plain text |
|---|---|---|
| `m_th(e)` | `min_r [ε_r − ΔG_r(e)]` over the target route's rungs, `ΔG_r(e) = ΔG0_r + n_e,r·U` (+ the pH, `kT·ln p` and modes terms as they ship), `ε_r` = 0.3 eV chemical / 0 electrochemical | the worst step's downhill margin: positive means every step is downhill within tolerance |
| `m_kin(e)` | `min_b [Ea_side,b − Ea_on,b]` over branch points on the target route (NEB; BEP where none, flagged) | at the worst fork, how much higher the side barrier is than the on-route one; U-flat under CHE |
| `m_HER(e)` | `ΔG_lim,HER(e) − ΔG_lim,route(e)` | the route's limiting step beats hydrogen evolution's by this much |
| `s(e)` | `kT·ln[S(e) / (1 − S(e))]`, `S` = fraction of the key element leaving as the target | selectivity as an energy: 0 at 50 %, +0.06 eV at 90 %, +0.12 eV at 99 % (298 K) |
| `r(e)` | `kT·ln[TOF(e) / TOF_floor]`, `TOF_floor` = the quest's `subject_to` floor | rate above the required rate, as an energy: one decade = 0.059 eV at 298 K |
| `M(e)` | 1 iff the resting termination at (U, pH) is the surface the pathway assumed, the bulk is not dissolved at `c_ion`, `reset_ok`, the solve is trusted (`bep_flux_share` ≤ 0.5, settled, not poisoned) and `TOF ≥ TOF_floor` | the setup is physically the one that was computed, and the numbers are trusted |

Plain text: **a setup is as good as its worst goal, in eV; the term that
sets the minimum is the limiting factor**, the same reading
`results.score.limiting_factor` gives at one point. Goal (a) is
`min(m_th, m_kin)`, goal (b) is `min(m_HER, s)`, goal (c) is `r`; the
`kT·ln` forms put rate and selectivity in the units the margins already
use, so one number compares them. A quest that wants trade-offs instead
of a bottleneck uses the weighted alternative `J_w(e) = Σ_k w_k·term_k`
with `rubric_composite` weights (human-set, never tuned by the agent).
The Pareto set is kept beside `J` for the explorer's tab; `J` is only
the scalar the search climbs and the number `env_best` reports.

Worked example, pw549461 at its run point (298 K, 1 bar reference, U = 0):
`M = 0` (BEP-priced, `bep_flux_share = 1`; no scan, so the termination is
unknown). Unmasked: `r = −2.9 eV` (TOF 7e-53 against a 1e-3 floor: fifty
decades short, surface held by N), `m_th = −0.025 eV` (NO@N→N+O at
+0.325 eV against ε = 0.3, U-flat, so no potential fixes it), `m_HER =
+0.39 eV` on route 1. Limiting factor: rate. What the cart would ask
for: a coverage scan (to set `M`), NEB on the N–O scission (to replace
the BEP `m_kin`), then a re-solve.

Availability of each term as a function of `e` today: `m_th` moves with
U (closed form), not yet with pH, T or p; `m_kin` is U- and T-flat and
needs barriers; `m_HER` moves with U; `s` and `r` move with T and p
inside a stored sweep and not with U (kinetics v2 slice 8); `M`'s
termination needs a scan, its bulk verdict a `pourbaix_bulk` job.

### Axes and where their derivatives come from (the `differentiation` skill applies)

| axis | smooth? | derivative source | discrete jumps |
|---|---|---|---|
| U_RHE | piecewise linear | `n_e` per rung, exact; the margin's slope is the limiting rung's `n_e` | kink where the limiting rung switches; the resting termination flips at a Pourbaix boundary |
| pH | flat on RHE for PCET rungs | protonation term `−kT·ln(1 + 10^(pKa−pH))` on the desorption rung, closed form; fixed-SHE shifts U by `0.059·pH` | HER/bulk masks |
| T | smooth | `ea_app` for the rate; `∂ΔG_rung/∂T = −ΔS_rung` from stored modes; a selectivity stencil `∂ln S/∂(1/T)` (to add beside `ea_app`) | MARI switches |
| p_i, p_total | smooth in ln p | `orders` for the rate; `∓kT` per exchanged molecule on gas rungs, exact; a selectivity stencil `∂ln S/∂ln p_i` (to add beside `orders`) | product preset boundaries |
| ion_conc_M | none in the model | zero, stated | bulk verdict boundary moves by `0.059/n·log c` |

Per the skill: every term names how its derivative is obtained
(analytic / stencil / none) and the stencil ones are checked against a
central finite difference on one fixture before any step uses them.

### The search: three layers, cheapest first

1. **Closed-form lines.** U and pH are swept at zero cost on the
   staircase (`ΔG0 + n_e·U`, the pH terms); the viable U interval per
   point of the (T, p) grid is read off directly. No solver.
2. **Stencil steps in T and p.** From the best stored grid point, a
   bounded trust-region step along (`ea_app`, `orders`, the selectivity
   stencils) with one mean-field solve per trial point (< 1 s with
   modes), stopping at the box edge or when the limiting rung / MARI
   switches (a kink: fall to layer 3). This is Reto's "wiggle": it is
   where a slope exists.
3. **Derivative-free, multi-objective over the box.** Multi-objective
   TPE or NSGA-II (the sampler `catalyst-discovery-quest` slice 7 names;
   Optuna in-process, no persistent study — the stored points *are* the
   study) over all axes at once, seeded with the sweep grid and the
   layer-2 points, budgeted in solver calls. It handles the kinks and the
   discrete masks and returns the Pareto set. Nelder–Mead (what D7 runs
   today) stays as the single-objective fallback.

Outputs per slab, stamped on the candidate as measures and
non-measures: `env_best` (the rubric's pick: the point, its objectives,
its trust), `env_pareto` (the set, capped), `env_next` (the point to
compute next and *why*: outside the sweep box, resting state unscanned
at that (U, pH), a decisive edge without a barrier, `ea_app` unresolved)
with the matching cart items.

### How the quest uses it

- Runs code-driven (no LLM call) after a slab's kinetics job lands and
  after a new coverage scan or NEB on that slab; minutes of CPU per
  slab (hundreds of mean-field solves), which is the "multiple of time
  per slab" Reto expects. GPU time is spent only when `env_next` asks
  for it, minted through the cart under the quest's caps and tote, the
  $25 rule per request, and the holds (an item held by
  `fidelity_restale_cap = 0` queues and runs at the lift).
- Slabs are ranked at their own best environment (SPSO §3); the Pareto
  set's size (how much of the box is viable and selective) is the
  robustness objective SPSO already names, so a slab that wins only at
  one corner does not beat one that wins broadly.
- "How the quest fits the next target": the tick keeps proposing the
  next *slab*; this item supplies, per slab, the next *environment*
  point and the next *computation*. The user's own ideas go through the
  same cart by hand.

### The U gap, stated

Until kinetics v2 slice 8 lands, `selectivity_target` and `TOF` do not
move with U: the Pareto set is the product of the thermodynamic (U, pH)
read and the kinetic (T, p) read, and `env_best` says so ("kinetics at
the run U"). After slice 8 the sweep gains a U axis and the three layers
run on one surface. Salt stays a bulk-verdict mask until the ladder
item supplies a cation term.

## In scope

The objective/mask set and the derivative table above; the three-layer
search as a `precis.quest` module (`environment_optimizer.py`) with the
selectivity stencils added to the engine's sweep output (one catpath
change, version bump); the stamps `env_best` / `env_pareto` / `env_next`
and their declaration as measures; the quest step that runs it; the cart
hand-off for `env_next`; the explorer snaps and the Pareto tab read the
stamps (owned by `pathway-explorer-controls.md`).

## Explicitly NOT in scope

- Kinetics as a function of U (kinetics v2 slice 8) and the cation term
  (electrochem-mlip-ladder): consumed when they exist.
- The outer slab loop and its Optuna advisor (catalyst-discovery-quest
  slice 7); rubric weights (human-set).
- Coverage effects, lateral interactions, bigger slabs.
- Replacing the (U, pH) window or the sweep optimum; both stay and are
  this item's layers 1 and 2.

## Acceptance criteria

- On pw511428 (sweep + optimum stored: 400 K, 10 bar, selectivity.NH₃
  1.34e-4 at TOF 0.265 /s) the optimizer with goals (b) and (c) alone and
  the same rate floor reproduces the stored optimum; adding (a) and the
  HER mask yields a Pareto set with the viable U interval attached to
  every point, and `env_best` names its trust.
- On pw549461 (screening, BEP barriers, `ea_app` unresolved, no scan)
  `env_next` is a cart list (coverage scan; modes; a kinetics re-solve
  at the resolved point) and no `env_best` is claimed beyond the
  thermodynamic read.
- Derivative checks: the layer-2 stencils agree with a central finite
  difference on a fixture sweep within the stencil's own error; the U
  slope equals the limiting rung's `n_e`.
- A point outside the stored sweep box is never reported as a value;
  it is `env_next`.
- The quest step runs once per landed kinetics job per slab, stamps the
  three keys, and the frontier ranks the slab at `env_best`.

## Target + blast radius

`src/precis/quest/` (new `environment_optimizer.py`, `compute.py` step,
`frontier.py` measure declarations), `src/precis_pathway/` (reading the
sweep rows and `che_affine`, the cart hand-off), catpath `mkm.py` sweep
output (selectivity stencils), `pathway-explorer-controls.md` slices 6,
8, 9 for the display. Prod effect: new measures on qu164903 candidates;
GPU spend only via the cart under the quest's caps.

## Open questions / decisions log

1. Budget per slab in solver calls (proposed 300) and the Pareto cap
   (proposed 24 points).
2. Whether `env_best` enters the rubric as the ranking point for every
   quest or only for quests that declare `meta.environment_space`
   (named axes and boxes, the `param_space` pattern of
   catalyst-discovery-quest).
3. Held `env_next` items: queue and run at the lift (my default; not
   ruled).
