---
status: draft
pillar: local-compute
title: Standing report — local vs. cloud share of LLM calls, routed vs. landed
prio: high
---

# Standing report — local vs. cloud share, routed vs. landed

Mining pass, 2026-09-30. `llm_call_log` already carries both `placement`
(what the router chose) and `placement_effective` (what actually ran) —
`src/precis/utils/llm/router.py`: `placement = "cloud" if
_rung_is_cloud(rung0) else "local"` at selection time, and a separate
`placement_effective` computed after `_apply_placement` resolves fallbacks.
Nothing reports the local-vs-cloud share from this data, and peer session
rustling-questing-wadler found the two columns *diverge in practice*: a
saturated local slot silently retries against the hosted endpoint unless a
caller pins `placement='local'`, so "routed local" and "landed local" are
different numbers and neither is currently measured.

## Motivation / why

The local-compute pillar has no visibility into its own baseline. Every
local-serving decision (`local-serving-eval.md`, `vllm-per-node-serving.md`,
`llm-tier-ladder-cloud-cutover.md`) is made without a standing number for
"how much of our LLM traffic is actually landing locally today, and how
much of what was *supposed* to land locally fell back to cloud."

## In scope

- A standing report — a `view` on `kind='llm'` or a `precis-status` section
  — of calls/tokens/cost, broken down by tier × `placement_effective`, per
  day.
- A routed-vs-landed split: for each cell, the count where `placement`
  (intended) matches `placement_effective` (actual) vs. where it diverged
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
- Cheapest item in the local-compute pillar — the data already exists in
  `llm_call_log`; this is a query + a view, no new instrumentation.

## Target + blast radius

A new `view=` on the `llm` kind handler, or a `precis-status` section
(`get(kind='skill', id='precis-status')`'s Runtime section is the existing
pattern for this kind of standing report). Read-only; `llm_call_log`
untouched.

## Open questions / decisions log

- Whether this lives as a queryable view (agent-pulled) or a
  `precis-status` row (always-visible) — leans view first, promote to
  status if the divergence number turns out to need daily eyes.

Closest existing items: `llm-cost-accounting.md`, `content-sensitivity-placement.md`
(the orthogonal constraint this report's `placement` column already
reflects the output of).
