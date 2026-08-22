---
status: draft
title: "the deploy's long-job drain has never run — a wrong become_user makes it fail open, silently, for 30 minutes on every full bounce"
---

# Deploy's job-drain wait is a silent 30-minute no-op

Found 2026-08-22 during the `50a0fbc5` deploy. Two costs: long jobs are
bounced mid-flight (the drain is a no-op), and every full-bounce deploy burns
30 idle minutes.

## Symptom

`scripts/deploy` sits in `TASK [Wait for in-flight long jobs leased to this
host to finish]` for exactly 30 minutes, then prints
`drain timed out (proceeding anyway)` and continues. Observed 15:10 → 15:40 on
a deploy where the drain condition was *already* satisfied — the same query run
against prod returned **0 rows** the whole time.

## Root cause

`deploy/redeploy-precis.yml` (the drain task, ~line 617) delegates the count
query to the data host and picks `become_user` with an os-split:

```yaml
become_user: >-
  {{ 'deploy' if hostvars[groups['data'][0]]['os_family'] | default('linux') == 'darwin'
     else 'postgres' }}
```

The data host is **caspar**, which is `Darwin` and runs postgres as **`deploy`**
(uid 806) — verified:

```
$ ssh caspar 'uname -s'                        → Darwin
$ ssh caspar 'ps -eo user,comm | grep postgres' → deploy  …/postgresql@17/bin/postgres
$ ssh caspar 'sudo -u postgres psql …'          → sudo: unknown user postgres
```

So `os_family` is **not defined** for the data host in the inventory overlay.
The `| default('linux')` fallback then selects `postgres`, a user that does not
exist on that machine, and every attempt dies with `sudo: unknown user
postgres`.

Two lines turn that into a silent stall:

- `failed_when: false` — the sudo error never fails the task.
- `until: (…stdout | default('') | trim) == '0'` — a failed command yields
  empty stdout, which never equals `'0'`, so it retries the full
  `precis_drain_retries: 60` × `delay: 30` = **30 minutes**, then gives up and
  proceeds.

The `default('linux')` is the trap: it makes an *undefined* inventory variable
look like a deliberate choice, and the failure it produces is indistinguishable
from "jobs are genuinely still draining."

## Why it matters beyond the 30 minutes

The drain has **never run**. Its whole job is to let in-flight `quest_tick`,
`autocatpath_seed`, `autocatpath_aggregate`, `struct_relax` and
`taproot_backfill` jobs finish before the daemons are bounced. Since the query
never returns a usable answer, every full-bounce deploy proceeds as if the
cluster were idle and kills whatever long jobs were running. Any "job died for
no reason around a deploy" report should be re-read with this in mind.

## Fix

Both halves, and the second matters more than the first:

1. **Correct the user.** Either define `os_family: darwin` for the data host in
   the (gitignored) inventory overlay, or — better, since it can't drift —
   resolve the DB superuser from the host's actual postgres owner instead of
   guessing from an os label.
2. **Stop swallowing the error.** A drain that cannot *ask* the question is not
   a drain that found zero jobs. Distinguish the two: fail loudly (or at least
   `debug` the stderr on the first retry) when the query itself errors, and
   keep `until`-retrying only on a genuine non-zero count. Right now the one
   safety mechanism in the deploy path fails open, silently, on every run.

A quick partial mitigation for an urgent deploy is `-e precis_drain_retries=1`,
which cuts the dead wait to 30 s — but it does not restore the drain.

## Verification

After the fix, a deploy on an idle cluster should clear the drain task in
seconds and report `drain complete`. Force the negative case by holding a
`STATUS:running` long job with a live `lease_until` and confirming the task
actually waits for it.
