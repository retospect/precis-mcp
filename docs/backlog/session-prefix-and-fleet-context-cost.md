---
status: draft
pillar: platform
---

# Session prefix and fleet context cost

The 2026-10-07 token-review pass (24h window, deduplicated by API message
id) measured 566M main-loop cache-read tokens over 13 sessions against
about 2M output tokens. The cost is context re-sent on every turn, not output.

- (a) **Fixed prefix.** Every main-loop session starts at 105-137k tokens on
  turn one. Named subagents start at 18-23k, fork subagents at about 90k. Over
  the window's ~2400 main-loop turns the prefix alone is about 260M tokens,
  roughly half the total. Suspected contents: tool schemas loaded in full
  whether used or not — the Figma plugin (~50 tools, enabled user-globally in
  `~/.claude/settings.json` `enabledPlugins`), the Claude Docs tools, and the
  large `precis` `put`/`edit` schemas — plus the skills list and the
  SessionStart inflight table. First step: measure. Start a fresh session with
  the Figma plugin disabled in project `.claude/settings.json` and compare its
  first-turn `cache_read + cache_creation`; repeat per component. Then decide
  which to disable by default (the web-landing Home thread uses Figma, so it
  needs an opt-in path) and whether the `precis` verb schemas can shrink.
- (b) **Fleet sessions run long and wide.** Eight concurrent thread windows,
  all on `claude-fable-5-1`, each 170-420 API turns, averaged 210-275k context
  per turn with peaks of 416-552k. The coordinator (`a7475d98`) alone was 105M
  (19%). Options for the next fleet cycle: thread windows on Sonnet unless the
  thread needs Fable, and a context ceiling (~200k) that forces a
  handoff-and-restart instead of running to 450k+.
- (c) **Coordination chatter.** Each peer message or wakeup is a full turn at
  ~250k context regardless of content. Coordinator ~180 message turns
  (72 in, 112 `SendMessage` out) plus the gripe loop's 41 `ScheduleWakeup`
  ticks ≈ 55M tokens (~10%). Batch status relays; lengthen idle wakeups.
- (d) **Compact thrash** recurred: `b753bd9a` auto-compacted 11 times in 24h
  (see `token-review-hook-gaps` (c)).

Measure with `scripts/mine-sessions/run.sh --since 1d`; its scoreboard
cache-read total counts each content block, so deduplicate by
`message.id` from the raw transcripts for per-session figures.
