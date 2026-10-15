---
status: ready
title: per-skill eval corpus — grow beyond the three seeds; executed runs
pillar: memory-graph
---

# Per-skill eval corpus

wip: devin skill-eval-corpus 2026-10-15

The harness shipped (`scripts/skill-eval`, `src/precis/skill_eval/`,
`docs/conventions/skill-evals.md`): YAML cases per skill, a fake runner for
CI, a budgeted planned-call `claude -p` runner for the host, JSON report +
table, advisory only. The corpus covers the three seeds
(`precis-overview`, `precis-paper-help`, `precis-draft-help`) plus one case
per `answers:` question for `precis-memory-help`, `precis-finding-help`,
`precis-todo-help` and `precis-search-help`, with a misleading-transcript
fixture per family in `tests/test_skill_eval.py`. The long-tail slice added
21 cases mined from the prod `tool_calls` ledger and open gripes (wrong
view/verb/kwarg families, each citing its call id or gripe id), growing the
corpus to eleven more skill files.
`/whatneedsdoing`'s hygiene wave runs `--runner live --diff` next to
`memory-lint` (never `scripts/ship`).

## Remainder

1. **Executed runs.** The live runner is a dry run (declared calls). An
   executed variant — `claude -p` with the precis MCP attached to the dev
   DB (`scripts/dev`), transcript read off stream-json tool_use events,
   record predicates checked against the dev DB — would test the verb path
   end to end. Prod is never a target (spec constraint).

test: edit a skill to be actively misleading on one of its `answers:`
questions — the live eval for that skill goes red; revert — green.
