# Mount-namespace cutover — macOS can't create /mnt (cutover incomplete)

The uniform `/mnt/cluster` + `/mnt/archive` namespace (shipped `03c58b72`,
was gr194012) cannot take effect on the three macOS hosts: the sealed
system volume makes `/mnt` read-only, so any mkdir/mount under it fails
(`[Errno 30] Read-only file system: b'/mnt'`). First cutover attempt
2026-08-15 failed exactly there (melchior, precis_web podcast dir); the
overlay was reverted (overlay commits `8ff83e5` cutover → `64ad6dc`
revert) and the fleet is stable on the legacy paths.

Role fix DONE (this repo): darwin `/mnt` provisioning via an
`/etc/synthetic.conf` synthetic **symlink** `/mnt ->
/System/Volumes/Data/mnt` + `apfs.util -B` activation
(`deploy/tasks/darwin_synthetic_mnt.yml`, included by
`nfs_client`/`nfs_server`/`autofs_client` when the target path is under
`/mnt`), and `legacy_path != canonical_path` guards at every
`legacy_mount_symlink.yml` include site.

Remaining — the rollout itself: `scripts/deploy` runs only
`redeploy-precis.yml`; the mount roles live in `playbooks/01-nfs.yml` +
`playbooks/05-autofs.yml` and must run first. Order: re-apply the overlay
cutover commit (revert `64ad6dc` in `deploy/inventory`, push
deploy-helper) → 01-nfs → 05-autofs → `scripts/deploy`. Delete this file
once the fleet is verified on `/mnt` (canaries + a live worker log).
