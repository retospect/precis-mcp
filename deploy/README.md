# `deploy/` — portable cluster provisioning

This tree ships **with** precis-mcp and knows how to stand up and redeploy a
precis cluster on any set of hosts. It is deliberately **cluster-agnostic**:
nothing here names a real machine, address, or secret. Everything specific to
*your* cluster lives in a small local overlay that this repo never sees.

Design-of-record: [`docs/backlog/factory-console-and-scheduling.md`](../docs/backlog/factory-console-and-scheduling.md)
(open scope; shipped portion in the `precis.workers` docstring + git history). This directory is **slice 12a** —
the repo rationalization, **complete since 2026-07-19**: `scripts/deploy` runs
from this tree by default, and it is the authoritative deploy path. The only
per-cluster piece is the gitignored local overlay (`deploy/inventory/` +
`deploy/.vault-pass`).

## Fresh cluster from zero

1. Copy `deploy/inventory.example/` → your private overlay per
   ["Setting up the overlay"](#setting-up-the-overlay-operator) below; fill
   in hosts + `all.vars` + the `precis_capabilities` map
   (`topology.example.yml`); encrypt `vault.yml` with `ansible-vault`
   (key list: `inventory.example/group_vars/all/vault.yml.example`).
2. `ansible-playbook bootstrap-macos.yml` — once per fresh macOS node, over
   LAN.
3. `ansible-playbook bootstrap-tailscale.yml --ask-vault-pass` — once; after
   this, everything runs over Tailscale.
4. `ansible-playbook site.yml` — full converge; imports the numbered
   playbooks in order.
5. Ongoing code pushes: `scripts/deploy [ref]` (wraps `redeploy-precis.yml`;
   ping-gate + report — see ["Canary-staged deploys"](#canary-staged-deploys-opt-in)).
6. App secrets: once the web role is up, load API keys via the `/secrets`
   page or `precis secret set` — **not** the ansible vault (scope boundary:
   `inventory.example/group_vars/all/vault.yml.example`).

## The three layers (by rate-of-change)

| Layer | Lives in | Changes | Public? |
|-------|----------|---------|---------|
| **Provisioning recipes** — roles + playbooks | `deploy/roles`, `deploy/playbooks` | rarely | ✅ yes (this tree) |
| **Placement overlay** — which host is what, network facts, secrets | `deploy/inventory/` (local) | per-cluster | ❌ **never** |
| **Runtime discovery** — what's actually installed, live prio | the worker + `service_config` in the DB | continuously | n/a |

Roles reference **inventory variables and capability groups**, never a literal
hostname or IP — so the recipes are the same everywhere and only the overlay
differs.

## Runtime topology (what runs where)

- **One collapsed worker per host.** Every host runs a single
  `precis worker --profile all` unit (union of the historical `system` +
  `agent` rotations), rendered by `playbooks/20b-precis-worker-collapsed.yml`
  through the `service_unit` role (one abstract spec → launchd plist or
  systemd unit; imported by `site.yml` + `redeploy-precis.yml`). The split
  system/agent units are retired (`retire-split-agents.yml`); the legacy
  playbooks `20-precis-worker.yml` / `37-precis-worker-agent.yml` stay on
  disk as per-host rollback, no longer imported. Profile is pass
  *ownership* only — the live on/off switch is the `service_config` row,
  not a plist env flag (see below).
- **Compute-lane exception (GPU nodes).** Each `compute`-group host (the DGX
  twins; the `inference` group is empty — its GPU node was permanently
  retired from cluster duty 2026-08-29 via
  `playbooks/retire-node-precis-units.yml`) runs
  one extra tiny worker alongside its collapsed/heartbeat unit:
  `precis-worker-compute.service`, `--only job_ssh_node`, polling every few
  seconds (`playbooks/43-precis-worker-compute.yml`). The collapsed worker's
  `run_loop` is a strictly-serial round-robin, so a slow pass starves
  `job_ssh_node` — the submit/poll executor driving every node-pinned
  detached GPU job — down to ~1 claim per multi-minute rotation. Safe
  alongside the collapsed worker: claims are `FOR UPDATE SKIP LOCKED` and
  the capacity-1 `gpu` resource slot still serializes GPU compute; distinct
  `PRECIS_PROCESS=precis-worker-compute` identity in `worker_logs`.
- **Agent-lane placement.** `job_claude_inproc` / `quota_check` claim only
  where OAuth + `PRECIS_MCP_CONFIG` exist (the gateway); minting is
  cluster-wide. The opus reviewers fire via fleet-wide scheduler leases,
  with eligibility (`PRECIS_STRUCTURAL_REVIEW` / `PRECIS_DEEP_REVIEW`)
  scoped by the collapsed-unit template to the gateway + inference hosts;
  `dream_agent` / `anki_sync` are host-pinned cadences on the gateway.
- **No standalone thin timers.** The old heartbeat/cron/watch/dream/anki
  launchd+systemd timers are retired (`retire-thin-timers.yml`); recurring
  work rides the worker's own `scheduler` pass, and the per-host heartbeat
  is a worker pass + dedicated thread (`src/precis/workers/heartbeat.py`).
- **Enable-flag lifecycle.** Live run control is `service_config`
  (`precis service prio|model|seed|clear|list`). Worker roles run
  `precis service seed` (INSERT-if-absent) before stripping a retiring
  `PRECIS_*_ENABLED` plist flag, so a cutover is behaviour-preserving and a
  console override set after first-seed survives every later redeploy.
- **Secrets discipline.** Deploy-run daemons render password-free
  `postgresql://…` DSNs; libpq resolves the password from `~/.pgpass` (the
  `pgpass` role). Documented in-template exceptions: env vars a third-party
  tool reads directly as a raw password (not a libpq DSN). Agentic daemons
  resolve `CLAUDE_CODE_OAUTH_TOKEN` env → vault → `~/.secrets/pw/` — no
  per-user token files (redeploy purges the known service-account paths).
- **Bounce discipline.** Roles use the idempotent `launchctl load -w`
  (no-op when loaded); a real reload-with-new-env fires only via each
  role's `notify` handler through the shared `tasks/reload_launchd.yml`.
  `scripts/restart-worker-and-watch` picks `launchctl kickstart -k` vs
  `systemctl restart` per host OS.
- **CPU fencing.** Spawned job containers splice `container_limit_flags()`
  (`PRECIS_JOB_CPUSET` / `PRECIS_JOB_CPU_SHARES`; a container doesn't
  inherit the worker's `nice`); systemd inference units carry matching
  `Nice`/`CPUAffinity`, macOS plists `Nice`/`LowPriorityIO` (worker plists
  stay `ProcessType=Interactive` for jetsam).
- **Deploy cadence.** A fix only helps once deployed — a spin-loop /
  ref_events alert spike on prod usually means "redeploy" (check the
  deployed sha under the deploy user's `~/.cache/uv/git-v0/checkouts/`),
  not a new bug.

## Canary-staged deploys (opt-in)

`scripts/deploy` runs one `ansible-playbook redeploy-precis.yml` pass against
the whole fleet by default — every health probe in the playbook is
`failed_when: false`, so the in-run convergence assert is the only hard gate.
Set `PRECIS_DEPLOY_CANARY=<ansible-host-name>` (e.g. `scheduler`, the least-
special collapsed-worker host) to stage that rollout instead: the target sha
is resolved once (`git ls-remote`, the same resolution the playbook's own
step-0 pin does) and pinned via explicit `-e precis_worker_git_ref=` /
`precis_web_git_ref=` / `precis_embedder_git_ref=` on **both** phases below —
an explicit `-e` beats the playbook's step-0 `set_fact` pin, so a `main` that
moves mid-rollout can't split the two phases onto different commits.

1. `ansible-playbook redeploy-precis.yml --limit <canary>` — the canary only.
2. Verify: poll `scripts/prod-psql` for the canary's `host_heartbeat` row —
   green once `ts` is newer than when phase 1 started *and* under 120s old
   (the deployed worker came up and is heartbeating on the new code), timeout
   `PRECIS_DEPLOY_CANARY_TIMEOUT_S` (default 300s). A `scripts/prod-psql`
   failure during the poll is treated as red (fail closed), same as a timeout.
3. Green → `ansible-playbook redeploy-precis.yml --limit 'all:!<canary>'` — the
   rest of the fleet, same sha pins. Red → abort non-zero **before** touching
   the fleet, with a loud mixed-state warning (canary on new code, fleet on
   old) and a rollback line (`scripts/deploy` from the previous sha with
   `PRECIS_DEPLOY_CANARY=<canary>`, which targets just the canary).

`PRECIS_DEPLOY_CANARY_DB_HOST` overrides the `host_heartbeat.host` value
polled in step 2, for the rare case where it differs from the ansible host
name (fqdn vs short, or a custom `precis heartbeat --host`) — default is the
canary name itself. Unset (the default), `scripts/deploy` is byte-identical
to the single-pass behavior above; no inventory group, no playbook change —
the host comes from the env var.

## The secret boundary (read before you commit)

precis-mcp is a **public** repository. A commit that leaks the real cluster's
Tailscale IPs, LAN addresses, node hostnames, or the encrypted vault is
**irreversible** — a public git push is forever, even after a later delete.

Therefore:

* **`deploy/inventory/`** — your live overlay (real `hosts.yml`,
  `group_vars/all/vault.yml`, `.vault-pass`, …). It is **gitignored** and
  local-only — real files in the main checkout's `deploy/` (no symlink). It is
  **never** committed and is **skipped** by the leak-gate.
* **`deploy/inventory.example/`** — the scrubbed template that documents the
  overlay's *shape* using RFC-5737 documentation addresses and placeholder
  node names. This **is** tracked, and it must stay scrubbed.
* **`tests/test_deploy_tree_no_secrets.py`** — the leak-gate. It runs in the
  normal `scripts/ship` pytest gate and fails the ship if any tracked file
  under `deploy/` contains a real IP (the Tailscale CGNAT range or the private
  LAN range), a real node hostname, the tailnet name, or an ansible-vault
  blob. A secret can never reach a push because it can never get past the gate.

## Setting up the overlay (operator)

Put your private overlay as **real files** in the **main checkout's** `deploy/`
(both paths are gitignored, so they never reach the public repo — no symlinks):

```sh
# one-time, in the MAIN checkout (not a worktree):
cp -R /path/to/your/private/inventory   deploy/inventory
cp    /path/to/your/private/.vault-pass deploy/.vault-pass
chmod 600 deploy/.vault-pass
```

`scripts/deploy` is install-from-tree by **default**. It resolves the overlay
symlink-free and checkout-independently: it uses **this** checkout's
`deploy/inventory` if present, otherwise falls back to the **main checkout's**
`deploy/inventory` (found via `git --git-common-dir`), or `$PRECIS_OVERLAY_DIR`.
So a deploy works from any worktree — `/go` ships+deploys from one — with the
overlay stored in exactly one place and no per-worktree secret copies.

> **Back up `.vault-pass`.** It is the only key to the ansible-encrypted
> `vault.yml`; it has no git history and no remote. Keep a copy in your password
> manager — if the main checkout is lost, the vault is unrecoverable without it.

## Sharing the overlay between operators

When more than one operator drives deploys, sync `inventory/` through a
**private git repo on shared cluster infra** (a bare remote on the NAS or a
node you both SSH to — never GitHub; a public push of real IPs/hostnames is
irreversible), and distribute `.vault-pass` **out-of-band** through 1Password.
Ciphertext (the already-vault-encrypted `vault.yml`) lives in git; the plaintext
key that unlocks it never does — so an exposed remote alone can't decrypt the
vault.

The overlay repo is laid out to match `scripts/deploy`'s resolution: its root
holds `inventory/` (what `$PRECIS_OVERLAY_DIR` points at) with `.vault-pass`
as `inventory/`'s sibling.

```sh
# 1. Create the bare remote once, on a host you both SSH to:
ssh node-gateway 'git init --bare ~/precis-overlay.git'

# 2. Operator with the live files seeds it:
mkdir ~/precis-overlay && cd ~/precis-overlay
cp -R /path/to/main/deploy/inventory ./inventory   # NOT .vault-pass
git init && git add -A && git commit -m 'cluster overlay'
git remote add origin node-gateway:precis-overlay.git
git push -u origin main

# 3. Each operator points deploys at the clone (shell profile):
git clone node-gateway:precis-overlay.git ~/precis-overlay   # 2nd operator
export PRECIS_OVERLAY_DIR=$HOME/precis-overlay/inventory

# 4. Each operator drops the key from 1Password (out-of-band, once):
#    → ~/precis-overlay/.vault-pass
chmod 600 ~/precis-overlay/.vault-pass
```

Thereafter `git pull` in `~/precis-overlay` is the whole sync. `.vault-pass`
changes only on a vault re-key — hand it over through 1Password, not the repo.
(The second operator also needs `~/.ssh/cluster`, the deploy key `ansible.cfg`
references — likewise shared out-of-band, not here.)

## Migration status

Populated so far:

- [x] leak-gate (`tests/test_deploy_tree_no_secrets.py`)
- [x] gitignore + `deploy/inventory.example/` scrubbed template
- [x] portable roles — **48 / 49** through the gate (only `litellm` left,
      deferred: it retires in slice 7)
- [x] portable playbooks (48) + `redeploy-precis.yml` + `site.yml` +
      `run-*.yml` + `bootstrap-*.yml` + `ansible.cfg`
- [x] `scripts/deploy` install-from-tree is the **DEFAULT** (2026-07-19); roll
      back to the legacy checkout with `PRECIS_DEPLOY_FROM_TREE= scripts/deploy`
- [x] top-level `tasks/reload_launchd.yml` carried (the shared safe-launchd-reload
      include every persistent-daemon handler pulls via `role_path/../../tasks/`)
- [x] `service_unit` role — §15h's multiplatform launch-unit abstraction (one
      abstract spec → launchd plist **or** systemd unit). Dark: no playbook
      includes it yet. `roles/service_unit/examples/collapsed-worker.yml` is
      the authored single-collapsed-worker spec (slice 10; the Phase-2 window
      swaps the four hand-written worker plists for this one delegation, and
      the retired `PRECIS_*_ENABLED` flags → `service_config.prio`)
- [x] Phase-2 drift carried in (2026-07-19): `precis_worker_agent` role
      (run-as deploy + colima autostart + Linux/systemd review-worker branch +
      container-executor env + autocatpath route), `playbooks/37` (`+inference`),
      `site.yml` (retire imports 30/39), new `playbooks/retire-thin-timers.yml`.
- [x] `ansible --check` rehearsal against the wired overlay: both trees resolve
      the same 4-host plan; the `precis-worker-agent` play converges — the only
      delta is scrubbed **comment** text in the inference node's rendered systemd
      unit (zero functional directives differ); self-heals on the first deploy.
- [x] overlay var aliases added (`~/work/cluster/inventory`, slice-12a commit):
      `postgres_host`/`gateway_host` + `nas_*` over the `finnmaccool_*` facts —
      additive, both trees resolve identically. (Full `finnmaccool_* → nas_*`
      *rename* still deferred to when the legacy tree is deleted.)
- [x] retired `06-litellm.yml` + its `site.yml` entry (2026-07-26, the central
      `:4000` proxy teardown) — the `litellm` role itself was never carried
      into this in-repo tree (only referenced by name), so there was nothing
      to `git rm` there.
- [x] **switched** (2026-07-19): default flipped + a full tree-deploy landed
      green on all 4 nodes and was health-verified (Phase-2 scheduler live).
- [x] **demoted** (2026-07-19): `~/work/cluster` retired; its roles/playbooks are
      the in-repo `deploy/` tree. The overlay (real `inventory/` + `.vault-pass`)
      moved into the main checkout's `deploy/` as gitignored files, resolved from
      any worktree via the `git --git-common-dir` fallback in `scripts/deploy`.

Overlay variables the portable roles expect (define these in your local
`deploy/inventory/`): `postgres_host`, `gateway_host`, `nfs_server`, plus
`nas_host` / `nas_mount_base` / `nas_nfs_export` / `nas_mount_name` and the
`precis_capabilities` map. The `deploy/inventory.example/` templates show
every one.

Never bulk-copy from the private checkout: move one file, run the gate, commit.
