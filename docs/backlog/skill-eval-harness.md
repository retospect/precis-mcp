---
status: ready
title: per-skill eval corpus — grow beyond the three seeds; executed runs
pillar: memory-graph
---

# Per-skill eval corpus

wip: devin bl-skill-eval 2026-10-12

The harness shipped (`scripts/skill-eval`, `src/precis/skill_eval/`,
`docs/conventions/skill-evals.md`): YAML cases per skill, a fake runner for
CI, a budgeted planned-call `claude -p` runner for the host, JSON report +
table, advisory only. The corpus covers the three seeds
(`precis-overview`, `precis-paper-help`, `precis-draft-help`) plus one case
per `answers:` question for `precis-memory-help`, `precis-finding-help`,
`precis-todo-help` and `precis-search-help`, with a misleading-transcript
fixture per family in `tests/test_skill_eval.py`.
`/whatneedsdoing`'s hygiene wave runs `--runner live --diff` next to
`memory-lint` (never `scripts/ship`).

## Remainder

1. **Executed runs.** The live runner is a dry run (declared calls). An
   executed variant — `claude -p` with the precis MCP attached to the dev
   DB (`scripts/dev`), transcript read off stream-json tool_use events,
   record predicates checked against the dev DB — would test the verb path
   end to end. Prod is never a target (spec constraint).
2. **Long-tail corpus.** Source new prompts from the LLM-confusion signal
   `/whatneedsdoing` mines from prod transcripts, so each case is a task an
   agent demonstrably fumbled.

test: edit a skill to be actively misleading on one of its `answers:`
questions — the live eval for that skill goes red; revert — green.
