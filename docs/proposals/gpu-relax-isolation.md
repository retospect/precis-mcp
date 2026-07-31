---
status: draft
title: Isolate in-process GPU-MLIP compute in the worker (spark wedge — autocatpath NEB)
model: opus
---

# Isolate in-process GPU-MLIP compute in the worker (spark wedge)

> **Revised after prod forensics (2026-07-31).** An earlier draft of this
> spec blamed `structure/relax.py`'s in-process `ml` relax. Investigation
> reclassified the root cause: the active wedger is **`autocatpath_explore`**
> (in-process MACE reaction-network NEB on spark), not the structure relax
> rung. The structure-relax isolation is retained below as a **secondary /
> latent** fix. See gr180096 for the full evidence trail.

## Motivation / why

spark's `precis-worker` (system profile) wedges repeatedly: CPU spins at
2+ cores, no logging, SIGTERM ignored until systemd's ~90 s `TimeoutStopSec`
SIGKILLs it — for hours, recurring for days. A restart re-wedges within a
minute (gr180096).

**Confirmed root cause (prod job history + live forensics):**

- **`autocatpath_explore` is the active wedger.** 81 job *starts*, **zero**
  completions/results over 2 days — every one `no_to_nh3_pd backend=mace
  (injected structure)`, the active NO→NH₃-on-Pd catalyst quest. It runs a
  MACE relax + reaction-network **NEB in-process** in spark's system worker
  (`executor="ssh_node"`, the `autocatpath[mace]` backend installed in the
  *worker venv* — **not** containerized like `struct_relax`). The quest loop
  re-dispatches a content-addressed job; spark re-claims it every restart,
  re-runs the in-process NEB, wedges the GPU (89 % SM, no progress, native
  CUDA call that never returns to the interpreter → SIGTERM handler can't
  run → SIGKILL), repeats. This is why a plain restart re-wedges instantly.

- **MACE itself is not broken.** Two faithful repros on spark's GB10 (a
  small molecule; a Pd(111) slab + N/O adsorbates + `inplane` variable-cell
  BFGS-200) both converge in seconds; 25 lighter in-process single-structure
  `ml` relaxes (`dispatch_relax`) have succeeded. torch 2.13.0+cu130 supports
  the GB10; no Xid/ECC errors. The wedge is specific to the **heavy
  reaction-network NEB workload** (many images, long runtime) under GPU
  co-tenancy — a true hang or an unbounded runtime that never completes
  within the job lease.

- **The `struct_relax` container path can't absorb it as-is.** ml has
  **never** succeeded via the `struct_relax` container job (2 attempts ever:
  one docker name-conflict rc=125, one wedged mid-run). So "just route it to
  the existing container" is blocked until that path is fixed and proven.

## In scope

A layered fix — the wedge is a rare/heavy nondeterministic hang that an
**in-process native CUDA call cannot self-abort**, so prevention (isolation)
+ containment (reap) + backpressure (circuit-break) are all needed:

1. **Isolate autocatpath's GPU compute (primary).** `autocatpath_explore`
   must not run MACE in-process in the shared worker. Either run it in a
   **container** (as `struct_relax` does — GPU-memory-bounded via
   `container_limit_flags()`, `_relax_timeout_s`-guarded, `kill_container` +
   `reset_gpu` on overrun), or run its pipeline in a **killable subprocess
   with a wall-clock timeout**, so a wedge/overrun self-aborts and the worker
   survives. (Touches the ssh_node dispatch boundary and/or the external
   `autocatpath` package's entrypoint.)
2. **Quest circuit-breaker (backpressure).** After N consecutive
   `autocatpath_explore` failures/timeouts on the same content key, stop
   re-dispatching and dead-end the candidate (mirror `harvest_measures`'s
   `ruled-out:` / `dead-end` pattern) — so a poison workload can't
   infinitely re-wedge the worker.
3. **External worker-watchdog reaper (containment, path-agnostic).** Detect a
   wedged `precis-worker` (CPU-spinning + no `worker_logs` progress for T +
   holding `/dev/nvidia*`) and SIGKILL→systemd-restart it, turning a
   multi-day silent stall into a self-healing minutes blip. Only safe when
   paired with (2), else it loops on the same poison job. Model it on the
   existing `precis_embedder_watchdog` role.
4. **Secondary / latent: `structure/relax.py` ml isolation.** `_run_ops`
   (:1306) runs the `ml` rung in-process whenever the local backend imports
   (only spark has `[dft-ml]`), bypassing the container path — a latent
   repeat of the same hazard even though it isn't today's active wedger.
   Route it through the isolated path too **once that path is proven for ml**
   (blocked on the prerequisite below).

## Explicitly NOT in scope

- **A deterministic MACE/CUDA code patch.** Ruled out — the stack works in
  isolation; the failure is heavy-NEB + contention, not a library bug.
- **Rewriting `run_loop`'s SIGTERM model** (it can't interrupt a native call
  mid-flight; isolation + reap is the answer).
