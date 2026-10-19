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
per-run token'd read-only MCP on a unix socket (`_sandbox_read_mcp`,
`mode:script` only). It ships
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
Its job path is in code: inline `code` or a `code_ref`, `inputs`
staging and `code_ref` resolution (`executors/_sandbox_inputs.py`), the
PyPI lock + wheel stage (`executors/_sandbox_script.py`),
`build_script_argv`, `precis_access:read` over a unix socket, and harvest
on any exit. What follows is the design the remaining slices build on.

## The new mode

`params` for `mode:script`:

| key | type | meaning |
|---|---|---|
| `code` | str | the script (`/work/main.py`); or `code_ref` |
| `code_ref` | str | a `plaintext`, `markdown` or `folder` ref target (`kind:identifier` or a handle) whose content is the script (exactly one of `code`/`code_ref`; § Decided) |
| `block` | int | with a markdown `code_ref`: the 1-based ```` ```python ```` block |
| `entry` | str | with a folder `code_ref`: the entry file when there is no `main.py` |
| `deps` | list[str] | PEP 508 requirements from PyPI; resolved to a lock executor-side |
| `inputs` | list[str] | ref targets staged read-only under `/work/in/` (§ Decided) |
| `resources.wall_seconds` | int | required, as for build/run |
| `target_node` | str | required, as for build/run |
| `precis_access` | `none`\|`read` | `read` needs `PRECIS_SANDBOX_READ_MCP=1`; a read-only MCP on a unix socket (§ Decided) |

The image's python (3.12, the `python:3.12-slim-bookworm` code-task image)
is the only interpreter; there is no `python` param. `PARAMS_SCHEMA`
(`additionalProperties:false`, in code — no migration) carries all of the
above; `semantic_rejection` checks shapes, and `validate_submit` resolves
`code_ref` and `inputs` against the store so a bad ref refuses at put
time (the launch resolves them again). The OAuth check is skipped for
`mode:script` as for `run`.

### Decided (slice 2, coordinator 2026-10-18)

- **`code_ref`.** A plaintext ref is its whole body. A markdown ref is
  its single ```` ```python ```` fenced block; zero or several blocks
  refuse with `next=` naming `args={'block': N}`. A folder ref (a
  harvested tree, staged under `/work/code/`) needs a `main.py`, or
  refuses with `next=` naming `args={'entry': '<file>'}`.
- **`inputs`.** As specced: ref targets staged into a host dir mounted
  read-only at `/work/in`. A plaintext or markdown ref lands as its file,
  a structure as `<handle>.extxyz` plus `<handle>.cif` when ASE is
  installed, a harvested folder as its tree under `<handle>/`.
  `in/manifest.json` maps each entry to its paths. Caps: 32 entries,
  256 MB.
- **Read MCP over a unix socket, not slirp.** The per-run `precis serve
  --read-only --uds` child binds a socket in a sibling host dir
  (`<run>-mcp/`), mounted read-only at `/work/mcp`. The container keeps
  `--network none`. Scripts read `/work/mcp.json` through the staged
  stdlib client `/work/precis_client.py`, not httpx, so it needs no
  `deps` entry. The token rides the child's env, not argv. slirp is
  removed outright, which closes gr496149.
- **Read verbs only, enforced on the host side.** `--read-only` removes
  every tool but `get`/`search`/`more` from the serving process before
  it starts, and refuses the `command` profile, whose single tool
  dispatches every verb. `tests/test_sandbox_read_mcp_socket.py` shows
  `put`/`edit`/`delete`/`tag`/`link` refused over the socket. The
  child also connects as the `agent_ro` login role, so the DB refuses
  writes as well.
- **`mode:build` refuses `precis_access:read`** until a socket bridge
  ships (Claude Code's `mcp.json` has no unix-socket transport); see
  Later.
- **No package allow-list in slice 2.** The boundary is wheels only,
  lock `--no-build`, the caps (50 packages, 500 MB of wheels) and no
  network. The allow-list is revisited before slice 4.

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

3. **Todo dispatch sugar** (§ Todo sugar). Exit: tagging a todo
   `run:python` mints one job; the result lands as a comment; a successful
   run leaves the todo open.
4. **Light castor + pollux.** Ops: the sandbox play (`35-precis-worker-
   sandbox.yml`) on pollux, the `service prio` row for `job_claude_docker`
   on both, a smoke job on each. Cluster-desk's lane, run from
   `origin/prod` after deploy. The lane lights only once `origin/prod`
   contains the `--no-build` lock fix (`_sandbox_script.compile_lock`).
   Builds before that can run an sdist's build backend on the host.
   It also waits on two code items. The `--read-only` kind allow-list
   shipped (gr496511, `READ_KIND_ALLOWLIST`): fetch and paid kinds are
   refused outright, not served on cache hits. Serving cache hits for
   fetch kinds needs a cache-hit-only handler path and is not built.
   - **Async staging** (gr496426), built as a separate stage job (Reto
     OK'd witty's pick). The lock and wheel download used to run in the
     run job's claim tick and could block the worker's poll for about
     20 min. Now a `sandbox_run` row with `params.mode:"stage"` (internal:
     `semantic_rejection` refuses it at put) does the lock and download.
     It is keyed by `idem_key` = target node + sha256 of the normalised
     `deps` (the lock hash is only known after the compile, so the dep
     set keys the job and the lock hash keys the wheel dir it fills).
     It has its own lease (short, heartbeated each pass by the worker
     that runs it in a thread; a dead worker's lease expires and the row
     is reclaimed and retried under the usual attempt cap) and its own
     failure. A run job that finds no finished stage mints or reuses one
     (advisory-locked, so concurrent requests share it), requeues itself
     with a short re-check lease, and launches once the stage
     succeeded. A failed stage fails the waiting run job with the stage's
     message; the next request mints a fresh stage. `wall_seconds` and the
     run lease start at the launching claim, so staging time never counts
     against the script. Dark: no default profile or config enables it.
   - **The package allow-list review.** Decide whether `deps` gets a
     package-name allow-list, which would bound whose code reaches the
     container at all.
   Exit: the smoke job on each host reads a staged structure CIF, makes
   one `search` over the socket, writes a plot to `out/`, and its argv
   carries `--network none`. Real podman shows whether a rootless
   container can connect to the bind-mounted socket; the code is
   covered only by stubbed podman.
5. **Skill.** `precis-job-compute-help` gains a `mode:script` section: the
   params table, the network rule, how to get data in (`inputs`), how to
   read the result.

## Later

- **Socket bridge for `mode:build`.** Claude Code's `mcp.json` speaks
  HTTP to a URL, not a unix socket, so `mode:build` refuses
  `precis_access:read`. A small in-image stdio↔socket bridge (an MCP
  `command` server that relays to `/work/mcp/precis.sock`) would let it
  read again without any network route.

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
