---
status: ready
title: Job scheduling fairness — origin classes on the job row, class-preserving ageing, a reserved interactive slot per lane, per-job_type caps for standing bands
pillar: platform
prio: normal
model: opus
---

# Job scheduling fairness

Asked by Reto 2026-10-22: "schedule evenly, with human jobs taking
precedence over background." Trigger: graph_garden jo504788 (recurring,
prio 2) waited 28.5 min on `job_inproc` behind prio-8 `derived_drain`.
Related: [worker-rotation-starves-rescue-passes](worker-rotation-starves-rescue-passes.md)
(rotation latency), [claude-inproc-lane-starves-old-jobs](claude-inproc-lane-starves-old-jobs.md)
(ageing shipped 2026-10-15), [cluster-scheduling](cluster-scheduling.md) (lane laws).

## Motivation / why

Claim today (`workers/executors/_common.py::claim_executor_jobs`, all
five executors): `ORDER BY aged_prio ASC, queued_since ASC, ref_id ASC`,
`aged_prio = max(prio - age/600 s, min(prio, 1.5))`
(`_aged_prio_sql`, `DEFAULT_CLAIM_AGE_STEP_S`, `_CLAIM_AGE_FLOOR`). No
per-job_type cap, no fair share, no reserved capacity. Prio is set by the
minter: recurring spawn 2 (`schedule/worker.py`), materialize bands /
diagnose / conflict sweep / draft refresh 8, `put(kind='job')` without
`prio=` NULL (reads 5). The job row records no actor: `set_by`,
`owner_login` are NULL and `trust_origin='unknown'` on all 993 jobs of the
last 72 h; the only origin signals are `meta.dispatched_from_todo`
(recurring) and `meta.idem_key` (system minters).

Prod, 72 h to 2026-10-22, queue wait = `meta.started_at - created_at`:

| lane / job_type | origin | n | prio | p50 | p95 | max (min) |
|---|---|---|---|---|---|---|
| job_inproc derived_drain | system | 547 | 8 | 0.0 | 70.5 | 121.9 |
| job_inproc embed_batch | system | 174 | 2,8 | 0.0 | 2.6 | 49.8 |
| job_inproc graph_garden | recurring/adhoc | 3 | 2/- | 28 | 52 | 52.2 |
| claude_inproc conflict_sweep | system | 59 | 8 | 64.6 | 75.3 | 77.5 |
| ssh_node autocatpath_seed | system | 84 | - | 69.1 | 890.6 | 1140.6 |
| ssh_node autocatpath_aggregate | recurring | 40 | - | 0.1 | 75.1 | 80.1 |
| coordinator quest_tick | system | 17 | - | 6.7 | 12.7 | 13.2 |

Recurring + ad-hoc jobs (the closest proxy for human intent): 7 of 53
(13%) waited over 5 min; every graph_garden run (3 of 3) waited 27–52 min.

Why jo504788 waited (queued 10:06:22Z, claimed 10:34:56Z on balthazar):

1. **Ageing collapses the classes.** Its own prio was 2. The materialize
   band keeps a standing queue of prio-8 `derived_drain` rows pinned to
   melchior; any row queued more than 65 min reaches the 1.5 floor, the same
   rank a prio-2 row reaches after 5 min. Ties break on `queued_since`,
   so on melchior the older background row always wins. Melchior's
   `job_inproc` workers claimed six prio-8 drains (queued 09:04–09:12)
   between 10:18 and 10:48Z, each a synchronous 11–36 min run.
2. **Balthazar's lane rides the shared rotation.** Its `job_inproc` pass
   runs every 7.0 min p50 / 13.4 p95 (max 25) with `limit=1`; it ran at
   10:05:51, 10:18:50 (claimed an older prio-2 embed_batch) and 10:34:57
   (claimed jo504788). Melchior's dedicated `job_inproc` processes poll
   every few seconds but were all busy draining.

