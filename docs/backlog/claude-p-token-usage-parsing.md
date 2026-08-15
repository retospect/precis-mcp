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

## Status (2026-08-15, 5da355c0)

Main scope DONE: `claude_p.py`'s `_extract_usage`/`_unwrap_envelope` now parse
the envelope's `usage` block into `ClaudePResult`, and
`result_from_claude_p()` threads it into `LlmResult` — the claude_p rung no
longer drops tokens. REMAINING: the `openai_tools` split below is untouched
by this ship (no changes to `openai_tools.py`), so this item stays open for
that half.

## Related gap: openai_tools split

The `openai_tools` multi-turn loop has the same symptom for a different
reason: `ToolChatClient.chat()` (`src/precis/utils/llm/openai_tools.py`)
reads only `usage.total_tokens` per turn, and `AgentLoopResult`/`ChatTurn`
carry only the summed total — so `_dispatch_openai_tools` rows get
`total_tokens`/cost but NULL `input_tokens`/`output_tokens`. Fix alongside
this item: read `usage.prompt_tokens`/`completion_tokens` per turn, sum
into the loop result, and thread the split into `LlmResult`.

## Out of scope

- Retroactive token estimation for past null rows.
- Changes to the claude_p transport signature.
