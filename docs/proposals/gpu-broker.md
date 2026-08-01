---
status: draft
title: GPU broker — one accounted queue for all GPU work, with time-sliced turns and mode-switched topology
model: opus
---

# GPU broker — one accounted queue for all GPU work

## Motivation / why

spark's worker wedges because **nothing arbitrates the GPU**. Heavy batch
jobs (`autocatpath_explore` NEB, `struct_relax`, surya OCR) launch onto the
one GB10 with no admission check, co-tenant with the resident embedder
(~3 GB) and local LLMs. When their footprints exceed the card they thrash;
an in-process, un-interruptible MACE/CUDA op then wedges the worker
un-killably (SIGTERM-deaf → SIGKILL). Root cause: **uncoordinated concurrent
GPU access** (gr180096; the confirmed active trigger was the NO→NH₃
autocatpath NEB, 81 starts / 0 completions).

The fix is not per-path whack-a-mole. It is a single **GPU broker**: every
GPU consumer books through one accounted queue, and the GPU is multiplexed in
**time** (exclusive turns) rather than **space** (memory fractions) —
because spatial sharing fails the moment one consumer needs most of the card,
which is exactly our case.

This supersedes the narrower `gpu-relax-isolation.md` (autocatpath-only).

## The core model

**Time-slice, don't memory-split.** A job acquires an exclusive GPU lease,
runs a *bounded, checkpointed unit* at full card, then yields. No two heavy
consumers overlap; no thrash.

**Account for everything — including the daemons.** The invisible-consumer
problem ("some model is eating 80% and we don't know") is solved by making
*every* GPU user hold a declared lease.

**One turn-taking law at every scale.** Whoever holds the card holds it for a
**bounded stretch**, and yields when there is **pressure** — and *only* then:

- **"Pressure" = queued demand for the resource you occupy** (a higher- or
  equal-priority job waiting on that GPU / that node / the pool). No pressure
  → keep your seat warm; there is no reason to pay a reload when nobody else
  wants the card. Pressure → finish the current bounded unit, release at the
  checkpoint, let the queue drain.
- **The stretch is a max-dwell + renewal.** A holder renews its lease each
  unit as long as it still has work *and* no higher-priority pressure has
  appeared; at the dwell cap (or when pressure shows up at a checkpoint) it
  gets off. The cap is per-participant: a **burst job** releases after one
  unit; a **resident small model** (a hot LLM, the embedder) runs a longer
  stretch (~30 min) to amortize its load, then gets off under pressure or at
  the cap.

So the "burst job vs resident service" distinction collapses to *how long
your turn is*, not *whether the rule applies*. A resident model is just a
long-dwell, stay-warm-if-idle citizen of the same queue — it holds a declared
lease, renews while unpressured, and evicts (unload/`shrink`) when someone
else needs the card. The eviction primitive already exists for LLMs
(`llama-swap` load/unload); the embedder gets the same treatment. This is how
it "backgrounds": warm when it can be, gone when it must be.

**Reuse, don't reinvent.** precis already has the semaphore
(`reserve_resource_slots` / `release_resource_slots`, used today by
`local_serving.acquire/release` for LLM-serving concurrency), the
per-`(host,resource)` capacity advertisement (`resource_slots`, synced by
heartbeat), the `requires={"gpu"}` declarations on job specs (struct_relax,
fold), and the Postgres-row-lock pull-claim + `Yield`/`WakeWhen` executor
primitives. The broker wires these around GPU-heavy jobs; it is ~a few
hundred lines, not a new subsystem.

## Cooperative yield — the load-bearing constraint

You **cannot preempt a running CUDA kernel** (that is the SIGTERM-deaf wedge).
So "prioritize and fall back" cannot mean interrupting mid-compute. It means
**cooperative yield at checkpoints**: a job runs one bounded unit, then the
broker decides whether it keeps the lease or a higher-priority job takes the
turn. Therefore **decomposition/checkpointing is not an optional complement —
it is the enabler of the entire scheme.** No bounded units → no yielding → no
time-slicing → no priority. (Concretely: the autocatpath NEB must be split
into per-elementary-step / per-seed units that each complete in ~1–2 min and
checkpoint — see Phase 2.)

## Crash-safe leases — the part to get right

Our failure mode is a job that holds the GPU and is SIGKILLed. A plain
counter leaks: the slot is never refunded and the GPU starves forever. So a
reservation is a **lease with a TTL + owner**, and an **orphan-slot reaper**
refunds a lease whose owner is gone. That reaper is a natural nursery
detector — it ties directly into the health-watchdog work. Get this right or
one wedge permanently starves the card.

## Priority + the interactive fast lane

Two or three coarse classes: **interactive > normal > background**.
Background = corpus embedding, OCR, catpath NEB. Interactive = a live search
**query embedding** (needs <1 s) and an **agent waiting on a local LLM**.
These must not queue behind a 5-minute background burst. Options (decide in
Phase 4): a small always-warm interactive reservation, and/or interactive
jobs jumping the queue at the next checkpoint boundary. The risk to avoid:
letting the interactive lane quietly become an un-managed daemon again — it
must still hold an accounted lease.

## Multi-node aggregation as a topology mode (future — most worth review)

Three more Spark units arrive soon (4 total, RDMA-networked). The same
hardware serves two mutually-exclusive shapes:

- **Aggregated:** spark[A|B|C|D] fused into one large accelerator (~4×
  unified memory) running a really large model, fronted by node A.
- **Disaggregated:** 4 independent nodes each doing small/independent work
  (CFD, per-node models, catpath, embedding bursts).

**Model it by generalizing the broker from a per-node lease to a topology-mode
selector.** Each mode publishes its own slot inventory to the *one* queue:

