---
status: built
title: Worker-generation epoch on the compute-job lease — instant restart-recovery without racing a live dispatch
model: opus
---

> **Built** (§H cycle c, `docs/proposals/cluster-scheduling.md` §H-
> lifecycle): shipped BEYOND this proposal's original scope — decided as
> one six-piece round with the master's §H-lifecycle acceptance criteria.
> `worker_boot_id` (`workers/heartbeat.py`, minted at startup, advertised
> in `host_heartbeat.meta.boot_ids` — the "extend host_heartbeat" open
> question below is DECIDED that way, no migration); the epoch-aware
> claim/reclaim + lease-identity stamp generalized to **every**
> `reclaim_stale_running` executor, not just `ssh_node` — `claude_inproc`
> and `claude_docker` opted in too (`executors/_common.py`'s
> `claim_executor_jobs`); the attempt cap moved into the shared claim path
> and made epoch-vs-expiry-aware (`poison_guard` — an epoch reclaim never
> burns it, only an expiry reclaim does); the sweeper's wall-clock orphan
> sweep retired for those three lease-owning executors (`sweeper.py`);
> `ssh_node` gained a detached submit/poll protocol (gr187627 — the
> original blocking-dispatch starve), backward-compatible with a legacy
> `dispatch` plugin (deprecation-warned); and the
> `wake_runner` gained a child-deadlock deadline so a `waiting_children`
> coordinator parent can't block forever on a permanently-stuck child.
> The "Explicitly NOT in scope" section below (the dead-worker case,
> cross-node stealing, physical-verdict recording) is still true — those
> remain the sweeper dead-node reap's job, not this mechanism's.

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

## Acceptance criteria (as shipped, §H cycle c — see also the master's own
   §H-lifecycle acceptance list)

- A `STATUS:running` job (any of `ssh_node` / `claude_inproc` /
  `claude_docker`) whose `meta.lease_boot_id` ≠ the current advertised
  boot-id for its (host, process) is reclaimed on the next claim pass
  **while its `lease_until` is still in the future**, and re-runs — **but
  this epoch-reason reclaim does NOT bump `meta.attempts`** (redeploy
  churn is not a crash-loop; refined from the original draft's "attempts
  bumped" — only an *expiry*-reason reclaim, a same-generation hang,
  counts toward the poison guard).
- A `STATUS:running` job whose `lease_boot_id` **equals** the current live
  boot-id is **never** stolen, regardless of lease age — proven by a test that
  seeds a live holder and asserts a second claim pass leaves it running.
- A job with a null `lease_boot_id` falls back to today's lease-expiry-only
  behaviour. Two cases stamp null, not just one: a caller that never
  minted a boot_id at all, AND — closing a review gap (Finding 3) — a
  worker that minted one but never *advertised* it (no `PRECIS_PROCESS`
  set: `mint_boot_id` succeeds, but `_own_boot_ids_meta` requires both a
  boot_id and a process name to advertise). `_this_worker_lease_identity`
  stamps `(None, None, None)` for that second case too — an unadvertised
  boot_id must never be stamped as `lease_boot_id`, or the epoch arm's
  "no live advertisement" COALESCE-sentinel reads it as *provably gone*
  and steals a genuinely live holder on the very next claim pass.
  `coordinator` never opts into `reclaim_stale_running` at all, so it's
  unaffected by any of this — it keeps depending on the sweeper's
  wall-clock backstop (piece 6 deliberately excludes it from the
  retirement).
- Slot reservations are still refunded on a stolen (crash-recovered) row
  (`meta.reserved` handling in `claim_executor_jobs` unchanged).
- Measured: worst-case restart-recovery latency drops from ~lease-floor (2h) to
  ~one claim-pass interval, for all three lease-owning executors, not just
  `ssh_node`.

## Target + blast radius (as shipped, §H cycle c)

- `src/precis/workers/executors/_common.py` — `claim_executor_jobs`
  (epoch + expiry reclaim arms, uniform lease-identity stamp on every
  claim, `poison_guard` — the generalized, epoch-aware attempt cap).
- `src/precis/workers/executors/ssh_node.py` — lease stamp; the detached
  submit/poll protocol + legacy-`dispatch` deprecation fallback (gr187627).
- `src/precis/workers/executors/claude_inproc.py` /
  `claude_docker.py` — opted into `reclaim_stale_running` +
  `poison_guard` too (not `ssh_node`-only, per the master's decided
  round).
- `src/precis/workers/executors/coordinator.py` /
  `src/precis/workers/wake_runner.py` — the `children_done` wake-deadline
  (§H piece 5, a separate but related crash-recovery gap the same round
  closed: "a parent never blocks forever on a child").
- `src/precis/workers/sweeper.py` — the wall-clock orphan sweep retired
  for the three lease-owning executors (`_LEASE_OWNING_EXECUTORS`);
  `coordinator` deliberately excluded from that retirement (no reclaim
  path of its own).
- Worker startup + heartbeat (`src/precis/workers/heartbeat.py`,
  `cli/worker.py`) — mint + advertise the per-process boot-id.
- Store: **no migration** — `host_heartbeat.meta.boot_ids` (JSONB),
  nested-merged per process on write (`store/_heartbeat_ops.py`).
- **Interacts with**: the sweeper dead-node reap (Option B, unchanged —
  still `ssh_node`-only, still the true dead-worker backstop) and the
  nursery dead-worker detector — all three read worker liveness; kept
  single-sourced. This touched the crash-recovery contract for every
  `reclaim_stale_running` executor, so it was protocol surgery — gated
  behind tests that assert a live holder is never stolen (the epoch arm
  never fires on a matching `lease_boot_id`, regardless of lease age).

## Open questions / decisions log

- **boot-id source** — uuid minted at startup vs. process start-timestamp.
  Timestamp is free and monotone but collides if two workers on a host start in
  the same second; a uuid is unambiguous. Lean uuid. **DECIDED (shipped):**
  uuid4 hex, `workers/heartbeat.py::mint_boot_id`.
- **advertise where** — extend `host_heartbeat` (needs a `process` dimension it
  may not have today) vs. a new `worker_heartbeat` row. A new row is cleaner and
  keeps `host_heartbeat` host-scoped; costs a migration + a write per loop.
  **DECIDED (shipped):** `host_heartbeat.meta.boot_ids: {process: boot_id}` —
  zero migration (meta is JSONB), a nested per-process merge on write so a
  host running two profiles never clobbers the other's advertised
  generation (`store/_heartbeat_ops.py::record_heartbeat`).
- **Is the 2h wedge frequent enough to justify protocol surgery?** comment 2
  downgraded the original incident to a non-orphan; Option B covers the scary
  dead-worker half. **SUPERSEDED:** a live incident (gr187627, 2026-08-02 —
  ssh_node's blocking dispatch starving the claiming worker's whole pass
  rotation, tripping host-dark) made the case moot — the master
  `docs/proposals/cluster-scheduling.md` §H-lifecycle decided the full
  six-piece round (this proposal's mechanism generalized to every
  `reclaim_stale_running` executor, plus the attempt-cap/sweeper/submit-
  poll/wake-deadline pieces) in one ship rather than waiting to count
  wedges further.
