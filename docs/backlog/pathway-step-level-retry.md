---
status: ready
title: automated step-level re-dispatch for trust-blocked pathway quantities
prio: medium
---

# Step-level retry: make per-step trust self-healing

Context (qu164903 variability discussion, Reto 2026-08-27): with per-step
trust records deployed (b333771a consumer), a 20-step route with one invalid
step measurement yields 19 trusted steps + one named `blocked_by` — but
re-running the blocked step is today a human/tick decision. Detected
failures should retry automatically; that converts multiplicative
pathway-validity attrition (0.95^20 ≈ 0.36 usable routes at a 5% per-step
failure rate) into ~5% additive compute cost.

## Design sketch

- Worker- or aggregate-side policy: when a quantity's `trust_summary` says
  `blocked_by: <step>#s<seed>#<check>`, re-queue ONLY that step's
  measurement with a fresh seed (new idem key — seed is already part of the
  citable id scheme `step#s<seed>#check`).
- Bounded: max N retries per step (default 2), then the blocker stands and
  surfaces to the tick/human as today. Never retry `endpoint-mismatch`-class
  defects blindly if catpath item 3 ships — those need the basin diagnosis,
  not a re-roll.
- Span-weighted budget (same discussion): retries justified for
  span-constituent / quantity-relevant steps; spectator steps far below the
  span don't block quantities and shouldn't consume retries.
- Interacts with catpath handoff item 3 (multi-start + min-aggregation): if
  the engine grows k-seed native support, this policy collapses into "top up
  n_valid to k for quantity-relevant steps".

## Definition of done

Blocked quantity on a fresh seed job → one automatic re-dispatch of the
blocked step (visible as a new job with distinct seed/idem key) → quantity
flips to available when the retry lands clean; retry cap respected; test
covers the parked-forever regression (retry must NOT fire on cost-cap or
spend-limit failures — only on trust-check verdicts).

## Status 2026-09-16 — the ladder half landed, the per-step half is still open

The deadlock this item's cost argument sits on top of turned out to be
upstream of step-level retry: `promote_tiers` only promoted neb→verify for
Pareto-frontier members, and with `P_side` a required rubric axis that no
neb-tier run can produce (best_first prunes competitor barriers; the quest
config pinned `seeds: [0]` so every Estimate was `insufficient_samples`),
the frontier was empty, nobody was promoted, and no verify run ever landed
(qu164903: 0 converged points, 23/23 schema-2 pathways with selectivity
unavailable). Shipped: neb→verify promotes any trusted-barrier neb-tier
candidate (frontier first), verify forces ≥ 3 seeds, and rubric axes can be
flagged `optional: true` (`frontier._optional_objectives_for`). What remains
here is exactly the design above: re-dispatching ONE blocked step with a
fresh seed instead of a whole verify run.
