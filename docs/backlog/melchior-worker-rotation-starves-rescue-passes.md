# Melchior's worker rotation starves the rescue passes; restart-worker does not heal it

> Found 2026-08-14 while trying to observe a quest tick. The quest loop was the
> victim, not the cause. Filed because the condition system detected this
> correctly and its prescribed heal did not work.

## Symptom

Melchior's `precis-worker` (`--profile all`) is alive and claiming work, but its
pass rotation has collapsed. Rotation log, 2026-08-14:

```
11:45  job_claude_inproc claimed=4 ok=4
12:21  job_claude_inproc claimed=4 / job_coordinator / job_ssh_node
12:27  job_inproc / wake_runner / auto_check / schedule
12:46 – 15:47   summarize:rake-lemma ONLY (five batches, ~3h)
16:44  job_claude_inproc claimed=4 / job_coordinator / job_ssh_node
```

`quest_loop_reconcile` last ran **10:23**. `sweeper`, `scheduler`,
`quota_check`, `tag_embeddings` likewise went silent. Prod raised the right
alerts unprompted:

- `207243 [condition] rescue-gap:quest_loop_reconcile`
- `pass-dead:melchior/precis-worker/{sweeper,scheduler,quota_check,tag_embeddings}`

## Why it matters

`quest_loop_reconcile` is the only thing that re-mints a `quest_tick`
coordinator loop after the sweeper terminalizes it. With the pass dead:

- all four quest loops were swept `cancelled` at 11:26 (normal — `quest_tick`
  is in `_SWEEP_CANCEL_JOB_TYPES`, cancel means "re-mint now"),
- nothing re-minted them,
- **no quest has ticked since 10:23** — dossier 202546's narrative has been
  frozen since 00:32.

So a single starved pass silently stops the entire quest subsystem. The
`_RESCUE_HANDLERS = ("sweeper", "nursery", "quest_loop_reconcile")` tier exists
precisely to prevent this and did not.

## The part that needs a decision

`conditions.py` prescribes `HealRequest("restart-worker", host, process)` for
the pass-dead class. **That heal ran and did not fix it.** The 15:47 deploy
restarted the worker (pid 74830, `Fri Aug 14 15:47:20`); 57 minutes later
`quest_loop_reconcile` still had not run and no `quest_tick` had been minted.
A restart therefore does not clear this state — the rotation re-collapses.

## Probable mechanism (unconfirmed)

Every full rotation opens with `job_claude_inproc claimed=4`, then the worker
is unaccounted for 30–60 minutes. `claude_inproc` jobs run only on melchior and
are long; four claimed at once plausibly monopolizes the cycle, with
`summarize:rake-lemma` (32/batch) filling the rest. The SLO passes sit at the
end of a rotation that never completes.

If that is right, the fix is not a bigger budget — it is that a long agent job
must not sit in the same rotation as the rescue passes. Candidates: give
`_RESCUE_HANDLERS` a dedicated cadence independent of rotation position, cap
`job_claude_inproc` concurrency on the shared worker, or move claude_inproc to
its own process (the `agent` profile exists; melchior currently runs `all`).

**Do not treat the alerts as noise.** They were correct and early; the gap is
between detection and remediation.

## Verify

```
ssh melchior "grep 'runner worker:' /var/log/precis-worker.log | tail -20"
scripts/prod-psql "SELECT ref_id, title FROM refs WHERE kind='alert'
  AND title LIKE '%rescue-gap%' AND deleted_at IS NULL"
```

Healthy looks like `quest_loop_reconcile` appearing every ~2h (its cadence
through 10:23 today) and `quest_tick` jobs re-minting within minutes of a sweep.
