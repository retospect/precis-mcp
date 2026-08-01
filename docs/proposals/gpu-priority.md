---
status: draft
title: Prioritized GPU slots — short background units + interactive priority + kill backstop
model: opus
---

# Prioritized GPU slots

## Motivation / why

The real want is simple: **when the human asks for something, it gets the next
GPU slot for a bit; when they stop asking, background work churns again.**
Everything else discussed (an accounted queue for all GPU work, memory
bin-packing, multi-node topology fusion, converting every worker pass to a
cron schedule) was scope creep around that want — and it rested on an
*unconfirmed* premise (that spark's wedge is GPU contention rather than an
over-scoped, un-checkpointed job that overruns its lease; two faithful repros
ran co-tenant with the live embedder and did **not** wedge).

This proposal scopes to the want and nothing more. It also **doubles as the
experiment that confirms the root cause**: Phase 1 decomposes catpath into
short units; if those complete reliably, the wedge was over-scoping and the
grand broker is unnecessary. The broker vision is preserved as an appendix,
gated on that experiment.

Supersedes `gpu-broker.md` (retired) — same lineage as gr180096.

## The model — two classes, cooperative yield

- **Two priority classes:** `interactive` (human-triggered) outranks
  `background` (embed, catpath, dream, OCR). Interactive claims sort ahead in
  the pull-claim precis already uses.
- **Background runs in short, checkpointed units.** At each unit boundary a
  background holder checks for a waiting interactive claim and **yields** (does
  not renew its turn).
- **Co-fit small, evict big.** A small interactive request that fits alongside
  running background just runs (spatial share, no wait). One that needs most of
  the card waits *one background unit* and the resident model unloads. The
  request decides — no general bin-packing solver.
- **Bounded interactive hold** — "for a bit": interactive gets a max stretch,
  then decays to normal so a runaway foreground job can't permanently starve
  background.

**Variable load handles itself.** With pull-claim + priority, an active human
dominates; a quiet one lets the interactive queue drain and background resumes.
No fixed rates, no cron, and **no "session" to track** — "when I'm done" is not
a signal the system needs; the human simply stops submitting and background
fills the vacuum. Priority *is* the scheduler.

## The one hard truth

**Foreground latency is capped by the coarsest background unit.** Priority
cannot preempt a running CUDA kernel (the SIGTERM-deaf wedge). So "next slot in
seconds" *requires* background GPU work chopped into short units. The scheduler
change is small; **the real work is decomposing the long workloads**, and that
lives in the workloads (partly the external `autocatpath` package), not here.

## The decomposition contract (keeps catpath/scheduler coupling thin)

Long GPU jobs decompose via a **generic resumable-job contract**, not
scheduler-specific per-workload logic:

- `enumerate_units()` → the ordered list of bounded units (catpath: per
  elementary reaction step, and/or per seed).
- `run_unit(i, checkpoint_in) -> checkpoint_out` → runs one bounded unit,
  reads/writes checkpoint state so a kill loses only that unit.

Catpath implements this by adopting the **coordinator/yield pattern quests
already use** (`workers/executors/_yield.py` — `Yield`/`WakeWhen`/`Done`;
`quest_tick` is already "run a bounded step, yield, get re-armed"). So catpath
does not couple to a *new* scheduler — it becomes a citizen of machinery precis
already runs, and the scheduler treats units **opaquely** (it never learns
chemistry). CFD and any other long GPU job reuse the same seam. **Guardrail:
the scheduler must not grow workload-specific knowledge — if it starts to,
the contract is leaking and the coupling is wrong.**

## Correctness backstop — the yield needs teeth

A background unit that **won't** yield at its boundary (genuinely wedged) holds
the slot and the human's "next slot" never comes. And a logical lease-refund
does **not** reclaim physical GPU from a live, spinning, SIGTERM-deaf process.
So yielding must be backed by **force-kill + verified GPU reclamation** — reuse
`struct_relax`'s proven `_relax_timeout_s` → `kill_container` + `reset_gpu`
(gr171381), which already frees the card and confirms it, rather than a bare
lease timeout.

