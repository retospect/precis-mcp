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
- A file `deploy` writes on one node shows `deploy` as its owner on every
  node. A write to `dbbackup` from any node but the DB node fails.
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
  - The identity mapping: **closed 2026-10-22** (ruling 6). Real uids
    pass through and root is squashed; Phase 0 step 1.
  - `soft` mounts for scratch that jobs write: soft returns EIO on a
    timeout and can truncate writes silently.
  - Name resolution that does not depend on avahi.
  - macOS TCC for launchd services: **closed 2026-10-03, nothing to
    re-approve.** The grant is Full Disk Access per binary, covering every
    volume; no Mac has a per-server or Network Volumes row. The Macs
    already write `botshome` on this NAS daily from launchd and cron jobs
    under the existing grants (python.org 3.12/3.14, cron, the shells,
    automountd). The DB node's pgbouncer has no grant and must not log to
    the NAS; the plan already moves its log host-local.
- 2026-10-02 22:40Z: spark read `botshome` from finnmaccool while the DB
  node's share hung on the same box. It listed instantly, read 200 MB at
  290 MB/s, and negotiated NFS 4.2. So the Linux clients and the LAN are
  fine, and the hang is specific to the DB node's macOS nfsd. That moves
  this item ahead of repairing the DB node's export.
- 2026-10-22, read-only probe after that day's TOS update (nothing
  changed): what TOS supports is in Phase 0 step 1. `botshome`'s one NFS
  rule admits any client (`*`) and maps every user to root.

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

**Principle:** the DB node's serving path (Postgres, pgbouncer) must
never depend on the NAS, and no retained backup lives on it. The mount path
stays `/mnt/cluster` on every node. That way no service path changes, and
no Full Disk Access grant has to be redone.

**What moves:** the DB node's share is 176 GB in about 1.1M files.
- `gguf` 113 GB: llama.cpp weights. `roles/llamacpp/tasks/sync.yml`
  rsyncs these to local disk.
- `backups` 63 GB: 3 nights of local DB dumps. They are already archived
  on the NAS, and after phase 1 none are retained on the DB node.
- `git/`, `souls/`, `data/`: B2-synced trees; they move with the share.
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

