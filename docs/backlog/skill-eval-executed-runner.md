---
status: draft
title: executed skill-eval runner — claude -p with the dev-DB MCP attached
pillar: memory-graph
---

# Executed skill-eval runner

Extracted 2026-10-15 from `skill-eval-harness.md` remainder item 1 (the
harness + corpus shipped; this is the open slice).

## Motivation / why

`LiveClaudeRunner` is a planned-call dry run: the model *declares* the verb
calls it would make, so a skill that misleads at execution time — right
plan, wrong verb arguments under a real tool surface, a record predicate
the declared JSON satisfies but the actual `put` does not — is invisible to
the corpus. Only an executed run against a real precis MCP exercises the
verb path end to end (handler dispatch, kwarg acceptance, record shape).

## In scope

- A third runner (`--runner executed`): `claude -p` with a precis MCP
  attached to the dev DB (`scripts/dev`), driven to actually call the
  tools; the `Transcript` is reconstructed from the run's stream-json
  `tool_use` events, not from a declared JSON reply.
- `expect.record` predicates checked against the dev DB after the run —
  the record must have landed, not just been declared.
- Same caps/skip semantics as the live runner: `--case-usd`, `--run-usd`,
  `--timeout-s`, env equivalents; a missing or unauthenticated `claude`
  skips the whole run, exit 0.
- Per-run namespacing or cleanup for records the run creates on the dev
  DB — it is shared, so an eval write must not masquerade as real data.
- Advisory only: never a `scripts/ship` stage (already pinned by
  `tests/test_skill_eval.py`).

## Explicitly NOT in scope

- Prod as a target. The MCP handed to the spawned `claude` binds the dev
  DSN only; nothing in the runner path may resolve prod coordinates.
- Changes to the YAML corpus schema beyond what the runner consumes
  (`expect.calls`/`expect.record`/`forbid.*` already suffice).
- Concurrency: cases run serially under the run-USD cap.

## Acceptance criteria

- `scripts/skill-eval --runner executed --skill precis-todo-help` (say)
  produces a report whose transcript entries come from stream-json
  `tool_use` events — a stub test can replay a canned stream-json log.
- A case whose `expect.record` would pass on the declared record but whose
  real `put` fails (or writes a different shape) reports FAIL on the
  executed runner and PASS on the live runner — pinned by a test with a
  fake MCP.
- The runner never contacts prod: a test pins that the only DSN the spawn
  path can produce is `scripts/dev`'s.
- An unauthenticated `claude` marks the run `skipped`, exit 0 (same as
  `LiveClaudeRunner`).

## Target + blast radius

`src/precis/skill_eval/runners.py` (new runner beside
`FakeRunner`/`LiveClaudeRunner`), `__main__.py` flag + runner table,
`docs/conventions/skill-evals.md` runner table row, tests in
`tests/test_skill_eval.py`. `scripts/dev` supplies the dev DB; the spawn
wires an MCP server pointing at it. No schema change, no prod writes.

## Open questions / decisions log

- Transport per run: spawn a private `precis serve` per case/run bound to
  the dev DB, or attach to a shared dev serve? (per-run is cleaner
  isolation; costs startup per case.)
- Cleanup shape: per-run marker/tag on created records + a teardown
  delete, vs. relying on dev-DB disposability.
