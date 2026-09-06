---
status: draft
prio: high
title: Todo claims are not claims — no CAS, no doable exclusion, no expiry
---

# Todo claims are not claims — no CAS, no doable exclusion, no expiry

Todo-infra review (2026-09-06), goal frame: self-continuing long-running
tasks across a fleet.

## Motivation / why

`claimed-by:<handle>` is documented in `precis-todo-tree-help` as an
"atomic claim marker", but three properties a fleet needs are absent:

1. **No mutual exclusion.** It's an open tag — two agents can both add
   `claimed-by:<self>` and both tag rows coexist. The tag *add* is atomic;
   the check-then-claim isn't, and nothing rejects a second claimer.
2. **Claimed leaves stay doable.** `claimed-by:` is not in
   `_DOABLE_EXCLUSION_TAGS` (`src/precis/handlers/_todo_views.py`), so
   `view='doable'` offers already-claimed leaves to every other fleet
   member. The tree view renders the `◀ claimed-by` icon, but the pull
   surface ignores it → duplicate work.
3. **No expiry.** A claimer that dies holding a claim leaves a stale tag
   forever. `kind='job'` has real lease semantics (ADR 0030 kept job
   un-folded partly *for* them); the todo claim has none.

## In scope

- Claim = CAS: adding `claimed-by:` when a live one exists → BadInput
  naming the holder (owner override allowed).
- Exclude claimed leaves from `view='doable'` and the dispatch candidate
  query (one registry entry — that's what the registry is for), with a
  staleness window: a claim older than N hours stops excluding, so
  dead-agent work re-surfaces instead of orphaning.
- Release on terminal STATUS (done / won't-do) so stale claims don't
  accumulate on finished leaves.
- Naming: consider `lease:` to match the job vocabulary agents already
  know; keep `claimed-by:` as a read alias if renamed.
- Skill update (`precis-todo-tree-help`): claim → work → release loop,
  and what expiry means for a long-running claimer (re-assert the claim).

## Out of scope

- Folding todo claims into `kind='job'` leases (ADR 0030 rejected).

test: two concurrent claimers — second add rejected; claimed leaf absent
from doable until the staleness window passes.
