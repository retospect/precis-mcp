---
status: draft
title: Alert when the nightly B2 offsite sync goes dark
pillar: platform
prio: high
---

# Alert when the nightly B2 offsite sync goes dark

## Motivation / why

The nightly B2 sync on the DB node failed on every run from 2026-08-11
to 2026-10-03. b2 4.x rejects the v3 option `--keepDays`. The 10-01
template fix was never rendered, because nothing re-runs
`deploy/playbooks/11-backups.yml` (ship-gate-ci owns that render gap). No
monitor noticed for seven weeks, for three reasons:

- the old script left no failure marker;
- the weekly restore test reads the local dumps, not B2;
- `<shared_mount>/logs/backup-b2.log` kept growing, and nothing reads it.

A backup that fails loudly into a log nobody reads is still a silent
monitor. Source: orchestrator 2026-10-03, review-queue
`organizer-backup-1.md`. Reto runs the sync fix itself.

## In scope

One check. It goes red when **either** of these holds:

1. `<shared_mount>/logs/backup-b2.log` has not gained a
   `B2 offsite sync complete` line in the last 26 h;
2. the log's tail since the last `Starting`/`complete` line carries
   `B2 offsite sync FAILED (exit N)`. That marker comes from the EXIT trap
   in `deploy/roles/backups/templates/b2_sync.sh.j2`.

A red check opens an alert through the normal alert-sync, router and push
path.

**Home: a new `health_digest` check, not a node-health tick.** The DB node
has no node-health tick to extend. It is daemon-free by design: no
worker, no `host_heartbeat` row (see the `precis.workers.heartbeat`
docstring, known gap ii). `<shared_mount>` is the `/mnt/cluster` NFS share,
so a worker host that mounts it can read the log. `health_digest` is the
hourly lane whose `CheckResult` rows already feed alert sync. Two points
for whoever builds it:

- Every existing `_check_*` in `health_digest`, and every `conditions.py`
  probe, is SQL-only. This would be the first filesystem read, so it
  needs a hard timeout. `/mnt/cluster` has hung on every client since
  2026-09-30 (thread Do-next 2), and an unguarded `open()` on a hung NFS
  mount blocks the whole digest pass. On a timeout, or a missing mount,
  the check reports `unknown`, not green.
- The check should run on exactly one host, wherever `health_digest` is
  scheduled, so that hosts without the mount don't false-positive.

A cheaper and sturdier variant, for the item's reviewer to decide: on
success, `b2_sync.sh` also writes `<shared_mount>/logs/backup-b2.last-ok`,
containing a UTC timestamp. The check then stats one small file instead
of scanning a growing log. This needs a backups-role render, which runs
into the same render gap.

## Explicitly NOT in scope

- Fixing the sync itself. Reto runs that fix.
- Rendering the backups role (ship-gate-ci's render-gap item).
- Verifying B2 contents. A restore-from-B2 test is a separate item.
- The `pg_backup`, `usb_backup` and `restore_test` logs. Each has the
  same unread-log shape; file them after this one proves the pattern.

## Acceptance criteria

- Against a log fixture whose last `complete` line is older than 26 h,
  the check is red. With a `complete` line 2 h old, it is green.
- A fixture whose newest run ends in `B2 offsite sync FAILED (exit 2)`
  is red even when an older `complete` line is under 26 h old.
- An unreadable, hung or absent mount yields `unknown` within the
  timeout, and the digest pass still finishes.
- In prod, after deploy: the check reads the real log. While the sync is
  still broken it is red, with an open alert; once Reto's fix has run
  one night, it is green.

## Target + blast radius

`src/precis/workers/health_digest.py` (new check plus registration),
its tests, and optionally `deploy/roles/backups/templates/b2_sync.sh.j2`
(the last-ok stamp). No migrations.

## Open questions / decisions log

- Log scan or a last-ok stamp file? (See the In scope note.)
- Which host runs `health_digest`, and does it mount `/mnt/cluster`?
  Confirm this before building.
