# Melchior's worker rotation starves the rescue passes for hours at a time

> Found 2026-08-14 while trying to observe a quest tick. The quest loop was the
> victim, not the cause. Filed because a single starved pass silently stops the
> whole quest subsystem, and the recovery takes hours with no bound on it.
>
> **Correction (same day):** an earlier draft of this file claimed
> `restart-worker` does not heal the state. It does — it just takes ~95 min.
> See "How it actually recovered" below. The defect is latency and silence,
> not a dead heal.

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

## How it actually recovered

`conditions.py` prescribes `HealRequest("restart-worker", host, process)` for
the pass-dead class. The 15:47 deploy restarted the worker (pid 74830,
`Fri Aug 14 15:47:20`). Timeline after that:

| time | state |
|---|---|
| 15:47 | worker restarted |
| 16:44 | rotation advanced, but still no `quest_loop_reconcile`; nothing minted |
| 17:22 | loops minted for all 4 active quests — **recovered** |
| 18:08 | normal re-mint cycle resumed |

So the heal works, at **~95 minutes**. During that window the subsystem is
indistinguishable from permanently broken by any available signal, which is
what made it look dead at the 57-minute mark. A manual
`reconcile_quest_loops(enabled=True)` run at 18:57 returned
`ensured=4, minted=0` — by then there was nothing left to fix.

## The part that needs a decision

Not "make the heal work" — it works. The questions are:

1. **Is ~95 min acceptable for the rescue tier?** Every active quest is frozen
   for that whole window, silently. If not, the SLO passes need a cadence
   independent of rotation position.
2. **Why does the gap open at all?** A restart should not take an hour and a
   half to reach a pass that had been running every ~2h.
3. **Nothing says "recovering".** The alerts fire on entry and the operator has
   no signal short of watching the rotation log. A "last ran / next due" per
   rescue handler would have answered this in one query.

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
