---
status: draft
title: Prioritized GPU work — chunk catpath into per-seed todos, human-first claim, reserve mode
model: opus
---

# Prioritized GPU work

## Motivation / why

Two separate problems, one small solution — and **no scheduler**, because there
is effectively one human user:

1. **The wedge (spark).** `autocatpath_explore` runs the whole NO→NH₃ ammonia
   network × 3 seeds × full NEB as one ~90-min **in-process** MACE blob that
   overruns its lease, never completes (81 starts / 0 completions, gr180096),
   and — being an un-interruptible native CUDA call — takes the worker down
   (SIGTERM-deaf → SIGKILL). This is *background-vs-background* GPU
   over-subscription plus an indivisible monolith. Nothing to do with the human.
2. **Responsiveness.** When the human wants the cluster, their work should run
   before the deferrable "could-just-as-well-run-at-2am" background work.

With a single user, #2 needs **a little priority** (human-first claim) and a
coarse **reserve mode**, not a multi-class fairness scheduler. #1 needs the
monolith broken into **short, resumable chunks** — which, happily, autocatpath
already supports.

Supersedes `gpu-broker.md` (retired). Full trail: gr180096.

> **Part of the unified scheduling frame** — this is §B (priority-claim + the
> wedge fix) of `cluster-scheduling.md`, which indexes all scheduling axes on
> the one decentralized claim substrate. Read that for cross-axis ordering; this
> spec carries the mechanics.

## The shape (single-user, no cooperative-yield dance)

