# local compute

**Status:** ends when the local box continuously improves the graph
(summarise, insert, mesh, link, categorise) on local rungs, with frontier
review as the gate, and the local-vs-cloud share is a number. Today 0% of
LLM traffic is local: 406,527 calls in the 7 days to 2026-10-01 all went
cloud (~$298); the castor/pollux/spark LLM servers have been stopped, GPUs
idle, DeepSeek-V4-Flash and Qwen3 weights staged on castor, last local call
2026-09-10. Order: make the share measurable, bring the summariser back,
then the big model on one spark, then the rungs that consume it.
**Last reviewed:** 2026-10-01
**Worktree:** `local-compute`
**Active:** yes (2026-10-01, Reto: "Bring it back we will").

## Do next

1. **Local share is measurable**: `get(kind='llm', id='/placement')`
   (routed vs landed, per tier and day). `placement_routed` (migration 0179)
   fills only from the round-1 deploy on. Read the number before and after
   each item below.
2. **backlog/local-summarizer.md** — the first workload to go local again
   (~1.8M-chunk backlog, bulk and content-light); gated on
   `backlog/model-qualification.md`, measured by 1. The gate's instrument is
   built (WIP on this branch, not landed): `llm_eval` scorer `summary`
   replays the production summariser messages and grades with the worker's
   own parse/reject plus an invented-number check;
   `scripts/llm_eval/build_summarize_gold.py` samples already-summarised
   paper chunks read-only into gitignored `gold_set/local/`. Resume: re-run
   `UV_WITH="--with numba" scripts/test tests/test_llm_eval.py` (last
   assertion edited after the last run), qland, then build the set and
   compare a local candidate vs `glm-4.7-flash`. Waiting on review item
   local-compute-2 (cluster read to find the candidate; gold-set location).
3. **Single-spark big model** — **backlog/vllm-per-node-serving.md Slice 0**
   (Nemotron NVFP4 vs gpt-oss control; go/no-go for the oversubscription
   design), after its two spark prerequisites,
   **backlog/serving-programme-followups.md item 1** (spark `/mnt/cluster`
   NFS hang) and **item 2** (spark host prep), moved here from
   serving-programme 2026-10-01; plus **backlog/local-serving-eval.md**
   (moved with them). No session is running that evaluation (verified
   2026-10-01). Unblocks 5, and picks the model 2 may run on.
4. **backlog/embedder-capacity-ownership.md** — the current bottleneck,
   under pre-search, dedup and minting all at once; two Reto decisions
   inside.
5. **backlog/local-rungs-small-medium.md** — blocked-by Slice 0 (3); wires
   the model Slice 0 picks into the tier ladder.

## Horizon

1. **backlog/graph-maintenance-queue.md** — the workload itself; supersedes
   `backlog/cluster-scheduling.md`'s tear-down clause. Waits on local
   capacity existing (1-3).
2. **backlog/content-sensitivity-placement.md** — precondition for anything
   proprietary going local at all.
3. **backlog/precis-dispatch.md** — the compute-runner layer; first consumer
   is the chemistry thread (DFT relax); seams extract when a second workload
   lands.
4. **backlog/router-cost-coverage.md**
5. **backlog/llm-judge-reliability.md** — needed before a local judge is
   trusted.
6. **backlog/vllm-per-node-serving.md beyond Slice 0**,
   **backlog/llamacpp-fleet-ops.md** — wait on 3's plateau; per-node model
   choice against a measured ceiling.
7. **backlog/spark-provisioning.md**, **backlog/torch-extras-conflict.md**
   — spark host and venv hygiene the single-spark model (Do next 3)
   stands on.
8. **backlog/slullama-hpc-placement.md** — the HPC chain rung; leg 2 is
   blocked on external cluster access, so it stays last.
9. **backlog/curation-gate.md** — owned by serving-programme; consumed
   here (seam below).
10. **backlog/dreaming.md**
11. **backlog/llm-cost-accounting.md**
12. **gr458727** — the capacity-idle monitor ("is the fleet working or
    idle"): roadmap pillar 3 says it is missing; measurement only, owned with
    `backlog/graph-maintenance-queue.md` (Ruled 2026-10-01, `INDEX.md`).

## Parked

- **embed drain** — **gr456034**, **gr454865**: the `embed_batch` backlog is
  not draining, by a different mechanism than the closed gr347576, and
  `chase_trigger` carries a dead batch-size knob. Unparks when Do-next 4
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

`serving-programme` keeps the MCP serve ceiling (py-spy, the multi-process
balancer), the load-test harness and the eval-run-spine; this thread owns
local model serving and what it does. The eval-run-spine's items 4 and 9
wait on the spark prerequisites ranked in Do-next 3.
