---
status: draft
title: Worker-generation epoch on the compute-job lease — instant restart-recovery without racing a live dispatch
model: opus
---

# Worker-generation epoch on the compute-job lease

## Motivation / why

An `ssh_node` compute job (`autocatpath_explore`, `struct_relax`) is claimed
with a lease sized to outlive a real multi-hour GPU run —
`_LEASE_FLOOR_S = 7200` (2h), `max(floor, wall + margin)`
(`workers/executors/ssh_node.py`). That long lease is deliberate: it is the
death-presumption signal, and a *live* dispatch must never be stolen out from
under itself.

The cost of that design: when the claiming worker **restarts mid-dispatch**
(a deploy bounce, a jetsam cull), the compute process dies but the lease still
has hours to run. Recovery is gated on lease *expiry* — the executor's own
`reclaim_stale_running` (and any external reaper) only treats a
`STATUS:running` row as claimable once `lease_until < now()`. So a job whose
compute died at t=0 sits `STATUS:running` doing nothing until t≈2h, then is
re-run; another restart costs another ~2h; after `_MAX_ATTEMPTS = 3` it
poison-fails — a **~3–6h wedge** during which the quest `_phase_tick`
backpressure gate blocks new batches (gr172886 part-b, comment 3).

gr172886 part-(b) **Option B** (the dead-node reap in the sweeper, shipped
separately) covers the case where the worker stays **dead** — it terminalizes
the orphan once the node's worker is provably gone. It does **not** help the
common **quick-restart-mid-lease** case: the worker comes back within minutes,
is alive again, so the dead-node reap correctly abstains — and we are back to
waiting out the full 2h lease before the live worker's own steal fires. This
proposal closes *that* half.

## In scope

Stamp the identity of the claiming worker **process** onto the running job's
lease, so a *different* process can prove the holder is gone without waiting
for the clock:

- **A per-process generation token** (`worker_boot_id`) minted once at worker
  startup (a uuid, or the process start timestamp) and advertised in a durable,
  already-heartbeated place — extend the `host_heartbeat` row, or a sibling
  `worker_heartbeat(host, process, boot_id, ts)` row, so the *current* live
  boot-id per (host, process) is queryable.
- **On claim** (`claim_executor_jobs` reserve step, and the ssh_node lease
  stamp at `ssh_node.py:154`): write `meta.lease_boot_id = <this worker's
  boot_id>` alongside `lease_until`.
- **On reclaim** (`reclaim_stale_running` in
  `workers/executors/_common.py:302`): additionally treat a `STATUS:running`
  row as claimable when its `meta.lease_boot_id` is non-null and **differs from
  the current advertised boot-id** for that (host, process) — i.e. the process
  that held it has been replaced — *even if `lease_until` has not yet expired*.
  A live holder shares the current boot-id, so it is still never stolen.

Net effect: a restarted worker reclaims its dead predecessor's in-flight jobs
on its **first claim pass** (seconds), not after the 2h lease. General — the
mechanism lives in the shared claim/lease path, so every executor that opts
into `reclaim_stale_running` inherits it.

## Explicitly NOT in scope

- **The dead-worker case** — a worker that restarts and never comes back is
  Option B's job (the sweeper dead-node reap). This proposal assumes a *live
  successor* exists to do the reclaiming; it makes that successor fast, it does
  not replace B.
- **Shortening the lease itself** or adding a mid-run heartbeat/renewal thread
  (that was Option C, rejected: a hung-but-alive process would renew forever).
- **Cross-node stealing.** The node-gate (`meta.params.target_node`) still
  binds a pinned job to its node; the epoch only lets the *same node's* new
  worker generation reclaim faster.
- Any change to how a *physical* verdict (converged / desorbed / ruled-out) is
  recorded — this is purely infra crash-recovery latency.

## Acceptance criteria

- A `STATUS:running` ssh_node job whose `meta.lease_boot_id` ≠ the current
  advertised boot-id for its (host, process) is reclaimed on the next claim
  pass **while its `lease_until` is still in the future**, and re-runs
  (attempts bumped, poison-guard intact).
- A `STATUS:running` job whose `lease_boot_id` **equals** the current live
  boot-id is **never** stolen, regardless of lease age — proven by a test that
  seeds a live holder and asserts a second claim pass leaves it running.
- A job with a null `lease_boot_id` (pre-migration / non-epoch executor) falls
  back to today's lease-expiry-only behaviour — no regression for
  `claude_inproc` / `coordinator`.
- Slot reservations are still refunded on a stolen (crash-recovered) row
  (`meta.reserved` handling in `claim_executor_jobs` unchanged).
- Measured: worst-case restart-recovery latency drops from ~lease-floor (2h) to
  ~one claim-pass interval.

## Target + blast radius

- `src/precis/workers/executors/_common.py` — `claim_executor_jobs`
  (`reclaim_stale_running` predicate + lease-boot-id stamp on reserve).
- `src/precis/workers/executors/ssh_node.py` — lease stamp (`~154`) writes
  `lease_boot_id`.
- Worker startup + heartbeat (`src/precis/cli/heartbeat.py`,
  `cli/worker.py`) — mint + advertise the per-process boot-id.
- Store: a new heartbeat column/row for the live boot-id (forward migration).
- **Interacts with**: the sweeper dead-node reap (Option B) and the nursery
  dead-worker detector — all three read worker liveness; keep the signal
  single-sourced. This touches the crash-recovery contract for **every**
  executor, so it is protocol surgery — gate behind tests that assert a live
  holder is never stolen (the sweeper.py:625 exclusion invariant).

## Open questions / decisions log

- **boot-id source** — uuid minted at startup vs. process start-timestamp.
  Timestamp is free and monotone but collides if two workers on a host start in
  the same second; a uuid is unambiguous. Lean uuid.
- **advertise where** — extend `host_heartbeat` (needs a `process` dimension it
  may not have today) vs. a new `worker_heartbeat` row. A new row is cleaner and
  keeps `host_heartbeat` host-scoped; costs a migration + a write per loop.
- **Is the 2h wedge frequent enough to justify protocol surgery?** comment 2
  downgraded the original incident to a non-orphan; Option B covers the scary
  dead-worker half. Revisit priority after B has been in prod a while and we can
  count real quick-restart-mid-lease wedges (search `reaped:dead-node-orphan`
  vs. actual observed 2h stalls). If they're rare, this stays a filed proposal.
