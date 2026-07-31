---
status: draft
title: Isolate the ml/MLIP relax rung — never run MACE in-process in the shared worker
model: opus
---

# Isolate the ml/MLIP relax rung — never run MACE in-process in the shared worker

## Motivation / why

spark's `precis-worker` (system profile) wedges immediately after every
restart: CPU spins to 2+ cores, no logging, and SIGTERM is ignored until
systemd's ~90s `TimeoutStopSec` elapses and SIGKILLs it ("State
'stop-sigterm' timed out. Killing." on every restart in journal history).
A restart never recovers it — it re-wedges on the next claim (gripe
180096).

**Root cause (confirmed by live forensics on spark, 2026-07-31):** the
worker PID itself holds 904 MiB of GPU memory and `/dev/nvidia*` handles;
its last log line is a **MACE** model load → CUDA init (CUDA 13.0,
aarch64). GPU sits at 89 % SM utilisation with zero forward progress
while the worker's main thread spins on-CPU inside the native CUDA/torch
call. Because that C call never returns to the Python interpreter, the
SIGTERM handler (`cli/worker.py::_install_signal_handlers`, which only
sets a flag) never runs, `run_loop`'s `should_stop` is never re-checked,
and the process is un-interruptible → SIGKILL.

The MACE compute is an **`ml`-rung structure relax running in-process**.
`StructureHandler._run_ops` (`handlers/structure.py:1306`) runs
`run_relax(fidelity="ml", model="mace_mp")` **in-process first**, and only
falls back to minting the isolated `struct_relax` container job in the
`except RelaxUnsupported` branch (`structure.py:1319`) — i.e. **only when
the local MLIP backend is absent**. spark is the one node with the
`[dft-ml]` extra installed, so on spark the backend *is* importable and
MACE runs in-process in the long-lived worker. Every other node lacks the
backend → raises `RelaxUnsupported` → dispatches a `struct_relax` job that
runs in a **container with a `_relax_timeout_s` wall-clock cap** and, on
`TimeoutExpired`, self-aborts via `kill_container()` + `reset_gpu()` — the
exact GPU-driver-wedge protection added under gripe 171381
(`workers/job_types/struct_relax.py:494-522`).

**The masking defect:** gripe 171381 already solved "a GPU-driver-wedged
relax must self-abort" — but only for the *dispatched container* path. The
*in-process* path on the backend-bearing node was left unguarded. So the
one node that can actually GPU-wedge (the GPU node) is the one node that
bypasses the protection. `quest/compute.py::dispatch_relax` — despite its
name — only truly *dispatches* on backend-less nodes; on spark it calls
`StructureHandler.edit(op=relax, fidelity=ml)` which runs synchronously
in-process. Restarting masks nothing (re-wedges); disabling MACE on spark
would stop this wedge but mask the architectural gap — any future hung
kernel re-wedges the shared worker un-interruptibly.

## In scope

- Route the **`ml` / MLIP (GPU) rung through the isolated, timeout-guarded
  `struct_relax` container path even when the local backend is importable**
  — so a GPU wedge self-aborts (existing 171381 guard) instead of taking
  down the shared worker. Concretely: `_run_ops` returns `_NeedsDispatch`
  for the `ml` rung when a GPU route node exists, rather than calling
  `run_relax` in-process.
- Keep the cheap, torch-free **`clean` / `emt` rungs in-process** —
  they can't GPU-wedge and are the preflight/repair lane the dispatch
  path itself depends on.
- Preserve the existing preflight-clean-before-dispatch and content-address
  caching behaviour (the `except RelaxUnsupported` branch already does
  this; the `ml`-always-dispatch path must reuse it, not duplicate it).

## Explicitly NOT in scope

- **Fixing the MACE-on-aarch64 / CUDA-13.0 hang itself** (why the kernel
  wedges at 89 % SM). That is a real, separate defect — file/track it
  independently; this proposal removes its blast radius, it does not
  diagnose the CUDA hang.
- **Reworking `run_loop`'s SIGTERM model** (it can never interrupt a
  native call mid-flight; isolating GPU compute removes the main offender,
  which is sufficient). Not touching the signal wiring.
- Changing the `struct_relax` container contract, GPAW/DFT rungs, or the
  run-cube cache schema.

## Acceptance criteria

- On a GPU node with `[dft-ml]` installed and a GPU route node advertised,
  a quest-tick / `StructureHandler.edit` `ml`-rung relax **mints a
  `struct_relax` job** (container) — it does **not** load MACE in the
  worker process. Verifiable: the worker PID holds no `/dev/nvidia*`
  handles and no GPU memory during an `ml` relax.
- A simulated GPU-wedged `ml` relax **self-aborts** via the existing
  container `_relax_timeout_s` path and records an `infra` failure; the
  worker stays responsive to SIGTERM throughout (finishes its batch and
  exits well under the systemd stop timeout).
- The `clean` / `emt` in-process rungs are unchanged (still run locally,
  still serve as the dispatch preflight).
- A regression test asserts the `ml` rung returns `_NeedsDispatch` (or
  mints a `struct_relax` job) **even when the local MLIP backend imports
  successfully**, given a GPU route node — the condition that currently
  fails on spark.

## Target + blast radius

- `src/precis/handlers/structure.py::_run_ops` — the `ml`-rung branch
  (the `run_relax` call at :1306 and the `RelaxUnsupported → _NeedsDispatch`
  fallback at :1319); factor so `ml` routes to `_NeedsDispatch` up front
  when a GPU route node exists.
- `src/precis/quest/compute.py::dispatch_relax` — verify it still behaves
  (it goes through `StructureHandler.edit`, so it inherits the routing).
- Route-node detection: reuse the GPU-host resolution already in
  `quest/compute.py:311-318` (`store.all_resource_slots()` → hosts
  advertising `resource == "gpu"`), so the dev/CI single-node shape (no
  GPU advertised) keeps the in-process fallback — see Open questions.
- No migration; no worker unit change.

## Open questions / decisions log

- **Single-node / dev fallback.** Forcing `ml → dispatch` unconditionally
  would strand the job on a box with no GPU route node (dev/CI, or a
  single-node deploy). `quest/compute.py` already models this: "single-host
  resource_slots is the dev/CI shape … falls through to the in-process EMT
  path." Decision to confirm: route `ml` to the container path **only when
  a GPU route node is advertised**; otherwise keep the current in-process
  behaviour (dev convenience, no shared-worker risk because dev isn't the
  cluster). This keeps the fix cluster-only.
- **Does the `struct_relax` container actually run the `ml`/MACE rung, or
  only GPAW?** The `_dispatch` runner stages `params.json` with
  `{fidelity, model, steps}` and calls `build_run_argv` → the container's
  `precis-dft-run`. Confirm the container image handles `fidelity="ml"`
  (mace_mp) and not just `gpaw-relax` before making `ml` always-dispatch —
  otherwise the fix trades a wedge for an unsupported-rung failure. (The
  content-addressed `struct_relax` jobs spark already runs *from other
  nodes* are the existing proof it does; verify.)
- **Immediate mitigation while this ships** (operational, not code — for
  the human to pick): (a) remove/hide the `[dft-ml]` extra from spark's
  *worker* venv so `run_relax(ml)` raises `RelaxUnsupported` → dispatches
  to the container (which has its own backend) — needs confirming the
  worker venv and container image are separate; or (b) `service_config`
  prio-0 the quest/autocatpath initiator on spark so it stops originating
  in-process `ml` relaxes. Both are deploy/config decisions, not part of
  this code change.
