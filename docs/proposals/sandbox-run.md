---
status: draft
title: sandbox_run — autonomous coding tasks in a container, results back into the DB
---

# sandbox_run — autonomous coding tasks in a container, results back into the DB

## Motivation / why

The dark factory can build+ship changes to *precis itself* (ADR 0048 fixer)
and run LLM planner ticks (`plan_tick`). It **cannot** take an open-ended task
— *"write a python script that does xyz"* — hand it to Claude inside a
throwaway sandbox with a real toolchain (uv, tests, network), and keep both
the produced code and its results as first-class, retrievable precis
artifacts. This is the `sandbox_run` job_type ADR 0048 deferred.

It turns a `kind='todo'` into a container run on a cluster node and harvests
the output back into the DB, so *"write me a scraper"* and later *"run it
every morning and store what it finds"* are the same substrate, one flag
apart. It reuses the proven `todo → dispatch → job` lifecycle, the
`target_node` pin, leasing, the failure-bubble, and the sweeper — the new
surface is a job_type, a poll-based executor, and a harvest contract, all
designed so a future slurm / aws-batch backend is a small adapter, not a
rewrite.

## In scope

The **precis-mcp code slice** — buildable and testable in this repo with a
stubbed container binary (the `PRECIS_CLAUDE_BIN` stub pattern), no live
sandbox host required.

### 1. `sandbox_run` job_type

`src/precis/workers/job_types/sandbox_run.py`, a plugin spec (`dispatch`,
`PARAMS_SCHEMA`, `validate_submit`) modeled on `fix_gripe`. Params:

- `prompt` (str, required for `mode:build`) — the task.
- `mode` — `build` (default) | `run`.
- `deliverable` — `code` | `result` | `both` (default `both`).
- `target_node` (str) — sandbox-host pin (reuses the `meta.params.target_node`
  claim gate). Must be an `agent_sandbox_host`; **never melchior** (see
  Security).
- `image` (str) — the `code-task` image tag; defaults to the deployed
  `code-task:<sha>`.
- `precis_access` — `none` (default) | `read`.
- `wall_seconds` (int) — hard timeout; sizes the deadline + heartbeated lease.
- `artifact` (str, required for `mode:run`) — the folder id/slug whose stored
  tarball to execute.
- `seed_files` (optional) — files staged into `/work/in/`.
- `secrets` (optional, list of names) — vault secret names to inject as env
  (see Security). Names only; values never in params/DB.
- Model is resolved via `resolve_model(Tier.CLOUD_SUPER)` (ADR 0046 router)
  with a `PRECIS_SANDBOX_MODEL` override — **not** a private constant.

### 2. `claude_docker` executor (poll-based)

Add to `EXECUTOR_PROVIDES` (`workers/executors/__init__.py`) and a
`job_claude_docker` ref-pass (`workers/executors/claude_docker.py`).
**Registration is gated**: the pass is enabled only where
`PRECIS_SANDBOX_ENABLED=1` (ansible sets it on `agent_sandbox_hosts` only),
mirroring how `classify` ships default-OFF — so no wrong node ever volunteers.
`PRECIS_SANDBOX_CONCURRENCY` caps in-flight runs per host (default **2**).

Lifecycle is **detached + poll**, reaped by container name (never a host pid):

1. **Claim** — `claim_executor_jobs` gates on `STATUS:queued` +
   `target_node == PRECIS_NODE`; writes a lease sized from `wall_seconds`.
   Optionally **load-gated** (`PRECIS_LOAD_CEILING`) so a hot node (spark's
   inference/DFT work) isn't further loaded.
2. **Launch detached** — `podman run -d --name sandbox-<job_id>` with cgroup
   caps (`--memory`, `--cpus`, `--pids-limit`) and **no `--device`** (never
   grabs a GPU). Store `meta.container`, `meta.run_host`,
   `meta.deadline = claim_ts + wall_seconds`.
3. **Poll (coordinator-style)** — each tick `podman inspect` the status +
   exit code and **renew the lease** (heartbeat), so a legit multi-hour run
   never trips the stuck-job sweeper. The worker does not block a pass slot.
4. **Complete** — `exited` → read exit code → harvest `/work` (on NFS) →
   `podman rm` → terminal `STATUS`. `child_job_succeeded` closes the parent.
