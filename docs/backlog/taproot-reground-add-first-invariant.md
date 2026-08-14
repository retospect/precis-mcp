---
status: draft
title: Reground applier must enforce the add-first invariant in code, not in a prompt
model: opus
---

# Reground applier — enforce add-first invariant in code

A taproot "reground" pass was run manually over 123 claim hubs of draft 173020 (evidence pass: 108 evidence-edge adds, 36 prunes). The pass exposed a structural defect that must not be carried into the future `reground_claim` job_type.

## The defect

The applier stage receives a scout's proposal (a set of evidence-edge adds and prunes) and executes it. The invariant "add before you prune, never strand a hub at zero evidence edges" existed **only as an instruction in the applier's prompt**. Under partial failure — when a permission classifier blocked the `link` add calls but left the prune calls to succeed — the invariant became a coin flip decided by each applier instance's judgment. Of four hubs that hit this condition, two (fi191317, fi191320) correctly self-skipped their prunes, and two (fi191307, fi192836) pruned anyway and were left stranded at zero evidence edges. Both were detected and restored by hand from the scouts' proposals, but only because the pass was being verified against the database rather than against the workflow's own success reports.

## Why a prompt cannot carry this

The guard is a safety property under adversarial/partial failure, which is exactly the regime where a model instruction is least reliable. The workflow script already had a deterministic would-strand guard, but it ran on the *scout's proposal* (adds.length === 0 && prunes.length > 0 && prunes.length >= n_current_edges) — it could not see that adds later failed at write time. The gap is between proposal-time and write-time state.

## What the job_type must do instead, in deterministic code

1. Issue all adds first, and **read back** that each intended add is actually committed (query `links` — do not trust the write call's return).
2. Issue prunes only for the subset whose replacement adds are confirmed committed. If an add failed, its paired prune is withheld and the hub is flagged for review, not silently skipped.
3. After the whole transaction, **re-check `count(live evidence edges) > 0`** for the hub and refuse/roll back if it would land at zero.
4. Surface partial-failure counts in the job result, so a caller reading only the summary still sees that something was withheld.

## Related lesson — intent-vs-committed diff

The technique that actually found the damage was not error-string chasing but an **intent-vs-committed diff**: rebuild each hub's intended end state from its scout result (KEEP edges plus adds), diff against the handles actually committed, and apply the delta adds-first. This surfaced residue no error string mentioned (10 missing adds and 8 stale edges in one wave alone). The future job_type should expose this diff as a first-class verification/repair mode.

## Manual pass spec and runbook location

The manual pass's spec and interim runbook live on `reto@melchior` at `.claude/worktrees/ethereal-discovering-barto/docs/backlog/taproot-reground.md` and are NOT in this repo — flag that as a thing to bring in-tree when the job_type is built.

test: reground applier applies all add ops and confirms each is committed in `links` before issuing any prune; a permission failure on an add withholds its paired prune and flags the hub for review.
