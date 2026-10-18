---
status: draft
title: Arbitrary Python in a container — sandbox_run mode:script, from a job or a todo
pillar: platform
prio: high
model: opus
---

# Arbitrary Python in a container (sandbox_run `mode:script`)

Reto, 2026-10-18: "todo based arbitrary python in a docker seems worthy and
flexible." Decided the same morning (review answers):

- **Trigger: both.** The job is the primitive
  (`put(kind='job', job_type='sandbox_run', mode='script', …)`); a todo
  carrying a script is sugar that `dispatch` turns into that job.
- **Network: PyPI only.** Declared deps install; nothing else is reachable.
- **Read MCP: opt-in per job** via `precis_access:read`.
- **Hosts: castor + pollux.** The small Macs are a later slice (§ Macs).

## What exists

`sandbox_run` (`src/precis/workers/job_types/sandbox_run.py`, executor
`src/precis/workers/executors/claude_docker.py`) already has the spine:
mint → claim → `podman run -d` → poll by inspect → kill/rm, `/work` as the
only IN/OUT bus, no DB creds in the container, cgroup caps
(`PRECIS_SANDBOX_MEMORY/CPUS/PIDS_LIMIT`), melchior hard-excluded, a
per-run token'd read-only MCP callback (`_sandbox_read_mcp`). It ships
default-off: a `service prio` row enables the `job_claude_docker` lane
(`PRECIS_SANDBOX_ENABLED` is the legacy env gate, `cli/_worker_passes.py`).
Modes:

- `mode:build` — Claude writes code; blocked in prod on gr329258 (the vault
  OAuth token is rejected).
- `mode:run` — re-runs a *harvested* `folder` artifact from a prior build
  (`sh -c 'cd /work && uv sync && <cmd>'` on the bridge network). It cannot
  take a script directly: `artifact` must be a harvest-produced folder id,
  by design.

`mode:script` needs no Claude and no OAuth, so it does not wait on gr329258.

## The new mode

`params` for `mode:script`:

| key | type | meaning |
|---|---|---|
| `code` | str | the script (`/work/main.py`); or `code_ref` |
| `code_ref` | str | a markdown/plaintext ref or `folder` id whose content is the script (exactly one of `code`/`code_ref`) |
| `deps` | list[str] | PEP 508 requirements from PyPI; resolved to a lock executor-side |
| `inputs` | list[str] | ref ids staged read-only under `/work/in/` (slice 2) |
| `resources.wall_seconds` | int | required, as for build/run |
| `target_node` | str | required, as for build/run |
| `precis_access` | `none`\|`read` | slice 1: `none` only (refused otherwise); slice 2 adds `read` |

The image's python (3.12, the `python:3.12-slim-bookworm` code-task image)
is the only interpreter; there is no `python` param. `PARAMS_SCHEMA`
(`additionalProperties:false`, in code — no migration) and
`semantic_rejection` gain `code`, `code_ref`, `deps`, `inputs`;
`_SUPPORTED_MODES` gains `script`; the OAuth check is skipped for it as for
`run`.

Results: `/work/out/` is harvested into a `folder` ref (size-capped, same
harvest path as `mode:build`) on **every** exit. Today harvest runs only on
`succeeded ∧ exit 0`, so the executor's terminate path changes for
`mode:script`: a failing script's partial output is the debugging
evidence. stdout and stderr tails are appended as `job_event` chunks
(stdout capture is new; only stderr is logged today); `meta.exit_code` and
`meta.deps_lock_sha` are set. There is no job `result` field.

### PyPI-only network — how

Do not filter egress inside the container. Instead:

1. **Executor side (trusted, has network):** `uv pip compile
   --generate-hashes --python-version 3.12` the `deps` to a lock against
   PyPI, then `pip download --only-binary=:all: --no-deps -r lock -d
   <rundir>/wheels` (uv has no `pip download`). Sdists are refused: the
   offline container cannot fetch build deps. Only PyPI requirements are
   allowed; VCS, `file:` and direct-URL requirements are refused at submit
   with `next=`. Check that `pip` is available to the executor user on the
   host.