5. **Kill** — `now > deadline` still running → `podman kill` + `rm -f` →
   `STATUS:failed` tagged `swept:wall-timeout`.
6. **Orphan recovery** — a boot reconcile lists `sandbox-*` containers with no
   live owning job and `rm -f`s them (a dead worker leaves the container
   running under conmon; the lapsed lease also lets the sweeper fail the DB
   job).

The container binary is `PRECIS_PODMAN_BIN` (default `podman`) so tests inject
a stub.

### 3. The `/work` four-lane bus + harvest contract

The executor (trusted, DB creds) stages a run dir on the artifact root and
mounts it as `/work`; the container (untrusted, no DB creds) reads/writes only
files:

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

Four typed lanes over one mount: **env evaporates** (keys via `--env`, never
on `/work`); **deps shrink to `uv.lock`** (the `.venv` is scratch); **code**
→ folder+plaintext (searchable) + tarball (runnable); **result** → surfaced in
the summary. `PROMPT.md` states the placement contract; the image entrypoint
(`code-task-run.sh`, baked in) invokes claude, tees forensics to `_run/`, runs
`tests/`, and writes `_run/result.json`.

### 4. Auth: long-lived OAuth token, not an API key

The container runs `claude -p --dangerously-skip-permissions --model <m>`
(**no `--bare`**) with `CLAUDE_CODE_OAUTH_TOKEN` via `--env`, inherited from
the executor daemon env. Runs against the Max subscription — token portability
means any sandbox host works, not just melchior. Cost guard is `--max-turns` +
the `wall_seconds` kill + shared `quota_check` awareness, **not** a dollar cap.

### 5. Harvest → DB + NAS