- **Serialize background GPU work** — one heavy background GPU job at a time
  (stops background self-contention, the wedge's substrate).
- **Keep background chunks short** — so the slot frees on its own frequently and
  a kill loses little. Priority can't preempt a running CUDA kernel; short
  chunks are what make "wait for the current one" cheap.
- **Human-first claim** — reuse the todo tree's existing `PRIO:` axis: a
  human-triggered GPU job sorts ahead of background in the claim query. One
  comparison, no classes, no fairness math.
- **Reserve mode** — a human toggle ("reserve the cluster for me for a few
  hours") that simply **stops dispatching new heavy background jobs**; the
  in-flight one finishes and the box is yours. Plus a **night `meta.schedule`**
  for heavy background. This handles "I want it now" coarsely and trivially, so
  fine per-chunk responsiveness is *not* needed up front.
- **Kill backstop** — for the rare "I want the card *now*, don't wait one
  chunk": force-kill the holder with verified GPU reclamation (reuse
  `struct_relax`'s `_relax_timeout_s` → `kill_container` + `reset_gpu`,
  gr171381). A bare lease-refund does **not** reclaim the card from a live
  wedged process — the kill must actually free and confirm the GPU.

## Chunking catpath — the chunks already exist

autocatpath is *built* for fan-out (its own docstring: "Snakemake fan-out").
Its CLI exposes exactly the units:

- `autocatpath seed --seed N <cfg>` → run one seed → a partial (standalone,
  JSON-serialisable — `run_one_seed`).
- `autocatpath aggregate` → combine partials → the barrier (`aggregate_partials`).

So chunking is **not a catpath change** — it is a precis *orchestration* change.
Today `quest/compute.py::dispatch_autocatpath` mints **one** `autocatpath_explore`
job (all seeds, whole network, ~90 min in-process). Instead mint a small **job
tree**, each node a todo, using machinery precis already runs:

```
autocatpath: NO→NH₃ on Pd            (parent)
├─ seed 0  →  autocatpath seed --seed 0    (content-addressed job)
├─ seed 1  →  autocatpath seed --seed 1
├─ seed 2  →  autocatpath seed --seed 2
└─ aggregate → autocatpath aggregate       (runs after the 3 seeds)
```

- **Dependency** "aggregate after all seeds succeed" uses the **existing**
  `auto_check` evaluator `child_job_succeeded` (×3) — no new coordinator.
- **Resumption needs no checkpoint blob** — each seed job is content-addressed
  (`idem_key` includes the seed), so a retry **skips completed seeds** and runs
  only the missing one. The persisted partials *are* the checkpoint. (Same
  content-address idempotency `struct_relax` already uses.)
- The aggregate produces the same scalar barrier the quest already harvests
  (`harvest_measures`).

**Wins immediately, before any priority work:** a kill loses one seed (~a third
of the run — minutes), not the 90-min monolith; the other partials persist;
retry resumes; and the 3 independent seeds fan out across nodes on the 4-Spark
cluster (the parallelism this design was for).

**Honest caveat:** a single seed still runs MACE in-process, so it *can* still
wedge — but at a third the exposure, with partial progress saved and retry
resuming. Fully isolating the in-process run (container/subprocess) is separate,
deferred work; per-seed chunking is the immediate, sufficient win for the wedge.

If a seed is still too coarse, autocatpath also exposes finer phases
(`run_states` = relax-only, `run_barriers` = the NEBs) — but per-seed is the
CLI-blessed place to start.

## The one residual truth

A **big** interactive request (needs most of the card) still can't start until
the current background chunk ends or is killed — its latency floor is the
coarsest background chunk. Reserve mode covers the "I planned to use it" case
without any chunking; the kill backstop covers "now, don't wait." A **small**
request that co-fits just runs alongside. So we do *not* need fine
chunking-for-responsiveness — only chunking-for-the-wedge, which per-seed gives.

## Phasing (small chunks; each ships value)

1. **Per-seed job tree for catpath.** `dispatch_autocatpath` mints seed jobs +
   an aggregate wired via `auto_check` `child_job_succeeded`, invoking
   autocatpath's existing `seed` / `aggregate`. **Fixes the spark wedge.** Also
   the root-cause confirmation: if per-seed jobs complete reliably, the cause was
   the over-scoped monolith, not ambient GPU contention. **Build of record:**
   `docs/design/autocatpath-integration.md` §3.8 (the code-grounded per-seed
   fan-out `run_one_seed` → `aggregate_partials`) — keep the two in sync.
2. **Human-first `PRIO:` on GPU jobs** — human-triggered work sorts ahead of
   background in the claim.
3. **Reserve mode + night `meta.schedule`** — the coarse "the box is mine for a
   few hours" lever + heavy background off-hours.
4. **Kill backstop** — force-kill + GPU-reclaim for the "now" case and for a
   chunk that won't finish.

## Explicitly NOT in scope

- Cooperative-yield handoff, multi-class priority, fairness accounting,
  bounded-hold decay — all moot with a single user (see above).
- An accounted queue for *all* GPU consumers incl. the embedder; memory-aware
  bin-packing; multi-node topology fusion; converting non-GPU passes to
  schedules; making the embedder evictable. Deferred to the appendix, gated on
  Phase 1 showing contention persists after chunking.

## Acceptance criteria

- `dispatch_autocatpath` mints a seed-per-job + aggregate tree; the aggregate
  runs only after all seed jobs succeed (via `auto_check`), and yields the same
  scalar barrier the quest harvests today.
- A killed seed job loses only that seed; a retry skips completed seeds
  (content-addressed) and the run converges — the worker stays SIGTERM-responsive
  and spark stops wedging on the monolith.
- A human-`PRIO:` GPU job is claimed ahead of queued background GPU work.
- Reserve mode stops new heavy background dispatch within one claim cycle; the
  in-flight job finishes and no new background GPU job starts until released.
- The kill backstop frees and confirms the GPU (injected-hang drill).

## Target + blast radius

- `src/precis/quest/compute.py::dispatch_autocatpath` — mint the seed/aggregate
  tree instead of one job; `harvest_measures` reads the aggregate's scalar.
- The `autocatpath_explore` **plugin dispatch** (location TBD — not in precis
  `job_types/` nor obviously in catpath; see Open questions) — teach it the
  `seed=` / `aggregate` mode, or split into `autocatpath_seed` /
  `autocatpath_aggregate` job_types.
- `auto_check` wiring (`workers/auto_check_evaluators/child_job_succeeded.py`) —
  reused as-is.
- Claim path (`workers/executors/ssh_node.py`) — honour `PRIO:` for GPU jobs;
  reserve-mode dispatch gate.
- Reuse `struct_relax`'s `kill_container` + `reset_gpu` for the backstop.

## Open questions / decisions log

- **Where is the `autocatpath_explore` plugin dispatch?** Not in precis
  `job_types/` nor obviously in the catpath package; find it and confirm whether
  it shells to the autocatpath **CLI** (subprocess — trivial to add `--seed`) or
  imports `run()`. Decides whether Phase 1 is `dispatch_autocatpath`-only or also
  the plugin. (Pin before building.)
- **Reserve-mode representation** — a `service_config`/app-state flag the
  dispatch gate reads, with a TTL so a forgotten reserve auto-expires.
- **`PRIO:` reuse** — confirm the todo `PRIO:` axis threads to job claim order
  for GPU jobs (vs a dedicated field).
- **Seed granularity sufficiency** — if per-seed (~minutes) still wedges under
  load, drop to `run_states` / `run_barriers` phases.

## Appendix — the broker vision (deferred, gated on Phase 1)

If per-seed chunking *doesn't* stop the wedge — i.e. genuine ambient contention
remains — escalate toward: exclusive per-`(host,gpu)` leases
(reuse `reserve/release_resource_slots`) → an accounted queue including resident
models (embedder/LLMs as evictable declared leases) → coarse multi-node topology
modes for big-model fusion (pull-based, hysteretic switch). The turn-taking law
("hold a bounded stretch, yield under pressure, stay warm if idle") and the
slurm line (no bin-packing / fair-share / gang / mid-kernel preemption) carry
forward. None of it is assumed until the small version proves insufficient.