1. **NAS shares.** Two shared folders on finnmaccool, both passing real
   uids through with root squashed (design: "Shares" below):
   - `cluster`: read-write for every node in `nfs_clients`.
   - `dbbackup`: read-write for the DB node, read-only for the rest.

   **What TOS 7 supports** (read 2026-10-22, after that day's update):
   - **Uid pass-through with root squashed: yes.** The NFS rule's
     Mapping field offers "No mapping", "Map superuser to guest", "Map
     all users to superuser" and "Map all users to guest" (the create-
     and edit-rule dialogs under `/usr/www/tos/js/`, labels in
     `/etc/tos/i18n/en-us/tos.lang`). "Map superuser to guest" is
     `root_squash`. "No mapping" is `no_root_squash` and is never used
     here. `botshome` today is "Map all users to superuser"
     (`all_squash,anonuid=0`). The 2026-10-03 read predates the update.
   - **Per-host rules: yes.** A rule names one client ("IP address, IP
     segment, domain name, or wildcard"), with its own Read only or
     Read/Write and its own Mapping, and a share takes several rules.
     Rules are rows of `acl_host` in the NAS's `tos` database
     (`service='nfs'`, options as JSON). `/usr/sbin/allconf` writes one
     `client(rw|ro,options)` entry per row into `/etc/exports`. When two
     entries match a client, a single host beats an IP segment whatever
     their order (exports(5)), so the DB node's rw rule wins.
   - **Owners are numeric.** NFSv4 idmapping is off on the server
     (`nfs4_disable_idmapping=Y`) and the Macs mount v3, so a file
     carries the uid the client sent. `deploy` has the same uid and gid on
     caspar, melchior, balthazar, spark, castor and pollux (checked
     2026-10-22). The NAS has no account for that uid. mountd runs
     `manage-gids=y`, so the NAS looks up supplementary groups itself and
     finds none for `deploy`: access by owner or primary group works,
     access by a secondary group does not.
   - **Root on any node writes as the NAS's `guest`.** It cannot write
     into `deploy`-owned folders.
   - **Not confirmed:** the server code that turns a Mapping choice into
     export options is unreadable (`TOSDaemon` has no strings). After the
     clicks, `/etc/exports` must show `root_squash` on both shares and
     neither `no_root_squash` nor `all_squash`.
   - **Fallback** if it does not: "Map all users to guest" on `cluster`
     (`all_squash` to `guest`). Every file is then `guest`'s, so owners
     are lost and any user on any node can delete anything in `cluster`.
     `dbbackup` keeps its protection, which rests on the per-host rule,
     not on uids.

   **Reto clicks, in the TOS web UI** (Control Panel → Shared Folder; the
   addresses come from the overlay, not this file):
   1. Create shared folder `cluster` on Volume1. Every other field at its
      default; NFS ignores TOS user privileges.
   2. Edit `cluster` → NFS → Create NFS Rule:
      - Client: the LAN segment of `nfs_clients`, in CIDR form;
      - Permissions: Read/Write;
      - Mapping: Map superuser to guest;
      - Security: Auth_sys (the only choice);
      - Enable asynchronous: off;
      - Allow users to access mounted subfolders: off;
      - Allow connections from non-privileged ports: off.
   3. Create shared folder `dbbackup` on Volume1, defaults as in step 1.
   4. Edit `dbbackup` → NFS → Create NFS Rule: Client is the DB node's
      address; Permissions Read/Write; the rest as in step 2. This needs
      a DHCP reservation for the DB node first, so the address holds.
   5. Edit `dbbackup` → NFS → Create NFS Rule: Client is the LAN segment
      of `nfs_clients`; Permissions Read only; the rest as in step 2.

   **Then an agent, with Reto's go:** reads `/etc/exports` for the check
   above, and runs one `chown` on the NAS that gives both share roots to
   the `deploy` uid and gid. TOS creates a share root as root-owned `755`
   (as `botshome` is), so until then no client can write into it. This is
   the only write an agent makes on the NAS. Repeat the check after any
   later TOS edit of either share.
   - Touches: the NAS's TOS configuration and two directory owners.
   - Rollback: delete the rules and the folders.
2. **Name resolution.** An overlay-driven `/etc/hosts` line for the NAS on
   every node, so the Linux nodes no longer depend on mDNS (`.local`).
   - Touches: `/etc/hosts` on six nodes.
   - Rollback: remove the line.
3. **Role changes, as a branch to the orchestrator:** built 2026-10-22
   (branch `worktree-nas-roles`; not applied to any host, cutover not done):
   - a `host_local_root` variable for the DB node, used by `backups`,
     `pgbouncer` (logfile), `config_pull` and `logrotate` in place of
     `shared_mount`;
   - `nfs_client` removes the old server's fstab line when the server
     changes (today it leaves a second static mount on the Macs);
   - the DB node leaves `nfs_servers`, and the NAS is not managed by
     `roles/nfs_server`;
   - no script that root runs lives on NFS (ruling 8): `backups`,
     `config_pull` and `api_monitors` install theirs to local disk. One
     uid serves `deploy` fleet-wide, so otherwise `deploy` on any node
     could rewrite a script root runs on another;
   - root-run jobs that write the share (`config_pull`, `api_monitors`)
     run as `deploy` or write host-local, since root writes as `guest`;
   - science containers that write scratch on the share run with
     `--user` set to `deploy`'s uid and gid, or their files land as
     `guest` and `deploy` cannot clean them up (`struct_relax`'s
     `docker run` passes no `--user` today);
   - the DB node mounts `dbbackup` read-write. `pg_backup`'s archive
     root and the B2 postgres leg point there in phase 6.

### Phase 1: take the DB node off the share (revised; Reto on local-compute-9)

Reto: "Backups should be on a different machine than db. Only the papers
live on the file server exclusively."

**Today (read 2026-10-02 23:35Z):**
- The 03:30 dump goes to a local directory on the DB node. At about 04:31
  it is tar+zstd'd onto the NAS, which holds 76 archives (1.2 TB, 90-day
  retention); the last 3 nights landed, about 21 GB each.
