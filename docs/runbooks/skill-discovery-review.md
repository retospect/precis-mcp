# Runbook — skill-discovery-review (discovery-condition cadence)

A recurring, bounded pass that asks: *can an agent that is NOT handed a
skill find the right guidance from the ordinary tool surface and still
complete a realistic task?* This is the paired condition from
`docs/conventions/skill-evals.md` (gr634773): the same task runs twice
against the scratch DB — once with the help text supplied, once with no
skill text and no slug — holding model, task, environment and budget
constant, and each failed pair is classified `retrieval` (passes when
supplied, so the agent never found it), `guidance` (fails even supplied,
clean calls), `tool` (calls errored) or `unattributed`.

The corpus is the discovery set under `src/precis/data/skill-evals/
discovery/<family>.yaml`: one skill-name-free task per family (memory,
gripe, todo, finding, quest, draft, paper, patent, pcb, structure), two
wording variants each, so a sample can't overfit one phrasing. A task
prompt never names a skill — that would leak the answer into the
discovery condition.

## When

`scripts/skill-discovery-review` prints `skill-discovery-review: DUE`
when the newest dated line in
`~/.cache/precis/skill-discovery-review.log` is >30 days old (or
absent). Run the pass when DUE, work the repair briefs, then
`scripts/skill-discovery-review --stamp "<headline>"` — that resets the
clock. The cadence is advisory like `skill-search-review`: the script
always runs when invoked; the DUE line only says how stale the last pass
is.

## The pass

```
scripts/skill-discovery-review                  # at most 3 cases, paired
scripts/skill-discovery-review --limit 5
scripts/skill-discovery-review --rerun-nearby   # last report's failures
                                                # + every same-family case
```

1. **Select** (`precis.skill_eval.discovery_review.select_review_cases`,
   pinned by `tests/test_skill_discovery_review.py`): every case whose
   skill's file changed since the last stamp (git diff over
   `src/precis/data/skills/`), then every case that failed or errored in
   any retained report, then the rest ordered by fewest retained discovery runs per family
   (the under-tested tail). At most `--limit` cases — small on purpose:
   every selected case runs twice, once per condition, and the pass is
   spend-capped like `scripts/skill-eval`.
2. **Run paired.** Each condition gets its own `ExecutedClaudeRunner`
   with identical caps (`--case-usd`/`--run-usd`/`--timeout-s`/
   `--max-turns`, defaults 1.00 / 10.00 / 600 s / 30 turns). A missing
   claude or an unservable test stack skips the pass, exit 0 — an
   unauthenticated container `claude` must never read as evidence.
3. **Retain** one JSON report per pass at
   `~/.cache/precis/skill-discovery/<UTC ts>.json`: model, code and
   skills revision, conditions, budgets, every case row (condition,
   verdict, calls, answer, discovery calls/bytes before the first
   correct call) and the repair briefs. The newest report feeds the next
   pass's "previously failed" tier and `--rerun-nearby`.
4. **Repair** from the printed briefs — one per failed pair, classified.
   A `retrieval` brief points at search bits / entry points (cross-check
   `skill-search-review`, which owns ranking); a `guidance` brief points
   at the skill's own teaching; a `tool` brief points at the handler.
   Propose the patch, land it through the normal review and ship, then
   rerun the failing case plus its family (`--rerun-nearby`).

## Standing

Advisory, never a `scripts/ship` gate — same standing as
`scripts/skill-eval` itself. The sampler proposes repairs; it never
patches a skill or ships unattended. Keep evaluations honest: hold out
new tasks and vary wording between passes, and optimise for successful
task completion, not prose volume.

## Log

The clock is `~/.cache/precis/skill-discovery-review.log` (written by
`scripts/skill-discovery-review --stamp "<note>"`).

Selection uses actual retained discovery runs, with case coverage breaking
family ties. Only the latest discovery verdict per case and model contributes
to the failure priority; a later pass clears an older failure. Supplied
failures do not enter that priority. `--rerun-nearby` selects failing cases
first, then same-family neighbors, always capped by `--limit`.
