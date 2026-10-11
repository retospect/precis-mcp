---
status: idea
title: Fleet agents stall on session-hook refusals and per-call hook cost
pillar: platform
---

# Fleet agent friction — session hooks

Bundled in the 2026-10-10 gripe triage. Each item stops fleet or lane
agents on something a human already decided or never needed to decide.

## 1. Session hooks reject read-only commands and cost a process per call (gr343750)

Isolation and pretool hooks refuse many read-only shell commands as "too
complex to verify" (pipelines, `for` loops, computed values), so agents
split one command into several. Each hook is also a separate process
started on every tool call, which adds latency to every call and to session
start. Fix direction: merge the per-call hook checkers into one process,
widen the read-only allow-list (pipelines and loops whose every stage is
read-only), and measure session-start and per-call hook cost before and
after. Files: `scripts/hooks/*`.

Who: agent-code, M.

## Acceptance

- A read-only pipeline or `for` loop runs without a "too complex" refusal,
  and a write inside one still does.
- One hook process serves each tool call. The measured per-call and
  session-start cost is recorded in the commit body.
