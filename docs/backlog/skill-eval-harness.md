---
status: idea
title: per-skill eval harness — test that a skill makes an agent succeed, pre-ship
---

# Per-skill eval harness

Decided want (Reto, 2026-09-09, from the anthropics-org comparison): skills
are the product's runtime docs, but nothing tests that an agent *given* a
skill completes the task the skill teaches. Skill edits ship on prose review
alone. Every feedback loop we have is post-hoc: the LLM-confusion signal
`/whatneedsdoing` mines from prod transcripts, and the planned injection
ledger ([skill-question-targets-and-injection](./skill-question-targets-and-injection.md)
§3). The external pattern is Claude Code's `claude plugin eval` — per-skill
eval suites (prompt + success criterion) run against a live model, JSON
report, CI-runnable.

Shape: an eval per skill (or per high-traffic skill first) = a task prompt a
prod agent would plausibly face + a checkable success criterion (verb calls
made, record produced, no forbidden detour). Run on skill-file diffs.

Constraints known up front:

- Live-model runs cannot sit in the container gate — container `claude` is
  unauthenticated and fakes a pass (memory: live-model-tests-need-host-claude).
  So this is a budgeted advisory pass like `scripts/mutate-diff`, not a gate
  stage.
- Write-path criteria must run against the dev DB, never prod.

Adjacent, not the same: [context-quality-eval](./context-quality-eval.md)
scores assembled contexts; this scores *skill efficacy* end-to-end.

test: edit a skill to be actively misleading on one of its `answers:`
questions — the eval for that skill goes red; revert — green.
