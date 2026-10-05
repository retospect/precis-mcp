---
status: ready
title: automated step-level re-dispatch for trust-blocked pathway quantities
prio: medium
pillar: 3d-design
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

## R14 slice 1 — convergence-blocker eligibility (2026-10-05)

Premise checked against fetched main `ab90f225a`: this item is still open.
`quest.compute` retries infrastructure failures at the seed/network level;
`precis_pathway.runner.run_seed_partial` calls `run_one_seed` for the whole
network. The pinned catpath `0.22.0@973491d4` has no single-step argument on
that entry point. Neither the aggregate nor the seed job consumes quantity
blockers to re-dispatch a step.

**Acceptance for this slice:** a pure, unwired selector returns only fatal
`neb_convergence` / `relax_convergence` records explicitly cited by an
unavailable barrier or selectivity quantity. Match structured records by
opaque id: barrier blockers are ids, while selectivity blockers carry ids
under `reasons`. Preserve complete records, including model, state, seed
and attempt; the id alone is not unique across models. Require a named
step and a nonnegative integer seed. Do not infer a step from a state id.

No selection for available quantities, spectators, marginal/pass records,
missing/unknown trust schema, unresolved ids, budget/job error strings,
or other checks. Any fatal-fail trust record with an endpoint
identity/agreement check or explicit `endpoint-mismatch` evidence withholds
the entire selection:
those records can be state-only, so assigning them to a safe step requires
a later basin-aware policy. This deliberately conservative first slice
does not establish that any returned record is safe to dispatch.

Synthetic tests must cover both blocker shapes, state-at-step ids with
attempt suffixes, model identity, and these exclusions; no calculation,
store write or scheduler call. This internal prerequisite adds no runtime
caller or public response field and needs no version/dependency bump.

**Still open:** all other check classes, endpoint/basin diagnosis, durable
per-step retry counts (default cap 2), fresh-seed/content-key allocation,
single-step execution, replacement/aggregation semantics, and automatic
dispatch → clean retry → available quantity. Do not substitute a full
network rerun. No catpath upgrade, release-SHA selection, production run,
or change to items 23/25 holds is authorized by this slice.

**Decision before execution wiring:** should the catpath owner provide a
supported single-step retry/partial-replacement API on an explicitly
approved release before precis wires dispatch? The current whole-network
API cannot meet this item's one-step acceptance criterion unchanged.
