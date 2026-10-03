---
status: draft
title: Dispatcher feedback controller keeps each local LLM server near ~32 busy sequences, overflowing to cloud
pillar: local-compute
prio: high
blocked-by: vllm-per-node-serving
---

# Dispatcher feedback controller keeps each local LLM server near ~32 busy sequences, overflowing to cloud

Reto, 2026-10-02 (review item local-compute-4, ruling 3): target ~32
parallel sequences per serving box with best-practice serving, plus a
feedback (PID-style) controller that keeps the boxes busy. Slice 0
(`vllm-per-node-serving.md`) decides model and server first; this item is
the separate build that follows it.

## Motivation / why

Today concurrency is a fixed number in three places that drift apart: the
`llm` card's `max_parallel`, the `resource_slots` `llm:*` capacity, and the
server's own limit (`--parallel` / `--max-num-seqs`). The 6-vs-4 mismatch
in `local-summarizer.md` is that drift. A fixed cap is also wrong in both
directions: long prompts exhaust KV cache below the cap, short ones leave
the box idle above it. A controller that reads the server's own load
signal and admits work against a setpoint keeps the box busy without
preemption thrash. Anything it cannot place locally goes to the cloud rung.

## In scope

- **Setpoint on the server's live state**, scraped from the server's
  metrics endpoint each tick (vLLM: `num_requests_running`,
  `num_requests_waiting`, `gpu_cache_usage_perc`, `num_preemptions_total`;
  SGLang equivalents if Slice 0 picks SGLang). The loop admits while
  waiting ≈ 0 and KV use is under threshold. It backs off when waiting
  climbs or preemptions tick. The control variable is the number of
  in-flight local admissions, and the target is about 32 running.
- **One number, three places in lockstep.** The controller's current
  admission limit is what `local_serving.acquire` reserves against. Card
  `max_parallel`, the slot capacity and the server's hard limit are set
  from one value: the server's limit is the ceiling, and the controller
  moves below it.
- **Overflow to cloud.** A dispatch the controller does not admit takes the
  chain's cloud rung immediately (the existing busy-slot path, logged as
  `fell_to_cloud` in the `/placement` view), never a queue wait for
  latency-bound callers.
- **Deferrable work fills the gap.** `graph-maintenance-queue.md` is the
  feed: when interactive load leaves headroom, the controller pulls from
  that queue; deferrable work never overflows to cloud.
- Measured by the `/placement` view: `kept_local` and `fell_to_cloud` per
  tier, plus a running-sequences-over-time series from the per-beat
  heartbeat counters (`vllm-per-node-serving.md` Slice 3).

## Explicitly NOT in scope

- MCP agent-session admission. `mcp-concurrency-load-test.md` slice 2
  (serving-programme) prototypes a loop over the same `/metrics` that tops
  agent sessions up. This item reuses its scrape and thresholds, and does
  not build a second scraper.
- Choosing the model or the server: Slice 0.
- Multi-box scheduling across the three Sparks. One controller per serving
  box; the role split is review item local-compute-5.

## Acceptance criteria

- Under a synthetic ramp of 1 to 64 concurrent dispatches, the serving box
  holds near the setpoint, `num_preemptions_total` stays flat, and the
  excess shows as `fell_to_cloud`.
- Card `max_parallel`, the slot capacity and the server limit cannot
  disagree: one source, with a test that fails if a second writer appears.
- With no interactive load, the deferrable queue keeps the box at the
  setpoint (the "is the box hot" number in `graph-maintenance-queue.md`).

## Target + blast radius

`utils/llm/local_serving.py` (acquire against the live limit) ·
`workers/heartbeat.py` (scrape on the serving host) · `llm_catalog.py`
(card `max_parallel`) · `store/_resource_slots_ops.py` · the vLLM/SGLang
deploy role from Slice 1.

## Open questions / decisions log

- Setpoint source: a fixed ~32 from Reto's ruling, or re-derived from the
  Slice 0 curve (the concurrency where aggregate tok/s plateaus). Proposal:
  Slice 0's plateau, capped at 32 until measured otherwise.
- PID on running sequences vs bang-bang on waiting/KV thresholds. Start
  with the threshold loop the admission prototype describes, and add the
  integral term only if the box oscillates.
- **[2026-10-03]** The deferrable feed's per-action eligibility comes from
  `local-mesh-upkeep.md`. Its review lane is cloud spend, budgeted on its
  own line; the "deferrable work never overflows to cloud" rule covers
  the maintenance units only.
