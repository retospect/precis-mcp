---
status: ready
title: scripts/deploy refuses a migration deploy inside the 03:00–04:20 UTC backup window
pillar: platform
prio: normal
---

# scripts/deploy refuses a migration deploy inside the 03:00–04:20 UTC backup window

## Motivation / why

The prod DB node reboots at 03:00 UTC and the nightly `pg_dump --jobs=4`
runs 03:30 to about 04:18 UTC. The dump holds ACCESS SHARE on every table,
so a migration's `ALTER TABLE … ADD COLUMN` (ACCESS EXCLUSIVE) waits for
the whole dump. On 2026-09-08 a `/go` deploy started at 03:22 UTC hung
17+ minutes on the migration task with a silent log. Aborting mid-deploy is
worse than waiting: new code installed, migrations unapplied. The rule
lives only in operator memory; `scripts/deploy` reads `date -u` for the
log name and nothing else. Reto filed it 2026-10-01 (pillar review).

## In scope

- `scripts/deploy` checks the UTC clock before it starts. Inside
  03:00–04:20 UTC, if the deploy carries a pending migration (any
  `src/precis/migrations/*.sql` not applied on prod), it refuses with the
  reason and the time the window ends.
- A deploy with no pending migration still prints a warning inside the
  window (the 03:00 reboot drops connections) but proceeds.
- An explicit override flag for an operator who has checked the dump is
  not running.

## Explicitly NOT in scope

- Moving the backup or the reboot schedule.
- Guarding `scripts/ship` (it does not touch prod).

## Acceptance criteria

- With the clock faked to 03:40 UTC and a pending migration, `scripts/deploy`
  exits non-zero before any ansible task runs, naming the window and its end.
- With the clock faked to 03:40 UTC and no pending migration, it warns and
  proceeds.
- Outside the window, behaviour is unchanged.
- The window bounds live in one place, next to the backup cron they mirror
  (`deploy/roles/backups/`), so a schedule change moves both.

## Target + blast radius

`scripts/deploy` start-up only; the backups role defaults for the window
bounds. No runtime code.
