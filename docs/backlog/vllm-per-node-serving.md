---
status: draft
title: vLLM per-node serving on castor — one model per box, replication as the throughput lever
pillar: local-compute
prio: normal
---

# vLLM per-node serving

Design session 2026-09-29 (Reto + agent, big-model-manage worktree). Start on
**castor alone**; add pollux for throughput only if one box proves
insufficient.

## Motivation / why

Everything the fleet serves locally today is llama.cpp
(`llamacpp-fleet-ops.md`, `spark-provisioning.md`), and the pair's
`llama-server` has been stopped and disabled since 2026-08-23 with big-tier
traffic falling to cloud Sonnet. The new workload — many concurrent agent
sessions doing multi-hop tool use — wants continuous batching, prefix
caching and OpenAI-compatible tool calling, which is vLLM/SGLang territory,
not llama.cpp's.

The router side already exists and does not need rebuilding: a static `llm`
card advertises the endpoint, `utils/llm/local_serving.py::acquire` reserves
the slot and repoints dispatch, and an operator `chain_override` puts a
`placement:"local"` rung in front of the cloud rung. This is an endpoint
swap into existing machinery. It also unblocks `good-search-coordinator.md`,
which asks for an `openai_compatible`/`vllm` executor that does not exist in
`src/precis` today.

**Replication, not sharding, is the throughput lever.** Sharding a model
across two boxes does not multiply throughput — it lets you run a model that
does not fit, at a latency cost. Published measurements put two-Spark
collectives over the ConnectX link in the high-teens GB/s (allgather ~18.8,
allreduce ~21.5) against 273 GB/s of local LPDDR5x, roughly 13× slower, so
tensor-parallel across the link makes every token slower. If the model fits
on one box, run two independent replicas behind the router: clean 2×
aggregate, no interconnect penalty, and no one-node-dies-kills-the-pool
failure mode. The fused multi-node pool is a different design and is already
owned by `cluster-scheduling.md` §F, which argues it is batch-only.

## In scope

**Slice 0 — does the model concurrency-scale at all (do this first).**
A published DGX Spark concurrency benchmark found Nemotron Nano 9B v2 NVFP4,
a hybrid Mamba-2 model, plateaued at ~156 tok/s aggregate from **c=8**, while
non-Mamba models on the same box kept climbing to c=256 (gpt-oss 120B MXFP4:
33.5 tok/s single-stream, 862 tok/s aggregate, still climbing; Nemotron Super
49B v1.5 NVFP4: 5.8 single, 695 aggregate at c=256, KV-cache-limited,
regressing at c=320). Nemotron 3 Super is also hybrid Mamba-Transformer. If
that ceiling carries, the whole oversubscription design dies at single-digit
concurrency on a box that can do 256 — so measure it before building
anything else. Serve the candidate and the control, ramp concurrency, find
where aggregate tok/s stops climbing. Host prep and order relative to the
GIL profile: `serving-programme-followups.md`.

**Slice 1 — serving.** A vLLM deploy role alongside `deploy/roles/llamacpp/`,
single-node, `--enable-prefix-caching`, `--enable-auto-tool-choice` with the
model's matching `--tool-call-parser`. Prefix caching is not only a latency
win: with the shared system+tools+skills prefix stored once rather than
per-session, it is concurrency headroom, and on the 49B NVFP4 measurement KV
cache was the binding constraint.

**Slice 2 — router integration.** Static `llm` card with `source="static"`,
slot capacity and card `max_parallel` kept in lockstep, `chain_override` rung
in front of cloud.

**Slice 3 — metrics, and the three surfaces.** Scrape vLLM `/metrics` and
land the counters where each consumer can reach them:

* *Real time* — the admission controller scrapes the endpoint directly and
  never reads Postgres (`mcp-concurrency-load-test.md` slice 2).
* *Per call* — `llm_call_log.cache_read_tokens` / `cache_creation_tokens`
  already exist (migration 0122) and are already populated from the OpenAI
  `usage` block by `result_from_openai`; vLLM reports
  `prompt_tokens_details.cached_tokens` with prefix caching on. That gives
  per-call cache accounting already joined to the run, with no new table.
  Caveat to write into the column comments: those names carry Anthropic's
  billing semantics, where read and creation are priced differently. vLLM
  prefix caching has no "creation" analogue and no cost, so a hit rate
  computed across placements mixes two mechanisms and means nothing.
