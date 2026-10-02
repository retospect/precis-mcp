---
status: draft
pillar: local-compute
title: Dedicated local summariser — one machine slice runs chunk summarisation, cloud only as overflow
prio: high
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
- **Slot-capacity mismatch.** Advertised `resource_slots` capacity must
  match the server's `--parallel` (the summariser was advertised at 6 while
  melchior's `llama-server` ran `--parallel 4`; two of six threads were
  guaranteed into backoff). Derive `derived_drain._DEFAULT_CONCURRENCY` from
  the advertised capacity rather than a hardcoded number when the local rung
  returns.

## In scope

- A local summarise rung ahead of the cloud rung in `llm.chain.small` (or a
  summarise-only chain), cloud as overflow when the local slot is saturated.
- Placement of the dedicated slice (a whole machine or a share of one) —
  open; depends on the model `vllm-per-node-serving.md` Slice 0 picks.

## Relation

`local-rungs-small-medium.md` is the general SMALL/MEDIUM local rung; this
item is the dedicated summariser slice and its first consumer.
`graph-maintenance-queue.md` feeds capacity to this pass.