- **Redesigning the catalyst reaction-network NEB numerics** — if the NEB is
  merely too slow (not truly hung), tuning image count / convergence is a
  separate autocatpath-package concern.

## Acceptance criteria

- A wedged/overrunning `autocatpath_explore` on spark **self-aborts** (via
  container timeout or subprocess wall-clock kill) and records a failure; the
  `precis-worker` main loop stays responsive to SIGTERM throughout and keeps
  running its other system-profile passes.
- After N consecutive same-content `autocatpath_explore` failures, the quest
  **stops re-dispatching** that content key (verifiable: no new job minted;
  candidate dead-ended).
- The worker-watchdog reaps a wedged worker within T minutes (not hours),
  proven by an injected-hang test or a documented drill.
- **Prerequisite for routing any ml to the container:** a `struct_relax`
  `ml`/MACE job completes end-to-end in prod (fix the docker-name-conflict;
  confirm the `precis-dft` image runs MACE; confirm `_relax_timeout_s` +
  `kill_container` + `reset_gpu` fire on an injected wedge). Until then, `ml`
  stays in-process (backed by 25 successes) and is covered only by the
  watchdog reaper.

## Target + blast radius

- `autocatpath_explore` execution: the `ssh_node` dispatch boundary
  (`src/precis/workers/executors/ssh_node.py`) and/or the external
  `autocatpath` package entrypoint; the `precis-mcp[catalyst-gpu]` install.
- Quest circuit-breaker: `src/precis/quest/compute.py`
  (`dispatch_autocatpath` / `harvest_measures` dead-end path).
- Worker-watchdog: a new `deploy/roles/precis_worker_watchdog` (model on
  `precis_embedder_watchdog`).
- Secondary: `src/precis/handlers/structure.py::_run_ops` (:1306/:1319) +
  the `struct_relax` container ml path (`workers/job_types/struct_relax.py`).

## Open questions / decisions log

- **Fix ordering / which layers to build now.** Options range from an
  immediate operational stop-gap (service_config prio-0 the autocatpath
  compute service on spark, or pause the `no_to_nh3_pd` quest — stops the
  bleeding, no code) → the watchdog reaper + circuit-breaker (self-healing,
  path-agnostic, aligns with the health-watchdog work) → full autocatpath
  containerization (proper isolation, largest, partly external-package).
  Needs a human decision — it touches the active catalyst research quest.
- **Is the autocatpath NEB truly hung or merely too slow?** Determines
  whether the fix is pure isolation+timeout (self-abort a legit-but-slow run)
  or also requires diagnosing a real CUDA-NEB hang on Blackwell. Capture a
  py-spy dump of a live wedged autocatpath job to settle it.
- **Containerize vs subprocess-isolate autocatpath.** Container reuses the
  proven `struct_relax` guard machinery but needs an image carrying
  `autocatpath[mace]` + weights; a killable subprocess is lighter but needs a
  clean autocatpath CLI entrypoint and its own timeout+GPU-reset wrapper.
