# `deploy/` — portable cluster provisioning

This tree ships **with** precis-mcp and knows how to stand up and redeploy a
precis cluster on any set of hosts. It is deliberately **cluster-agnostic**:
nothing here names a real machine, address, or secret. Everything specific to
*your* cluster lives in a small local overlay that this repo never sees.

Design-of-record: [`docs/design/factory-console-and-scheduling.md`](../docs/design/factory-console-and-scheduling.md)
§15 (convergence) and §16 (two-phase build). This directory is **slice 12a** —
the repo rationalization, **complete since 2026-07-19**: `scripts/deploy` runs
from this tree by default, and it is the authoritative deploy path. The only
per-cluster piece is the gitignored local overlay (`deploy/inventory/` +
`deploy/.vault-pass`).

## The three layers (by rate-of-change)

| Layer | Lives in | Changes | Public? |
|-------|----------|---------|---------|
| **Provisioning recipes** — roles + playbooks | `deploy/roles`, `deploy/playbooks` | rarely | ✅ yes (this tree) |
| **Placement overlay** — which host is what, network facts, secrets | `deploy/inventory/` (local) | per-cluster | ❌ **never** |
| **Runtime discovery** — what's actually installed, live prio | the worker + `service_config` in the DB | continuously | n/a |

Roles reference **inventory variables and capability groups**, never a literal
hostname or IP — so the recipes are the same everywhere and only the overlay
differs.

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
`deploy/inventory/`): `postgres_host`, `gateway_host`, `litellm_host`,
`redis_host`, `nfs_server`, plus `nas_host` / `nas_mount_base` /
`nas_nfs_export` / `nas_mount_name` and the `precis_capabilities` map. The
`deploy/inventory.example/` templates show every one.

Never bulk-copy from the private checkout: move one file, run the gate, commit.
