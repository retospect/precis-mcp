# local compute

**Status:** ends when the local box continuously improves the graph
(summarise, insert, mesh, link, categorise) on local rungs, with frontier
review as the gate, and the local-vs-cloud share is a number. Today 0% of
LLM traffic is local: 406,527 calls in the 7 days to 2026-10-01 all went
cloud (~$298); the castor/pollux/spark LLM servers have been stopped, GPUs
idle, DeepSeek-V4-Flash and Qwen3 weights staged on castor, last local call
2026-09-10. Order: make the share measurable, bring the summariser back,
then the three Sparks back on duty (big model, embeddings, science lanes; Reto 2026-10-02), then the rungs that consume them.
**Last reviewed:** 2026-10-02
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
   big-model slot (4), embedder load (7), and the Spark role split (4). First rows already read
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
   castor/pollux serve nothing. Gold set BUILT 2026-10-02 on melchior's
   prod checkout (40 tasks, 10 non-prose; gitignored
   `scripts/llm_eval/gold_set/local/summarize_v1.json`). **Next, after the
   round-2 deploy carries the placement guard (fcf5b1c1, 85c79e02)** (the
   local endpoint is loopback-only on melchior): `precis llm eval glm-4.7-flash
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
4. **Three Sparks back on duty; Slice 0 picks model + server.** Reto
   2026-10-02 (review item local-compute-4, ruled 21:03Z) reversed the
   2026-08-29 paper-box rule: castor, pollux and spark (all GB10) split
   into one exclusive big model, one local embeddings and one GPU science
   lanes. Ruled 22:03Z (local-compute-5): **castor big model, pollux
   science, spark embeddings**. In order:
   a. **backlog/serving-programme-followups.md items 1-2** — the
      `/mnt/cluster` NFS hang (server caspar) hits only the Linux Sparks;
      the Macs read it fine. caspar's nfsd is healthy, so the approved
      restart (local-compute-6) stopped at its read-only gate. The leading
      suspect is asymmetric routing: caspar's replies to the Sparks take the
      Tailscale tunnel (MTU 1280) while the Sparks send to caspar directly on
      the LAN (design note §6). Any fix goes to Reto as its own review item.
      Then spark host prep. These are duty prerequisites, not bench prep.
      **Then backlog/cluster-fileserver-move.md** — Reto ruled
      (local-compute-7) that no Mac or Spark serves files: the share moves to
      finnmaccool, after a read-only probe and an approved plan.
   b. **backlog/vllm-per-node-serving.md Slice 0** — gpt-oss 120B vs
      Nemotron 3 Super NVFP4, each on vLLM and SGLang, at 1/8/32 streams on
      one box (spec in its decisions log). **Runs on castor now, from local
      NVMe, independent of the NFS move** (Reto 2026-10-02,
      local-compute-9: "why is local llm gated on filesystem stuff?"). It
      never needed the share. The gate came from sequencing: the bench was
      on spark, and spark's host-prep list put "unhang the NFS mount"
      first for the eval-run-spine. castor is the box the model will
      serve on anyway. It picks the model 3 may run on, and unblocks 5 and
      6. Also **backlog/local-serving-eval.md** (moved here 2026-10-01).
   c. **backlog/spark-provisioning.md** — nvidia docker runtime in a role,
      plus scheduled OS/driver updates for all three Sparks inside the round
      deploy window (Reto's ruling 4).
   d. **backlog/spark-fleet-embedder.md** — Reto ruled the split
      2026-10-02 (local-compute-5, option 1): castor big model, pollux
      science, spark embeddings. spark's role is a fleet LAN embedder that
      replaces the per-node loopback copies, in a new `embedder` group, NOT
      `inference` (which would bring the worker/watch/dft plays with it).
      All machines, the file server included, are in
      `llm-capacity-plan.md`. Ad-hoc heavy compute goes to spark at half
      its cores, never melchior (ruled, local-compute-7).
5. **backlog/local-rungs-small-medium.md** — blocked-by Slice 0 (4b); wires
   the model Slice 0 picks into the tier ladder.
6. **backlog/llm-dispatch-feedback-controller.md** — ~32 running sequences
   per serving box, held by a feedback loop on the server's
   running/waiting/KV metrics; card `max_parallel`, slot capacity and the
   server limit set from one number; overflow to the cloud rung;
   `graph-maintenance-queue.md` (Horizon 1) as the deferrable feed. After
   Slice 0 (4b) and Slice 1 serving.
7. **backlog/embedder-capacity-ownership.md** — decided (Reto 20:47Z
   2026-10-02, td461158, §Decided in the item): this thread holds aggregate
   embedder capacity; owner is the embedder service; provisional capacity
   12.7–13.6 texts/s mixed, query p50 ~2 s / p95 ~5 s (gr459844 rig, a
   floor); host-level admission and a shared vector cache declined. Left:
   the N-client load test once local LLM rungs share the box with the
   embedder, so after 5; then delete the item. The capacity plan (2) carries
   the embedder rows meanwhile.

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
7. **backlog/torch-extras-conflict.md** — venv hygiene on the Sparks
   (spark-provisioning moved up into Do-next 4c).
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
  `chase_trigger` carries a dead batch-size knob. Unparks when Do-next 7
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
