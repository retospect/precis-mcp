---
status: draft
pillar: local-compute
title: Dedicated local summariser — one machine slice runs chunk summarisation, cloud only as overflow
prio: high
snooze-until: 2026-10-12
---

# Dedicated local summariser — one machine slice runs chunk summarisation, cloud only as overflow

Reto, 2026-10-01: summarisation is cloud for now; once local LLM serving is
back at scale it goes local again, on a dedicated machine (or part of one),
with cloud only as overflow.

## Motivation / why

- Last local LLM call 2026-09-10 (the melchior summariser model). Every
  `llm.chain.*` tier is cloud-only today; `llm.chain.small` is
  `z-ai/glm-4.7-flash` over `openai_compat`.
- The summarize backlog is ~1.8M chunks. Bulk, content-light,
  latency-insensitive work is the natural first local lane for the pillar's
  "the local share is a number" end state.
- melchior's llama-swap is up but idle.

## Reverses a standing warning

`llm-tier-ladder-cloud-cutover.md` (closed and deleted 2026-10-01) carried a
2026-08-15 "do not re-apply the local-only SMALL cutover" warning, which
reversed the SMALL placement half of `small-llm-derived-drain-band`
(melchior-local-only, never spill). **This item explicitly reverses that
warning:** local-first summarise is intended again. Nobody should revert
this on the strength of the old note.

## Gate

A local model replaces cloud `glm-4.7-flash` for summarise only once it
passes `model-qualification.md` against the incumbent on the fixed
summarise eval set. Measured afterwards by `get(kind='llm',
id='/placement')` (the `small` tier row).

## Carried over from the tier-ladder doc

- **Each summarize job idled 85–93% of wall clock — unexplained.** Per-minute
  `llm_call_log` histograms inside two consecutive drain jobs showed 36–94
  minutes of zero calls, then a ~7-minute burst at ~4,000 chunks/h against a
  285/h average. Re-check on cloud SMALL: if the stall persists it was never
  slot starvation. Two closers: (1) rule out a `llm_call_log` batch-flush
  artifact by comparing other passes' rows in the same window; (2)
  `sample <worker-pid> 30` on melchior during a live drain. Unexcluded
  candidate: the shared client wiring in
  `workers/job_types/derived_drain.py::_make_summarize_runner`, or the first
  `client.complete()` blocking on a long timeout.

## Recheck due 2026-10-12 — first full day of the live chain (td478882)

The prod row `llm.op.llm_summarize` was written 2026-10-11 11:53:54Z (local
`glm-4.7-flash` on melchior's 4-slot `llm:glm-4.7-flash`, cloud
`z-ai/glm-4.7-flash` as overflow). Revert: delete that `app_settings` row.

- Before: 11,857 summarise calls in 24 h, all cloud.
- First 12 min, during a drain burst that began 09:00Z (77–336 calls/min):
  local held 20–40 calls/min, its 4-slot ceiling; cloud took 70–88%.
- Read a full day, `--ro`:
  `SELECT placement_routed, placement, count(*) FROM llm_call_log WHERE
  source='llm_summarize' AND ts > now()-interval '24 hours' GROUP BY 1,2;`
- Decide: if quiet hours run mostly local and only drain bursts overflow,
  the slice is right-sized; if overflow dominates off-burst too, raise the
  slot (`--parallel`) or add a second local server. Then drop the snooze.

## In scope

- Mechanism built: `llm.op.llm_summarize` with a `chain` (summarise-only chain, `operations.resolve_op_chain`); prod row written 2026-10-11 (local `glm-4.7-flash`, cloud overflow).
- A local summarise rung ahead of the cloud rung in `llm.chain.small` (or a
  summarise-only chain), cloud as overflow when the local slot is saturated.
- Placement of the dedicated slice (a whole machine or a share of one) —
  open; depends on the model `vllm-per-node-serving.md` Slice 0 picks.

## Relation

`local-rungs-small-medium.md` is the general SMALL/MEDIUM local rung; this
item is the dedicated summariser slice and its first consumer.
`graph-maintenance-queue.md` feeds capacity to this pass.
