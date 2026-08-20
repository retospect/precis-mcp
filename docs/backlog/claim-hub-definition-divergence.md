---
status: draft
title: "two definitions of 'claim hub' in live code — the dedup index retrieves 280 non-hubs as merge candidates"
---

# What is a claim hub? Production gives two answers

`mint_hub` (`taproot/hub.py`) writes **both** `TAPROOT:claim` and
`STATUS:canonical`, in one transaction, and is the only writer of
`STATUS:canonical` anywhere. So a real hub always carries both. But readers
disagree, measured against prod 2026-08-20:

| reader | predicate | population |
|---|---|---|
| `hub_refine` due-set (`workers/hub_refine.py`, the `EXISTS` pair) | both tags | 1,244 |
| `chase_trigger` embedding refresh | both tags | 1,244 |
| **`taproot/canon.py::block()`** — the dedup candidate index | `TAPROOT:claim` alone | **1,524** |
| **`nanopub/overview.py::hub_rows()`** | `TAPROOT:claim` alone | **1,524** |

The 280-row delta is not hubs at all. They carry `STATUS:established` (212),
`STATUS:dead_chain` (65) or `STATUS:multi_candidate` (3) — the *chase* finding
lifecycle, written by `workers/chase.py::_set_status`, which deletes every
existing `STATUS:` tag before inserting (the `replace_prefix=True` semantics in
`store/_tags_ops.py`). They are `axis_pass`-classified findings that carry the
`TAPROOT:claim` label without ever having been minted. Sample titles are
semiconductor-scaling material (Moore's Law, Dennard 1974, Frank 2001, IRDS
2023) — recognisably a research tree, not the claim corpus.

## Why this is a correctness bug and not a bookkeeping one

`block()` is the candidate retrieval feeding `place()`. Its result set includes
280 rows that are not hubs, so the merge path can be offered — and an automated
judge can accept — a merge between a canonical claim hub and an ordinary tree
finding. That is the **over-merge** direction, the one `eval_canon`'s live gate
exists to keep at zero. The blast radius was small only because `block()`
retrieved over `card_combined` and saw almost nothing until 2026-08-19; the
index repair that fixed coverage also turned this latent bug live.

`hub_rows()` has the same predicate, so the nanopub overview lists those 280 as
hubs with a publish posture they can never have.

Note the near-miss: `canon.py`'s own comment above the query flags a *different*
divergence (`rt.expires_at`, inert today) and reasons carefully about denominator
drift — while the `STATUS:canonical` divergence sitting in the same predicate is
unremarked.

## Fix

Add the `STATUS:canonical` `EXISTS` clause to `block()` and to `hub_rows()`,
matching `hub_refine`. Two cautions:

- `block()` is the hot dedup path and its own comment warns against adding
  filters casually — an `EXISTS` on an indexed tag pair is cheap, but check the
  plan rather than assuming.
- `workers/health_digest.py::_check_claim_hub_dedup_index` watches `block()`'s
  coverage invariant against its own denominator. Changing the retrieval
  population without changing the check makes the health digest wrong in the
  reassuring direction. Change both together.

Better still, hoist the predicate into one named helper so a third reader cannot
invent a third definition.

## What this invalidates

- **`claim-hub-dedup-sweep.md`'s 24 pairs** were measured over the 1,524
  population, so the cohort is contaminated and must be re-measured under the
  strict definition before the sweep runs.
- Any figure in the backlog quoted against "~1,524 live hubs" is quoting the
  permissive denominator. The strict count is 1,244. State which one is meant.
