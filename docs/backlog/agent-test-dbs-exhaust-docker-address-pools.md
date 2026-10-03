---
status: draft
pillar: platform
title: idle test-dbs of sessionless agent-* worktrees exhaust docker's address pools, failing every scripts/test fleet-wide; reap them in ~2 h, not 48 h
---

# Agent worktrees' idle test-dbs exhaust docker's address pools

## What happened

At about 14:40Z on 2026-10-03 melchior's docker refused new networks with
"all predefined address pools have been fully subnetted". Every
`scripts/test` in the fleet then failed before its first test. The cause was 7
`precis-test-agent-<hash>_default` compose networks, each holding the idle
test-db of a sessionless subagent worktree 3–27 h old. The orchestrator
cleared them with `PRECIS_REAP_DB_MIN_AGE_SECONDS=7200 scripts/reap-test-dbs`;
all 7 passed its no-session, stale-purpose and no-gate checks.

`scripts/reap-test-dbs` left them alone because its third pass reaps a
present-project DB only once the container is ≥ 48 h old
(`PRECIS_REAP_DB_MIN_AGE_SECONDS`, default 172800). That age guard protects
thread sessions that pause and resume. A subagent never resumes its session,
so for an `agent-*` tree the guard only delays the reap.

Headroom: docker's default pools give about 30 networks. About 22 thread
networks are permanent, which leaves room for roughly 8 agent trees. One
busy session dispatches that many in an afternoon, so this recurs within a
day without a fix.

## Fix

1. **Reap agent trees sooner.** In `scripts/reap-test-dbs`, a worktree
   whose name starts with `agent-` gets its own minimum age (default about
   2 h, `PRECIS_REAP_AGENT_DB_MIN_AGE_SECONDS`). The other checks are
   unchanged: no live session, no fresh purpose, no running gate container.
2. **Or tear down at the source.** In `scripts/test`'s EXIT trap, when the
   tree is an `agent-*` worktree, run `compose down` for its project after
   the run. The subagent will not reuse the warm DB, so warm-start is worth
   nothing there. Cheaper than 1 and immediate, but it costs a cold start
   when one subagent runs `scripts/test` several times. Doing 1 and 2
   together is fine; 1 alone is enough to stop the recurrence.
3. **Name the remedy.** When `docker compose up` fails with "fully
   subnetted", `scripts/test` prints
   `docker is out of address pools: scripts/reap-test-dbs (PRECIS_REAP_DB_MIN_AGE_SECONDS=7200 for agent trees)`
   instead of the bare compose error.

## Acceptance criteria

- An `agent-*` worktree with no session, a stale purpose and a test-db
  container 3 h old is reaped by a default `scripts/reap-test-dbs` run; a
  thread worktree in the same state is not.
- A simulated "fully subnetted" compose failure prints the hint line.
- `tests/test_worktree_lock_reap.py`'s carve-out tests still pass.
