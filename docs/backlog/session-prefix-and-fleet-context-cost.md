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
  ticks ≈ 55M tokens (~10%). Acceptable for now (Reto 2026-10-07); the
  longer-term option is `fleet-coordination-via-precis`.
- (a) status 2026-10-07: Reto disabled the Figma, Claude Docs and
  claude-context MCP servers for this project. 2026-10-08: fresh Opus
  sessions now open at 81-85k on turn one, down from 121-137k (about -30%).
  `/context` on a fresh session after "hi" (85.2k): system tools 25.7k,
  messages 18.7k (SessionStart hook output, agent list, environment and
  other reminders), `precis` MCP tools 14.5k (8 tools; put/edit carry most),
  memory files 12.8k (CLAUDE.md, MEMORY.md, RTK.md), skills 9.4k (60, of
  which ~13 Figma plugin and ~15 anthropic-skills), system prompt 2.7k.
  Remaining levers by size: deny unused built-in tools (DesignSync,
  RemoteTrigger, Cron*, ReportFindings, …) if denial removes them from the
  prompt; slim the `precis` put/edit schemas (also cuts every cluster agent's
  prefix); disable the Figma plugin per project; trim MEMORY.md to its 15KB
  floor and the inflight table in the SessionStart hook.
- (f) **Graph-mode memory index is bigger, not smaller.** Flipping MEMORY.md
  to the graph marker makes `scripts/hooks/session-start-memory.sh` print
  `precis memory index`. On 2026-10-08 that rendered 292 entries, 31.5 KB
  (~8k tokens) even at `--budget-tok 1500`: over budget,
  `precis.cli.memory._render_loaded` only cuts each hook to
  `HOOK_CUT_CHARS`, it never drops entries. It also carries stale imported
  thread states and doubled titles ("catalysis-selectivity campaign (me464081)
  — catalysis-selectivity campaign …"). Today's hand-kept MEMORY.md is
  17.7 KB (~4.5k). Not flipped. For a saving the render must print only an
  always-on subset (in-flight threads, hazards) and leave the rest to
  `search(kind='memory', tags=['SPACE:repo-dev'], …)` recall; owner: the
  graph-memory-consumers thread.
- (e) **Miner defects** (`scripts/mine-sessions`): the scoreboard's
  cache-read total counts each content block, not each API message (1.45B
  vs 566M deduplicated by `message.id`); `render_obesity` ranks by call
  count, so 22-56 B `tag`/`put` gripe results top it; `abandon_detour`
  matched the word "se" in a Bash grep of a transcript.
- (d) **Compact thrash** recurred: `b753bd9a` auto-compacted 11 times in 24h
  (see `token-review-hook-gaps` (c)).

Measure with `scripts/mine-sessions/run.sh --since 1d`; its scoreboard
cache-read total counts each content block, so deduplicate by
`message.id` from the raw transcripts for per-session figures.
