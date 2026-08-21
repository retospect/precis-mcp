---
status: draft
title: tool-call ledger — verb/kind/key-set/outcome row per dispatch; no bodies
prio: high
---

# Tool-call ledger

(Merges the earlier `tool-call-ledger.md` item, 2026-08-21.)

**What.** No per-tool-call telemetry exists: the live `precis serve` path logs
nothing, and `/whatneedsdoing`'s confusion-mining samples only `plan_tick` job
transcripts (4.9% of transcript-bearing jobs even contain tool calls). Add a
`tool_calls` table (sibling of ref_events/alert; numeric, not embedded):
call_id, ts, agentlog_id, source/profile, verb, kind, input key-set (jsonb —
**key names only, never bodies**), outcome, error_type, result_count,
latency_ms. Written from the verb-dispatch chokepoint
(`runtime/dispatch.py` / `tools/core.py::_dispatch`).

**Why.** "Which verb/kind/arg-shape confuses agents" becomes a
`GROUP BY (verb, kind, error_type)` instead of a forensic transcript-mining
pass (~130k subagent tokens each, and blind to interactive sessions). It is
also the measurement that keeps the command-profile honest (per-profile usage
and error rates) and the detector for the next psql-detour-shaped coverage
hole (see `mcp-aggregate-surface-gaps.md`).

**Follow-on (cheap once the table exists):** a nursery friction-detector pass
that auto-files a gripe when a (verb, kind, error_type) rate crosses a
threshold.

**Test:** ledger row per dispatch in a dev-DB session; no payload content in
any column; `/whatneedsdoing` step 6 gains a ledger query replacing the
transcript-regex path where available.
