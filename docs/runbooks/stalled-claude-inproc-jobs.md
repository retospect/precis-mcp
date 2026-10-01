# Stalled `claude_inproc` jobs — triage, restart, expedite

**When.** `executor=claude_inproc` jobs (`plan_tick` coroutines, `news_poll`,
`briefing`, quest ticks, card_forge) are queued or stuck and you need to decide
whether the worker is dead, blocked, or merely not selecting them.

## Topology (what to expect)

- `job_claude_inproc` runs on **melchior only**. ALL `claude_inproc` jobs
  cluster-wide drain through that **one single-threaded worker**; if it hangs,
  they all stall.
- Since ~2026-08-07Z the agent profile is folded into the single
  `com.precis.worker` unit on melchior (`precis worker --profile all
  --batch-size 32 --idle-seconds 2`), per the 2026-08-19Z check. The separate
  `com.precis.worker-agent` plist was retired ~2026-07-22Z (only a `.bak`
  remains). **Don't diagnose a stall from the daemon's absence:** a missing
  `com.precis.worker-agent` and a 0-byte, frozen
  `/var/log/precis-worker-agent.log` are both expected and mean nothing — two
  agents once read them as "the worker is dead".
  ⚠ Verify against the live host (`launchctl print`) before relying on this:
  [`cluster-logs`](./cluster-logs.md) still lists the agent log and the
  `playbooks/37-precis-worker-agent.yml` role still exists.
- Log: **`/var/log/precis-worker.log`** — grep for `job_claude_inproc`.
- `job_coordinator` (system profile, every node) is the *other* executor path;
  it shows `claimed=0` constantly because `claude_inproc` work doesn't route
  through it.
- History: the pass moved off the system profile onto the agent profile
  2026-06-15Z (`src/precis/cli/worker.py`, "Planner-coroutine slice"), then
  into `--profile all`.

## Diagnose

1. **`claimed=0` while jobs are queued ≠ dead worker.** The lane is polling and
   selecting nothing — a *selection* failure: `service_config` prio, `llm:`
   affinity vs advertised `resource_slots`, lease state, parked `child-failed`
   bubbles (a spend-limit park is permanent until the ref_tag is deleted).
   **No restart fixes it.** (Seen: 102 jobs queued for 3 days, lane alive,
   `claimed=0`.)
2. A job `STATUS:running` with **no `chunk_kind='job_event'` forensics** and the
   worker log frozen = the worker is **blocked**. `ps -o state` → `S`/`Ss` =
   I/O wait. The **sweeper** marks the DB row failed at 1 h
   (`swept:claim-orphaned`) but that does **not** unblock the OS thread.
3. Recover a blocked worker: `sudo launchctl kickstart -k
   system/com.precis.worker` on melchior. If the unit is gone entirely:
   [`worker-jetsam-bootout-recovery`](./worker-jetsam-bootout-recovery.md).
4. Precedent: `news_poll`'s `_default_parse_feed` called
   `feedparser.parse(url)` (un-timed urllib GET); one stalled RSS feed hung the
   worker 18 min, stalling all `claude_inproc` jobs (2026-06-25Z). Fix: fetch
   via `safe_get` (bounded + SSRF-guarded), parse bytes offline.

## Expedite one stuck job: prio is ASC — LOWER is more urgent

`src/precis/workers/executors/_common.py` claim ordering is
`COALESCE(prio, 5) ASC, queued_since ASC, ref_id ASC` (SQL `ORDER BY`; the
Python re-rank key is `(-scarcity, prio, …)`). Default prio is 5; a
`refs_prio_check` constraint forbids prio 0, so **use prio 1** for "claim this
next". Bumping prio *high* is backwards.

`_CLAIM_OVERFETCH=3`: the SQL fetches only `limit*3` rows in ASC order, so a
HIGH-numbered job sits at the bottom of that window and is never fetched →
never claimed (no lease ever stamped).

    scripts/prod-psql "UPDATE refs SET prio=1 WHERE ref_id=<job>"

(`agent_rw` is write-capable — [`prod-db-access`](./prod-db-access.md).) The
job kind has no `PRIO:` tag axis, so the column UPDATE is the only lever.
Scarcity is the *first* sort key, but plain `claude_inproc` jobs (plan_tick,
taproot_backfill) carry `requires=none` → scarcity 0, so prio decides.

## Read the code at the DEPLOYED sha, not your worktree

The prio direction flipped between a worktree base and the deployed sha, and a
scheduler model built from the worktree tip was exactly backwards (~20 wasted
probes). When debugging live-cluster dispatch, get the deployed sha from the
venv `direct_url.json` `commit_id` ([`cluster-deploy`](./cluster-deploy.md))
and read `git show <deployed>:<path>` — not the worktree tip.
