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
> 2. An earlier draft blamed melchior specifically. **It is not host-specific.**
>    Melchior recovered at 17:22 and spark entered the identical state by
>    17:25. See "It moves between hosts". Do not scope a fix to one host.

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

## It moves between hosts

Melchior recovered at 17:22. By 19:05 **spark was in the same state**:

```
17:00:24  job_coordinator claimed=0
17:00:42  paper_reconcile / openalex_enrich / paper_meta_enrich / disk_check
17:25:11  summarize:rake-lemma claimed=32 ok=32      <- last entry, 1h40m of silence
```

This matters more than the melchior instance, because `quest_tick` jobs carry
`params.target_node: spark` and are claimed by **spark's** `job_coordinator`.
Job 207662 (quest 202469, minted 18:08) sat `queued`, unclaimed, for over an
hour for exactly this reason — melchior's coordinator polls but always claims 0,
since the work is targeted elsewhere.

Earlier the same afternoon spark was saturated with `bib_parse`/crossref calls
(15:54), which is the starvation `conditions.py` already names from 2026-08-12.
So this is at least the third occurrence, on two hosts, with two different
monopolizing passes.

## Probable mechanism (unconfirmed)

The common factor across all three occurrences is a high-volume derived-queue
pass that refills as fast as it drains — `summarize:rake-lemma` (32/batch, back
to back) on both hosts, `bib_parse` on spark, with `job_claude_inproc claimed=4`
opening melchior's rotations and going unaccounted for 30–60 minutes. The SLO
passes sit at the end of a rotation that never completes.

That the same shape appears on two hosts with different monopolizing passes
points at the rotation discipline itself rather than any one pass.

If that is right, the fix is not a bigger budget — it is that a saturating queue
must not share a rotation with the rescue passes. Candidates: give
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
