---
status: implemented (slices 1-3 shipped — catpath 04012e1/ff15ea0, precis 8c25c61e; slice 4 parked, slice 5 deferred)
title: Pathway potential lever — CHE electrochemistry, closed-form optimal U, selectivity in the quest cost function
model: opus
---

# Pathway potential lever — CHE electrochemistry, closed-form optimal U, selectivity in the quest cost function

> Cross-repo design (catpath computes, precis persists/ranks, explorer
> displays). Motivated by NO→NH₃ on quest 164903: NOx reduction is really an
> *electro*catalysis problem, and the quest's cost function should reward
> selectivity and operating potential, not just one thermal barrier. Sibling:
> `reaction-pathway-explorer.md` (the display surface).

## The physics: computational hydrogen electrode (CHE), zero extra compute

catpath's curated networks already model hydrogenation as **supply edges**
(`+H*` staged from a reservoir, bookkept ΔE = 0). Under the CHE formalism
(Nørskov), an applied potential U (vs RHE) enters *only* through that
reservoir's chemical potential: each supplied H represents H⁺ + e⁻, so

    G_node(U) = G_node(0) + n_H(node) · eU

where `n_H(node)` = number of reservoir H atoms the node has absorbed —
derivable from the node's own composition (count H in the '+'-split fragment
labels; the root has none), so it is node-intrinsic, not path-dependent.
Chemical steps (N–O scission, on-surface recombination) keep their barriers;
supply steps pick up the ±eU shift. **No new relax/NEB runs** — the whole
lever is post-processing over energies we already have. (Refinement, later:
a symmetry factor β·eU on electrochemical barriers; v1 is thermodynamic-only,
which is the standard CHE approximation.)

## No binary search, no LLM — the optimum is closed-form

Every downstream objective is an extremum of functions **affine in U**:

- **Limiting potential**: U_L = −(1/e) · max over electrochemical steps of
  ΔG_step(0) — one pass over edges, exact.
- **Optimal-span potential**: span(U) is a max of affine functions
  (piecewise-linear convex); its minimizer is a line intersection — O(E²)
  worst case, trivially exact at this graph size.

So the "voltage adjustment" the quest wants is a deterministic catpath
function call, cheaper than even a binary search and with zero agent/LLM
involvement. Agents *choose candidates*; arithmetic finds each candidate's
best U.

## Cost function (quest rubric) integration

Today the quest ranks on one scalar (`rate_Ea`). Extend `meta.results` with
`U_L`, `span_at_UL` (or `span_at_Uopt`), and a **selectivity penalty**, and
let the rubric objective be a declared composite, e.g.
`minimize α·span(U*) + β·|U_L| + γ·P_side`. Selectivity P_side from fork
analysis: at each state with ≥2 competing *reaction* edges, branch fraction
∝ exp(−ΔEa/kT) (equal prefactors; supply edges excluded — bookkeeping, not
kinetics). Guards: a fork is scored only when every competing barrier is
computed and none of its states is flagged wrong-site / low-confidence /
infeasible — else "insufficient data", never a fabricated ratio.

**Honesty gap to close before selectivity means much:** the ammonia network
has no **HER** channel (H* + H* → H₂ — *the* dominant parasitic reaction in
NOx electroreduction) and no N–N coupling (N₂O / N₂). Adding HER is one
template edge + one NEB; N–N coupling needs two-adsorbate steps (bigger
lift, later slice).

## pH input (rides the same machinery)

With U referenced to **RHE**, every proton-coupled electron transfer (PCET)
step — which is *all* current H-supply steps under CHE — is pH-independent;
that's the point of the RHE choice. pH therefore enters as:

- **Scale conversion for display/literature comparison**:
  `U_SHE = U_RHE − (ln10·kT/e)·pH` (−0.0592 V/pH at 298.15 K). Explorer
  shows both scales given a pH.
- **A real per-step shift only for *decoupled* proton or hydroxide steps**
  (∓(ln10·kT)·pH per H⁺/OH⁻ transferred without an electron). The ammonia
  template has none today, so this branch is dormant until such a step is
  added — but the arithmetic stays affine, so `U_L`/span optimizers are
  unchanged.
- **Out of scope, stated honestly**: solvation, surface charging, and
  coverage-vs-pH effects are outside the vacuum-slab ML envelope; the pH
  lever must never imply they're modeled.

A quest rubric may declare an operating (U, pH) window; the closed-form
optimum is computed within it.

## Explorer surface

The graph payload carries `n_H` per node → the energy diagram re-renders at
any U **client-side, instantly** (levels shift by n_H·eU; supply-edge slopes
change; chemical humps ride their shifted endpoints). One slider, zero
server calls. Fork-probability annotations (small % labels at forks, with
the guards above, T shown explicitly) live on the same surface.

## Slices

1. **catpath — BUILT** (`04012e1`/`ff15ea0`): CHE post-processing — `n_H`
   per node, G(U), `U_L`, exact span-vs-U minimizer, RHE/SHE + pH
   machinery; DAG semantics (full-DAG `U_L`, worst-of-required-leaves
   span) for non-convex candidates. Sibling engine work landed alongside
   (not scoped to this proposal but feeding the same `meta.results`
   contract): relax-only screening mode (`07a2df4`, no NEB — barriers
   absent end-to-end, CHE thermodynamic spans only), a coadsorbed ammonia
   template (`b4e33ba`, drops the fragment-parking approximation — the
   `verify` tier), and a mid-NEB detachment guard + one-shot auto-retry
   (`23bc87e`).
2. **precis — BUILT** (this session): `n_H` rides `meta.graph` nodes
   verbatim (`_pathway_graph_payload`); `U_L`/`U_opt`/`span_at_UL`/
   `span_at_Uopt`/`P_side` harvest onto candidate measures
   (`_AUTOCATPATH_ELECTRO_KEYS`), gated by the same barrier-trust check as
   `barrier`; the quest rubric's declared composite objective
   (`meta.rubric_composite`, human-set) reads them. Rides the new
   screening/verify tier ladder (`docs/architecture/state-map.md`, quest
   section) rather than a flat single-fidelity run.
3. **explorer — BUILT** (`8c25c61e`): U slider + pH field (RHE/SHE dual
   display), `U_L`/`U_opt` snap buttons, guarded fork-probability display.
4. **network — PARKED, needs a design pass.** HER (H* + H* → H₂, the
   dominant NOx-electroreduction parasitic channel) isn't a bare template
   edge as originally scoped: scoring its **selectivity** against the main
   pathway needs Heyrovsky-step competition, which requires the β-corrected
   electrochemical-barrier slice (item 5) — a thermodynamic-only U_L/span
   read on HER alone is not yet honest; a Tafel-mechanism read additionally
   needs a genuine H+H co-adsorbed state (a *second* adsorbate template
   variant, not what slice 1's coadsorbed template models). Scope HER once
   the barrier-correction design is settled, not before.
5. **later — deferred**: β-corrected electrochemical barriers; N–N coupling
   states; decoupled-proton steps (activates the dormant pH shift above).

## Decisions (Reto, 2026-08-07)

- **Reference convention: RHE.** pH-independent for PCET steps under CHE;
  SHE shown as a derived display scale.
- **Temperature: 298.15 K default** (25 °C — standard ambient, the T
  electrochemical reference states are tabulated at; NOT 300 K, which is
  just the computational round-number convention — kT differs by 0.6%,
  immaterial, but the default should match the standard) **+ a small T
  input** next to the U slider.
- **Composite-score weights (α, β, γ): per-quest, human-set rubric
  fields.** The agent may not tune its own objective.
