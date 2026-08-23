---
status: draft
title: "deploy drain: re-read historical job deaths that happened around a deploy (drain itself fixed + verified)"
---

# Deploy drain — residual verification after the 2026-08-22 fix

The drain itself is fixed (see `tests/test_scripted_psql_skips_psqlrc.py` for
the root cause and the class it belongs to: `psql` sourced the DB user's
`~/.psqlrc`, whose `\timing on` appended a line to the `-tAc` scalar the
`until:` compared against, so the comparison could never succeed at any job
count). Full-bounce verification is done (2026-08-22: melchior/balthazar
cleared in seconds when idle; spark demonstrably waited ~20 min for a live
lease — both directions exercised). One thing it leaves open.

## Re-read job deaths that happened around a deploy

The drain had **never run** — not since it was written. Every full-bounce
deploy proceeded as if the cluster were idle and bounced whatever
`quest_tick` / `autocatpath_seed` / `autocatpath_aggregate` / `struct_relax` /
`taproot_backfill` job was in flight. Any past "job died for no reason" report
whose timestamp lands near a deploy has a candidate explanation now, and some
of those may have been filed as their own bugs. Worth a pass over open gripes
for that shape before investigating any of them further.
