# local compute

**Status:** ends when the local box continuously improves the graph
(summarise, insert, mesh, link, categorise) on local rungs, with frontier
review as the gate, and the local-vs-cloud share is a number. Today none of
that is measured; Do-next 1 is the cheapest thing that makes the rest
measurable, so it goes first.
**Last reviewed:** 2026-09-30
**Worktree:** `local-compute`
**Active:** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/local-cloud-share-report.md** — cheapest; the data already
   sits in the ledger. Makes everything below measurable instead of
   asserted.
2. **backlog/embedder-capacity-ownership.md** — the current bottleneck,
   under pre-search, dedup and minting all at once; two Reto decisions
   inside.
3. **backlog/graph-maintenance-queue.md** — the workload itself;
   supersedes `backlog/cluster-scheduling.md`'s tear-down clause.
4. **backlog/local-rungs-small-medium.md** — blocked-by
   `backlog/local-serving-eval.md` (a peer session is running that
   evaluation now — "the model that just fits one spark").
5. **backlog/content-sensitivity-placement.md** — precondition for
   anything proprietary going local at all.

## Horizon

1. **backlog/router-cost-coverage.md**
2. **backlog/llm-judge-reliability.md** — needed before a local judge is
   trusted.
3. **backlog/curation-gate.md** — owned by serving-programme; consumed
   here (seam below).
4. **backlog/dreaming.md**
5. **backlog/reading-prep-loop.md**
6. **backlog/quest-loop-activation.md**
7. **backlog/llm-cost-accounting.md**
8. **backlog/plan-tick-context-cut.md** — overdue 2026-08-24.

## Parked

- **embed drain** — **gr456034**, **gr454865**: the `embed_batch` backlog is
  not draining, by a different mechanism than the closed gr347576, and
  `chase_trigger` carries a dead batch-size knob. Unparks when Do-next 2
  (embedder capacity ownership) picks this up — it is the same bottleneck seen
  from the queue end. The **ingest-fidelity** half of what was parked here as
  one cluster left on 2026-10-01: Reto ruled it its own thread,
  `threads/ingest-and-fetch.md`, so gr228652, gr228699, gr453859, gr453860,
  gr453862, gr453913 and gr456181 are ranked there, not here. gr458393
  (`_greedy_split` pagination) was mis-clustered here at the 09-30 review and
  is ranked in se-3d-viewer.

## No action needed

- (none yet)

## Seam

`serving-programme` owns the MCP ceiling, vLLM Slice 0 go/no-go and the
eval-run-spine; this thread owns what the local capacity *does* once
served.
