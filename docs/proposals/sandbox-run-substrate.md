---
status: built
title: sandbox_run slice 1 — the container-execution substrate (stub-gated, dark)
---

> **Built** (branch `fix/sandbox-run-substrate`): `sandbox_run` job_type
> (`workers/job_types/sandbox_run.py`) + `claude_docker` poll executor
> (`workers/executors/claude_docker.py`), registered default-OFF under
> `PRECIS_SANDBOX_ENABLED`. Merges dark. Harvest (folder+tarball) is slice 2;
> `mode:run` is slice 3; the cluster ops half (`~/work/cluster`) remains a human
> prerequisite for a live run.

# sandbox_run slice 1 — the container-execution substrate

> Slice 1 of the `sandbox_run` design (`docs/design/sandbox-run.md`). Builds the
> mint → claim → launch → poll → terminal spine with a **stub container binary**,
> registered **default-OFF** (`PRECIS_SANDBOX_ENABLED`), so it merges **dark** —
> no live sandbox host, DB creds, or Max quota touched by the gate, and nothing
> runs in prod until a human enables it. Harvest (folder+tarball) is slice 2;
> `mode:run` is slice 3. Read the design doc for the full picture and the
> decisions log — this proposal is the buildable subset.

## Motivation / why

The `sandbox_run` feature is large for one fixer tick and its cluster/ops half
lives in another repo the fixer can't reach. This slice is the part that is
fully buildable and testable **inside precis-mcp** against a stub podman: prove
a `kind='todo'` can mint a container job, get claimed only on a sandbox host,
launch a detached container with the right argv, be polled and reaped by name,
and reach a terminal status with a failure bubble — all without a live host.

## In scope

1. **`sandbox_run` job_type** (`src/precis/workers/job_types/sandbox_run.py`) — a
   plugin spec modeled on `fix_gripe`, **`mode:build` + `precis_access:none`
   only** this slice. `PARAMS_SCHEMA`: `prompt` (required), `target_node`
   (required; must be an `agent_sandbox_host`, **rejected if melchior**),
   `wall_seconds` (required), `image` (default `code-task:<sha>`), `model`
   (resolved via `resolve_model(Tier.CLOUD_SUPER)` + `PRECIS_SANDBOX_MODEL`
   override). `validate_submit` **fails closed** at `put` time on:
   `mode:run`, `precis_access:read`, a `secrets` list, a non-sandbox
   `target_node`, or a missing `CLAUDE_CODE_OAUTH_TOKEN` in the daemon env.
2. **`claude_docker` executor** — add to `EXECUTOR_PROVIDES`
   (`workers/executors/__init__.py`) and a `job_claude_docker` ref-pass
   (`workers/executors/claude_docker.py`). **Registration gated**: the pass is
   appended in `cli/worker.py` only when `PRECIS_SANDBOX_ENABLED=1` (mirrors
   `classify` default-OFF). `PRECIS_SANDBOX_CONCURRENCY` caps in-flight runs per
   host (default 2). The container binary is `PRECIS_PODMAN_BIN` (default
   `podman`) so tests inject a stub.
3. **Claim** — `claim_executor_jobs` gates on `STATUS:queued` +
   `target_node == PRECIS_NODE`; writes a lease sized from `wall_seconds`.
   Optionally load-gated (`PRECIS_LOAD_CEILING`).
4. **Detached launch** — `podman run -d --name sandbox-<job_id>` with `--env
   CLAUDE_CODE_OAUTH_TOKEN` (inherited from the daemon env, **no `--bare`, no
   `ANTHROPIC_API_KEY`**), cgroup caps (`--memory`/`--cpus`/`--pids-limit`),
   **no `--device`** (never a GPU). Stage `/work` with `PROMPT.md` (the harvest
   contract) and record `meta.container` / `meta.run_host` / `meta.deadline`.
