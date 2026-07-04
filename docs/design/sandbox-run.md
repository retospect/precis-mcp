# sandbox_run — autonomous coding tasks in a container (design-of-record)

> Design-of-record for the `sandbox_run` job_type (the piece ADR 0048
> deferred). The **buildable units** are carved into `docs/proposals/`:
> `sandbox-run-substrate.md` (slice 1, ready), with harvest and run-mode as
> fast-follows. The **cluster/ops half** lives in `~/work/cluster`
> (`roles/code_task_image` + `playbooks/40-code-task-image.yml`) — the fixer
> can't reach it, so it is a human prerequisite, not intake. This file is the
> full picture the slices reference; keep it true.

## Why

Take an open-ended task — *"write a python script that does xyz"* — hand it to
Claude inside a throwaway container with a real toolchain (uv, tests, network),
and keep both the produced code and its results as first-class, retrievable
precis artifacts. Reuses the proven `todo → dispatch → job` lifecycle, the
`target_node` pin, leasing, the failure-bubble, and the sweeper — the new
surface is a job_type, a poll-based executor, and a harvest contract, designed
so a future slurm / aws-batch backend is a small adapter, not a rewrite.

## Shape

`todo(meta.executor='claude_docker', job_type='sandbox_run', params={…})` →
**dispatch** mints the `kind='job'` → **`job_claude_docker`** (registered only
where `PRECIS_SANDBOX_ENABLED=1`, i.e. the sandbox hosts) claims it by
`target_node` → runs a **detached** container, **polls** by container name,
**harvests** `/work` back into the DB + NAS → terminal `STATUS` closes the todo.

### Params

`prompt` (build), `mode` (`build`|`run`), `deliverable` (`code`|`result`|`both`),
`target_node` (an `agent_sandbox_host`; **never melchior**), `image`
(`code-task:<sha>`), `precis_access` (`none`|`read`), `wall_seconds`,
`artifact` (run), `seed_files`, `secrets` (vault names → env). Model resolves
via `resolve_model(Tier.CLOUD_SUPER)` (ADR 0046) with a `PRECIS_SANDBOX_MODEL`
override — never a private constant.

### The `/work` four-lane bus

The executor (trusted, DB creds) stages a run dir on the artifact root, mounts
it `/work`; the container (untrusted, **no DB creds**) reads/writes only files:

```
/work/PROMPT.md   IN   task + harvest contract          (executor writes)
/work/mcp.json    IN   only when precis_access:read     (executor writes)
/work/in/         IN   seed files / input data          (executor writes)
/work/out/        OUT  deliverable:
    <code>            code  → folder+plaintext + tarball
    tests/            code  → harvested AND executed (green = proof)
    pyproject.toml    deps  → harvested (the dependency recipe)
    uv.lock           deps  → harvested (reproducible pin)
    RUN.json          recipe→ {cmd, inputs, outputs, image}
    artifacts/        result→ produced data (large → tarball+pointer)
    RESULT.md         result→ the answer (small → inlined in job_summary)
/work/.venv/      EPHEMERAL  never harvested (reconstructible from uv.lock)
/work/_run/       FORENSICS  transcript, tests.log, result.json
```

Four typed lanes over one mount: **env evaporates** (keys via `--env`, never on
`/work`); **deps shrink to `uv.lock`** (the `.venv` is scratch); **code** →
folder+plaintext (searchable) + tarball (runnable); **result** → surfaced in the
summary. The volume mount is the whole IN/OUT bus; the executor is the only
thing touching both the DB and `/work`, so a prompt-injected agent can spend the
capped token and scribble in `/work` but cannot reach the database.

### Reaping: detached + poll, by container name

Claimer and runner are the same box (the pass runs on the sandbox host), so
it's **local podman, no ssh**.

