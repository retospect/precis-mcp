---
status: idea
title: SMALL derived-drain throughput starvation — 12 jobs/day vs 184k-chunk backlog
---

# SMALL derived-drain throughput starvation

Found diagnosing gr204385 (2026-08-14). The melchior-pinned `derived_drain`
lane (summarize + classify SMALL queues) drains ~1 job per ~2h — the
`job_inproc` pass claims `limit=1` per rotation tick, each job chews
`params.limit=500` chunks (~30 min on the local SMALL model), and the
collapsed worker's serial rotation only hands the pass a turn every couple
of hours. Net throughput ≈ 12 jobs/day ≈ 6k chunks/day against a 184k-chunk
backlog — weeks of lag, and any band at its 50-job cap (classify's 50 sat
queued 3 days without being *reached*) looks exactly like a dead handler.

The design contract this violates: `job_inproc`'s module docstring —
"Every job_type on this lane MUST self-limit its own work (minutes, not
hours)". A 30-min drain job starves the whole lane; the band-full watchdog
("50 live, 0 running") fires on the idle 75% of each cycle and reads as a
wedge.

Fix is design-tier, options not exclusive:

- **Smaller jobs, more turns** — cut `_DEFAULT_SMALL_DRAIN_LIMIT` (500)
  so a job is minutes, AND raise job_inproc's per-tick `limit` so
  throughput doesn't drop further. Only helps if the rotation ticks often;
  measure melchior's actual rotation period first.
- **Dedicated drain lane** — a second tiny worker unit on melchior running
  only job_inproc (the 43-compute-lane pattern, `--only job_ssh_node`
  precedent) so SMALL drain isn't time-sliced against the full rotation.
- **Queue fairness** — claim order is prio ASC, ref_id ASC; two same-prio
  bands mean one source's older refs starve the other's entire batch.
  Round-robin per `params.pass` (or per-band prio staggering at mint) if
  both lanes should make progress concurrently.

Sizing input: watch whether the classify batch (refs 203732–203781) drains
on its own by ~2026-08-16; gr204385's residuals (role3 never written,
`chunks_classified` watching an empty namespace) are only checkable after
it does.
