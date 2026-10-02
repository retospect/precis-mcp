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

- 2026-10-02 22:42Z, Reto (local-compute-8) **held all NFS changes**,
  including the caspar nfsd restart approved under local-compute-6, until
  a full plan is ready. His inputs:
  - a dedicated `cluster` user on the NAS;
  - hard mounts everywhere;
  - DB backups stay on the Mac ("db backups are huge");
  - answer whether a direct USB link from the NAS to a Mac would help.
  Still unruled: who creates the export.
- Blast radius found 2026-10-02: on the DB node, `shared_mount` is a
  symlink to its local export. Its pgbouncer log
  (`roles/pgbouncer/templates/pgbouncer.ini.j2`), the backup scripts and
  logs (`roles/backups`), `config_pull` and logrotate all write there.
  Moved to the NAS with hard mounts as-is, a NAS outage would stall
  pgbouncer's log writes, and with them prod DB access. The full plan
  must give the DB node host-local paths for all of these first.
  Other users of `shared_mount`:
  - nginx and code-sandbox logs;
  - `mcps` workspaces;
  - `api_monitors` pip-audit;
  - `extract_watch` logs;
  - the `/opt/shared/corpus` fallback in `precis_web`.
- 2026-10-02: the Linux Sparks' hang on the current share is under
  diagnosis (design note §6). The last read points at caspar sending its
  replies to the Sparks through the Tailscale tunnel (MTU 1280) while the
  Sparks send to caspar directly on the LAN. If that is the cause, the same
  routing will hit finnmaccool unless the plan pins the LAN path. Probe
  item 1 checks each node's route to finnmaccool for this reason.

## Full migration plan (review item local-compute-9)

**Principle:** the DB node must never depend on the NAS, and the mount path
stays `/mnt/cluster` on every node. That way no service path changes, and
no Full Disk Access grant has to be redone.

**What moves:** the DB node's share is 176 GB in about 1.1M files.
- `gguf` 113 GB: llama.cpp weights. `roles/llamacpp/tasks/sync.yml`
  rsyncs these to local disk.
- `backups` 63 GB: stays on the DB node (Reto).
- `aizynth-models` 0.75 GB, `logs` 0.1 GB, `scripts`, `workspaces` and
  the rest are small.

**What reads it, by host:**
- **DB node:** pgbouncer's log, the backup cron jobs (pg_dump, restore
  test, B2 offsite sync), `config_pull`, and log rotation.
- **melchior:** the precis web service (the corpus fallback).
- **balthazar:** the openclaw agents.
- **The Sparks:** the dft, fold and retrosynth container scratch, and
  `config_pull`.

### Phase 0: prepare (no client sees a change)

1. **NAS export.** Create a user `cluster` and a folder `cluster` with a
   `shared/` subfolder owned by that user. Add an NFS rule for the LAN
   subnet only: `rw`, `sync`, `all_squash` to the `cluster` user,
   `secure`, NFS v3 and v4.
   - Touches: the NAS's TOS configuration only.
   - Rollback: delete the folder and the rule.
   - Who creates it is open question 1.
2. **Name resolution.** An overlay-driven `/etc/hosts` line for the NAS on
   every node, so the Linux nodes no longer depend on mDNS (`.local`).
   - Touches: `/etc/hosts` on six nodes.
   - Rollback: remove the line.
3. **Role changes, as a branch to the orchestrator:**
   - a `host_local_root` variable for the DB node, used by `backups`,
     `pgbouncer` (logfile), `config_pull` and `logrotate` in place of
     `shared_mount`;
   - `nfs_client` removes the old server's fstab line when the server
     changes (today it leaves a second static mount on the Macs);
   - the DB node leaves `nfs_servers`, and the NAS is not managed by
     `roles/nfs_server`.

### Phase 1: take the DB node off the share (DB node only)

4. **Repoint the DB node.** Apply the `host_local_root` change on the DB
   node. `mv` `backups/`, `scripts/` and the pgbouncer and backup logs
   into it; this is the same volume, so it is instant. Re-run the
   backups, pgbouncer and config_pull roles on the DB node. Check that
   the 03:30 pg_dump lands and that pgbouncer is logging.
   - Touches: the DB node's cron jobs, the pgbouncer plist and its config.
   - pgbouncer needs a restart. Prod DB access drops for a few seconds;
     do it in the deploy window.
   - Rollback: set the variable back and `mv` the directories back.

