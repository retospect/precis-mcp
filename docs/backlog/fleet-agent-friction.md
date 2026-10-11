---
status: idea
title: Fleet agents stall on prompts and lack a checkout — prod-psql approvals, plan_tick repo access
pillar: platform
---

# Fleet agent friction — prompts and missing repo access

Bundled in the 2026-10-10 gripe triage. Each item stops fleet or lane
agents on something a human already decided or never needed to decide.

## 1. prod-psql hook asks on every write (gr462727, merged gr462731)

`guard-prod-psql.py` asks for confirmation on every prod write, including
writes Reto already approved. That contradicts
`docs/conventions/thresholds.md`, so approved prod work stalls on a prompt
in the pane.

Ruling (Reto, 2026-10-11): the hook asks only on destructive writes
(DELETE, DROP, TRUNCATE, ALTER … DROP, an UPDATE/DELETE without WHERE, or
anything over the `thresholds.md` limits); every other write passes.

Who: agent-code, S. Files: `scripts/hooks/guard-prod-psql.py`.

## 2. plan_tick has no repo checkout (gr477865)

plan_tick agents (`claude -p`) have no `PRECIS_PYTHON_ROOTS` and no worktree,
so they cannot check repo-dev memory-review todos against the code. The
options from the gripe are: map a read-only checkout into the lane, route
repo-touching todos to a lane that has one, or tag them `waiting-for:reto`.

Related: gr456287 / [worktree-path-guard-false-positives.md](worktree-path-guard-false-positives.md),
the other high-frequency prompt-friction source.

## 3. Session hooks reject read-only commands and cost a process per call (gr343750)

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
- An approved non-destructive prod write runs without a prompt, and a
  `DELETE` without a `WHERE` still asks.
- A repo-dev memory-review todo is either checked against the code by its
  lane or visibly parked for Reto. It is no longer re-ticked without
  progress.
