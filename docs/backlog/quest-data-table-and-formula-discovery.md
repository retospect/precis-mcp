---
status: draft
title: Quest data table + staged formula discovery (linear baseline first)
---

# Quest data table + staged formula discovery (linear baseline first)

## Motivation / why

Quests accumulate (candidate → measured objectives) evidence, but there
is no materialized, normalized data table: `quest/frontier.py` assembles
`(params, measures)` per candidate at read time — a deliberate seam (the
"§7.8 optimizer advisor") — yet a prod audit (2026-08-17) found the seam
unfed: **0** structures carry `meta.params` (no writer exists), all 23
harvested barriers sit `barrier_trusted=false` behind the `wrong_site`
gate (qu164903), and `material_values` holds 0 rows against a
19-property registry. Any regression or formula-discovery layer built
today would spin on an empty table. LLM-Feynman (arXiv:2503.06512,
paper id=210166) motivates the end state — its eval domains (perovskite
synthesizability, ionic conductivity, 2D-material classification) are
precis quest domains — but its many-LLM-call loop is the *last* stage
here, not the first: most of the findable signal at quest-scale row
counts (tens) is reachable by sparse linear regression over engineered
features, and the canonical catalysis relation (BEP: barrier ≈
α·ΔE_rxn + β) is literally linear.

## In scope (staged — each stage independently shippable, in order)

1. **Substrate.**
   - Stamp `meta.params` on quest candidates at mint time (the quest
     mint path; `frontier._candidate_from_structure` already reads it).
   - Resolve the `wrong_site` barrier-trust blocker (all current
     barriers untrusted; independently valuable — it is also why the
     Pareto frontier is empty).
   - Data-table export: `precis quest table <quest>` emitting tidy rows
     (candidate handle, `params.*`, `measures.*`, trust flags) from the
     frontier's existing `Candidate` assembly. This is the single seam
     every later stage consumes, and the human check that data exists.
2. **Deterministic baseline (no LLM lane, no budget risk).** Engineered
   features (log/ratio/product transforms of params + cheap composition
   descriptors) → sparse linear regression (OLS/LASSO with a simplicity
   prior). First target: rediscover the BEP relation once trusted
   barriers exist. Output: a `finding` ref carrying coefficients, fit
   metrics, and n.
3. **LLM symbolic regression** (LLM-Feynman rubric; nearest open
   reference architecture LLM-SR, ICLR 2025,
   https://github.com/deep-symbolic-mathematics/LLM-SR — LLM-Feynman
   itself released no code as of 2026-08-16): feature proposal →
   LLM-guided propose/fit/refine with self-evaluation → MCTS-style
   simplicity/accuracy trade-off, outputting `finding` refs
   (nanopub-mintable). **Gated twice:** build only when the stage-2
   baseline demonstrably plateaus AND the table holds ≥~100s of trusted
   rows (tens ⇒ overfit toy formulas at real LLM cost). Needs a bounded
   per-round budget and its own lane decision from day one
   (taproot_backfill lane-monopoly precedent); all calls through the
   router.

## Out of scope

- Extracting tables from held paper PDFs into `material_values`
  (own item if wanted — would feed the cross-material domains).

## Test

- Stage 1: a freshly minted candidate carries `meta.params`; the export
  emits ≥1 trusted row after the `wrong_site` fix.
- Stage 2: rediscovers a planted linear relation from a fixture table
  within tolerance, and reports honest fit metrics on shuffled labels.
- Stage 3: rediscovers a known nonlinear formula from a fixture dataset
  within its per-round budget.

Owner: `precis.quest` (params stamp, export) + `precis.workers`
job_types (stage 3). Supersedes `formula-discovery-job.md` (folded in).
