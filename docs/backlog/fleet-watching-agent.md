---
status: draft
title: Fleet watching-agent — standing load manager (T0 telemetry → T3 review)
model: opus
---

# Fleet watching-agent — standing load manager (T0 telemetry → T3 review)

## Motivation / why

The 2026-08-10 manual utilization watch (8 samples, ssh fan-out + Opus
judgment every 20 min) found: a 2-day silent embed-lane outage behind an
alive-but-wedged daemon, a 7-week silent agent-lane starvation, a
restore-driven swap storm on the pg host, an idle DeepSeek serving pair,
and a fleet running at ~20% of budgeted capacity. Every one maps to a rule
or a deterministic scheduler decision — the expensive part was only ever
*noticing*. The steady-state shape: reuse workers on every host, the
nursery tier, recurring todos, and the materialize.py demand pattern; LLM
judgment only where rules can't reach.

## In scope — four tiers, cheapest first, each independently shippable

1. **T0 telemetry (no model).** A `fleet_sample` worker pass on every host
   (self-reporting; no ssh): every ~5 min INSERT one row into a new
   `host_samples` table — host, ts, load1, cores, mem_used/free,
   swap_used/total, gpu_util, gpu_power, daemons jsonb (alive per expected
   unit), extras jsonb (llm slot busy-count, pg backend count). 14-day
   retention. ALSO: stamp `claimed_at` / `completed_at` / `wall_seconds`
   into every job's meta at claim/complete time (today only plan_tick has
   wall_seconds) — per-job resource attribution and slot-cost calibration
   both hang off this.
2. **T1 reflex rules (nursery SQL).** Over host_samples + the job queue:
   swap > 6G on the pg host; expected daemon dead; **end-to-end lane
   probes** — jobs of lane L queued AND zero L successes over 60 min →
   critical (embed lane shipped 6b1f27f5 as `_detect_embed_lane_stalled`;
   generalize per-lane) — process-exists and even /readyz pass in
   alive-but-wedged modes; **drain-stall** — pending of type T > high-water
   AND zero completions 60 min → alert + pause the materializer's minting
   for T; clock skew > 5 min. Small whitelisted runbook actions only.
3. **T2 governor (deterministic scheduler pass).** Fleet-singleton ~5-min
   pass doing slot-based balancing: per-job_type slot costs (calibrated
   from T0), per-host budgets from live host_samples minus non-precis
   baseline; per-host-class utilization composite (worker boxes
   max(load/cores, active-mem); serving boxes = llm-slot busy-fraction;
   pg host EXCLUDED — alert on >60% instead); refill rule: U < 0.5 with
   eligible deferrable work pending → raise that host's claim concurrency /
   release bulk work (embed, classify, backfills, summaries); U > 0.8 →
   cap. Backpressure counts PENDING, not just leased (materialize.py's
   blind spot). Absorbs/replaces materialize.py hysteresis.
4. **T3 judgment agent (sonnet, 6–12 h recurring).** `fleet_review`
   agent-lane job: reads a day of host_samples + T1 alert history, files
   gripes/todos for anomalies rules missed and proposes threshold/slot
   changes. Proposals only — never mutates the fleet.

## Explicitly NOT in scope

- Any autonomous prod mutation beyond the T1 whitelisted runbooks.
- Cross-host embedder routing, castor slot expansion (separate decisions).
- Replacing the deploy/ansible layer.

## Acceptance criteria (per tier, gate each tier's own ship)

- T0: host_samples rows arrive from every node; jobs carry
  claimed_at/completed_at/wall_seconds; nightly retention DELETE works.
- T1: each rule has a fixture test (fires on the incident signature, quiet
  on healthy data); embed-lane rule generalized to per-lane.
- T2: simulation test — given synthetic host_samples + queue, the governor
  raises/caps concurrency per the composite and never releases beyond free
  slots counting pending.
- T3: recurring todo exists, runs on the agent lane, output is a filed
  digest; zero mutating verbs.

## Open questions / decisions log

- Slot-cost v0 from the 2026-08-10 watch (session plan): NEB=8
  (GPU-exclusive), plan_tick=3, embed=2, fetch/classify=1; host budgets
  melchior 12, spark 1 GPU + 4, castor 4 llm-slots + 2, caspar 1 light,
  balthazar suspended pending the wired-flap gripe. Re-fit from T0 data.
- Open: where the governor lives (new pass vs materialize.py rewrite).
- Open: castor demand routing (benchmark 2026-08-10: ~10–14 tok/s
  aggregate, bandwidth-bound → big-tier only, not the classify burst).