* *Per beat* — `host_heartbeat_log` (migration 0113) is the precedent: narrow
  append-only row per 60 s beat, written by
  `workers/heartbeat.py::_collect_and_upsert` alongside the snapshot upsert,
  pruned by an env var, rolled up hourly by `precis stats --utilization`. The
  heartbeat pass already runs on every host; on the serving host it
  additionally records preemptions, cache usage and running/waiting. The
  counters are monotonic, so rollups take deltas.

**Key on preemptions, not prefix-cache hit rate.** `vllm:num_preemptions_total`
counts sequences evicted and restarted — that *is* the
stalled-session-lost-its-blocks event. Hit rate is the tempting number and is
misleading here: a byte-identical shared prefix always hits and dominates the
ratio, so per-session tail thrash hides inside a healthy-looking 95%. Use hit
rate for narrative, gate on preemptions.

**Slice 4 — surfacing (tail; worth nothing until something serves).** A
`health_digest` (Layer 2) check on preemption rate over the last hour past
budget — the right lane, since it is slow rot rather than a page, and the
numbers are in Postgres by then so the module's zero-`llm`-import constraint
holds. The doctor report (Layer 3) narrates it daily and, when the fix is a
config change such as lowering the oversubscription cap, its
`## Needs a human` section already converts the bullet into a
`waiting-for:reto` todo. Nursery/`kind='alert'` only for the collapse case,
where preemption rate is high enough that aggregate throughput drops.

## Explicitly NOT in scope

* Multi-node tensor or pipeline parallel, and the fused pool —
  `cluster-scheduling.md` §F.
* Retiring llama.cpp. The two coexist; llama.cpp keeps whatever it still
  serves.
* Bringing `spark` back on cluster duty is its own change (review item
  local-compute-5, the role split), not part of this item. The 2026-08-29
  paper-box rule was reversed on 2026-10-02 (decisions log).
* Choosing the model on paper. Slice 0 measures; `scripts/llm_eval/`
  (15 models × 17 tasks through the router) scores.

## Acceptance criteria

* Slice 0 produces an aggregate-tok/s-versus-concurrency curve for the
  candidate and the control, and the concurrency at which each plateaus.
* A model serves on castor through vLLM with tool calling working end to end
  from a real agent session.
* `llm.chain.big` (or the chosen tier) resolves to the local rung on the
  serving host and falls to cloud elsewhere, with zero `llm_call_log` errors
  over a day.
* Preemptions and cache usage are queryable from Postgres, and the Layer-2
  check fires on a deliberately induced thrash.
* Adding the second replica is a config change, not a redesign.

## Target + blast radius

New `deploy/roles/` role and playbook · `src/precis/llm_catalog.py` (card
seed) · `utils/llm/local_serving.py` (endpoint) · `workers/heartbeat.py` ·
`workers/health_digest.py` · `route_log.py` (cache-token mapping for the
local placement).

## Known footguns (do not re-derive)

* **A redeploy clears a hand-set `served_by` but leaves the `resource_slots`
  row.** The router's endpoint comes from the card's `served_by`, not the
  slot, so the result is a reservable slot with no endpoint and silent
  fallback to cloud. Use `source="static"` and expect to re-set it.
* **The `llm:*` slot row is the fleet-wide big-LLM semaphore**, not stale
  bookkeeping, and its host label is a historical accounting key rather than
  the serving machine. Three separate sessions have now mislabeled it as
  deletable. Card `max_parallel` and slot capacity must move together.
* **`advertise_local_llm` prunes auto-discovered entries per heartbeat** —
  a direct vLLM endpoint is not llama-swap, so it is never re-discovered and
  must be operator-registered.

## Open questions / decisions log

**Decided 2026-10-02 (Reto, review item local-compute-4), supersedes the
spark-as-bench-only framing below where they conflict.**
- spark is back on cluster duty. The three Sparks (castor, pollux, spark;
  all GB10 Blackwell, so NVFP4 runs on any of them) split into one
  exclusive big model, one local embeddings and one GPU science-lanes box.
  Which host takes which role: review item local-compute-5.
- Slice 0 is first and decides **model and server**. Candidates: gpt-oss
  120B (MXFP4) and Nemotron 3 Super 120B-A12B (NVFP4). Servers: vLLM and
  SGLang. Measure each pair at **1, 8 and 32 concurrent streams** on one
  box; report single-stream tok/s, aggregate tok/s, p95 time-to-first-token,
  and peak KV use at each level. The 32-stream target and the feedback
  controller that holds it: `llm-dispatch-feedback-controller.md`.

