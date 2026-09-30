---
status: idea
title: a subagent that exits without reaping its scripts/test child leaves a fleet-wide gate slot held until a human notices — cost ~2h of gate throughput on 2026-09-28
pillar: platform
---

# Orphaned `scripts/test` runs hold gate slots that nothing will ever release

## What

`scripts/lib/gate-slot.sh` is an N-slot (default 2) mkdir-mutex on the git
common dir, taken for the whole life of a `scripts/test` / `scripts/ship`
container run. Reclaim is deliberately conservative: on this host the holder
pid decides — dead pid steals at once, **live pid waits however long its gate
takes**, with the 45-minute timer applying only where the pid cannot decide
(a foreign host, or a holder file never written). That rule is correct and
was added on purpose; the header records that the timer used to fire on live
local holders and let a third gate into a 2-slot semaphore, causing the very
OOM churn the guard exists to stop.

The gap is that "live pid" and "someone is waiting on this result" are not
the same thing. A subagent can spawn `scripts/test`, finish its work, report
back, and exit — leaving the child alive and holding a slot that no one will
ever consume the output of. The pid is live, so the semaphore waits it out
forever.

## Observed, 2026-09-28

Two orphans were found holding both slots at 1h21m and 2h18m elapsed, left
by subagents that had already finished and reported. One of those agents
stated in its own report that its queued run had never got a container and
that it would not block on it further — then exited, leaving the process
holding the slot.

Cost, measured across sessions that day:

- a `witty-tinkering-hopper` verification run sat 65+ minutes and was
  abandoned unrun, so `169d69ee` (a real plane-drop-via short fix) landed on
  `main` unverified
- a `deployer` settle-up gate could not start for over an hour
- a `shimmering-weaving-cosmos` settle-up gate queued and was stopped
- at least three sessions independently misdiagnosed the cause

## Reading the signal

`docs/runbooks/gate-queue-bypass-named-files.md` already carries the
diagnosis half — a slot is a holder file and not a container, `ps` elapsed
far exceeding container age is queue wait rather than a hang, and
`PRECIS_GATE_SLOTS` must not be raised. That runbook is the place to look
when a queue is deep; nothing here repeats it. What it does not cover is
*why an orphan exists in the first place*, which is the gap below: every
signal it describes reads identically for a healthy long-queued run and for
a run nobody will ever consume.

## Fix — sketch, not decided

The reclaim rule should not be loosened; a live-pid-waits semaphore is what
keeps the VM alive. Better to stop creating orphans and to make the state
legible:

1. **Reap on agent exit.** A subagent that starts `scripts/test` should kill
   it when it exits without consuming the result — process-group kill, or a
   parent-death watchdog in `scripts/test` that releases the slot when its
   invoking shell is gone. This is the actual fix; the rest is mitigation.
2. **Record intent alongside the pid.** The holder file could carry the
   invoking session/agent id, so a waiter can tell "live agent still waiting
   on this" from "pid alive, owner gone".
3. **Surface the queue.** A `scripts/inflight`-style line — who holds each
   slot, elapsed, and whether its container is doing work — would have
   turned a two-hour mystery into a glance. Every session that hit this had
   to hand-roll `cat`ting two holder files plus `docker stats`.

## See also

`docs/backlog/per-agent-green-is-not-integrated-green.md` — the other
verification gap from the same day.
