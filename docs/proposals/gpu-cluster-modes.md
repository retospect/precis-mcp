---
status: draft
title: GPU cluster topology modes — fuse N Sparks for a big model, or split for many jobs
model: opus
blocked-by: gpu-priority
---

# GPU cluster topology modes

> **Part of the unified scheduling frame** — this is §C (GPU topology) of
> `cluster-scheduling.md`; it is gated on the one-Spark-quantized test and
> `blocked-by: gpu-priority`. See the master frame for cross-axis ordering.

## Motivation / why

Soon there are N DGX Spark units (1 today + 3 incoming). The same hardware
serves two mutually-exclusive shapes:

- **Aggregated:** the units fused into one large accelerator (~N × 119 GB
  unified) running a really large model, fronted by one node (A).
- **Disaggregated:** N independent nodes each doing small/independent work
  (CFD, catpath seeds, per-node models, embedding).

This proposal specifies how precis *chooses* and *switches* between those
shapes. It is the elastic-topology layer above the per-node slots from
`gpu-priority.md` (which must exist first — hence `blocked-by`).

**Read the "When fusion is (and isn't) worth it" section first** — it may
argue you need less of this than it looks.

## Hardware reality — the constraint that shapes everything

Spark units cluster over **ConnectX Ethernet/RDMA (~200 GbE), not NVLink**
between chassis. So a fused model is **interconnect-bound**:

- **Tensor-parallel** (all-reduce every layer) is chatty and punished by an
  Ethernet fabric → poor fit.
- **Pipeline-parallel / expert-parallel** (activations between stages) is far
  more forgiving → the realistic way to shard across Sparks.

Consequence, and it's load-bearing: **the fused pool is a throughput /
batch engine, not a low-latency one.** Per-token latency across 4
Ethernet-linked Sparks is high. So the fused model is right for *"queue a batch
of hard reasoning jobs and drain them"* and wrong for *"chat with a 400B model
snappily."* Design for batch `bigpool` jobs, not interactive big-model serving.
(Verify the exact interconnect + the serving stack's PP support before
committing — see Open questions.)

## When fusion is (and isn't) worth it — the honest counter

One Spark's ~119 GB unified memory **already serves large quantized models** on
its own (order ~120 B @ 8-bit, ~200 B @ 4-bit) via the existing llama-swap
path. **Fusion only earns its complexity for frontier-size models you cannot
quantize onto a single unit** (~400 B+ at usable precision). So:

- If the models you actually want fit on one Spark quantized → **don't build
  fusion**; serve them per-node and use the disaggregated cluster for
  everything. This whole proposal stays on the shelf.
- Fusion is worth it only when you have a standing need for a model too big for
  one unit. That need is likely **occasional**, which is the strongest argument
  for *manual, rare* aggregation over frequent autonomous switching (below).

Decide this first. It gates whether any of the rest is worth building.

## The abstraction — schedule topologies, not nodes

The broker picks a **mode**; each mode publishes its slot inventory to the one
queue:

- **Disaggregated** → N independent single-node `gpu` slots (the `gpu-priority`
  slots, unchanged).
- **Aggregated** → 1 composite `bigpool` slot (the fused units, via node A).

Jobs declare `requires={"gpu"}` (any one node) vs `requires={"bigpool"}`. Modes
are mutually exclusive. **"Models pull the jobs" — already how precis works:**
the aggregated pool is a *transient consumer* that comes into being when the
mode flips, claims and drains `bigpool` jobs "for a while," then dissolves so
the nodes rejoin the disaggregated pool. The broker only decides the mode and
runs fuse/defuse; the existing pull-claim drains whichever slots are published.

## Start manual, earn the autonomy

Given single-user + likely-occasional fusion, do **not** open with an
autonomous demand-batching controller. Start with a **human topology toggle**,
mirroring `gpu-priority`'s reserve mode:

- `precis cluster fuse` → drain disaggregated work to a low-water mark, stand up
  the pool, serve `bigpool` jobs.
- `precis cluster split` → drain in-flight `bigpool` jobs to a boundary, tear
  the pool down, resume disaggregated.

This covers "I have big-model work, bring it up; I'm done, give me the cluster
back" with almost no control logic. **Autonomous demand-driven switching**
(aggregate when queued `bigpool` demand crosses a threshold or a job ages past a
deadline; defuse under disaggregated pressure) is a *later* phase, added only if
manual proves tedious.

## The control problem — hysteresis (only once autonomous)

Fusing + loading a frontier model across N units over RDMA is **minutes**, as is
teardown. So autonomous switching must **batch**: aggregate only above a demand
threshold (or human reserve, or job deadline); drain to a low-water mark; switch
back only under disaggregated pressure; **min-dwell timers** in each mode prevent
flip-flopping. Mis-tuned, this is thrash at a coarse grain. This is the same
turn-taking law as a small model holding a node — just a much larger stretch and
entry threshold.

