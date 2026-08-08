# Per-tool-call ledger

Today's telemetry has no per-tool-call row, so "which verb/kind/arg-shape
confuses agents" isn't queryable. Proposed: a `tool_calls` table (sibling of
ref_events/alert; numeric, not embedded — call_id, ts, agentlog_id, source,
verb, kind, arg_shape jsonb, outcome, error_type, result_count, latency_ms)
written from the verb-dispatch chokepoint in `src/precis/runtime.py`. Feeds
an error-rate GROUP BY (verb, kind) MCP-improvement backlog; a nursery
friction-detector could auto-file a gripe past a threshold. Sonnet-shaped.
