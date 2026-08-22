---
status: idea
title: tool-ledger friction detector — auto-file a gripe on (verb, kind, error_type) rate spikes
---

# Tool-ledger friction detector

The `tool_calls` ledger shipped (migration 0133, `src/precis/tool_ledger.py`,
written from `runtime/dispatch.py::dispatch_with_status`, sweeper GC; mining
queries in the module docstring). Remaining follow-on, cheap now the table
exists: a nursery friction-detector pass that auto-files a gripe when a
`(verb, kind, error_type)` rate crosses a threshold — the standing detector
for the next psql-detour-shaped coverage hole
(`mcp-aggregate-surface-gaps.md`). Also unblocks
`skill-question-targets-and-injection.md` §3 (ledger calibration).
