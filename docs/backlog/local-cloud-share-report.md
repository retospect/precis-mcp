---
status: draft
pillar: local-compute
title: Standing report — local vs. cloud share of LLM calls, routed vs. landed
prio: high
---

# Standing report — local vs. cloud share, routed vs. landed

Mining pass, 2026-09-30. Both halves are now recorded per call:

- `llm_call_log.placement` (migration 0112) is the **landed** placement —
  the rung that actually ran (`FailoverProvider.run` stamps it per rung).
- `llm_call_log.placement_routed` (migration 0179) is the **routed**
  placement — rung 0 after the strict `placement=` pin and the cloud throttle,
  before the unserved-local skip, the saturated-slot hosted retry and the
  failover walk; `local` whenever a `served_by` slot exists for it
  (`router.py::_routed_placement`). NULL on pre-0179 rows and on writers
  outside the router.

Slice 1 shipped with this, plus a landed-side fix: the saturated-slot hosted
retry used to stamp an operator rung labelled `placement: "local"` as local
although it ran (and billed) at `PRECIS_LLM_BASE_URL`, hiding it from the
dollar caps. Remaining work is **Slice 2**, the query/view below.
`placement_routed = 'local' AND placement = 'cloud'` is the fell-back count.

## Motivation / why

The local-compute pillar has no visibility into its own baseline. Every
local-serving decision (`local-serving-eval.md`, `vllm-per-node-serving.md`,
`local-summarizer.md`) is made without a standing number for
"how much of our LLM traffic is actually landing locally today, and how
much of what was *supposed* to land locally fell back to cloud."

## In scope

- A standing report — a `view` on `kind='llm'` or a `precis-status` section
  — of calls/tokens/cost, broken down by tier × `placement` (landed), per
  day.
- A routed-vs-landed split: for each cell, the count where
  `placement_routed` (intended) matches `placement` (actual) vs. where it diverged
  (a local-intended call that fell back to cloud, or vice versa).

## Explicitly NOT in scope

- Fixing the silent-fallback behavior itself (the saturated-local-slot
  retry) — that's a router behavior question, not a reporting gap; file
  separately if the report's data makes the case for it.
- Alerting/thresholds on the divergence rate — this item is the report;
  turning a report number into an alert is a follow-on.

## Acceptance criteria

- The report answers "what fraction of calls landed local yesterday" and
  "what fraction of calls that were *supposed* to land local actually did"
  as two distinct numbers, per tier.
- Rows with `placement_routed IS NULL` (pre-0179) are reported as
  landed-only, never folded into the routed-vs-landed split.

## Target + blast radius

A new `view=` on the `llm` kind handler, or a `precis-status` section
(`get(kind='skill', id='precis-status')`'s Runtime section is the existing
pattern for this kind of standing report). Slice 2 is read-only.

## Open questions / decisions log

- Whether this lives as a queryable view (agent-pulled) or a
  `precis-status` row (always-visible) — leans view first, promote to
  status if the divergence number turns out to need daily eyes.

Closest existing items: `llm-cost-accounting.md`, `content-sensitivity-placement.md`
(the orthogonal constraint this report's `placement` column already
reflects the output of).