5. **Poll + reap by name** — each tick `inspect`s status + exit code and renews
   the lease (heartbeat); `exited` → read exit → terminal `STATUS`; `now >
   deadline` still running → `kill` + `rm -f` → `STATUS:failed` +
   `swept:wall-timeout`. A boot reconcile `rm -f`s orphaned `sandbox-*`
   containers with no live owning job.
6. **Minimal forensics + terminal** — write a `job_summary` (asked / exit /
   duration) and a `job_event` (stderr tail); success → `STATUS:succeeded`
   (parent `child_job_succeeded` closes the todo); non-zero exit / empty run /
   timeout → `STATUS:failed` + `child-failed:<job_id>` bubble (no auto-retry).
   The `/work/out` → folder + tarball **artifact projection is slice 2** — this
   slice discards `out/`, keeping only forensics.

## Explicitly NOT in scope

- **Harvest / addressing** (folder+plaintext, content-addressed tarball,
  `RUN.json`) — slice 2.
- **`mode:run`, recurring, `precis_access:read`, `secrets`** — later slices;
  all rejected fail-closed here.
- **Cluster ops** (`~/work/cluster`: `code_task_image` play, dedicated OAuth
  token, read-only DB role, network mode) — human prerequisites for a *live*
  run; irrelevant to this slice's stub-podman gate.
- **The `code-task` Dockerfile target** — belongs with the ops slice; this
  slice only references the image tag.

## Acceptance criteria

All green in the container gate with a **stub `PRECIS_PODMAN_BIN`** (no live
host):

1. `put(kind='todo', meta={executor:'claude_docker', job_type:'sandbox_run',
   params:{prompt, target_node, wall_seconds}})` → `dispatch` mints
   `kind='job'`, `STATUS:queued`, node-pinned; model resolves via
   `Tier.CLOUD_SUPER`.
2. `validate_submit` raises a clear error at `put` time for each fail-closed
   case (mode:run, precis_access:read, secrets, non-sandbox/melchior
   target_node, missing OAuth token).
3. `job_claude_docker` is registered **only** under `PRECIS_SANDBOX_ENABLED`;
   claims only when `target_node == PRECIS_NODE`; honors
   `PRECIS_SANDBOX_CONCURRENCY`; writes a `wall_seconds`-sized lease.
4. The launch argv asserts: `-d --name sandbox-<job_id>`, `--env
   CLAUDE_CODE_OAUTH_TOKEN`, **no `--bare` / no `ANTHROPIC_API_KEY`**, cgroup
   caps present, no `--device`.
5. Poll renews the lease (heartbeat); a stub that reports `exited 0` → 
   `STATUS:succeeded` and the parent todo closes; `exited 1` / empty →
   `STATUS:failed` + `child-failed` bubble; `now > deadline` → kill + `rm -f` +
   `swept:wall-timeout`.
6. Boot reconcile reaps an orphaned `sandbox-*` container with no live job.
7. Unit + integration tests cover 1–6. Skill stub + `CLAUDE.md` (Workers list)
   updated to mention `job_claude_docker` (default-OFF) and point at
   `docs/design/sandbox-run.md`. `ruff` + `mypy` + `pytest` green.

## Target + blast radius

- **New:** `workers/job_types/sandbox_run.py`,
  `workers/executors/claude_docker.py`, tests.
- **Edited:** `workers/executors/__init__.py` (`EXECUTOR_PROVIDES`),
  `cli/worker.py` (gated registration), `workers/job_types/__init__.py`
  (register built-in), `CLAUDE.md`.
- **Not touched:** ingest, search, web, embeddings, the paper corpus. Registered
  default-OFF, so a deploy of this slice changes **nothing** in prod until
  `PRECIS_SANDBOX_ENABLED=1` is set on a sandbox host.

## Open questions / decisions log

All load-bearing decisions are settled in `docs/design/sandbox-run.md`
(decisions log). This slice inherits them; nothing blocker-severity remains for
`mode:build` / stub-podman scope. The design doc is the reference; this file is
the buildable contract.