- Disaggregated mode → 4 independent single-node `gpu` slots.
- Aggregated mode → 1 composite `bigpool` slot (the fused nodes, via A).

Jobs declare `requires={"gpu"}` vs `requires={"bigpool"}`; the broker chooses
the mode that best serves queued demand and effects the transition. **"The
models pull the jobs" is exactly right and already how precis works:** the
aggregated pool is a *transient consumer* that comes into existence when
queued `bigpool` demand crosses a threshold, claims and drains those jobs "for
a while," then dissolves so the nodes rejoin the disaggregated pool. The
broker only decides the mode + runs fuse/defuse; the existing pull-claim
drains whichever slots the current mode publishes.

**Hysteresis is the whole control problem — and it is the same turn-taking
law at cluster scale.** The aggregated pool holds a bounded stretch and
yields under pressure, exactly like a small model holds a node: fusing 4
nodes + loading a 400B-class model is minutes, as is teardown, so the stretch
(and the demand threshold to *enter* it) is just much larger. *Batch*:
aggregate only when queued `bigpool` demand crosses a threshold (or a job
ages past a deadline, or a high-priority one lands); drain to a low-water
mark; switch back only when there is disaggregated **pressure** (pending
independent work); min-dwell timers in each mode prevent flip-flopping.
Mis-tuned, this is just thrash at a coarser grain.

Consequences that stay consistent with the rest of the design: a `bigpool`
job can't be preempted mid-inference, so a switch-back waits for its
checkpoint (bounded/checkpointed big-model jobs, same rule one level up); the
**fuse/defuse mechanics are a pluggable topology driver** (NCCL/RDMA init,
sharded serving à la vLLM tensor-parallel, health, fronting through A) behind
a clean `aggregate()` / `disaggregate()` interface so the broker stays
topology-agnostic; **cross-mode fairness stays coarse** (time-boxed windows +
deadlines: "aggregate at most T; guarantee independent work a window every N
min if pending"), never per-job fair-share.

This stages **last** (gated on the hardware + Phases 1–4), but the slot/mode
interface is designed now so nothing precludes it.

## Where the slurm line is

In scope: a priority queue (2–3 coarse classes) + cooperative yield at
checkpoints + per-GPU leases with crash-safe TTL + a coarse topology-mode
selector with hysteresis. Explicitly **out** (this is what makes it slurm):
memory bin-packing / constraint-solving over arbitrary node subsets,
fair-share accounting, gang/multi-node co-scheduling beyond the single
all-or-nothing pool, true mid-kernel preemption, backfill. Few coarse modes,
demand-driven switching, cooperative yield. Hold that line.

## Phasing (each phase ships value alone)

1. **GPU lease around batch jobs + embedder declares its reservation.** Wire
   `reserve/release_resource_slots` around `requires={"gpu"}` job execution
   (add `autocatpath_explore` + OCR to the gpu-requiring set); capacity 1 per
   GPU; `paused` → yield via the existing executor primitive; release in a
   `finally`; lease TTL + orphan reaper. The embedder holds a standing
   declared lease. **This alone fixes spark** (no overlap, nothing invisible).
2. **Decompose catpath into bounded, checkpointed units** (per elementary
   step / per seed) — the enabler for yielding + short turns; also stops one
   job holding the card 90 min.
3. **Make the embedder (and LLMs) evictable + burst the corpus-embed
   backlog**, keeping a warm lane for query embeds.
4. **Priority classes + interactive fast lane.**
5. **Multi-node aggregation as a topology mode** (gated on the new hardware).

## Explicitly NOT in scope

- A deterministic MACE/CUDA patch (ruled out — the stack works in isolation;
  the failure is contention, not a library bug).
- Rewriting `run_loop`'s SIGTERM handling (isolation + cooperative yield is
  the answer; a native call can't be interrupted mid-flight).
- The slurm-creep list above.

## Acceptance criteria (Phase 1 — the buildable slice)

- No two `requires={"gpu"}` jobs run concurrently on a GPU host; a second one
  yields (does not run, does not busy-spin) until the first releases.
- The embedder's footprint is visible as a held lease; the broker will not
  schedule a heavy job that would collide with it.
- A SIGKILLed job's GPU lease is auto-refunded within the TTL by the orphan
  reaper (proven by an injected-kill drill); the GPU is not permanently
  starved.
- spark's `autocatpath_explore` no longer wedges the worker: it either runs
  in an exclusive turn to completion, or yields — the worker stays
  SIGTERM-responsive and keeps serving its other passes.

## Target + blast radius

- Executor claim/run path (`workers/executors/ssh_node.py`,
  `coordinator.py`) — acquire/yield/release around `requires={"gpu"}` jobs.
- `store/_resource_slots_ops.py` (`reserve`/`release`) + a lease TTL/owner
  column + an orphan-slot reaper (nursery detector).
- Registry `requires` sets (`workers/registry.py`) — add autocatpath/OCR.
- `quest/compute.py` (`dispatch_autocatpath`) — per-unit job granularity
  (Phase 2).
- Embedder + local-serving eviction (`utils/llm/local_serving.py`, the
  embedder service) — Phase 3.
- A topology driver + mode arbiter — Phase 5 (new).

## Open questions / decisions log

- **Interactive fast lane** — warm reservation vs queue-jump-at-checkpoint;
  how to keep it accounted and not a de-facto daemon. (Flagged for a design
  pass.)
- **Lease granularity** — start exclusive (capacity 1 per GPU); resist
  memory-aware capacity / bin-packing.
- **Orphan-reaper home** — nursery detector (fits the health work) vs sweeper.
- **Multi-node**: the aggregate/disaggregate hysteresis thresholds + the
  fuse/defuse driver's serving stack (vLLM-TP? other?) — deferred to Phase 5,
  but the slot/mode interface must not preclude it now.
