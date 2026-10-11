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

Checked 2026-10-11: the "too complex to verify that it stays inside the
worktree" refusal is not ours. No file in `scripts/`, `.claude/` or
`~/.claude/hooks` contains that text. It comes from the Claude Code
harness's own worktree isolation, and it fired four times in one session
that day: a `for` loop over `git log`, a heredoc that mentioned git, and a
`scripts/ship` call whose message had parentheses. A repo hook change
cannot widen it. Agents should keep each `git`/`ship` call a plain single
command. The repo-side work is our own pretool checkers and the per-call
process cost.

Who: agent-code, M.

## Acceptance

- A read-only pipeline or `for` loop passes our own hooks (the harness's
  worktree-isolation refusal is out of scope, see above), and a write
  inside one is still caught.
- One hook process serves each tool call. The measured per-call and
  session-start cost is recorded in the commit body.
