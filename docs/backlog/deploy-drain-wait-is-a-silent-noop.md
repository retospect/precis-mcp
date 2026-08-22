---
status: draft
title: "deploy drain: confirm it actually clears on the next full-bounce, and re-read historical job deaths that happened around a deploy"
---

# Deploy drain — residual verification after the 2026-08-22 fix

The drain itself is fixed (see `tests/test_scripted_psql_skips_psqlrc.py` for
the root cause and the class it belongs to: `psql` sourced the DB user's
`~/.psqlrc`, whose `\timing on` appended a line to the `-tAc` scalar the
`until:` compared against, so the comparison could never succeed at any job
count). Two things it leaves open.

## 1. Confirm on the next full-bounce deploy

`psql -X` now returns a bare `0` as `deploy` on caspar, so the `until` *can*
be satisfied — but that's verified at the shell, not through ansible. On the
next `scripts/deploy` with `precis_bounce_scope: full`, check that
`TASK [Wait for in-flight long jobs leased to this host to finish]` clears in
seconds and reports `drain complete` rather than stalling 30 minutes.

Force the negative case too, once: hold a `STATUS:running` long job with a
live `lease_until` and confirm the task actually waits for it. The drain has
never demonstrably waited for anything, so "it returns 0 quickly" and "it
works" are still different claims.

## 2. Re-read job deaths that happened around a deploy

The drain had **never run** — not since it was written. Every full-bounce
deploy proceeded as if the cluster were idle and bounced whatever
`quest_tick` / `autocatpath_seed` / `autocatpath_aggregate` / `struct_relax` /
`taproot_backfill` job was in flight. Any past "job died for no reason" report
whose timestamp lands near a deploy has a candidate explanation now, and some
of those may have been filed as their own bugs. Worth a pass over open gripes
for that shape before investigating any of them further.
