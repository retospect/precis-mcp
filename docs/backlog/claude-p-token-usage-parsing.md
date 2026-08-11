# Parse token usage from claude_p output

The CLAUDE_P transport (`claude -p`, one-shot judges: chase/verify, figure,
mermaid) leaves `LlmResult` token fields `None`, so its `llm_call_log` rows
carry NULL `input_tokens`, `output_tokens`, `cache_read_tokens`, and
`cache_creation_tokens`. Cost USD is
still recorded. Parse usage metadata from `claude -p --output-format json` in
`result_from_claude_p()` so those rows carry token counts too, enabling
proper token accounting across all transports.

## Motivation

- Per-call token columns now exist in `llm_call_log` (shipped 2026-08-11).
- The claude_p transport reports cost but not token counts (claude_agent
  already populates them from stream-json usage).
- Ambient accounting needs completeness: partial data obscures true cost
  distribution and per-model benchmarking.
- Upstream: the `claude -p --output-format json` response includes usage
  metadata; the data is available, just not extracted.

## In scope

- Extract `usage` fields from claude_p JSON output.
- Map them to `LlmResult.input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_creation_tokens`.
- Backfill live rows if feasible; new rows post-fix will be complete.

## Out of scope

- Retroactive token estimation for past null rows.
- Changes to the claude_p transport signature.