2. **Container:** `--network none` (replacing `_network_mode()`'s bridge
   default for this mode) and an explicit argv — a new `build_script_argv`
   beside `build_rerun_argv`, never the image CMD (`code-task-run` runs
   claude): `sh -c 'uv venv /tmp/v && uv pip install --python /tmp/v
   --offline --no-index --find-links /work/wheels -r /work/lock.txt &&
   /tmp/v/bin/python /work/main.py'`.

So the script itself never reaches the internet; data comes in via
`inputs`. The wheel dir is reused across jobs by lock sha.

### Todo sugar

A todo whose body holds a fenced ```` ```python ```` block (plus an optional
```` ```deps ```` block) and the tag `run:python` is turned, at todo put
time, into the meta that `workers/dispatch.py` already dispatches on:
`meta.executor=claude_docker`, `meta.job_type=sandbox_run`,
`meta.params={mode:script, code, deps, resources, target_node}`. Dispatch
is then unchanged: `_claim_and_dispatch` validates against the registry and
inserts the child job with `parent_id=todo` (no link; `runs` is not a
relation). Dispatch injects `auto_check: child_job_succeeded`, which closes
the todo on success; the sugar sets the todo's own `meta.auto_check` to a
non-closing check, so a successful run stays evidence and the owner decides
completion. Failure reaches the todo through the existing
`bubble_job_failure`. On any terminal status a comment carries the exit
code, the `folder` id and the stdout tail. Re-running = re-tag; each run is
a new job.

## Slices

1. **`mode:script` on the job path.** Schema, `validate_submit`
   (fail-closed: no `code` and no `code_ref`, both, a non-PyPI requirement,
   oversize code, `secrets`, `precis_access:read`), the executor-side lock
   + wheel stage, `build_script_argv` with `--network none`, harvest of
   `/work/out/` on any exit, the stdout tail. One builder, two commits:
   (a) schema/validation/argv, (b) executor stage + harvest + tests.
   Stub-podman tests like the existing sandbox tests. Exit: a fake-podman
   test runs `print(numpy.__version__)` end to end with a recorded wheel
   dir; the podman argv carries `--network none` and the explicit `sh -c`;
   a non-zero exit still harvests `out/`.
2. **`inputs` staging + `precis_access:read` for scripts.** Read access
   must not use the slirp mode: `slirp4netns:allow_host_loopback=true`
   gives general NAT egress plus every host loopback port (the same leak
   in `mode:build` is gr496149). Instead bind the per-run
   read MCP on a unix socket (uvicorn `--uds`), bind-mount it into
   `/work`, keep `--network none`, and give scripts a small client (httpx
   UDS transport). Exit: a script reads a staged structure CIF, makes one
   `search` over the socket, and writes a plot to `out/`; the argv still
   carries `--network none`.
3. **Todo dispatch sugar** (§ Todo sugar). Exit: tagging a todo
   `run:python` mints one job; the result lands as a comment; a successful
   run leaves the todo open.
4. **Light castor + pollux.** Ops: the sandbox play (`35-precis-worker-
   sandbox.yml`) on pollux, the `service prio` row for `job_claude_docker`
   on both, a smoke job on each. Cluster-desk's lane, run from
   `origin/prod` after deploy.
5. **Skill.** `precis-job-compute-help` gains a `mode:script` section: the
   params table, the network rule, how to get data in (`inputs`), how to
   read the result.

## Macs

Reto asked whether the small Macs can run these. Only through a Linux VM
(`podman machine` / colima): the rootless `agent_sandbox` user model, the
cgroup caps and `--network none` are Linux podman features, and balthazar
was dropped from S1 on 2026-09-07 for "macOS/no-podman". A Mac lane is a
separate slice after castor + pollux run cleanly: one `podman machine` per
Mac, the executor talking to it over its socket, caps applied at the VM.
Not started until a capacity need shows.

## Non-goals

- GPU access (compute-lane jobs own GPUs).
- Writes to prod from the script (no write MCP, no DB creds, ever).
- Open egress or arbitrary URL fetches from the container; use `inputs` or
  a `web`/`paper` ref fetched through `safe_get` first.
- Long-lived containers (`agent-workspace-containers.md` § Design stance).

Related: `agent-workspace-containers.md` (persistent workspaces; its slice
2 can later give `mode:script` a `workspace:` param),
`render-sandbox-network-jail.md`.