**Decided 2026-09-29 (Reto):** castor first, pollux later for throughput if
needed; replication over sharding; one model fully resident per box.

**Superseded 2026-10-02: Slice 0 runs on castor, from local NVMe** (Reto,
review item local-compute-9). castor is the big-model box under the role
split (local-compute-5), and it serves nothing today. spark now carries
embeddings and ad-hoc compute, and its share is hung. Slice 0 does not need
`/mnt/cluster`: weights and images go to castor's local disk. The frozen
eval world stays wherever `eval-run-spine.md` places it.

**Slice 0 result, vLLM arm, Nemotron 3 Super NVFP4 on castor (2026-10-03).**
vLLM v0.20.1, `--trust-remote-code`, no other flag changes; server start
641 s to `/health`; no `precis-aizynth` container ran during any level.
Output tok/s is the bench's `output_throughput`; per-stream is 1000 / mean
TPOT.

| C | out tok/s | per-stream tok/s | TTFT p50 / p95 (ms) | peak KV % |
|---|---|---|---|---|
| 1 | 14.4 | 15.4 | 896 / 7809 | 0.8 |
| 8 | 56.4 | 7.6 | 2584 / 19746 | 6.2 |
| 32 | 107.9 | 3.5 | 2758 / 24657 | 24.9 |
| 64 | 141.5 | 2.3 | 3973 / 47462 | 49.8 |

- Aggregate still climbs at 64 but flattens: ×1.9 from 8 to 32, ×1.3 from
  32 to 64. KV is half used at 64, so memory is not the limit.
- 3.5 tok/s per stream at the 32-stream target is slow for interactive use;
  acceptable only for bulk work where aggregate counts.
- C=1 TTFT p95 7.8 s is likely first-request warm-up (p50 0.9 s).
- Raw results: castor `/home/deploy/slice0/results/`.

**Slice 0 result, vLLM arm, gpt-oss 120B MXFP4 on castor (2026-10-03).**
Same run script and settings (max-model-len 16384, max-num-seqs 64,
gpu-memory-utilization 0.8); server start 505 s; 0 failed requests; no
`precis-aizynth` container during any level. vLLM's gpt-oss path loads the
harmony tokenizer vocab from the internet at startup and the container
cannot reach it: stage `o200k_base.tiktoken` and `cl100k_base.tiktoken` on
the host, mount them read-only, set `TIKTOKEN_ENCODINGS_BASE` (castor:
`/home/deploy/slice0/tiktoken/`). Any serving role for gpt-oss needs the
same.

| C | out tok/s | per-stream tok/s | TTFT p50 / p95 (ms) | peak KV % |
|---|---|---|---|---|
| 1 | 32.3 | 33.6 | 524 / 1017 | 0.3 |
| 8 | 143.5 | 18.6 | 406 / 3221 | 2.5 |
| 32 | 290.1 | 9.6 | 1520 / 12475 | 10.2 |
| 64 | 399.8 | 6.7 | 917 / 23560 | 20.5 |

- gpt-oss gives 2.7–2.9× Nemotron's aggregate at every level and 2.7× its
  per-stream rate at 32 (9.6 vs 3.5 tok/s), with a fifth of the KV use.
- **Above 64 (same settings, `--max-num-seqs 256`, 0 failed):**

  | C | out tok/s | per-stream tok/s | TTFT p50 / p95 (ms) | peak KV % |
  |---|---|---|---|---|
  | 96 | 393.2 | 4.4 | 2223 / 40676 | 29.0 |
  | 128 | 410.1 | 3.5 | 2362 / 54561 | 38.5 |
  | 192 | 443.3 | 2.5 | 3176 / 86045 | 57.0 |
  | 256 | 450.3 | 1.9 | 3477 / 119898 | 75.1 |

  **Measured ceiling on vLLM: about 450 out tok/s.** The curve flattens
  at 64 (400, 89% of ceiling): from 64 to 256, 4× the streams adds 13%
  throughput while per-stream drops from 6.7 to 1.9 tok/s and TTFT p95
  grows fivefold. KV is not the limit (75% at 256); decode compute is.
  The 32-stream setpoint gets 65% of the ceiling at 9.6 tok/s per stream;
  64 gets 89% at 6.7 (ruling below).