## Hard parts (consider these before building)

- **Interconnect-bound latency** (above) — the fused model is batch, not
  interactive. Don't sell it as interactive big-model chat.
- **Pool-node failure.** One unit dies mid-aggregate → a missing PP/TP shard →
  the whole model is down. Need health + graceful handling: fail+requeue the
  in-flight `bigpool` jobs, and either revert to disaggregated or stand up a
  smaller pool (N-1 units, smaller model). The `gpu-priority` lease/orphan-reaper
  extends: a dead pool member releases the *whole* pool cleanly.
- **One big model at a time.** Serving multiple frontier models adds a second
  reload axis (which model is loaded) → more thrash. Simplest: one configured
  `bigpool` model; switching models is itself a mode-internal reload. Multiple
  big models = deferred.
- **Interruptibility, one level up.** A `bigpool` job mid-inference can't be
  preempted; a switch-back waits for its checkpoint. So `bigpool` jobs must be
  bounded/checkpointed too (same law), and defuse only at a low-water mark, never
  per-job.
- **Fuse/defuse is a pluggable driver.** `aggregate(nodes) -> endpoint` /
  `disaggregate()` behind a clean interface; the mechanics (serving stack — vLLM
  pipeline-parallel? SGLang? llama.cpp RPC? — RDMA/NCCL init, health, fronting
  through A) live there. The broker stays topology-agnostic so the serving stack
  can evolve without touching scheduling.

## The slurm line

In scope: **few coarse modes** (disaggregated / aggregated; maybe one
intermediate), demand-driven-or-manual switching, hysteresis, coarse cross-mode
windows/deadlines for fairness. Out (this is what makes it slurm): combinatorial
scheduling over arbitrary node subsets, per-job fair-share accounting, gang
scheduling of arbitrary groups, true mid-inference preemption. Keep modes few
and switches coarse.

## Phasing

1. **Manual topology toggle** — `fuse` / `split` commands: drain, stand up / tear
   down the pool via the driver, republish slots. Plus the `bigpool` slot type +
   `requires={"bigpool"}` job routing. (Depends on `gpu-priority` per-node slots.)
2. **The fuse/defuse driver** for the chosen serving stack (the real infra:
   RDMA/NCCL init, PP sharding, health, node-A front).
3. **Autonomous demand-driven switching** with hysteresis — only if manual is
   insufficient.
4. **Pool-node-failure handling** (fail+requeue, degrade to smaller pool).

## Explicitly NOT in scope

- Interactive low-latency serving *from the fused pool* (interconnect makes it
  batch — serve interactive models per-node instead).
- Combinatorial / fair-share / gang scheduling (the slurm line).
- Multiple simultaneous big models; arbitrary node-subset pools.
- Anything, if the "when fusion is worth it" test says one-Spark-quantized
  covers your models.

## Acceptance criteria

- `precis cluster fuse` drains disaggregated work to a low-water mark, stands up
  the pool, and `requires={"bigpool"}` jobs run against it; `split` tears it down
  cleanly and disaggregated work resumes — no orphaned GPU reservations either
  way.
- A `bigpool` job and a disaggregated `gpu` job cannot both hold a unit at once
  (modes are mutually exclusive; verified).
- A killed / dead pool node releases the whole pool and re-queues its in-flight
  `bigpool` jobs (injected-failure drill).
- (If autonomous) the pool does not flip modes more than once per min-dwell
  under a mixed demand workload.

## Target + blast radius

- New: a topology-mode selector + the `bigpool` slot type; `requires={"bigpool"}`
  routing; the `fuse`/`split` CLI.
- The fuse/defuse **driver** (serving-stack-specific; likely a new deploy role +
  a runtime control plane) — the bulk of the real work.
- Reuses `gpu-priority`'s per-node slots, leases, orphan-reaper, PRIO, reserve
  mode; `resource_slots` for the composite slot.
- Node A serving front (llama-swap / the chosen stack).

## Open questions / decisions log

- **Is fusion even needed?** Run the one-Spark-quantized test against the models
  you actually want. If they fit, shelve this. (Gate on everything.)
- **Interconnect + parallelism** — confirm ConnectX/RDMA topology and that the
  serving stack does pipeline/expert-parallel well enough over it to be useful.
- **Serving stack for the pool** — vLLM PP / SGLang / llama.cpp RPC / other; this
  choice drives the driver.
- **Trigger** — manual-only for a long time, or is there real autonomous
  `bigpool` demand (system jobs, not just the human) that justifies the
  demand-batching controller?
- **Model set** — one configured big model, or a switchable set (adds reload
  thrash)?

## Relationship to `gpu-priority.md`

`gpu-priority` builds the per-node slots, leases, reserve mode, and `PRIO`
claim — the disaggregated substrate. This proposal adds the *composite* slot and
the mode selector on top. It cannot start until those exist, and it should not
start at all until the fusion-worth-it test passes.