## Phasing (small chunks; each ships value)

1. **Decompose `autocatpath_explore` into short checkpointed units** via the
   coordinator/yield seam (per elementary step / per seed). Fixes spark (no
   90-min in-process blob; a kill loses one step) **and** confirms the root
   cause: if decomposed units complete reliably, over-scoping was the cause.
2. **Add a priority class + priority-ordered claim** to GPU jobs. Interactive
   claims sort ahead; the human gets the next slot within one short unit.
3. **Hard-kill backstop** for a unit that won't yield by its boundary
   (`kill_container` + `reset_gpu`), so the bounded wait is guaranteed.

Later, only if Phase 1 shows contention *persists* after decomposition:
per-`(host,gpu)` exclusive leases (reuse `reserve/release_resource_slots`) so
two heavy background units can't overlap. This is the smallest step toward the
broker and is *not* assumed necessary.

## Explicitly NOT in scope (demoted; see Appendix)

- An accounted single queue for *all* GPU consumers incl. the embedder.
- Memory-aware bin-packing / co-scheduling as a general solver.
- Multi-node aggregate/disaggregate topology fusion.
- Converting non-GPU passes (nursery, sweeper, review) to scheduled todos.
- Making the embedder evictable / bursting the corpus-embed backlog.

Each waits until interactive priority proves out *and* contention is confirmed.

## Acceptance criteria

- A decomposed `autocatpath_explore` run completes on spark without wedging the
  worker; a killed unit loses only that unit (checkpoint proven), and the
  worker stays SIGTERM-responsive throughout.
- With background GPU work running, an `interactive` GPU job acquires a slot
  within **one background unit's duration** (measured; the target sets the
  required unit granularity).
- A background unit that ignores its yield boundary is force-killed and the GPU
  memory is confirmed freed (injected-hang drill), so the interactive job is
  not starved.
- The scheduler contains no catpath-specific logic — decomposition rides the
  generic resumable-job/coordinator contract.

## Target + blast radius

- `workers/job_types/` + `quest/compute.py::dispatch_autocatpath` — mint
  per-unit work instead of one whole-network job; the `autocatpath` package's
  unit interface (external dependency — see Open questions).
- `workers/executors/_yield.py` + the ssh_node/coordinator claim path —
  priority-ordered claim; the generic resumable-job contract.
- Reuse `struct_relax`'s `kill_container` + `reset_gpu` for the backstop.
- `registry.py` — a priority axis on GPU job specs.

## Open questions / decisions log

- **autocatpath unit interface.** Does the external package already expose
  per-step / per-seed execution + checkpoint, or does that land there first?
  Phase 1 depends on it. (This is the coupling seam — keep it generic.)
- **Foreground latency target.** The acceptable "next slot" wait sets the
  required background-unit granularity (seconds vs a minute or two). Decide the
  target; it drives how finely catpath must decompose.
- **Priority representation.** Reuse the todo tree's `PRIO:` axis for GPU job
  claims, or a dedicated field? Prefer reuse.
- **Interactive hold cap.** The max stretch before interactive decays to
  normal — pick a value that feels responsive without starving background.

## Appendix — the broker vision (deferred, gated on Phase 1)

If Phase 1 shows genuine contention remains after decomposition, escalate
toward: exclusive per-GPU leases → an accounted queue including resident models
(embedder/LLMs as evictable, declared leases) → coarse topology modes for
multi-node model fusion (pull-based, hysteretic switch). The turn-taking law
("hold a bounded stretch, yield under pressure, stay warm if idle") and the
slurm line (no bin-packing / fair-share / gang / mid-kernel preemption) carry
forward. But none of it is assumed until the small version proves insufficient.
