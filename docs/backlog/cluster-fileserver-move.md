---
status: idea
title: The cluster share moves to the file server — no Mac or Spark serves files
pillar: local-compute
prio: high
---

# The cluster share moves to the file server — no Mac or Spark serves files

Reto, 2026-10-02 (review item local-compute-7): "Really I don't want any of
the macs or sparks to be fileservers in addition. I'd want them to directly
talk to that." "That" is finnmaccool, the file server.

## Motivation / why

`/mnt/cluster` is exported today by the DB node (caspar, a Mac) over
NFSv3/TCP and autofs-mounted on the Linux nodes. The twins stage
dft/fold/retrosynth container scratch on it. On 2026-10-02 it hung on all
three Sparks while the Macs read it fine (review item local-compute-6,
design note §6). The DB node also carries prod Postgres, so a file-serving
fault there sits next to the database. A dedicated file server takes both
roles off the Macs.

## In scope

1. A read-only probe of finnmaccool first: OS, exports and protocols,
   capacity and health, and how each node reaches it (on the LAN directly
   or through the tailnet). Reto granted ssh for this. No change before a
   migration plan is approved.
2. A migration plan: the export on finnmaccool, copying the share's
   contents, autofs maps on the Linux nodes, mounts on the Macs, a cutover
   order that keeps the science lanes' scratch working, and a rollback.
3. macOS per-app access to network volumes (TCC): launchd services on the
   Macs may be refused files on a network mount. Find a supported way to
   grant it, for example Full Disk Access for the service binary or a
   mount location TCC does not gate, instead of working around it per
   service.
4. Retire the DB node's NFS export once every client mounts finnmaccool.

## Explicitly NOT in scope

- Postgres placement (stays on the DB node).
- Corpus/PDF storage layout beyond what lives on `/mnt/cluster` today.

## Acceptance criteria

- Every node mounts the share from finnmaccool; no Mac or Spark exports NFS.
- A launchd service on a Mac reads and writes the share with no TCC prompt.
- A DFT relax on pollux stages scratch on the new share end to end.
- `tests/test_deploy_tree_no_secrets.py` green: the server address lives
  only in the gitignored overlay; the deploy tree names a group, not the host.

## Target + blast radius

Inventory overlay (`nfs_server` / `nfs_clients`), the NFS server and client
roles under `deploy/`, macOS mount config on the Macs. Deploy-role changes
go to the orchestrator as a branch; overlay edits go to Reto.

## Open questions / decisions log

- 2026-10-02 ~22:30Z read-only probe of finnmaccool (nothing changed):
  - A TerraMaster NAS (TOS 7 on an Ubuntu 22.04 base). Load is near zero.
  - Storage: 8 NVMe drives in RAID6 under one btrfs volume, 22 TB with
    17 TB free. SMART passes on every drive and the last scrub is clean.
  - One 2.5 GbE NIC at MTU 1500. Every Spark and every Mac reaches it
    on-link on the LAN, not through the tailnet.
  - It serves NFS v3/v4.0-4.2 (8 threads) and SMB.
  - One NFS export, `botshome`, with `all_squash` mapping every client
    user to a single server identity.
  - The cluster already uses it. The Linux nodes autofs-mount `botshome`
    at `/mnt/archive` (nfs4, soft). The Macs carry it in `/etc/fstab`
    (v3, soft, resvport), live on two of the three.
  - The Linux nodes resolve the server only by its mDNS `.local` name.
- Gaps the plan must close:
  - A new export (or tree) for the cluster share, created in TOS.
  - The identity mapping: `all_squash` to a single root-equivalent
    identity defeats per-user ownership. Squash to a dedicated
    non-privileged identity, or map real uids.
  - `soft` mounts for scratch that jobs write: soft returns EIO on a
    timeout and can truncate writes silently.
  - Name resolution that does not depend on avahi.
  - macOS TCC for launchd services, which cannot be tested read-only.
- 2026-10-02 22:40Z: spark read `botshome` from finnmaccool while the DB
  node's share hung on the same box. It listed instantly, read 200 MB at
  290 MB/s, and negotiated NFS 4.2. So the Linux clients and the LAN are
  fine, and the hang is specific to the DB node's macOS nfsd. That moves
  this item ahead of repairing the DB node's export.

## Migration plan (proposed, awaiting Reto: review item local-compute-8)

The deploy roles already take the server as a variable.
`roles/nfs_client` mounts `hostvars[nfs_server].lan_ip:{{ nfs_export_path }}/shared`
on `shared_mount`. Moving is therefore mostly overlay values plus one
playbook run, with these steps:

1. **Export on finnmaccool** (TOS UI or approved root shell): a new folder
   with a `shared/` subdirectory and its own NFS rule, LAN subnet only,
   `rw,sync,no_subtree_check`, squashed to one dedicated non-privileged
   identity (not root). Keep it separate from `botshome`, which holds
   paper and home trees with other permissions.
2. **Copy** the DB node's `/opt/nfs/shared` to the new export: rsync from
   the DB node, which reads its own disk locally. Measure the size first
   (`du` on the DB node). Do a second delta pass at cutover.
3. **Keep the DB node's backup path local.** `roles/backups` runs
   `pg_backup.sh` and friends from `{{ shared_mount }}/scripts` and writes
   `{{ shared_mount }}/backups/postgres` on the DB node's own SSD. It
   assumes `shared_mount` is host-local there. Before cutover, point that
   role at a host-local directory on the DB node. Otherwise the database
   backups would start depending on the NAS being up.
4. **Overlay:** add finnmaccool as a host with `lan_ip`, set `nfs_server`
   to it and `nfs_export_path` to the new folder, and drop the DB node
   from `nfs_servers` (the NAS is TOS-managed, so `roles/nfs_server` does
   not run on it). Add a hosts entry so the Linux nodes do not depend on
   mDNS.
5. **Clear the hung mounts on the Sparks** (`umount -l /mnt/cluster`). The
   processes stuck in D state (`find`, `ls`, `config_pull_spark.sh`)
   probably clear only with a reboot. Do that inside the OS-update window
   (`spark-provisioning.md`), after catalysis's PBE run on spark has
   finished.
6. **Run `playbooks/01-nfs.yml --tags nfs_client`**. Role gap: the macOS
   fstab `lineinfile` matches on the new source, so the DB node's old line
   would stay as a second static mount. Remove the old line in the same
   change. The canary `.nfs-canary` must exist on the new export.
7. **TCC check on one Mac**: a throwaway launchd job reads and writes
   `/mnt/cluster`. If it is refused, grant Full Disk Access to that
   service's binary, or move the mount out of TCC's network-volume scope.
   Record which one works here.
8. **Retire** the DB node's export (`/etc/exports` line, `nfsd disable`)
   once every client reads the new share for a day.

Rollback up to step 8: point `nfs_server` back at the DB node and re-run
step 6. The old export and its data stay untouched until step 8.

- 2026-10-02: the Linux Sparks' hang on the current share is under
  diagnosis (design note §6). The last read points at caspar sending its
  replies to the Sparks through the Tailscale tunnel (MTU 1280) while the
  Sparks send to caspar directly on the LAN. If that is the cause, the same
  routing will hit finnmaccool unless the plan pins the LAN path. Probe
  item 1 checks each node's route to finnmaccool for this reason.
