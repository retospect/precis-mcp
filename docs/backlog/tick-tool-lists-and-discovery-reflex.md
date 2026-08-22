---
status: draft
title: per-job-type tool lists in tick executors + NotFound→search retry line
---

# Per-job tool lists + discovery reflex in tick prompts

**What.** Two fleet-side prompt-economy fixes in the executors
(`workers/executors/claude_inproc.py`, `quest/tick.py`, `workers/planner_prompt.py`):

1. **Per-job-type tool lists.** Ticks currently carry the full 8-verb typed
   schema (~10k tokens) per metered LLM call. Measured prod usage (14d): 97% of
   subagent-and-tick calls are `get`/`search` with 2-key inputs; `put`/`edit`
   served 2 calls. Give each job_type its tool list: prep-input/parse-output
   jobs (classify archetype) get **no tools**; looping small-rung jobs get a
   thin `get`/`search`/`tag` trio; big-rung ticks get the frontier profile
   (shipped 2026-08-21: `PRECIS_MCP_PROFILE=command`, `tools/command_parser.py`). Biggest payoff on the OpenRouter small rung, which has **no
   Anthropic prompt caching** — every schema token is full price every call
   (`perplexity-reasoning:233797` + session cache-usage measurement: Anthropic
   rungs read prefixes at 0.1×; OpenRouter does not).

2. **Discovery reflex.** Prod agents used skill discovery in 5 of ~19,853 jobs;
   the observed failure mode is a memorized stale skill name → NotFound → give
   up (all three live prod errors in 14d traced to todo 204876's template naming
   `precis-taproot-help`, which doesn't exist on prod). Add one line to the tick
   system prompts: on NotFound, run the `search(kind='skill', q=…)` the error
   suggests before abandoning. Separately (prod content, needs user-approved
   mutation): fix todo 204876's template.

**Verify first** (merged from `recheck-native-skill-tool-confusion.md`): the
2026-08 audit saw prod agents invoke Claude Code's native `Skill` tool with
precis skill ids — but every instance overlapped the gr197478 outage window
when zero precis tools were registered. Re-check on clean post-outage evidence
(old-format `plan_tick` transcripts, or accrued `quest_tick`
`meta.transcript_raw` failure captures — `quest/tick.py:_persist_job_transcript`)
before touching templates for that specific confusion; if it doesn't recur,
drop that sub-fix. The NotFound-retry line stands regardless (independent
evidence).

**Test:** a classify-family job's assembled request carries zero tool schemas;
a plan_tick request's tool block shrinks accordingly; prompt-line covered by the
planner-prompt tests.
