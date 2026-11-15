# Builder hosts — run_slice/land_slice on every builder-capable host (gr676054)

WIP spec (sparks-builder). Survey below is code-verified to file:line unless
marked UNVERIFIED. Vendor CLI linux-arm64 claims are web-verified
(2026-11-14): all four ship arm64-linux builds.

## Survey

### macOS-only assumptions on the run_slice / land_slice / scripts/test path

- `src/precis/workers/job_types/run_slice.py:2970` `_codex_writable_roots`:
  docker socket fallback is `Path.home()/".colima"` only — on Linux with no
  DOCKER_HOST no `--add-dir` is granted for `/var/run/docker.sock` (graceful,
  but codex's sandbox will deny docker there the same way me570526 measured
  on colima — needs a `/var/run/docker.sock` parent grant + live check).
- `run_slice.py:899-962` `_darwin_process_identity` / `_process_alive`:
  already dual-platform (Linux handles carry `boot_id`/`start_time` from
  `/proc`, Darwin carries `ps_identity`). Same guard in
  `land_slice.py:2707,3517,4299`. No change needed.
- `capability_probe.py:701-780` `_darwin_mem_total_mb`: darwin-only branch
  already guarded by `sys.platform`; Linux reads `/proc/meminfo`.
- `scripts/test`: portable. bash 3.2 floor; `shasum`-or-`sha256sum` fallback
  (`scripts/test:1110`); all container work via `docker compose`;
  `scripts/lib/gate-slot.sh:139-146` branches `uname -s` (sysctl on Darwin,
  `/proc/meminfo`+`nproc` on Linux); `docker-disk.sh` `colima ssh` is a
  fallback leg only. Gate slots live in the shared git common dir → already
  per-host.
- Deploy defaults hard-code `/Users/deploy`: `precis_fix_repo_dir` default <!-- home-path: allow — the deploy user's fixed home on every host -->
  `/Users/deploy/precis-fix-repo` in `20b/20d/20e/20g` playbooks; <!-- home-path: allow — same -->
  `provision.yml` (`precis_worker_agent`) gates nearly every task on
  `os_family == "darwin"` including the fix-repo clone (line ~202-217),
  pgpass verify, colima LaunchDaemon, mactex, `~/.claude/mcp.json`.
- `precis_worker/provision.yml` is already dual-OS (venv via
  `/root/.local/bin/uv` on Linux, python.org 3.12 symlink on darwin).
- `_launch` (`run_slice.py:~840`): `/bin/sh -c` detached + `mem_limit_preexec`
  (RLIMIT_DATA, `utils/container_limits.py:216`) — portable.
- Auth: secrets are DB-vault + `~/.secrets/pw/<NAME>` file — platform-neutral
  (`src/precis/secrets.py`, `utils/claude_oauth.py:56`). `factory_git.py`
  credential helper rides `GIT_CONFIG_*` env — portable. No `security`
  keychain call on the run path; osxkeychain is explicitly reset
  (`factory_git.py:158`).
- Codex sandbox: `--sandbox workspace-write --add-dir <git common> --add-dir
  <docker sock parent>` (`_slice_adapters.py:574-596`). Whether codex's Linux
  sandbox (landlock) permits a mounted unix socket connect is UNVERIFIED.
- `.mcp.json` pins `http://127.0.0.1:8765/mcp`. On a remote builder the
  session MCP is absent → claude lane (`adapter.mcp=True`) needs the serving
  host reachable: either an ssh tunnel (ssh_tunnels role) or a
  `PRECIS_MCP_URL`-style override the worker rewrites into the worktree
  `.mcp.json` at submit. RESOLVED — tunnel: mcp-http's Caddy publishes
  `127.0.0.1:8765` only (`deploy/mcp-http/precis-mcp-http-ensure.sh:147`,
  only the proxy publishes, loopback), so a URL override needs a bind
  change AND puts the bearer surface on the LAN; a `ssh -L
  127.0.0.1:8765:127.0.0.1:8765` keepalive keeps `.mcp.json`
  byte-identical everywhere and the surface loopback-only.
- `PRECIS_MCP_SLICE_TOKEN_DIR` = `<mcp-http state>/slice-tokens` on the
  MCP-serving host only (`deploy/mcp-http/precis-mcp-http-ensure.sh:765`).
  Remote hosts mint nothing → session keeps the shared token (acceptable)
  unless the dir is NFS-shared; kimi/codex/devin lanes need no grant
  (`adapter.mcp=False`), scoped read grant is optional.

### How the agent host is provisioned today

