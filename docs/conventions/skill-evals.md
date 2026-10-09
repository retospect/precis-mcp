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

The live runner is a planned-call dry run: nothing executes, no database
is touched, so a write-path criterion is judged on the declared record.
Caps: `--case-usd` (default 0.25, becomes `--max-budget-usd`), `--run-usd`
(default 2.00; remaining cases are `SKIP`ped once the next case would
exceed it), `--timeout-s` (180). Env equivalents
`PRECIS_SKILL_EVAL_{CASE_USD,RUN_USD,TIMEOUT_S,MODEL}`. A missing or
logged-out `claude` makes the whole run `skipped`, exit 0 — the container
`claude` is unauthenticated and exits 0 with a banner, which must never
read as a pass.

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
