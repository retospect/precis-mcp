---
status: ready
title: Uniform mount namespace for shared storage — role-named paths under /mnt
---

# Uniform mount namespace for shared storage

## Motivation / why

No uniform namespace for shared storage: the same logical content sits at
different absolute paths per node, and some paths are overloaded (same
path, different content per node) — any hardcoded absolute shared path is
non-portable. Concrete divergences (2026-08-05): NAS
`finnmaccool:/Volume1/botshome` mounts at `/opt/nas/botshome` on melchior
but `/nas/botshome` on spark; `/opt/shared` is a LOCAL dir on caspar but
the NFS mount of caspar's own export on melchior; the `shared_mount` var
renders per-node (`/opt/shared/gguf` vs `/shared/gguf`); `/opt/nfs` is the
export root on caspar (server-side, `nfs_export_path`) but reads like a
client mount.

Decided (2026-08-05) — scheme A: role-named under `/mnt`, uniform on ALL
nodes including the server via a bind mount. `/mnt/cluster` = caspar SSD
hot share (NFS, `hard,intr,nosuid,nodev,noatime`). `/mnt/archive` =
finnmaccool NAS (NFSv4, Backblaze-backed cold/backup,
`soft,intr,bg,nosuid,nodev`). Rationale: both stores are NFS, so
`/opt/nfs` vs `/opt/nas` named a protocol both speak — name by ROLE
(cluster/archive) under FHS `/mnt`; the durability difference lives in
mount OPTIONS, documented in the role, not the path. Anti-pattern (do not
re-propose): naming mounts by flag (`/hard`, `/soft`) or transport
(`/nfs`).

Evidence gr194012 (2026-08-05). SEPARATE from GGUF model-set convergence
(`llamacpp-model-convergence.md`) — do not conflate.

## In scope

- Single source of truth in `group_vars` — derive `shared_mount` /
  `nas_mount_base` / `nas_root` from the role map; delete per-host
  overrides (the root cause of the `/opt/nas` vs `/nas` split).
- caspar bind-mounts its own export to `/mnt/cluster` for server-side
  parity with clients.
- Transition symlinks (`/opt/shared` → `/mnt/cluster`, etc.) bridge
  cutover for anything not yet repointed.
- Fix the Prometheus `NFSMountMissing` alert, which keys on
  `mountpoint="/shared"` (likely dead post-rename).

## Explicitly NOT in scope

- GGUF model-set convergence (catalog drift, sync automation) —
  `llamacpp-model-convergence.md`.
- Choosing new content layouts within either share — path renaming only.

## Acceptance criteria

- `/mnt/cluster` and `/mnt/archive` resolve to the same content on every
  node, including caspar (bind mount verified).
- `deploy/inventory.example/hosts.example.yml` and all `group_vars`
  declare `shared_mount` / `nas_mount_base` / `nas_root` from one role map,
  no per-host path overrides remain.
- `NFSMountMissing` Prometheus alert fires against the new mountpoint(s).
- Transition symlinks in place at old paths (`/opt/shared`, `/opt/nas`,
  `/nas`, `/opt/nfs`) pointing at the new roles during cutover.

## Target + blast radius

- `deploy/roles/nfs_client/`, `deploy/roles/autofs_client/` (mount
  definitions).
- `deploy/inventory.example/hosts.example.yml`, `group_vars` (single
  source of truth for the role→path map).
- Every role template referencing `shared_mount` / `nas_mount_base` /
  `nas_root` (logrotate, api_monitors, extract_watch, mcps, backups,
  config_pull, pgbouncer, nginx — see grep for current consumers).
- caspar's `/opt/nfs` export (`nfs_export_path`) + new bind mount.
- Prometheus alert rules (`NFSMountMissing`).

## Open questions / decisions log

- Cutover order (mounts first vs symlinks first) and how long the
  transition symlinks stay before deletion — not yet decided.