- `20g-precis-worker-slices.yml`: one launchd unit (service_unit role)
  `precis worker --only job_ssh_node --only plan_tick --batch-size 1`, env
  `/opt/precis/venv`, `HOME=/Users/deploy`, `PGPASSFILE`, `PRECIS_DEVIN_BIN`, <!-- home-path: allow — same -->
  `DOCKER_HOST=unix://~/.colima/docker.sock`, DSN `agent_rw@pgbouncer`,
  cost caps, `PRECIS_SLICE_ROOT` (default `precis_fix_repo_dir`), embedder,
  optional `PRECIS_MCP_SLICE_TOKEN_DIR` + `PRECIS_CLAUDE_BIN` wrapper
  (`/opt/precis/bin/claude-run-user`).
- `service_unit` role: already dual-OS — `launchd.plist.j2` /
  `systemd.service.j2`, dispatched on `os_family`; linux renders keepalive
  daemons only + supports `service_unit_env_files`.
- `precis_worker_agent/tasks/provision.yml`: fix-lane clone
  (`ansible.builtin.git` → `precis_fix_repo_dir`, `become_user: deploy`) —
  darwin-only + `gateway` group + `precis_fix_lane_enabled` gated.
- `agent_clis` role + `files/agent_cli.py`: report/update claude
  (`npm i -g @anthropic-ai/claude-code@<pin>`), codex
  (`@openai/codex@<pin>`), kimi (`@moonshot-ai/kimi-code@<pin>`), devin
  (`devin update`, unpinnable). Pins come from overlay vars
  (`agent_cli_pin_*`); needs npm on the host — gr543827.
- Lane auth: claude = vault OAuth (`ensure_oauth_token`, claude-oauth probe);
  kimi = `KIMI_MODEL_*` env from vault key (`_kimi_launch_env`,
  run_slice.py:3156); codex = per-host ChatGPT login `$CODEX_HOME/auth.json`;
  devin = `devin auth login` + `trusted_workspaces.json` (preflight seeds
  `.claude/worktrees`, `utils/devin_trust.py`).
- Linux precedent: `43-precis-worker-compute.yml` already runs
  `--only job_ssh_node` on compute/inference hosts via systemd
  (`service_unit_env_files: /etc/precis/{autocatpath,dft}.env`), venv from
  20b/20c `precis_worker/provision`.

### linux-arm64 CLI builds (verified 2026-11-14)

- claude-code: YES — `@anthropic-ai/claude-code` resolves a per-platform
  optional dep; `@anthropic-ai/claude-code-linux-arm64` is published
  (npmjs.com/package/@anthropic-ai/claude-code-linux-arm64). Node ≥22 to
  install via npm; binary itself is native.
- codex: YES — `@openai/codex` publishes platform payloads under dist-tags
  (`@openai/codex@linux-arm64` → `aarch64-unknown-linux-musl`); GitHub
  releases carry `codex-aarch64-unknown-linux-musl.tar.gz`.
- kimi-code: YES — releases carry `kimi-code-linux-arm64.tar.gz` (seen on
  the 2.6.x release asset lists); install.sh or
  `npm i -g @moonshot-ai/kimi-code` (Node ≥22.19) both work.
- devin: YES — `cli.devin.ai/install.sh` supports Linux x64 and ARM64
  (docs.devin.ai/cli troubleshooting + install docs); `devin update`
  self-updates in place under `~/.local/share/devin/cli/_versions`.
  Sandbox on Linux needs `bwrap`+`socat`; slices don't use `--sandbox`, so
  not a blocker.

### Lane advertisement + host pick

- `capability_probe._PROBES` → `resource_slots` rows per host:
  `repo_checkout` (slice_root()+`.git`), `claude_bin`, `git`,
  `claude_config_mount`, `gpu`, `podman`, `tts`, `embedder`, `clones_dir`,
  `autocatpath`, `has_mace` (capability_probe.py:474).
- `ServiceSpec.placement` (registry.py:1398+): run_slice/lane_health
  `{repo_checkout, claude_bin, git}`; land_slice + release/ci/pr ops
  `{repo_checkout, git}`. Check-only at claim (`_common._placement_ok`);
  `params.target_node` pins, else claiming host must advertise all.
- GAP: run_slice requires `claude_bin` even for non-claude lanes → a
  kimi/codex/devin-only host can never claim. Needs lane-aware placement
  (params.lane → per-adapter capability tokens) or a `builder` token = any
  lane binary.
- Executor cap: `claim.executor_cap.<executor>` in app_state — GLOBAL value,
  per-host count (`lease_host = claim_host`, `_common.py:2287-2307`). Code
  default `ssh_node:8`, reserve `{run_slice:1, land_slice:1}`. The
  `put(kind='job', mode='setting')` door validates leaf ∈ EXECUTOR_PROVIDES —
  no per-host suffix exists today → per-host caps need
  `claim.executor_cap.ssh_node.<host>` (code change) or a unit env override.
