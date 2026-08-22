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

## 1. Confirm on a full-bounce deploy — DONE 2026-08-22, both directions

Verified on the first full-bounce deploy after the fix, and it happened to
exercise both cases without being staged:

- **melchior, balthazar** — cleared in seconds, `drain complete — running long
  jobs (lease alive) leased here: 0`.
- **spark** — retried 40 times (~20 min) and *then* reported `drain complete
  … 0`. Spark had a genuine in-flight long job with a live lease, and the
  deploy waited for it before bouncing.

That second one is the negative case this section asked for, so it needs no
separate staging. Before the fix all three hosts would have burned the full 60
retries and printed `timed out (proceeding anyway)` regardless of job count;
now the wait tracks actual work. The drain has both returned promptly when
idle and demonstrably waited when not.

## 2. Re-read job deaths that happened around a deploy

The drain had **never run** — not since it was written. Every full-bounce
deploy proceeded as if the cluster were idle and bounced whatever
`quest_tick` / `autocatpath_seed` / `autocatpath_aggregate` / `struct_relax` /
`taproot_backfill` job was in flight. Any past "job died for no reason" report
whose timestamp lands near a deploy has a candidate explanation now, and some
of those may have been filed as their own bugs. Worth a pass over open gripes
for that shape before investigating any of them further.
