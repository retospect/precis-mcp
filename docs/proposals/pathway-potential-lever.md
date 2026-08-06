---
status: draft
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

## Explorer surface

The graph payload carries `n_H` per node → the energy diagram re-renders at
any U **client-side, instantly** (levels shift by n_H·eU; supply-edge slopes
change; chemical humps ride their shifted endpoints). One slider, zero
server calls. Fork-probability annotations (small % labels at forks, with
the guards above, T shown explicitly) live on the same surface.

## Slices

1. **catpath**: CHE post-processing — `n_H` per node, G(U), `U_L`,
   span-vs-U minimizer; config `electrochemistry: {U_vs_RHE | 'optimal'}`;
   tests on the ammonia template.
2. **precis**: persist `n_H` into `meta.graph` nodes and `U_L`/`span_at_U*`
   into `meta.results`; quest rubric reads the composite objective.
3. **explorer**: U slider + fork-probability display (guarded).
4. **network**: HER edge in the ammonia template (small).
5. **later**: β-corrected electrochemical barriers; N–N coupling states;
   pH via RHE→SHE conversion if ever needed.

## Open questions (need Reto)

- Reference convention: U vs **RHE** assumed (pH-independent with CHE) — ok?
- Operating temperature for fork ratios: fixed 300 K, or a small T input
  next to the U slider?
- Composite-score weights (α, β, γ): fixed defaults, or per-quest rubric
  fields the agent may tune?