- Lane caps (`plans/lanes.py:535`): global per-lane concurrency counted over
  live run_slice rows; `pick_lane` cheapest-fit free lane; `_HostRefusal`
  stamps `meta.host_refusal` → lane backoff (`HOST_REFUSAL_BACKOFF`).

## Design (as built)

- New role `precis_builder_host` (not an extension of `precis_worker_agent`
  provision — that file's tasks are darwin-gated one by one and the builder
  surface is a different concern): fix-repo clone at `precis_fix_repo_dir`
  (per-OS deploy home), docker (`docker.io` + `docker-compose-v2` on Linux
  only when absent, deploy→docker group; colima stays the Macs' own
  provisioning), the slice-MCP ssh `-L` keepalive unit, and the
  `worker-builder` unit via `service_unit` (`--only job_ssh_node`, NO
  `plan_tick` — one ticker stays on the gateway; `KillMode=process` on
  systemd so a bounce can't SIGKILL detached slice sessions, same as 43).
  Env mirrors 20g's parity keys with per-OS paths.
- Playbook `20i-precis-builder-hosts.yml` targets new group
  `builder_hosts` (+gateway as batch anchor) and imports
  `precis_worker/provision` first (same venv + service seed, idempotent).
  Lane CLIs stay out of the role — `agent_clis` is the one door
  (`scripts/agent-cli-update --host <h> --apply` after this play).
- `redeploy-precis.yml` imports `20i-precis-builder-hosts.yml` after the
  compute lane (untagged, gateway-anchored; an empty `builder_hosts` is a
  no-op): every release refreshes the builder venv to the pinned sha and
  the role bounces `precis-worker-builder`. The orphan sweep lists the
  unit as owned, so it is not restarted twice nor drift-flagged.
- Portability changes landed (fedc700f5 + bd017a354):
  `/var/run/docker.sock` fallback in `_codex_writable_roots`; per-host
  suffix on `claim.executor_cap`/`claim.reserve` keys validated by the
  claim-setting door; lane-aware placement (probe per adapter:
  `claude_bin`/`codex_bin`/`kimi_bin`/`devin_bin`; run_slice placement is
  `{repo_checkout, builder, git}` with `builder` = any lane binary —
  `placement_requires` substitutes the pinned/picked lane's token, an
  unpinned pick filters lanes to bins the host has, a pinned lane whose
  bin is absent parks host-locally with no `host_refusal` stamp).

## Builder user

Builder hosts run the worker, the lane CLIs, the checkout and the MCP tunnel
as a dedicated least-privilege account, `coder` (`precis_builder_user` in
`group_vars/builder_hosts.yml`), not `deploy`. `deploy` stays the default, so
the gateway is unchanged. `agent_cli_user` follows `precis_builder_user` on
`builder_hosts` members, so the CLI report/install runs as the same account.

What `roles/precis_builder_host` makes of it:

- Linux: a system user, home `~coder` mode 0700, shell `/bin/bash`, locked
  password, no sudo, no supplementary groups (never `docker`: that group is
  root-equivalent). macOS: a standard (non-admin) account with its own primary
  group, created with `dseditgroup` / `sysadminctl` / `createhomedir`; the role
  fails if the account is in `admin`. The macOS path is written against the
  stock tools but not yet run on a Mac: the first Mac canary is its test.
- Docker without the docker group. Linux: rootless Docker for `coder`
  (`uidmap`, `docker-ce-rootless-extras`, subuid/subgid ranges,
  `dockerd-rootless-setuptool.sh install --force`, `loginctl enable-linger`);
  the unit env sets `DOCKER_HOST=unix:///run/user/<uid>/docker.sock` and
  `XDG_RUNTIME_DIR`. run_slice forwards `DOCKER_HOST` to the session and
  `--add-dir`s the socket's parent for codex (`_codex_writable_roots`;
  `/run/user/<uid>` is neither `$HOME` nor an ancestor, so it is granted).
  Needs Docker's apt repository on the host (DGX OS has it). macOS: a colima VM
  per user, kept up by a `builder-colima` LaunchDaemon run as `coder`; the unit
  env points `DOCKER_HOST` at its socket under `~coder/.colima`.
- `~coder/.pgpass` with one DB login: the role the worker's DSN uses
  (`agent_rw`, `precis_system` after the db-own cutover), rendered from the same
  vault variable as deploy's by `roles/pgpass/tasks/user.yml`. No admin,
  read-only or owner line. Nothing secret is in the repo.