- The DB node also keeps 3 nights locally (63 GB) for fast restore. Those
  are the backups sitting next to the database.
- The 04:30 B2 sync uploads the DB node's local dumps, plus
  `backups/configs` and the share's `git/`, `souls/` and `data/` trees.
  No log line confirms B2 success, so that is unverified.

4a. **No retained backups on the DB node.**
    - The dump still writes to local disk while it runs. Writing it over
      NFS puts lockd in the path, which is the hang `roles/backups` warns
      about.
    - Once the NAS archive is written and `zstd -t` verifies it, the local
      dump is deleted. The DB node then holds a backup only from 03:30 to
      about 04:31.
    - A restore reads the NAS archive (21 GB, about 75 s at 290 MB/s, then
      a parallel `pg_restore`).
    - Touches: `pg_backup.sh.j2`. Local retention becomes "until
      verified"; the free-space guard stays.
    - Rollback: the 2-day value.
4b. **B2 reads the off-box copies.**
    - The postgres leg syncs the NAS archives (on `dbbackup` from phase
      6) instead of the DB node's local dumps. The other legs read the
      moved trees on the NAS.
    - It stays where the B2 credentials live (the DB node), now as a NAS
      client.
    - Add a success log line and a check that last night's upload exists.
4c. **Host-local operational paths on the DB node.** Scripts, the backup
    and config-pull logs and pgbouncer's logfile move to
    `host_local_root`. They are not backups, so local is right.
    - pgbouncer restarts, a few seconds of downtime. Reto: run when ready,
      no round window needed.
    - Rollback: the variable back, `mv` back.
4e. **Sequencing (orchestrator verdict 6b):**
    - 4c's pgbouncer restart also clears the open read-only incident (a
      poisoned pooled connection). If Reto has already given the
      reconnect go, this is a second restart in the same window, which is
      fine.
    - Before the DB node leaves `nfs_servers`, the 03:30 backup must have
      run once from `host_local_root`, and its NAS archive must have been
      read back.
    - Do not stop keeping local dumps (no such flag exists today) until
      `drill_pull` can serve the NAS archive. Otherwise the offsite
      restore drill silently gets nothing.
4d. **Where the copies end up:** the NAS (90 days, RAID6) and B2 offsite.
    Both are off the DB machine and none are on it. A third copy on a
    Mac's local disk is possible but not proposed: the NAS and B2 are
    already two independent off-box copies.

### Phase 2: copy (no client sees a change)

5. **Copy.** Mount `cluster` on the DB node at a temporary path. As
   `deploy`, rsync the share without `-o`/`-g`, excluding `backups/` and
   the moved trees: root is squashed, so no client can set owners, and
   the copies become `deploy`'s. Files `deploy` cannot read show in
   rsync's errors and are fixed by hand. That is about 114 GB, roughly
   7 minutes at the measured 290 MB/s. A delta pass runs again at
   cutover.
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
   - a file `deploy` writes on spark shows `deploy` as owner on melchior;
   - a write to `dbbackup` from spark fails as read-only;
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

### Shares (rulings 5–8, 2026-10-22; replace the 2026-10-03 role-share proposal)

Two shares, both passing real uids through with root squashed. Narrower
access, when needed, is per user or at the container mount, not per share.

- **`cluster`** at `/mnt/cluster`: read-write for every node, real
  owners. Models, the library (corpus, podcast, `precis_root`, the
  B2-synced trees), logs and scratch are folders in it.
- **`dbbackup`**: the DB dumps and botshome's archive of them.
  Read-write for the DB node, whose `deploy` writes the backups;
  read-only for every other node. The per-host rule is the gate, not the
  uid: `deploy` has one uid fleet-wide, so on another node it can read a
  backup but the export refuses its writes. On the DB node itself, any
  process running as `deploy` can still delete one.
- Kept from the proposal as a role change, not a share (Phase 0 step 3):
  no script that root runs lives on NFS.

