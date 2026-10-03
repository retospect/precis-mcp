---
status: draft
pillar: platform
title: scripts/deploy runs only redeploy-precis.yml, so a fix to any other role (backups, monitoring, pgbouncer) stays unrendered until someone runs its playbook by hand — 7 weeks for the B2 sync
---

# `scripts/deploy` leaves non-precis roles unrendered

## What

`scripts/deploy` runs exactly one playbook, `deploy/redeploy-precis.yml`.
That file imports the precis service playbooks (03, 06, 15, 19, 19a,
20b/c/e, 28, 32–35, 38, 41, 43, 45). Every other role sits in `site.yml`'s
numbered playbooks and is rendered only when someone runs that playbook by
hand. This covers `11-backups.yml`, `08-monitoring.yml`, `02-postgres.yml`
(pgbouncer) and `16-api-monitors.yml`, among others. A merged fix to one of
those roles therefore reaches `prod` (the ref) without reaching the host.
Nothing says so: the deploy is green, `origin/prod` moves, and the template
on the host is still the old one.

## Why it matters

The finding behind this item is `review-queue/open/organizer-backup-1.md`
(orchestrator, 2026-10-03). The nightly B2 offsite sync has failed on every
run since 2026-08-11, because b2 4.x rejects the v3 option `--keepDays`. The
template fix landed 2026-10-01 in `deploy/roles/backups/templates/b2_sync.sh.j2`
but was never rendered, because nothing re-runs `11-backups.yml`: about seven
weeks of dark offsite backups behind a fix that was already on `main`. The
missing alert is a separate item, filed by monitors-that-go-quiet as
`b2-offsite-sync-dark-alert.md`.

## Options

(i) **Advisory, now.** At the end of a deploy, `scripts/deploy` lists the
roles under `deploy/roles/` whose files changed between the previously
deployed sha (the deploy-state marker / `origin/prod`) and the target sha
but which no playbook in `redeploy-precis.yml` covers. For each it prints
the playbook to run, e.g.
`backups changed since 63301c5c, not rendered by this deploy: ansible-playbook deploy/playbooks/11-backups.yml`.
Role → playbook comes from parsing `roles:` in `deploy/playbooks/*.yml`;
no behaviour change and no new privilege. Cost: one `git diff --name-only`
plus a YAML read per deploy.

(ii) **`--roles`, later.** The same list, and with `--roles` (or
`--roles=backups,monitoring`) `scripts/deploy` runs those playbooks after
the precis rollout under the same lock and log. The convergence check covers
only the rollout hosts, so each extra playbook needs its own recap check.
Risk: these roles touch the DB node and monitoring; a bad pgbouncer render
mid-round takes every agent down, which is why they were left out of the
default path.

(iii) **Drift alarm.** A weekly `ansible-playbook site.yml --check --diff`
cron on the controller, alerting on `changed>0`. It catches drift from any
source (hand edits, an unrendered fix) but needs check-mode-safe roles and
a controller that is up on schedule. Today's controller is a laptop.

Lean: (i) now, because it costs nothing and would have named `backups` on
every deploy since 10-01; (ii) once (i) has shown which roles change often
enough to matter. (iii) is the complement for drift that git cannot see.
Reto decides.

## Acceptance criteria (for (i))

- A deploy whose range touches `deploy/roles/backups/**` prints the
  `11-backups.yml` line; a range touching only precis roles prints nothing.
- A role no playbook names is printed as `no playbook`, never dropped.
- Tested with the fake-ansible harness in `tests/test_deploy_render_worktree.py`.