- The checkout (`PRECIS_SLICE_ROOT`), slice worktrees, tunnel key and
  known_hosts are under `~coder` and owned by it. The tunnel key's line in the
  MCP host's deploy `authorized_keys` is `restrict,port-forwarding,
  permitopen="127.0.0.1:<port>",command="/bin/false"`: it opens the one
  forward and nothing else.
- The worker unit runs as `coder` (`service_unit_run_as`).

What `coder` can touch: its own home, the checkout and worktrees, its rootless
Docker (or colima) daemon and anything it runs there, the worker venv
(read-only, `/opt/precis/venv`), the DB as the one role in its `.pgpass`,
anything that DB role can read or write (including the vault values that role
may reveal, which is how the worker gets OAuth / KIMI / git tokens), and the
tunnel to the MCP port.

What it cannot touch: `deploy`'s home (0750 deploy), deploy's vault file and
ssh keys, `sudo`, the root-equivalent `docker` group and the system docker
socket, other users' processes, and a shell on the MCP host.

Residual risk, stated plainly: `coder` holds the DB password the worker needs
(`agent_rw`, or `precis_system` after the cutover), and an agent session
running as `coder` can read `~coder/.pgpass`. The account bounds the
host-level blast radius (no root, no deploy credentials, no docker group), not
the database-level one. Splitting the worker (runs as `deploy`, holds the
password) from the CLI sessions (run as `coder`, hold none) is a follow-up.

## Per-host sizing

`claim.executor_cap.ssh_node.<host>` default: `(free cores − 2) / 2` for
pytest-heavy work, floor 1; overridden per host where gr647996 footprint
admission gives a signal. Set through the claim-setting door (see
Coordinator steps); no key → the code default (8) and the unsuffixed
`claim.executor_cap.ssh_node` apply.

## Canary plan

The GPU node first with one kimi slot → the second DGX node → the local-serving node → non-DB Macs → DB host
last, conservative cap.

## Coordinator steps (do not run here)

- inventory: add `[builder_hosts]` the GPU node, the second DGX node, the local-serving node, <macs>, <db-host>
  (canary order — the GPU node first with one kimi slot, DB host last with a
  conservative cap).
- group_vars/builder_hosts.yml:
  ```yaml
  # The mcp-http serving host's inventory name — required; the role
  # refuses a remote host without it and skips the tunnel on the server.
  # On the MCP host itself set it to that host's OWN inventory name —
  # the default '' fails loud there; set, the tunnel leg no-ops.
  precis_builder_mcp_host: <mcp-host-inventory-name>
  # The dedicated builder account (see "Builder user"); default deploy.
  precis_builder_user: coder
  # The checkout defaults to precis-fix-repo under the user's home on both
  # OS families; override precis_fix_repo_dir only to move it.
  ```
- host_vars per host (canary): `agent_cli_pin_*` pins if they diverge
  from group pins. The per-host cap is NOT an ansible var — it is a
  `claim.executor_cap.ssh_node.<host>` app_state key set through the
  claim-setting door below (the door audits; a hostvar would sit inert).
- Prereqs on a fresh host, in order: 00-users and 19-pgpass for `deploy`
  (the `coder` account and its `.pgpass` come from this play), then this
  playbook (`scripts/host-ops/play deploy/playbooks/20i-precis-builder-hosts.yml`
  — it pins `precis_worker_git_ref` itself); node ≥22 if the npm-door
  lane CLIs are wanted; then
  `scripts/agent-cli-update --host <h> --apply` per canary host.
- Per-host caps through the claim-setting door (heartbeat host name —
  `precis status`'s host column — is the `<host>` suffix):
  `put(kind='job', mode='setting', args={'key':
  'claim.executor_cap.ssh_node.<host>', 'value': <cap>, 'reason': 'gr676054 builder canary'})`;
  optional `claim.reserve.ssh_node.<host>` replaces the reserve map on
  that host.
- vault/secret wiring identical to the gateway's (OAuth, KIMI key,
  factory git token come from the DB vault — no per-host copy needed
  once DSN+pgpass land).
- codex/devin/claude logins per host are vendor flows — one-time per
  host, on the builder account (`coder`; `deploy` where it is the builder).
- Decide: NFS-share `slice-tokens` dir or accept shared-token
  attribution (spec survey; shared token is the default).
- `builder_hosts` IS in the redeploy flow (20i is imported after 43):
  each release refreshes the builder venv to the pinned sha and bounces
  the unit, so worker code cannot drift from prod (the gr489462 skew).
  Populating the group is the only step; an empty group changes nothing.
  `scripts/deploy` pings `builder_hosts` with the other lane groups, so
  an unreachable builder is skipped like any non-critical host.
- The tunnel's authorized_key leg lands `precis-builder-tunnel@<host>`
  in the MCP host's deploy authorized_keys via delegate_to — needs
  ansible reachability to the MCP host (already true).