### Phase 2: copy (no client sees a change)

5. **Copy.** Mount the new export on the DB node at a temporary path.
   rsync the share, excluding `backups/` and the moved trees. That is
   about 114 GB, roughly 7 minutes at the measured 290 MB/s. A delta pass
   runs again at cutover.
   - Touches: the NAS's disk.
   - Rollback: delete the copy.

### Phase 3: cutover (one deploy window, after catalysis's PBE run on spark ends)

6. **Pause the science lanes** through the job system, so no dft, fold or
   retrosynth job is mid-write. Run the rsync delta pass.
7. **Linux Sparks.** Run `nfs_client` to point fstab at the NAS, then
   reboot inside the scheduled OS-update window (`spark-provisioning.md`).
   The reboot is the only way to clear today's processes stuck in D state
   on the hung caspar mount.
   - Touches: fstab and systemd automount on spark, castor and pollux.
   - Rollback: point fstab back and reboot.
8. **Macs.** Stop the precis web service on melchior and the openclaw
   agents on balthazar. Run `nfs_client` (it replaces the fstab line and
   runs `automount -c`), then start the services again.
   - Rollback: same steps with the old line.
9. **Checks.**
   - `.nfs-canary` is readable on every node;
   - one dft job runs on pollux with scratch on the new share;
   - the precis web service serves a held PDF;
   - the openclaw agents write their logs;
   - unpause the lanes.

### Phase 4: Full Disk Access (TCC) check

10. **Full Disk Access.** The mount path is unchanged, so the existing grants
    from `roles/tcc_profile` still apply. Those are Full Disk Access on
    the python.org-signed interpreters, clicked by hand because macOS 26
    refuses unsigned profiles. Check with a throwaway launchd job on
    melchior that reads and writes `/mnt/cluster`. A new service that
    needs the share gets its binary added to `tcc_profile_binaries`.

### Phase 5: retire (a day later, then a week later)

11. **Retire the old export.** After a day of clean running, remove the DB
    node's export line and stop its nfsd. Keep the old `shared/` data on
    the DB node for a week as the rollback copy, then delete `gguf/` there
    to recover 113 GB.

**Rollback overall:** until step 11, set `nfs_server` back to the DB node
and re-run steps 7–8. The old export and data stay untouched until then.

### Reto's question: would a USB cable from the NAS to a Mac help?

No.
- **Hardware:** the NAS (TerraMaster F8 SSD Plus) has no USB device or
  gadget mode, so a plain cable carries nothing. A host-to-host bridge
  cable, or two USB NICs, would give a point-to-point link to one Mac
  only, while the Sparks and the other Macs would still use the LAN.
- **Backups:** the DB backups stay on the Mac by your ruling, so the
  cable wouldn't carry them.
- **Full Disk Access:** the macOS permission is about the mount, not the
  wire, so the cable doesn't change it.
- **Bandwidth:** the NAS's only NIC is already 10 GbE (Aquantia AQC). It
  links at 2.5 Gb/s because the switch port offers no more. spark's read
  of 290 MB/s is that 2.5 Gb/s line rate.
- **Better option:** if bandwidth ever matters, use a multi-gig (5/10 GbE)
  switch port for the NAS. No new hardware on the NAS. Today the only
  bulk traffic is the weights sync (113 GB, about 7 minutes at 2.5 Gb/s),
  so it isn't needed now.

### Open questions for Reto

1. **Who creates the export and the `cluster` user?**
   - Recommended: you, in the TOS web UI. TOS probably regenerates
     `/etc/exports` from its own configuration, so a hand edit over ssh
     may not survive an update. Not verified.
   - Either way, I give you the exact field values. The alternative is
     an agent over the root ssh, but only through TOS's own tooling,
     after checking that the change survives a TOS update.
2. **The window:** phase 1 restarts pgbouncer, and phase 3 reboots the
   Sparks and pauses the science lanes for about 30 minutes. Is the next
   round deploy window acceptable?
3. **The DB node's nightly 03:00 "update + reboot" root cron:** it takes
   the share away from every hard-mounted client nightly today. It goes
   away for the share with this move. Keep it for the DB node itself?
