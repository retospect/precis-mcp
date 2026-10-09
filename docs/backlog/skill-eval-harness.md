---
status: ready
title: per-skill eval corpus — grow beyond the three seeds; executed runs
pillar: memory-graph
---

# Per-skill eval corpus

The harness shipped (`scripts/skill-eval`, `src/precis/skill_eval/`,
`docs/conventions/skill-evals.md`): YAML cases per skill, a fake runner for
CI, a budgeted planned-call `claude -p` runner for the host, JSON report +
table, advisory only. Three seeds cover `precis-overview`,
`precis-paper-help`, `precis-draft-help`. What remains is corpus and depth.

## Remainder

1. **Corpus growth.** One case per `answers:` question for the high-traffic
   skills next (`precis-memory-help`, `precis-finding-help`,
   `precis-todo-help`, `precis-search-help`), then the long tail. Source the
   prompts from the LLM-confusion signal `/whatneedsdoing` mines from prod
   transcripts, so each case is a task an agent demonstrably fumbled.
2. **Run on skill diffs.** `scripts/skill-eval --runner live --diff` exists;
   wire it into `/whatneedsdoing`'s hygiene wave (next to `mutate-diff` and
   `memory-lint`), never into `scripts/ship`.
3. **Executed runs.** The live runner is a dry run (declared calls). An
   executed variant — `claude -p` with the precis MCP attached to the dev DB
   (`scripts/dev`), transcript read off stream-json tool_use events, record
   predicates checked against the dev DB — would test the verb path end to
   end. Prod is never a target (spec constraint).
4. **Regression evidence.** Keep one deliberately misleading-skill fixture in
   `tests/test_skill_eval.py`'s style for each new case family so a corpus
   that stops discriminating is caught.

test: edit a skill to be actively misleading on one of its `answers:`
questions — the live eval for that skill goes red; revert — green.
