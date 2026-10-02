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
   each item below. **Baseline, read in prod 2026-10-02 after the round-1
   deploy (567f207f):** 7 days, 0/366,218 calls landed local (0.0%), $338
   billed — small 354,373 calls / $28, medium 11,301 / $96, big 537 /
   $210, frontier 7 / $3.
2. **backlog/llm-capacity-plan.md** — what model runs where, on what
   memory and slots, against what demand, with the competing GPU/container
   work and the planned external-HPC row (Reto 2026-10-02,
   `reto-llm-capacity-1`). Ranked above 3-5 because each of them picks a
   placement this table is the input for: the summariser slice (3), the
   big-model slot (4), embedder ownership (5). First rows already read
   (decisions log in the item).
3. **backlog/local-summarizer.md** — the first workload to go local again
   (~1.8M-chunk backlog, bulk and content-light); gated on
   `backlog/model-qualification.md`, measured by 1. The gate's instrument
   landed (d2abcbc7): `llm_eval` scorer `summary` replays the production
   summariser messages and grades with the worker's own parse/reject plus an
   invented-number check; `scripts/llm_eval/build_summarize_gold.py` samples
   already-summarised paper chunks read-only into gitignored
   `gold_set/local/` (Reto 2026-10-02: the set never enters the public
   repo). `llm eval --compare` prints mean/n beside the ordinal.
   **First candidate exists already:** cluster read 2026-10-02 13:17Z —
   melchior llama-swap (port 11445) serves `glm-4.7-flash` (Q5_K_M),
   `qwen3.6-27b-q8_0` and `qwen3-next-80b-a3b-q4_k_m`, all idle; prod
   `resource_slots` holds `melchior|llm:glm-4.7-flash` cap 4. The `small`
   chain buys `z-ai/glm-4.7-flash` from OpenRouter, so the first
   qualification is the local quantisation against its own cloud original.
   castor/pollux serve nothing. **Next, after the round deploy carries
   d2abcbc7 to melchior** (the local endpoint is loopback-only there): on
   melchior, build the set (`--n 40`), then `precis llm eval glm-4.7-flash
   --compare z-ai/glm-4.7-flash --tier small --gold <set> --placement-a
   local --placement-b cloud`. The placement flags are strict: a local-arm
   reply that ran on the cloud raises `PlacementMismatch`, and a chain with
   no reachable local rung errors every task (mean 0), so a false tie is
   impossible. Still open: whether the router builds a local rung for an
   explicit `glm-4.7-flash` when `llm.chain.small` is cloud-only; if the
   first run errors, that is the bug to trace. (`--endpoint-a` is an
   OpenRouter provider pin, not a local URL; the local base URL comes from
   the reserved slot.) The compare prints, per arm, the mean with and
   without the number rule and the number-rule-only zero count; if the arms
   differ by more than 2 of 40 such zeros the delta is unusable (review
   verdict 2026-10-02). Proposed promote rule (Reto to confirm with the
   result): candidate mean ≥ incumbent mean − 0.05 and no transport errors.
   No absolute mean goes on a model card until the false-zero share is
   known.
4. **Single-spark big model** — **backlog/vllm-per-node-serving.md Slice 0**
   (Nemotron NVFP4 vs gpt-oss control; go/no-go for the oversubscription
   design), after its two spark prerequisites,
   **backlog/serving-programme-followups.md item 1** (spark `/mnt/cluster`
   NFS hang) and **item 2** (spark host prep), moved here from
   serving-programme 2026-10-01; plus **backlog/local-serving-eval.md**
   (moved with them). No session is running that evaluation (verified
   2026-10-01). Unblocks 6, and picks the model 3 may run on.
5. **backlog/embedder-capacity-ownership.md** — the current bottleneck,
   under pre-search, dedup and minting all at once; two Reto decisions
   inside. Reto 2026-10-02 (td461158): **session-mcp-shared-server files
   the review item** (owner, fleet capacity number, host-level admission
   gr450123 (a), shared vector cache), so this thread does not file a
   second one. This thread supplies the embedder rows and the fleet
   capacity number from 2, then builds what Reto rules.
6. **backlog/local-rungs-small-medium.md** — blocked-by Slice 0 (4); wires
   the model Slice 0 picks into the tier ladder.

## Horizon

1. **backlog/graph-maintenance-queue.md** — the workload itself; supersedes
   `backlog/cluster-scheduling.md`'s tear-down clause. Waits on local
   capacity existing (1-4).
2. **backlog/content-sensitivity-placement.md** — precondition for anything
   proprietary going local at all.
3. **backlog/precis-dispatch.md** — the compute-runner layer; first consumer
   is the chemistry thread (DFT relax); seams extract when a second workload
   lands.
4. **backlog/router-cost-coverage.md**
5. **backlog/llm-judge-reliability.md** — needed before a local judge is
   trusted.
6. **backlog/vllm-per-node-serving.md beyond Slice 0**,
   **backlog/llamacpp-fleet-ops.md** — wait on 4's plateau; per-node model
   choice against a measured ceiling.
7. **backlog/spark-provisioning.md**, **backlog/torch-extras-conflict.md**
   — spark host and venv hygiene the single-spark model (Do next 4)
   stands on.
8. **backlog/slullama-hpc-placement.md** — the HPC chain rung; leg 2 is
   blocked on external cluster access, so it stays last. Reto registers the
   melchior tunnel key with Meluxina himself (ruled 2026-10-02, review item
   local-compute-3) and writes the coordinates into the gitignored overlay
   only; on his "done", verify the tunnel and the login-node daemon-reaping
   risk. Git history keeps the host:port from 04004d7aa (no rewrite, Reto).
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
  `chase_trigger` carries a dead batch-size knob. Unparks when Do-next 5
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
wait on the spark prerequisites ranked in Do-next 4.