So nothing reserved a slot for interactive work, and ageing, meant to
stop starvation of old background rows, made them outrank fresh foreground
ones.

## Design

### D1. Origin class on the job row

`meta.origin` ∈ `human | agent | recurring | background`, stamped at mint:

- `put(kind='job')` from the web UI / Discord bridge → `human`; from an
  MCP session → `agent` (the handler knows the transport). As built
  (slice 1): the web UI's authenticated write context is the only
  `human` door — the Discord bridge has no human context yet, so its
  puts stamp `agent` too.
- Recurring spawn (`dispatched_from_todo`) → `recurring`.
- Every `insert_ref` minter with an `idem_key` (materialize, sweeps,
  scans) → `background`. A helper `mint_background_job()` sets it so new
  minters can't forget.
- Missing (old rows) reads as `background` unless prio ≤ 2.

### D2. Class rank before prio

Claim key becomes `(class_rank, aged_prio, queued_since, ref_id)` with
`class_rank`: human 0, agent 1, recurring 2, background 3. Prio orders
inside a class. Prio 1 (preempt) stays absolute across classes.

### D3. Ageing that never crosses classes outright

Ageing stays, but moves a row at most one class up, and only after a
long bound: a background row queued longer than `PRECIS_CLAIM_CLASS_AGE_S`
(default 30 min) ranks as recurring, never as agent or human. Inside a
class, today's band ageing applies with the floor at the class's best
prio, so prio 8 can't tie prio 2 inside a class until it has waited
`6 × step`. Background never starves (bounded by the class-age step plus
its in-class age), and foreground never queues behind aged background.

### D4. Per-job_type fair share inside a class

Weighted round-robin by `job_type` inside a class: among candidates of
the winning class, prefer the job_type with the fewest running rows on
this lane (`running_by_type`, one aggregate in the claim tx), then the
key above. Weights default 1; `service_config` row
`(job_type, max_running)` caps a standing band, e.g. `derived_drain
max_running=2` per host so a band can never occupy every `job_inproc`
process.

### D5. Reserved interactive slot per lane

Each lane keeps `PRECIS_LANE_RESERVED_INTERACTIVE` (default 1) of its
concurrent processes for `human|agent|recurring` work: a claimer whose
claim would leave zero idle-or-interactive processes skips background
candidates. For a shared-rotation lane (balthazar `job_inproc`), the
same effect comes from a wake: `put(kind='job')` with origin
`human|agent` sends `NOTIFY job_queued_<executor>` and the runner runs
that executor's pass out of turn (the rescue-ageing path in
`workers/runner.py` already supports out-of-turn passes).

### D6. Visibility

`get(kind='job', id=N)` on a queued row shows class, aged rank, ahead
count by class (extends the existing "N ahead" hint in
`handlers/job.py`). `/factory` gets a wait p50/p95 per class.

## Slices

Ruled (Reto 2026-10-22): laissez-faire and monitor; adjust only if it
goes sideways. Keep one channel open for interactive work. Build slices
1, 4 and 6 (origin stamp, reserved interactive slot + wake, queue-wait
visibility by origin). Slices 2, 3 and 5 and the open questions below are
parked until the slice-6 numbers show a problem the open channel does not
solve; the runtime caps of D4 are not set now.

1. **Origin stamp (D1)** — handler + `mint_background_job()` + recurring
   spawn; backfill none. Pure data, no claim change. Ships first so the
   metrics in acceptance can be measured by class.
2. **Class rank + class-bounded ageing (D2, D3)** — `_aged_prio_sql` and
   `_order_key` in `_common.py`; the "N ahead" hint mirrors it.
3. **Per-job_type cap (D4 cap half)** — `service_config` `max_running`
   checked in the claim tx; set `derived_drain` to 2/host on melchior.
4. **Interactive wake (D5)** — NOTIFY on foreground put; runner runs the
   named executor pass out of turn. Reserved-slot rule for multi-process
   lanes.
