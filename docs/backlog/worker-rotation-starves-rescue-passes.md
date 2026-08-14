# Worker rotations starve the rescue passes for hours at a time

> Found 2026-08-14 while trying to observe a quest tick. The quest loop was the
> victim, not the cause. Filed because a single starved pass silently stops the
> whole quest subsystem, and the recovery takes hours with no bound on it.
>
> **Two corrections during the same investigation**, both worth keeping because
> each one changed the diagnosis:
>
> 1. An earlier draft claimed `restart-worker` does not heal the state. It
>    does — it just takes ~95 min. See "How it actually recovered".
> 2. A later draft generalized this to spark as a second instance. **That was
>    wrong** — spark was busy, not starved. See "Spark is NOT a second
>    instance". The evidenced occurrence is melchior's, and it is the one a fix
>    should target.

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

## Spark is NOT a second instance — don't chase it

A draft of this file claimed spark entered the same state, on the evidence that
its log showed no `runner worker:` completion line between 17:25 and 19:05.
**That inference was wrong and is recorded here so nobody repeats it.**

`runner worker:` lines only emit when a pass *completes*, so a long pass looks
identical to a dead worker if you grep only for them. Spark's worker was in
fact busy throughout: raw log writes at 19:05, Semantic Scholar queries (some
429-throttled), and a steady stream of `autocatpath_seed` /
`autocatpath_aggregate` jobs succeeding with leases out to 21:2x. It was also
claiming quest ticks normally — 207632 (quest 202468) ran until 19:09.

Job 207662 (quest 202469, minted 18:08) did sit `queued` for over an hour, but
the explanation is contention with heavy pathway work, not a starved rotation.

**Diagnostic lesson:** to tell a starved rotation from a busy one, check the raw
log tail and `ps` uptime, not the pass-completion lines. Melchior's case is real
because `quest_loop_reconcile` had a *known 2h cadence* it stopped meeting and
two independent prod alerts fired; neither of those held for spark.

## Probable mechanism (unconfirmed)

On melchior, every full rotation opens with `job_claude_inproc claimed=4` and
the worker is then unaccounted for 30–60 minutes, with `summarize:rake-lemma`
(32/batch, back to back) filling the rest. `claude_inproc` jobs run only on
melchior and are long. The SLO passes sit at the end of a rotation that, under
that load, does not come round within its budget.

This is one corroborated occurrence, not a pattern — see the spark section for
an inference that looked like a second one and wasn't. `conditions.py` does
record an earlier 2026-08-12 `bib_parse` starvation, so melchior's is at least
the second in three days.

If the mechanism is right, the fix is not a bigger budget — it is that a
saturating queue must not share a rotation with the rescue passes. Candidates: give
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
