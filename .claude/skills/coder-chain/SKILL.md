---
name: coder-chain
description: >-
  Chain small stateless coder rounds via compact handoffs, so a long build
  never grows one huge coder transcript — for a build too large for a single
  coder call to finish cleanly (many files, many test-fix cycles). Each round
  gets a fresh coder seeded only by the prior round's handoff, not its full
  history. Runs on sequential Agent-tool calls (the Workflow tool is
  disabled in this repo). Repo-dev tool for developing precis-mcp; NOT a
  precis product skill.
---

# Coder-chain — sequential fresh-coder rounds with compact handoffs

Drive the loop yourself with the **Agent tool** (`subagent_type: 'coder'`),
one round at a time, **sequentially** — never parallel rounds. Default cap:
**8 rounds** (caller may override). Stop early on `done` or `blocked`.

## Handoff contract

Every round must end its final message with exactly one fenced JSON block:

```json
{
  "status": "continue | done | blocked",
  "summary": "what this round did",
  "filesChanged": ["path", "..."],
  "testStatus": "e.g. \"scripts/test --impacted: pass\" or failing test ids",
  "nextStep": "concrete instruction for the next round — empty if done/blocked",
  "question": "only when blocked: the specific decision needed from the caller"
}
```

Parse it from the round's result. If a round returns no parseable handoff,
re-prompt that same round's agent once (SendMessage: "end with the handoff
JSON block per your instructions"); if it still fails, stop the chain and
report.

## Round 1 prompt

> You are round 1 of a chained implementation. Overall task:
> `<task>`
>
> This may take more than one round — if you reach a natural stopping point
> with a coherent chunk done and tests green, but the overall task isn't
> finished, return status='continue' with a nextStep another coder round can
> pick up cold (they will NOT see your reasoning, only your handoff and the
> current repo state). Return status='done' only once the whole task is
> complete and verified. Return status='blocked' with a specific question if
> you hit an architecture/API/domain decision outside your remit.
> End your final message with the handoff as a single fenced JSON block:
> `{status, summary, filesChanged, testStatus, nextStep, question?}`.

## Round N prompt (N ≥ 2)

> You are round N of a chained implementation. Overall task:
> `<task>`
>
> Prior round's handoff (you have no memory of its reasoning — only this and
> the current repo state):
> - summary: `<summary>`
> - filesChanged: `<comma-joined, or "(none)">`
> - testStatus: `<testStatus>`
> - nextStep: `<nextStep>`
>
> Continue from nextStep. Same rules: status='continue' + a fresh nextStep if
> more remains, status='done' once the whole task is complete and verified,
> status='blocked' + a specific question if you hit a decision outside your
> remit. End your final message with the handoff as a single fenced JSON
> block: `{status, summary, filesChanged, testStatus, nextStep, question?}`.

## Loop rules

- After each round, log one line to the user: `round <n>: <status> — <summary>`.
- **Commit after each round**, before starting the next: `git add -u && git
  commit`. Rounds are the only natural checkpoint a chain has — the coders are
  forbidden to touch git, and `scripts/ship` (the workflow's usual commit) must
  not run mid-chain, so without this an 8-round build has zero commits ahead
  and one bad edit loses all of it. It is not a ship: no gate, no push, no
  `main`.
- `continue` → next round with the new handoff. `done`/`blocked` → stop.
- On `blocked`, surface the `question` to the user verbatim and stop.
- At the round cap with status still `continue`, stop and report the last
  handoff — never keep going unbounded.
- Report at the end: rounds used, final status, cumulative filesChanged,
  last testStatus.
