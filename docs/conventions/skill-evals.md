# Convention — per-skill evals (`scripts/skill-eval`)

**Advisory, never a gate.** `scripts/ship` does not run it and must not
(`tests/test_skill_eval.py::test_skill_eval_is_not_a_ship_gate_stage`).
Same standing as `scripts/mutate-diff`: a red result is a to-do for the
skill author, not a blocked land.

## What it checks

A skill edit ships on prose review; this is the one pre-ship check that an
agent *given* the skill plans the verb path the skill teaches. One YAML per
skill under `src/precis/data/skill-evals/<skill>.yaml`; each case is a task
prompt plus a criterion over the calls the agent makes:

- `expect.calls` — verb/kind/args patterns, each must match at least one
  call (args are a subset match; `re:<pattern>` values full-match a regex).
- `expect.record` — subset match on the one record the agent produced.
- `forbid.calls` / `forbid.text` — detours: a call pattern that must not
  appear, substrings the answer must not contain (`SELECT `, `psql`).
- `fake` — the transcript the fake runner replays. Required, so the corpus
  is self-checking: `scripts/skill-eval` with no flags is green by
  construction and a case whose fake does not satisfy its own criterion is
  a corpus bug. Schema and matching rules: `src/precis/skill_eval/cases.py`.

## Runners

| runner | what runs | where | cost |
|--------|-----------|-------|------|
| `fake` (default) | replays `fake:`; records the prompt | anywhere, CI | 0 |
| `live` | tool-free `claude -p` reads the injected skill(s) + task and declares the calls it would make as JSON | host only | capped |
| `executed` | `claude -p` with a precis MCP bound to a scratch DSN; the transcript is reconstructed from stream-json `tool_use` events and `expect.record` is judged on the row that landed | host + test stack | capped |

The live runner is a planned-call dry run: nothing executes, no database
is touched, so a write-path criterion is judged on the declared record.
The executed runner is the MDCrow-style suite (gr608716): the model
*actually calls* the tools against a test stack, so a skill that misleads
at execution time (right plan, wrong kwargs under a real tool surface, a
record that never lands) goes red here where live stays green. `--execute`
is a shortcut for `--runner executed`.

Executed-run specifics:

- **DSN**: `--dsn` / `PRECIS_SKILL_EVAL_DSN`; default is `scripts/dev`'s
  `postgresql://postgres@precis-test-db:5432/precis_dev`. Nothing in the
  spawn path resolves prod coordinates — the ambient `PRECIS_DATABASE_URL`
  never reaches the spawned `precis serve` (the `--mcp-config` `env` block
  pins the eval DSN). Records the run mints carry a
  `eval-run-<run_id>` default tag so they can't masquerade as real data on
  the shared dev DB.
- **Serve**: `--serve-cmd` / `PRECIS_SKILL_EVAL_SERVE_CMD` (default
  `precis serve`, i.e. inside the dev container); `PRECIS_MCP_PROFILE` is
  pinned `typed` and `PRECIS_BIO_ENABLED=1` for the protein corpus.
- **Models**: repeat `--model` — one corpus pass per model; the report's
  *accuracy by subtask count* table and `accuracy_by_depth` JSON block
  split by model (the llm-scorecard shape). A case declares its depth via
  `subtasks: N` (default 1).
- **Caps**: same flags/env as live — `--case-usd`/`--run-usd`/`--timeout-s`
  (defaults 1.00 / 10.00 / 600 s; an executed case is a multi-turn tool
  session, not a one-shot reply) plus `--max-turns` (30;
  `PRECIS_SKILL_EVAL_MAX_TURNS`). The MCP gate (`require_mcp`) means a
  test stack that can't serve marks the run `skipped`, exit 0 — same
  standing as a missing/unauthenticated `claude`.

## Running it

```
scripts/skill-eval                                  # corpus self-check
scripts/skill-eval --runner live --diff             # skills HEAD touched
scripts/skill-eval --runner live --skill precis-paper-help --out /tmp/r.json
```

Output: a table on stdout and a JSON report (`.skill-eval-report.json`,
gitignored; `--out` moves it). Exit 1 only when a case failed or errored.

## Adding a case

Write the prompt a prod agent would plausibly send, then the smallest
criterion that a misleading skill would fail — the spec's own test is
"make the skill wrong on one of its `answers:` questions, the eval goes
red; revert, green". Run the live runner once on the new case before
relying on the fake: the fake proves the criterion is satisfiable, the
live run proves the skill as written satisfies it.
