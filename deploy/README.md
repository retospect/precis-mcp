# `deploy/` — portable cluster provisioning

This tree ships **with** precis-mcp and knows how to stand up and redeploy a
precis cluster on any set of hosts. It is deliberately **cluster-agnostic**:
nothing here names a real machine, address, or secret. Everything specific to
*your* cluster lives in a small local overlay that this repo never sees.

Design-of-record: [`docs/design/factory-console-and-scheduling.md`](../docs/design/factory-console-and-scheduling.md)
§15 (convergence) and §16 (two-phase build). This directory is **slice 12a** —
the repo rationalization. It is being populated incrementally; until the
migration completes, the authoritative deploy path is still the operator's
private ansible checkout (`scripts/deploy` → `$PRECIS_CLUSTER_DIR`).

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
  local-only. Point it at your private cluster inventory (symlink or clone).
  It is **never** committed and is **skipped** by the leak-gate.
* **`deploy/inventory.example/`** — the scrubbed template that documents the
  overlay's *shape* using RFC-5737 documentation addresses and placeholder
  node names. This **is** tracked, and it must stay scrubbed.
* **`tests/test_deploy_tree_no_secrets.py`** — the leak-gate. It runs in the
  normal `scripts/ship` pytest gate and fails the ship if any tracked file
  under `deploy/` contains a real IP (the Tailscale CGNAT range or the private
  LAN range), a real node hostname, the tailnet name, or an ansible-vault
  blob. A secret can never reach a push because it can never get past the gate.

## Setting up the overlay (operator)

```sh
# one-time: point the local overlay at your private cluster inventory + vault pass
ln -s ~/work/cluster/inventory   deploy/inventory
ln -s ~/work/cluster/.vault-pass deploy/.vault-pass
# (or copy them if you prefer detached working copies — both stay gitignored)
```

`scripts/deploy` runs ansible from this tree **only when
`PRECIS_DEPLOY_FROM_TREE` is set** (dark). Unset — the default — it uses
`$PRECIS_CLUSTER_DIR` (your private checkout) exactly as before, byte for byte.
Flip the flag to rehearse install-from-tree; make it the default at the Phase-2
cutover.

## Migration status

Populated so far:

- [x] leak-gate (`tests/test_deploy_tree_no_secrets.py`)
- [x] gitignore + `deploy/inventory.example/` scrubbed template
- [x] portable roles — **48 / 49** through the gate (only `litellm` left,
      deferred: it retires in slice 7)
- [x] portable playbooks (48) + `redeploy-precis.yml` + `site.yml` +
      `run-*.yml` + `bootstrap-*.yml` + `ansible.cfg`
- [x] `scripts/deploy` install-from-tree (dark, behind `$PRECIS_DEPLOY_FROM_TREE`)
- [x] `service_unit` role — §15h's multiplatform launch-unit abstraction (one
      abstract spec → launchd plist **or** systemd unit). Dark: no playbook
      includes it yet. `roles/service_unit/examples/collapsed-worker.yml` is
      the authored single-collapsed-worker spec (slice 10; the Phase-2 window
      swaps the four hand-written worker plists for this one delegation, and
      the retired `PRECIS_*_ENABLED` flags → `service_config.prio`)
- [ ] `ansible --check` rehearsal against the wired overlay (needs the local
      overlay + vault-pass symlinked; Phase-2 pre-cutover)
- [ ] retire `litellm` role + `06-litellm.yml` + its `site.yml` entry (slice 7)
- [ ] overlay var renames at cutover: `finnmaccool_* → nas_*` (autofs_client)

Overlay variables the portable roles expect (define these in your local
`deploy/inventory/`): `postgres_host`, `gateway_host`, `litellm_host`,
`redis_host`, `nfs_server`, plus `nas_host` / `nas_mount_base` /
`nas_nfs_export` / `nas_mount_name` and the `precis_capabilities` map. The
`deploy/inventory.example/` templates show every one.

Never bulk-copy from the private checkout: move one file, run the gate, commit.
