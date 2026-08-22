---
status: idea
title: quest-tick slicing residuals — requeue-from-checkpoint, stale-stage agentlog finalize
---

# Quest-tick slicing residuals

The tick stage machine shipped (`run_quest_tick` split into resumable
llm→apply→search→compute→ladder-per-rung→finish stages, checkpoint parked
on the tick's agentlog `meta.checkpoint`, explicit `timeout_s` on every
tick LLM call). Three residuals, ranked:

1. **Requeue-from-checkpoint is still future work** — the coordinator
   claim SQL requires `STATUS:queued`, so a slice killed mid-run sits at
   `STATUS:running` until the sweeper terminally fails the job; the parked
   checkpoint is never resumed (a fresh coordinator job starts clean).
   Building requeue makes the stage table's *not-idempotent* replay cases
   live: plain WORM ledger notes double-write on `apply` replay, and the
   cascade counter + cost deed double-apply on `finish` replay — those
   need dedup keys (or replay guards) BEFORE the requeue path exists.
2. **Unknown-stage fallback orphans the old agentlog row** —
   `_TickRun.step()` resets `self.st = {"stage": "llm"}` wholesale when a
   checkpoint names a stage this build doesn't know (deploy-bounce with an
   older checkpoint mid-flight). That drops `agentlog_id`, so the old row
   is never finalized: it shows as perpetually running in `/agentlogs`
   until retention GC. Fix: `finalize_log(status="stale-stage")` on the
   old row before resetting. Uncovered branch — add the test with the fix.
3. **First-slice-only gating (accepted hole, documented)** — backpressure
   and starvation gates run only on a tick's first slice; a gate tripping
   between slices doesn't stop mid-tick dispatch. Bounded by the 1
   proposal/tick WIP cap and deliberate (re-checking mid-tick would
   misfire on the tick's own not-yet-dispatched work). Revisit only if
   compute-queue starvation shows up in practice.
