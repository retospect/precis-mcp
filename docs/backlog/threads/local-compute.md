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

- **ingest pipeline — no thread owns it yet.** The OA-acquisition/ingest-
  fidelity cluster — **gr453859**, **gr453860**, **gr453862**,
  **gr456181**, **gr453913**, **gr228652**, **gr228699** (backlog "no OA
  copy" terminal state missing, fetched-but-bodiless papers uncounted,
  arxiv_html losing its own identifier, venue dropped on S2 enrich, and
  the Greek/micro-character font-encoding corruption pair) — and the
  embed-drain cluster — **gr456034**, **gr454865** (embed_batch backlog
  not draining by a different mechanism than the closed gr347576;
  chase_trigger's dead batch-size knob). gr458393 (`_greedy_split`
  pagination) was mis-clustered here at the review; it is a read-surface
  bug and the se-3d-viewer owner has a fix scoped — ranked there. Both clusters are ingest/pipeline
  throughput and fidelity work with no other thread claiming the files
  they touch; parked here rather than left fully orphaned, since this
  thread is closest to "what runs on local/background compute".

## No action needed

- (none yet)

## Seam

`serving-programme` owns the MCP ceiling, vLLM Slice 0 go/no-go and the
eval-run-spine; this thread owns what the local capacity *does* once
served.