**Process topology — a pass, not a new daemon.** `job_claude_docker` runs
**inside the existing per-node `precis worker`**, gated ON only where
`PRECIS_SANDBOX_ENABLED=1` (balthazar/spark) — no new launchd/systemd unit (the
cluster already carries ~12 per host). This is viable *because* the executor is
detached-poll: each tick is a cheap `inspect` + heartbeat, the heavy work is
out-of-process in the container, so it's a good round-robin citizen. The worker
runs as **`deploy`** (the trusted executor — holds DB-write creds + the token);
it launches the container **as the locked-down `agent_sandbox`** (via a `sudo
-u` / user-socket rule), so **executor-user ≠ container-user** — an escape lands
on `agent_sandbox` (rootless subuid), never on the creds-bearing `deploy`
process. Being "just a gated pass" also means it drops unchanged into a future
one-supervisor-per-machine consolidation. Launch `podman run -d --name
sandbox-<job_id>`;
store `meta.container`, `meta.run_host`, `meta.deadline`. Each poll tick
`inspect`s status+exit and **renews the lease** (heartbeat) so a legit
multi-hour run never trips the stuck-job sweeper. `exited` → harvest → `rm` →
terminal. `now > deadline` → `kill` + `rm -f` → `swept:wall-timeout`. A boot
reconcile `rm -f`s orphaned `sandbox-*` containers with no live owning job.
Reap by **name**, never a host pid — the name survives worker restarts
(conmon keeps the container alive independent of the worker).

### Harvest → DB + NAS, and addressing

After exit: mint a `folder`; write `out/` files (incl. `uv.lock`/`RUN.json`) as
`plaintext`/`python` refs (legible, searchable projection; pathological guard
only); tar `out/` to a **content-addressed** store; write
`job_summary`/`job_event`/`meta.transcript`; link `job→folder`; delete the
scratch workdir (tarball persists — NAS is 21 TB, no GC yet).

Address the **folder ref**, not a path. `meta.artifact = {sha256, size, key}`
with `key` a **relative** content-addressed path
`sandbox-artifacts/<sha256>.tar.zst`, resolved against per-host
`PRECIS_SANDBOX_ARTIFACT_ROOT` (default `<shared_mount>/sandbox-artifacts`).
Fetch verifies the sha; on miss, reconstruct from the folder's `plaintext` refs.
Mirrors the paper-PDF `storage_path`/`pdf_locations` pattern. Each build/run
mints a new folder version (`supersedes` lineage) pinning one immutable tarball.

### Re-run + operationalize (`mode:run`)

Same substrate, claude swapped out: stage the stored tarball, `uv sync`, run
`RUN.json.cmd`, harvest result/forensics only (`run-of` link). Recurring =
`mode:run` under a `level:recurring` umbrella with `meta.schedule` — the
produced script becomes a scheduled pipeline writing a dated result series.
Determinism boundary: same code + same deps + same image; the external world
may differ (accepted).

### ComputeBackend + Staging seams (slurm/aws)

Not a separate system — same job_type, harvest, addressing, todo lifecycle.
Only a `ComputeBackend` adapter (`{submit, poll, collect, kill}`) and a
`Staging` location differ. `claude_docker` is the first backend (`podman run -d`
/ `inspect` / read-NFS / `rm -f`); `slurm` (`sbatch`/`squeue`/`scancel`) and
`aws_batch` are later entries in `EXECUTOR_PROVIDES`. The poll lifecycle *is*
the submit→poll shape they need — which is why the local executor is poll-based,
not blocking. The one genuinely backend-specific axis is **staging** (NFS local
+ slurm; S3/EFS + sync for aws) — keep it behind an interface from day one.

## Build plan (slices)

1. **Substrate** (`docs/proposals/sandbox-run-substrate.md`, **ready**) —
   job_type + `claude_docker` executor happy-path (poll-reap, stub podman),
   dispatch mint, node-pin claim, `/work` staging, `--env`/no-`--bare` argv,
   detached launch + deadline kill + orphan reconcile, minimal forensics,
   fail-closed `validate_submit`, `PRECIS_SANDBOX_ENABLED`-gated registration.
   Ships **dark** (gated off), so it merges safely without the cluster ops.