**Slice 0 result, SGLang arm on castor (2026-10-03).** `lmsysorg/sglang:latest-cu130`
(v0.5.21, pulled on castor directly after 7 TLS-timeout retries; Docker
Hub is flaky from the LAN, not blocked). Same prompts and client as vLLM;
`--mem-fraction-static 0.8 --max-running-requests 256`, defaults otherwise;
0 failed requests, no aizynth contamination.

| model | C | out tok/s | per-stream | TTFT p50 / p95 (ms) | peak KV % |
|---|---|---|---|---|---|
| gpt-oss 120B | 1 | 30.9 | 31.9 | 484 / 831 | 2 |
| gpt-oss 120B | 8 | 107.5 | 13.9 | 1632 / 2609 | 13 |
| gpt-oss 120B | 32 | 198.5 | 6.5 | 3687 / 9435 | 52 |
| gpt-oss 120B | 64 | 236.6 | 4.3 | 10839 / 19600 | 100 |
| gpt-oss 120B | 128 | 240.0 | 4.1 | 138633 / 150711 | 100 |
| Nemotron 3 Super | 1 | 15.8 | 16.5 | 1033 / 2052 | 6 |
| Nemotron 3 Super | 8 | 59.1 | 7.8 | 3220 / 6591 | 50 |
| Nemotron 3 Super | 32 | 62.2 | 6.2 | 182802 / 188235 | 69 |
| Nemotron 3 Super | 64 | 64.1 | 6.4 | 435123 / 444673 | 69 |

- gpt-oss on SGLang reaches 68% of vLLM's aggregate at 32 streams (198 vs
  290) while its KV pool is only half used, so the gap is decode speed, not
  memory. Its KV pool fills at 64 (vLLM: 20%), capping it near 240 tok/s.
- Nemotron on SGLang stops admitting at 11 running requests (likely the
  default Mamba state pool), so its 32/64 rows are queueing, not load.
- Untuned defaults on both servers; tuning SGLang's pool sizes could lift
  its ceiling but not its 32-stream decode rate, which is already below
  vLLM's with memory to spare.
- Not diagnosed: the metrics show more running requests than C at some
  levels (12 at C=8, 117 at C=64).

**Server picked: vLLM** (2026-10-03). It leads on both models at every
level that matters, and gpt-oss on vLLM leads everything (ceiling about
450 out tok/s). The model pick waits on the quality check below.

- **Channels ruled** (Reto 2026-10-03, local-compute-15): the server is
  sized for 64 streams (`--max-num-seqs 64`). The feedback controller holds
  about 32 while interactive tiers use it and fills to 64 with graph
  maintenance. First target: the medium tier plus graph maintenance; big
  stays on the cloud until its own check.
- **Quality check = the knowledge-mesh task set** (Reto 2026-10-03): the
  big local model is judged by what it does for the mesh (fix it, add
  links, add findings, alone or at reviewable quality). knowledge-mesh owns
  that eval and review-ledger spec and runs both candidates through the
  `llm_eval` compare harness; the km-8 taxonomy set goes first. No separate
  quality set here.

**Bench host: `spark` (decided 2026-09-29; superseded above for Slice 0).** Slice 0 and the frozen eval
world (`eval-run-spine.md`) run there, not on a serving box. It is the same
128 GB hardware as castor, so the concurrency curve transfers exactly; it
runs zero precis units, so an eval loop hammering it cannot disturb prod
(`host_heartbeat` on 2026-09-29 shows only balthazar / castor / melchior /
pollux beating — spark is absent, consistent with `90797a34` having deleted
every unit and `/etc/precis` on 2026-08-29); and it is earmarked for nothing
else, unlike pollux, which is the second replica. **A bench host is not
cluster duty** — it joins no service group and no capability list, so the
inventory guard is untouched.

Two spark-specific traps, both from `spark-provisioning.md`, and both bite a
containerized vLLM deploy specifically:

* The nvidia docker runtime is **not** configured by any ansible role — the
  live fix was a hand-run `nvidia-ctk runtime configure --runtime=docker` plus
  a docker restart, so a from-scratch box has no GPU inside containers.
* **Docker Hub / ECR egress stalls from spark** while ghcr.io works. vLLM's
  published images are not on ghcr, so plan on pre-seeding the image from
  another host (`docker save | ssh | docker load`) or serving from a local
  registry. Budget for this; it is the most likely first-day blocker.