After the container exits (`podman rm`), the executor reads `/work`: mints a
`folder`, writes `out/` files (incl. `uv.lock`/`RUN.json`) as
`plaintext`/`python` refs (the legible, searchable projection — pathological
guard only: a single file > N MB is pointer'd, a sanity ceiling on ref count);
tars `out/` to the **content-addressed** artifact store; writes
`job_summary`/`job_event`/`meta.transcript`; links `job → folder`. The
per-run scratch workdir (the `.venv` churn) is deleted on harvest; the tarball
persists (no GC for now — the NAS is 21 TB). On `mode:run` it harvests
result/forensics only (code unchanged) and links `run-of`.

### 6. Artifact addressing

Logical handle = the **folder ref**. Its `meta.artifact = {sha256, size, key}`
where `key` is the **relative** content-addressed path
`sandbox-artifacts/<sha256>.tar.zst`, resolved against a per-host
`PRECIS_SANDBOX_ARTIFACT_ROOT` (default `<shared_mount>/sandbox-artifacts`).
Fetch verifies the sha; on miss it reconstructs from the folder's `plaintext`
refs. Mirrors the paper-PDF `storage_path`/`pdf_locations` pattern. Each
build/run mints a new folder version (`supersedes` lineage) pinning exactly
one immutable tarball.

### 7. ComputeBackend + Staging seams (for slurm/aws reuse)

Factor two interfaces so future backends are adapters, not rewrites:

- **`ComputeBackend`** — `{submit, poll, collect, kill}`. `claude_docker` is
  the first instance (`podman run -d` / `inspect` / read-NFS / `rm -f`). A
  later `slurm` (`sbatch`/`squeue`/`scancel`) or `aws_batch`
  (`submit-job`/`describe-jobs`/`terminate-job`) is a new entry in
  `EXECUTOR_PROVIDES`. The poll lifecycle above is *already* the submit→poll
  shape those need — that is why the local executor is poll-based, not
  blocking.
- **`Staging`** — where `/work` lives and how it's staged/collected. Local +
  slurm share the NFS mount; aws needs S3/EFS + a sync step. Keep it behind an
  interface from day one; it's the one genuinely backend-specific axis.

### 8. Skill + docs

`precis-sandbox-help.md` (the job_type, the `/work` contract, `mode:build|run`,
`RUN.json`, re-run/recurring recipes) + `precis-job-help` / `precis-overview`
/ `CLAUDE.md` updates in the same commit.

### Suggested build slices (decide at `/ready`)

Large for one fixer tick; the natural split:

1. **Substrate** — job_type + `claude_docker` executor happy-path
   (poll-reaping, stub podman), dispatch mint, node-pin claim, `/work`
   staging, `--env`/no-`--bare` argv. Fail-closed `validate_submit`.
2. **Harvest + addressing** — folder+plaintext projection, content-addressed
   tarball, forensics, folder→sha→root round-trip, failure bubbles.
3. **`mode:run` + recurring** — stage stored tarball, `uv sync`, run
   `RUN.json.cmd`; recurring via a `level:recurring` umbrella.
4. (parallel, human) the cluster ops below.

## Explicitly NOT in scope

- **Cluster/ops changes** (live in `~/work/cluster`, outside the fixer's git
  world; a human applies them — hard prerequisites for a *live* run, not for
  this repo's gate):
  - the `code_task_image` ansible play (build-in-place on
    `agent_sandbox_hosts`, lean `code-task` target, tagged by git sha,
    idempotent on the tag; no registry/GHCR/multi-arch);
  - shipping a **dedicated sandbox `CLAUDE_CODE_OAUTH_TOKEN`** (distinct from
    the agent-worker token, independently revocable) to the sandbox executor
    daemon env from the vault;
  - a genuine **read-only DB role** + a reachable precis **MCP endpoint** for
    `precis_access:read` (today `agent_ro` lacks SELECT) — so
    `precis_access:read` is **deferred**; MVP ships `precis_access:none` only.
- **A dedicated `code`/`script` kind** — MVP uses `folder`+`plaintext` (ADR
  0045); first-class kind is a later call.
- **Independent deliverable verification** — MVP trusts self-authored green
  `tests/` (the agent grades its own homework). A second-agent verify pass is
  a **named deferral**, not an omission.
- **Rollback / migration-class safety** — inherited from ADR 0048:
  fix-forward only, single-user blast radius, daily backups.

## Acceptance criteria

Done (MVP: `mode:build`, `precis_access:none`) means, all gating green in the
container gate with a **stub podman** (no live sandbox host):

1. `put(kind='todo', meta={executor:'claude_docker', job_type:'sandbox_run',
   params:{prompt, target_node, wall_seconds}})` → `dispatch` mints a
   `kind='job'`, `STATUS:queued`, node-pinned. Model resolves via
   `Tier.CLOUD_SUPER`.
2. `job_claude_docker` is registered **only** under `PRECIS_SANDBOX_ENABLED`;
   claims only when `target_node == PRECIS_NODE`; writes a `wall_seconds`-sized
   lease; honors `PRECIS_SANDBOX_CONCURRENCY`.
3. Launch is detached (`-d --name sandbox-<job_id>`) with cgroup caps and
   `CLAUDE_CODE_OAUTH_TOKEN` via `--env`, **no `--bare`/`ANTHROPIC_API_KEY`**;
   the poll renews the lease; `now > deadline` kills + `rm -f` + `swept:
   wall-timeout`.
4. Given a stub container that populates `/work/out/` (code + `tests/` +
   `uv.lock` + `RUN.json`) and `/work/_run/result.json`, the executor:
   - mints a `folder`, writes each `out/` file as a `plaintext`/`python` ref
     (retrievable via `get(kind='folder', …)` and search);
   - writes the content-addressed tarball under
     `PRECIS_SANDBOX_ARTIFACT_ROOT` and records `meta.artifact.{sha256,size,
     key}`; a fetch re-verifies the sha;
   - writes `job_summary` + `job_event` + `meta.transcript`; links
     `job→folder`; deletes the scratch workdir;
   - sets `STATUS:succeeded`; the parent todo's `child_job_succeeded`
     closes it.
5. Failure paths: non-zero exit / red `tests/` / empty `out/` →
   `STATUS:failed` + `child-failed:<job_id>` bubble (no auto-retry); the
   failure taxonomy (transient/gave-up/impossible/infra) is recorded in
   `job_summary`, while the todo sees only pass/fail. Timeout → same, tagged
   distinctly.
6. `precis_access:read`, an un-provisioned `secrets` name, a non-sandbox
   `target_node`, or any missing prereq raises a clear `validate_submit` error
   at `put` time (fail closed).
7. A boot reconcile reaps orphaned `sandbox-*` containers with no live job.
8. Unit + integration tests cover: dispatch mint, gated registration, node-pin
   claim, detached launch argv (`--env`/no-`--bare`/caps), poll+heartbeat,
   deadline kill, harvest → folder+tarball, content-hash round-trip, both
   failure bubbles, orphan reconcile. Skill + CLAUDE.md updated. `ruff` +
   `mypy` + `pytest` green.

## Target + blast radius

- **New:** `workers/job_types/sandbox_run.py`,
  `workers/executors/claude_docker.py` (+ `ComputeBackend`/`Staging` seams),
  `data/skills/precis-sandbox-help.md`, `docker/Dockerfile` `code-task`
  target, tests.
- **Edited:** `workers/executors/__init__.py` (`EXECUTOR_PROVIDES`),
  `cli/worker.py` (gated `job_claude_docker` registration),
  `workers/job_types/__init__.py` (register built-in), `CLAUDE.md`,
  `precis-job-help` / `precis-overview`, `docs/decisions/README.md` (ADR index
  on graduation).
- **Not touched:** ingest, search, web routes, embeddings, the paper corpus.
  Stub-podman gate → no live container, DB creds, or Max quota consumed in CI.
- **Post-deploy look:** a `mode:build` job on a real sandbox host produces a
  folder + tarball and closes its todo; no planner-quota starvation; no
  sandbox jobs land on melchior.

## Open questions / decisions log

Resolved during design (2026-07-04):

- **Where does "the thing" live?** DB holds the *legible projection*
  (`folder`+`plaintext`) + *provenance*; NAS holds the *faithful runnable
  tarball*. "Recipe not materialization" — harvest `uv.lock`, never `.venv`.
  Mirrors CAD/structure/paper-PDF.
- **Network:** open egress (tasks legitimately fetch packages/data). Internal
  reachability bounded by the container **network mode** (bridge/internet-only
  preferred over `--network=host`) — a decision the ops play must pin.
- **Claude auth:** long-lived `CLAUDE_CODE_OAUTH_TOKEN` (Max), NOT
  `--bare`/`ANTHROPIC_API_KEY`. A **dedicated** sandbox token (scoped,
  revocable) — not the agent-worker token.
- **Host allowlist = threat model.** melchior → *escape* is the concern (holds
  OAuth/gateway/creds) → **excluded**. balthazar/spark → *load*-dominant →
  cgroup caps + concurrency=2 + load-gated claim; residual escape capped by
  dedicated-token + no-DB-creds + network mode.
- **Model:** `Tier.CLOUD_SUPER` (opus-4.8 once `PRECIS_MODEL_OPUS` is bumped),
  not a private constant. See the companion opus-4.8-consolidation proposal.
- **Precis access:** `precis_access: none|read` dial. `none` ships first;
  `read` blocked on a read-only DB role + an MCP endpoint (external prereq).
- **Task secrets:** `params.secrets` = vault names → injected as env; never in
  params/DB. Bounds what tasks are doable without the agent hardcoding creds.
- **Image distribution:** none — built in place by an ansible play per sandbox
  host, tagged by git sha, idempotent. No laptop build, no registry,
  no multi-arch juggling.
- **Written code vs result:** separate lanes (`out/` code vs `out/artifacts/`
  + `RESULT.md`), selected by `deliverable: code|result|both`. Success
  collapses to one pass/fail bit at the todo; taxonomy is forensic.
- **Reaping:** detached + poll, reaped by **container name** (survives worker
  restart); heartbeated lease defeats the stuck-job sweeper false-reap; boot
  reconcile kills orphans. Local podman (claimer == runner, no ssh).
- **Re-run:** same substrate in `mode:run` — stage the stored tarball, `uv
  sync`, run `RUN.json.cmd`. Recurring = `mode:run` under a `level:recurring`
  umbrella with `meta.schedule`. Determinism boundary: same code + same deps +
  same image; the external world may differ (accepted risk).
- **Addressing:** address the **folder ref**, not a path; resolves via a
  content hash + relative key under a per-host artifact root. Immutable per
  version; integrity-checked; node-independent.
- **slurm/aws:** *not* a separate system — same job_type + harvest + addressing
  + todo lifecycle; only a `ComputeBackend` adapter + a `Staging` location
  differ. Local podman is the first backend.

No blocker-severity open question remains for the MVP scope. Before
`status: ready`: run `/ready`, and choose whether to build the whole precis
slice at once or by the suggested slices (it is large for a single tick).