2. **Harvest + addressing** (fast-follow proposal) — folder+plaintext
   projection, content-addressed tarball, `RUN.json` parse, folder→sha→root
   round-trip, the failure taxonomy in `job_summary`.
3. **`mode:run` + recurring** (fast-follow proposal) — stored-tarball staging,
   `uv sync`, `RUN.json.cmd`, recurring umbrella.
4. **Cluster ops** (human, `~/work/cluster`) — prerequisites for a *live* run;
   see `roles/code_task_image/README.md`. Note **podman is not currently
   installed on balthazar/spark** (the `services:[…podman]` + `agent_sandbox`
   layer is unrealized — `podman_installed` is set nowhere, so the `mcps`
   image tasks are dormant too), so **install-podman is prerequisite zero**.
   Then: `code_task_image` build-in-place play; **enable the pass in
   `precis_worker`** on the sandbox hosts (`PRECIS_SANDBOX_ENABLED=1` + token +
   `PRECIS_SANDBOX_*` env) — **no new daemon**; the `deploy`→`agent_sandbox`
   podman rule; the read-only DB role (for `precis_access:read`); the artifact
   root dir; the network mode.

## Decisions log (2026-07-04)

- **Where "the thing" lives:** DB holds the legible projection
  (`folder`+`plaintext`) + provenance; NAS holds the faithful runnable tarball.
  "Recipe not materialization" — harvest `uv.lock`, never `.venv`. Mirrors
  CAD/structure/paper-PDF.
- **Network:** open egress; internal reachability bounded by container network
  mode (bridge/internet-only preferred over `--network=host`) — pinned by the ops play.
- **Claude auth:** long-lived `CLAUDE_CODE_OAUTH_TOKEN` (Max), NOT
  `--bare`/`ANTHROPIC_API_KEY`; a **dedicated** sandbox token (scoped,
  revocable), not the agent-worker token. Portability killed the melchior-only
  OAuth asymmetry.
- **Host allowlist = threat model:** melchior → *escape* (holds
  OAuth/gateway/creds) → **excluded**; balthazar/spark → *load*-dominant →
  cgroup caps + concurrency=2 + load-gated claim; residual escape capped by
  dedicated-token + no-DB-creds + network mode.
- **Model:** `Tier.CLOUD_SUPER` (opus-4.8 once `PRECIS_MODEL_OPUS` is bumped —
  see `docs/proposals/opus-4.8-consolidation.md`).
- **Precis access:** `none`|`read` dial; `read` blocked on a read-only DB role +
  an MCP endpoint (external prereq).
- **Task secrets:** `params.secrets` = vault names → env; never in params/DB.
- **Image distribution:** none — built in place by an ansible play per sandbox
  host, tagged by git sha, idempotent. No laptop build, no registry, no
  multi-arch juggling.
- **Written code vs result:** separate lanes; success collapses to one pass/fail
  bit at the todo, taxonomy is forensic.
- **Reaping:** detached + poll, reaped by container name (survives restart);
  heartbeated lease defeats the sweeper false-reap; boot reconcile kills orphans.
- **Process topology:** a **gated pass in the existing per-node worker**, NOT a
  new daemon (avoids adding to ~12 units/host; fits a future one-supervisor
  consolidation). Executor-user `deploy` (trusted, holds creds+token) ≠
  container-user `agent_sandbox` (locked-down, rootless) — launched via a `sudo
  -u`/socket rule. Enabled per-host by `PRECIS_SANDBOX_ENABLED`.
- **Cluster reality (2026-07-04):** podman is installed on *neither* sandbox
  host; the whole `agent_sandbox` layer is unrealized. Installing podman +
  configuring rootless-for-`agent_sandbox` is prerequisite zero — independent
  of, and blocking, everything else in the ops half.
- **Verification:** MVP trusts self-authored green `tests/` (a named deferral;
  an independent verify pass is future work).
- **Addressing:** folder ref → content hash → relative key under a per-host root.
  Immutable per version; integrity-checked; node-independent.