Also note `/opt/precis/venv` on spark is a frozen orphan at 8.32.0 (`86aeff3f`,
2026-08-29). The bench wants a fresh install, not that.

**Inspected 2026-09-29 — mostly better than the notes above predicted.**
A read-only pass over the box found no precis, llama-swap or ollama service
running and `/etc/precis` genuinely gone, so the retirement held. Three
corrections to the traps:

* **The vLLM image is already cached locally** — `vllm/vllm-openai:v0.20.1`,
  24 GB, sitting in the local docker store from prior use. The Docker Hub
  egress block is *confirmed* (`registry-1.docker.io` times out at 20 s,
  exit 28; `ghcr.io` answers 401 in 0.13 s), but it is no longer the
  first-day blocker for this image. Pre-seeding is only needed to move to a
  newer tag.
* **The nvidia container runtime is already configured** — it is in
  `/etc/docker/daemon.json` and `docker info` lists it. The trap stands as
  written (no ansible role establishes it, so a rebuild loses it) but it does
  not block today.
* **Disk is not a constraint**: 3.6 TB total, 2.8 TB free on root.

Four things that do need doing, none of them cluster duty:

* **A hung NFS mount.** `/mnt/cluster` (autofs) does not respond — `statvfs`
  timed out at 120 s. This blocks anything that reads the share, which
  includes `eval-run-spine.md`'s content-addressed blob store. Fix or unmount
  before the bench depends on it.
* **A live graphical session.** Xorg plus gnome-shell hold the GPU (tens of
  MB) and the box idles at load ~1.4 with nothing else running. Small, but a
  benchmark host should not have an unaccounted background load — stop the
  display manager before slice 0 measures anything.
* **`uv` is absent**, and `/opt/precis` (venv, embedder-venv, kokoro-venv,
  wheels) is orphaned and should be removed rather than reused.
* **Stale unit files**: `llamaswap.service` (disabled) and an
  `ollama.service.d/` drop-in with no parent unit.

One claim in that pass to disregard: it read the single GB10 as meaning spark
is *not* equivalent to castor. A DGX Spark is one GB10 superchip with 128 GB
unified memory, so one GB10 is exactly what castor is too. The
concurrency-curve transfer argument is unaffected. Driver 580.159.03, CUDA
13.0; `nvidia-smi` reporting memory as "Not Supported" is normal for unified
memory and not a fault.

**Open:**

* **Model choice.** Nemotron 3 Super 120B-A12B NVFP4 is built for this
  workload — hybrid Mamba-Transformer latent MoE, 12B active, 1M context,
  natively NVFP4-trained, MTP layers for speculative decoding, a published
  vLLM DGX Spark recipe, tool calling via `--tool-call-parser qwen3_xml`.
  gpt-oss 120B MXFP4 is the control: same size class, fits one box, much
  better published single-stream and aggregate numbers, Apache 2.0 so no
  licence question. Decide on slice 0's measurement, not on paper.
* **Licence — APPROVED for the test by Reto, 2026-09-29.** Re-confirm before
  anything built on this model reaches production or ships to anyone else;
  the approval as given covers evaluation. The NVIDIA Nemotron Open Model
  License terminates if *you*
  institute patent or copyright litigation. It does not restrict commercial
  use, redistribution, derivative models, or training on outputs; NVIDIA
  disclaims ownership of outputs; attribution in redistribution notices is
  the only real obligation. Low risk against a defensive-publication
  strategy, which structurally never initiates such litigation — but note
  the scope reads "against any entity", broader than Apache 2.0's, which
  terminates only the patent grant and only for claims against the work.
  Read the licence file shipped in the model repo before this is
  load-bearing; the wording above came from a web fetch plus search
  summaries and a third-party analysis, which disagreed about a second
  guardrail-circumvention clause and may describe a different document.
* **KV budget per session.** Weights at NVFP4 leave roughly half the box for
  KV; how many concurrent sessions that buys depends on per-session context
  length, which the fisheye view controls. Falls out of slice 0.
* **Which tier the local rung joins** — BIG has precedent; FRONTIER would
  suit a model too big for one box. Same undecided question as
  `slullama-hpc-placement.md` leg 2.
* Whether SGLang beats vLLM for this model. Not worth answering until slice 0
  says the model is viable at all.
