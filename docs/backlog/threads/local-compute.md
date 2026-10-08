# local compute

## Resume

- **Pillar:** local-compute
- **Next:** Build the summarise-only local chain described in [ranked work](#do-next).
- **Blocked by:** NAS role shares await Reto’s local-compute-12 answer; model pick/vLLM slice 1 await knowledge-mesh’s km-8 task set. Check whether either blocks this slice.
- **Unblocks:** Local serving for [knowledge-mesh](knowledge-mesh.md#resume) and graph maintenance.
- **Acceptance:** Use [the latest handoff](#thread-context) and [llm-capacity-plan](../llm-capacity-plan.md); verify the summarise-only chain’s placement and local-vs-cloud measurements.
- **Worktree:** `local-compute`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: [fleet roster](../../../.claude/fleet/threads.tsv); dated allocation decisions below are historical.

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
**Allocation decision (historical):** yes (2026-10-01, Reto: "Bring it back we will").
**Resume (2026-10-03, after round 3 deployed 929107f3):**
- Waiting on:
  - Reto, for local-compute-12. The NAS role shares are proposed in `cluster-fileserver-move.md`.
  - knowledge-mesh, for its km-8 task set (model pick and the vllm Slice 1 branch).
- Next build: a summarise-only local chain.

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
   **Cluster re-layout (Reto 2026-10-03 ~21:50Z, via the orchestrator).** He asked
   whether the big Mac should become a 192 GB LLM box, whether another Spark
   should serve a medium model, and what a greenfield allocation of all
   machines would look like. Deliverables:
   - an options note in `reviews/local-compute.md`: the greenfield layout plus
     incremental layouts A/B/C, all from measured demand and supply;
   - a `local-compute-17` decide item.

   Estimate: 1 build cycle for the note. Carrying out the chosen layout is
   estimated per layout inside the note.
   **Ruled 2026-10-03 22:13Z (local-compute-17).** Reto: "A is good but we just
   write it down for now, as a plan for later."
   - The plan is recorded in `llm-capacity-plan.md` § "Agreed layout plan, NOT
     scheduled".
   - Nothing in it is built until he reopens it. castor stays the big-model
     box, so (b) below is off.
   - (c)'s 27B drop is part of A's hardening, so it is off too.
   - Reto 22:18Z confirmed: "continue with the work … But don't reorganize the
     network based on the greenfield gedankenexperiment". There are no
     node-role changes from A/B/D/G. Two pieces continue:
     - (a) the summarise-only chain: lc-14 "promote if the compare passes",
       and it passed;
     - the Docker disk alert: gate hygiene after the 10-03 ENOSPC.

   Superseded by that ruling: the orchestrator note of 22:02Z, which had said
   pilot step 1 proceeds under the 10-02 rulings without waiting for the item:
   - (a) **Summarise-only local-first chain.** `llm.op.<source>` gains a
     `chain` key and `llm_summarize` is registered. Landed with this
     commit.
     - Rung 0 must be `transport: "local"`. A pinned OpenAI-style rung 0
       would send the bare served id to the hosted endpoint.
     - A saturated slot overflows to rung 1, after the breaker and
       admission gates re-check rung 1.
     - After the deploy, write `llm.op.llm_summarize` =
       `{"chain": [{"transport":"local","model":"glm-4.7-flash","placement":"local"},
       {"transport":"openai_compat","model":"z-ai/glm-4.7-flash","placement":"cloud"}]}`.
       Then read `/placement` before and after.
   - (b) **Medium on castor** once km-8 picks the model.
   - (c) **Big-Mac hardening**, a deploy-role branch to the orchestrator:
     - drop the idle 27B from llama-swap;
     - add a Docker VM disk alert at >85%. ship-gate-ci owns the refusal
       below 10 GB and the cache cap (`docker-vm-disk-fills-silently.md`);
       the alert is ours. It is a textfile metric from inside the colima VM
       plus a Prometheus rule, because node_exporter on the host cannot see
       the VM's `/var/lib/docker`.
2b. **External HPC, three uses (Reto 2026-10-03 21:50Z: "bump that up in
   priority").** Moved up from Horizon. It is still blocked on access: the
   tunnel key is unregistered and the overlay has no coordinates. Reto ruled
   option 1 in local-compute-3 but has not done it yet. Re-asked in
   `local-compute-16`. The public key is in that item, and the private key
   stays out of the DB store. Reto plans the setup on 2026-10-04; blocked
   until then. **Batch path, 2026-10-05/07:** the Codex-owned branch
   `work/meluxina/bootstrap` (paused handoff td471801, never landed) holds
   `precis.remote` (SSH + Slurm stage/submit/recover/collect with a durable
   intent journal) and authenticated with a vault-held key, so (b) and (c)
   are not blocked on the tunnel key; they are blocked on the unbuilt
   Apptainer image (SIF-1..5 in td471801) and on storage: the project space
   read 97% full by bytes and inodes (about 26k inodes free), so a loose
   Python environment cannot be installed there. Minimal footprint is one
   image per workload, per-job writes on node-local scratch only, stage
   deleted after collect. The uses:
   - (a) LLM operations: the slullama rung, `backlog/slullama-hpc-placement.md`
     leg 2.
   - (b) DFT relax for chemistry. This is batch Slurm (stage, sbatch, poll,
     fetch), not tunnel-and-serve. It is the "second backend" trigger in
     `backlog/precis-dispatch.md`, which is now real: extract the runner seam
     from the pollux relax, shaped by both cases.
     - Precondition, chemistry's: one GPAW relax must complete in prod on
       pollux first. None has, and the MPI image is still on branch
       `chemistry-dft-mpi-env`.
   - (c) ML-potential relaxes (MACE-MP; GFN-xTB rides the same jobs) for
     hexfold-toolkit's nanobud geometry vetting: many short jobs, so they
     batch per sbatch. Same runner as (b).
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
   `scripts/llm_eval/gold_set/local/summarize_v1.json`; rebuilt 2026-10-03
   with the same 40 prompts plus the 220-token cap per task). It lives in
   reto's prod clone on melchior (`~/precis-mcp-prod`, reto-owned; `ssh
   melchior` lands as `deploy`, which cannot write there). Run builds and
   compares as reto, with the env from the web service plist.
   **First compare, 2026-10-03, deployed 63301c5c.**

   | arm | mean | without number rule | number-rule-only zeros | hit cap |
   |---|---|---|---|---|
   | local `glm-4.7-flash` (melchior) | 0.750 | 0.800 | 2/40 | 0/40 |
   | cloud `z-ai/glm-4.7-flash` | 0.825 | 0.850 | 1/40 | 0/40 |

   - The CLI could not run it then. It runs since round 3: it never bound its store,
     and the operator chain's pinned rung model overrides the candidate id.
     The local arm was driven through `run_eval(dispatch_fn=...)`, using the
     LOCAL transport at the served endpoint: **hook-driven, bypasses the
     breaker and slot accounting** (concurrency 1 on an idle slot). This is
     the measurement of record until the CLI fix lands (orchestrator §9a).
     Past damage (§9c, read-only 2026-10-03): **zero `record=True` eval
     runs affected.** Prod has no `record_eval` entry at all. The one
     `measured-eval` review (2026-08-10, deepseek-v4-flash) is a manual
     note with no axis or ordinal. CLI runs never saw the DB chains, so
     they kept the candidate id; only a settings-bound process would have
     hit the override. The number-rule zeros differ by
     1, so the delta is usable.
   - The gap is non-prose tagging, not summary quality. 8 of the local
     arm's 10 zeros and 6 of the cloud arm's 7 are chunks the incumbent
     labelled non-prose (references, credits, metadata) where the model
     wrote a prose brief instead of a tag. On the 30 prose chunks the arms
     have 2 and 1 zeros.
   - Re-sampling the local zeros flipped 2 of 10, so the 3-task gap is
     inside run-to-run noise at n=40.
   - The proposed promote rule (≥ cloud − 0.05) says no.
   - **Reto 2026-10-03 (local-compute-14, option 1): measure properly.**
     200 tasks, prose and non-prose reported separately, each arm run
     twice. Promote if prose is level and non-prose is within the measured
     noise. The set (150 prose, 50 non-prose, seed 2) is
     `summarize_v2_200.json` next to v1.
   - **200-task compare, 2026-10-03: PASSES the rule.** Run through the
     fixed `pinned_dispatch` path (this tree's code, slot-accounted, breaker
     on), so it also exercises the eval fix: no void arm, one cloud 504.

     | arm | run | prose (n=150) | non-prose (n=50) | wall |
     |---|---|---|---|---|
     | local `glm-4.7-flash` | 1 / 2 | 0.913 / 0.913 | 0.120 / 0.160 | 266 / 281 s |
     | cloud `z-ai/glm-4.7-flash` | 1 / 2 | 0.827 / 0.906 | 0.040 / 0.140 | 830 / 911 s |

     - Prose: local is level with cloud's better run and above its worse one.
       Non-prose: local is at or above cloud in both runs.
     - Noise, measured: tasks whose pass/fail flipped between the two runs
       were 12 of 150 prose and 2 of 50 non-prose on the local arm, and 26
       and 9 on the cloud arm. The local quantisation is the steadier arm.
     - Both arms score about 0.1 on non-prose. Neither model writes the tag
       the incumbent chose, so this is the label question (§9), not a
       local regression.
     - Local runs 3× faster at concurrency 1.
   - **Next: the summarise-only local rung.** `llm.chain.small` carries
     all 354k small calls a week, not just summarise. An `llm.op.<source>`
     override only picks a tier, and a chain rung pins its model. So
     promoting summarise alone needs a summarise chain: local
     `glm-4.7-flash` at the melchior slot (capacity 4, matching
     `--parallel 4`), with cloud as overflow. Build it under
     `local-summarizer.md` "In scope", with the slot-capacity note there.
**Order for putting the big local model to work** (Reto 2026-10-03,
endorsed; sequence and ETA in review item local-compute-13):
(1) finish Slice 0: load above 64 streams, quality on knowledge-mesh's task
set, then pick model and server (4b); (2) Slice 1: a permanent managed
service on castor, castor exclusive to it, with its own `resource_slots`
capacity row (`vllm-per-node-serving.md`); (3) local rungs in the tier
ladder with cloud overflow (5; after round 2 carries the placement guard);
(4) the feedback controller holding about 32 in flight, graph maintenance
as filler (6). **Reto confirmed 2026-10-03 (local-compute-15, option 1):**
the server is sized for 64 streams; the controller holds about 32 while
interactive tiers use it and fills to 64 with graph maintenance. First
local target: the medium tier plus graph maintenance; big stays on the
cloud until its own check. The model pick still waits on knowledge-mesh's
task set.

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
      (local-compute-7/8/9) that no Mac or Spark serves files and that the
      FS fix is a priority now ("it is when it is", no round window).
      - Plan approved with phase 1 revised: no retained DB backups on the
        DB node.
      - Next: Reto creates the NAS export in the TOS web UI (TOS has no
        CLI; local-compute-12), then agents verify, test-mount and copy.
      - The deploy-role branch is `worktree-agent-af6719b05abd3f35e`. It
        changes no behaviour at the overlay defaults; a restore_test /
        drill_pull fix for verified-only mode is in progress. It goes to
        the orchestrator.
      - The caspar nfsd restart (local-compute-6) stays held.
   b. **backlog/vllm-per-node-serving.md Slice 0** — gpt-oss 120B vs
      Nemotron 3 Super NVFP4, each on vLLM and SGLang, at 1/8/32 streams on
      one box (spec in its decisions log). **Runs on castor now, from local
      NVMe, independent of the NFS move** (Reto 2026-10-02,
      local-compute-9: "why is local llm gated on filesystem stuff?"). It
      never needed the share. The gate came from sequencing: the bench was
      on spark, and spark's host-prep list put "unhang the NFS mount"
      first for the eval-run-spine. castor is the box the model will
      serve on anyway. castor's docker has the GPU (local-compute-11),
      both weight sets and the vLLM image are on local disk.
      - **vLLM × Nemotron done 2026-10-03:** 14 / 56 / 108 / 142 out tok/s
        at 1 / 8 / 32 / 64 streams; 3.5 tok/s per stream at 32; KV 50% at
        64 (table in the item).
      - **vLLM × gpt-oss done 2026-10-03:** 32 / 144 / 290 / 400 out
        tok/s at 1 / 8 / 32 / 64; 9.6 tok/s per stream at 32; KV 20% at
        64. About 2.8× Nemotron throughout. Needs the harmony vocab staged
        offline (item has the recipe). Above 64: ceiling about 450 out
        tok/s (443 at 192, 450 at 256); the knee is at 64. SGLang arm
        running (Docker Hub reachable from castor 2026-10-03). Next: the quality
        check on knowledge-mesh's task set (km-8 taxonomy first; Reto
        2026-10-03, co-owned with knowledge-mesh). Spec:
        `backlog/local-mesh-upkeep.md` slice 0; its categorise task IS
        this check, run once for both threads. Open: the bench server is
        loopback-only, so the eval needs a serving window where
        `llm_eval` can reach castor (a LAN bind plus a `resource_slots`
        row for the window, or run the harness on castor). Ping
        knowledge-mesh when castor serves gpt-oss. When the server is
        picked, a review item answers Reto's "how many channels"
        (ceiling, setpoint, KV headroom).
      - **SGLang done 2026-10-03; server picked: vLLM.** gpt-oss on SGLang
        gives 198 tok/s at 32 streams against vLLM's 290, and fills its KV
        pool at 64. Nemotron on SGLang stalls at 11 running requests.
        Channels ruled (local-compute-15): server limit 64, controller
        about 32. The model pick waits on the quality check.
      - **Slice 1 role built, not deployed:** branch
        `worktree-agent-aad7be76ec69cd553` (role `vllm`, playbook
        `49-vllm.yml`, model as a variable) is with the orchestrator, held
        until the model pick. It is untested on a host.
      It picks the model 3 may run on, and unblocks 5 and 6. Also **backlog/local-serving-eval.md** (moved here 2026-10-01).
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
   Slice 0 (4b) and Slice 1 serving. This and the queue are the path that
   puts castor to work (Reto 2026-10-03).
7. **backlog/embedder-capacity-ownership.md** — decided (Reto 20:47Z
   2026-10-02, td461158, §Decided in the item): this thread holds aggregate
   embedder capacity; owner is the embedder service; provisional capacity
   12.7–13.6 texts/s mixed, query p50 ~2 s / p95 ~5 s (a
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
8. *(moved up to Do-next 2b on 2026-10-03.)* On Reto's "done" for the tunnel
   key, verify the tunnel and the login-node daemon-reaping risk. Git
   history keeps the host:port from 04004d7aa (no rewrite, Reto).
   **Tuned open-weight models for precis operations** (Reto 2026-10-03, relayed
   by nanobuds-paper). The plan, in order:
   1. Eval each operation from `llm_call_log` with `precis llm eval`.
   2. Distill the extraction-type operations.
   3. Keep the judgment operations (claim fidelity, polarity) on Claude.
   4. Geometry and mesh operations: the model picks, a checker scores, and
      the model learns by RL against that score.

   Waiting on nanobuds-paper's Perplexity report on candidate models. It
   feeds the cluster re-layout (Do-next 2).
9. **backlog/curation-gate.md** — owned by serving-programme; consumed
   here (seam below).
10. **backlog/dreaming.md**
11. **backlog/llm-cost-accounting.md**
12. **gr458727** — the capacity-idle monitor ("is the fleet working or
    idle"): roadmap pillar 3 says it is missing; measurement only, owned with
    `backlog/graph-maintenance-queue.md` (Ruled 2026-10-01, `INDEX.md`).
13. **backlog/local-coder-batch-harness.md** — idea (Reto 2026-10-04); last
    because it needs the standing castor server and has no quality number.

## Parked

- (none)

## No action needed

- (none yet)

## Seam

`serving-programme` keeps the MCP serve ceiling (py-spy, the multi-process
balancer), the load-test harness and the eval-run-spine; this thread owns
local model serving and what it does. The eval-run-spine's items 4 and 9
wait on the spark prerequisites ranked in Do-next 4.
