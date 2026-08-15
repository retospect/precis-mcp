# Mount-namespace cutover — macOS can't create /mnt (cutover incomplete)

The uniform `/mnt/cluster` + `/mnt/archive` namespace (shipped `03c58b72`,
was gr194012) cannot take effect on the three macOS hosts: the sealed
system volume makes `/mnt` read-only, so any mkdir/mount under it fails
(`[Errno 30] Read-only file system: b'/mnt'`). First cutover attempt
2026-08-15 failed exactly there (melchior, precis_web podcast dir); the
overlay was reverted (overlay commits `8ff83e5` cutover → `64ad6dc`
revert) and the fleet is stable on the legacy paths.

To finish the cutover:

1. **Provision `/mnt` on darwin** — add an `/etc/synthetic.conf` entry
   (`mnt` synthetic directory) + activation (`apfs.util -B`, else reboot)
   to `nfs_client`/`nfs_server`/`autofs_client` before any task touches
   `{{ shared_mount }}`/`{{ nas_mount_base }}`. No `synthetic` handling
   exists anywhere in `deploy/` today.
2. **Guard the transition symlink** — `deploy/tasks/legacy_mount_symlink.yml`
   unmounts `legacy_path` then symlinks it to `canonical_path`; with a
   stale overlay (canonical == legacy) it unmounts the live share and
   self-symlinks. Add `when: legacy_path != canonical_path` at the
   include sites.
3. **Sequence the rollout** — `scripts/deploy` runs only
   `redeploy-precis.yml`; the mount roles live in
   `playbooks/01-nfs.yml` + `playbooks/05-autofs.yml` and must run first.
   Order: re-apply the overlay cutover commit (revert `64ad6dc` in
   `deploy/inventory`, push deploy-helper) → 01-nfs → 05-autofs →
   `scripts/deploy`.

Ops; needs the role fix shipped before the overlay is re-applied.