5. **Round-robin by job_type (D4 share half)** — only if slices 2–4 leave
   a measured background-vs-background starvation (e.g. conflict_sweep p50
   65 min behind other prio-8 work).
   **Shipped 2026-10-24 (gr506754):** measured — the ``llm_summarize``
   band sat 0-running for hours behind ``classify`` on the shared capped
   ``derived_drain`` lane. The share key is ``job_type`` refined by the
   ``params.pass`` discriminator (both bands share one job_type, so a
   job_type-granular share can't separate them); the claim's ORDER BY and
   greedy pick prefer the fewest-running share inside the winning class,
   and the materialize stuck WARN/promotion fires only when the lane has
   free capacity.
6. **Visibility (D6).**

## Acceptance criteria

- 100% of jobs minted after slice 1 carry `meta.origin`; a contract test
  fails a new minter that inserts a job without it.
- Over any 72 h window after slice 4: `human|agent` jobs on every lane
  p95 queue wait ≤ 2 min and max ≤ 5 min while the lane is not reserved
  or capacity-gated (`requires` slot full is excluded and reported
  separately); `recurring` p95 ≤ 5 min.
- Background still drains: `derived_drain` p95 wait no worse than today's
  70 min, and no background row waits more than 3 h on a live lane.
- Unit tests in `tests/workers/` pin: a fresh prio-2 recurring row outranks
  a 2-h-old prio-8 background row; a 31-min-old background row outranks a
  fresh background prio-2 row but not a fresh recurring one; prio 1 beats
  everything; `max_running` refuses the (cap+1)th claim.
- Repeat of the 2026-10-22 shape (band backlog + graph_garden put) in the
  dev DB: graph_garden claimed within one pass.

## Explicitly NOT in scope

- Capacity: `autocatpath_seed` (p50 69 min, max 19 h) waits on GPU
  `requires` slots, not on ordering. More GPUs or fewer seeds fix it.
- Separately supervised lanes (option 3 of the rotation spec) — this
  spec works inside the current runner.
- Cross-host global scheduling; each host still claims for itself.

## Open questions (with recommendations)

1. **Is `agent` above `recurring`?** Recommend yes: an agent put is
   usually a chat waiting on it; a recurring tick tolerates minutes.
   Ruled (Reto 2026-10-23, relayed): yes — `agent` ranks above
   `recurring`.
2. **Does a chat agent's put get `human`?** Recommend no; keep `human`
   for UI/Discord so it stays rare and meaningful. Revisit if chat waits
   show up in the class metrics.
   Ruled (Reto 2026-10-23, relayed): no — a chat agent's put is `agent`,
   not `human`.
3. **Cap `derived_drain` by count or shorten its runs?** Recommend both:
   `max_running=2` now, and `params.limit` sized to ≤ 10 min so a slot
   frees often (today's runs are 11–36 min).
   Ruled (Reto 2026-10-23, relayed): both — `derived_drain` gets
   `max_running=2` and runs of at most 10 min.
4. **Keep the 1.5 floor at all?** Recommend replacing it with D3; a floor
   that equals the cron band is what collapsed the classes.
   Ruled (Reto 2026-10-23, relayed): replace it — the 1.5 floor is
   replaced by class-preserving ageing (D3).
5. **Where does prio stop mattering?** Recommend prio stays the in-class
   order and the only cross-class override is prio 1.
   Ruled (Reto 2026-10-23, relayed): as recommended — prio orders within
   a class, and only prio 1 crosses classes.
6. **What class is a worker-internal `put(kind='job')`?** `spawn_child`,
   the quest compute/loop re-mints and the pathway aggregate/handler
   mints go through `JobHandler.put` with no human context and would
   read `agent` — outranking `recurring` work once slice 2 reorders.
   Ruled (Reto 2026-10-23, relayed): option (a) — a job created by a
   worker through `put` takes its parent job's `meta.origin`, or
   `background` when it has none.
