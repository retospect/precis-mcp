---
id: coder-chain
family: work
title: precis — chain fresh coder rounds with compact handoffs
summary: sequential stateless coder rounds for a build too large for one coder call — each round ends with a fenced JSON handoff the next picks up cold, capped at 8 rounds with a commit between them
answers:
  - how do I run an implementation too big for one coder call?
  - what does each coder round hand off to the next?
  - how many chained coder rounds are too many?
  - what do I do when a round returns blocked or no handoff?
applies-to: a multi-file repo-dev build delegated to fresh coder subagents
status: active
tags: [workflow]
governs: factory
---

# coder-chain — sequential fresh-coder rounds with compact handoffs

Drive the loop yourself with the **Agent tool** (`subagent_type: 'coder'`),
one round at a time, **sequentially** — never parallel rounds. Default cap:
**8 rounds** (caller may override). Stop early on `done` or `blocked`.
(The Agent tool and SendMessage are this harness's names — with no subagent
runtime, play each round yourself under the same contract; otherwise map to
your runtime's equivalent.)

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

## See also

- [[flow-stages]] — the stage map this loop sits inside (the Coding stage).
- [[bug]] — a root-cause dossier is a natural chain input.
- [[quiet-output]] — keep each round's noisy test output digested.