### Phase 6: retire botshome (after both shares run clean)

12. Identify botshome's unknown owners: the rest of `backups/`, stray
    tarballs, and the numeric uids. Everything there is root-owned on
    the NAS today, because the rule maps every user to root.
13. Copy each class into a folder of `cluster`, and the DB archive (about
    1.2 TB, roughly 70 minutes at 290 MB/s) into `dbbackup` from the DB
    node, the only host that can write it. Copies run as `deploy` on a
    client, so they arrive `deploy`-owned. Verify file counts and
    checksums.
14. Repoint the overlay paths in one round deploy: corpus, inbox, podcast,
    `PRECIS_ROOT`, the pg_backup archive root (to `dbbackup`), the
    autofs map, and `PRECIS_NAS_PROBE_PATH`. The heartbeat probe defaults to
    `/opt/nas/botshome` (`workers/heartbeat.py`), and so does the
    `nas-denied` message (`workers/nursery.py::_detect_nas_denied`).
15. Soak for 7 days with botshome's NFS rule set to read-only. A forgotten
    writer then fails loudly while reads keep working. Rollback: set the
    rule back to read-write and revert the overlay.
16. Remove botshome's NFS rule. Keep the folder and its snapshots for 30
    days (rollback: re-add the rule). Deletion is Reto's call.

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

### Rulings (Reto, local-compute-9, 2026-10-02 22:49Z)

1. **Export and `cluster` user:** not ruled. The default is Reto in the
   TOS web UI, with field values from local-compute (review item
   local-compute-10).
2. **Timing:** "it is when it is". Run each phase when it is ready; no
   round window needed. The FS fix is a priority now.
3. **The DB node's 03:00 update+reboot cron:** keep.
4. **Backups:** off the DB machine (phase 1 revised above). Only the
   papers live exclusively on the file server.

### Rulings (Reto, chat-99, 2026-10-22, during the maintenance window)

Answers the role-shares proposal (local-compute-12) in favour of fewer
shares. Reto: "Non-root files is good, owners are good, but general access
is fine ... Separate out the backup user so agents cant accidentally rm the
backup? But KISS minimal otherwise."

5. **Server:** the NAS itself, every node mounting it directly; no Mac or
   Spark relays it ("no middle hops").
6. **Ownership:** files keep their real, non-root owners. That needs uid
   pass-through with root squashed, not `all_squash` to one identity; the
   plan must confirm TOS 7 offers it (the 2026-10-03 read found only
   squash-to-root/admin/guest) and rely on the cluster's uid/gid parity.
7. **Access:** general read-write for every cluster node on the main
   share. Narrower access, when needed, is per user (read-only) or at the
   container mount, not per share.
8. **Shares:** two, not the role set. `cluster` (general) and a backup
   share writable only by the backup writer, so an agent on any other node
   cannot delete a backup. The `models`, `library`, `logs` and `scratch`
   shares are dropped as separate shares (they are folders in `cluster`).
   Still kept from the proposal because it is a local-disk change, not a
   share: no scripts that root runs live on NFS.
9. **The DB node is the database and nothing else.** Reto: "I concur with
   caspar db only ... File server can go if we no longer need it." Its NFS
   export retires in phase 5 once no client mounts it. Two DB-node changes
   ride with this plan:
   - Its nightly `daily-update-reboot.sh` installs nothing: on Apple
     silicon a scripted `softwareupdate --install` needs a user's
     credentials, so the node sat on 26.4 while rebooting every night.
     Replace it with manual minor updates in a planned window and
     automatic Security Responses. Dropping the nightly reboot needs a
     check of what else relies on it.
   - Pin the database stack in Homebrew (`postgresql@17`, `pgvector`,
     `pg_cron`, `libpq`) so only a deliberate upgrade with a planned
     restart moves it. The 2026-10-22 window did 17.9 → 17.11 that way.
   - Shares created and verified 2026-10-22: `cluster` and `dbbackup`
     exist with the rules above, the roots belong to deploy, and a mount
     test from two Macs passed (deploy writes, root refused, `dbbackup`
     writable from the DB node only).
