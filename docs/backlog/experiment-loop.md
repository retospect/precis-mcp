---
status: draft
title: experiment loop — hypothesis → plan → measured result → verdict, walked end to end through the verbs, with the skill that teaches it
prio: normal
model: sonnet
blocked-by: measures-substrate
---

# experiment loop — idea, experiment, result rejects the hypothesis

## Motivation / why

Reto's goal (2026-09-30) names the loop: an agent has an idea, sets up an
experiment, the result rejects the hypothesis, and all of it lives in the
graph with the linear logbook linked from the nodes. Every part exists or
is planned, in four places nobody has walked in one pass:

- the hypothesis: `finding` with `hypothesis=True`, `motivation`,
  `testable_by` (`handlers/_finding_hypothesis.py`);
- the experiment: a `todo` (or `plan`) that `tests` the finding (the
  `tests` relation exists; `quest/rulings.py::mint_measurement_rulings`
  already mints `tests` edges and `STATUS:refuted`);
- the result: a `measure` row on a hub (`measures-substrate.md`), with its
  evidence edge;
- the logbook: the run's `agentlog` `touched` the chunks it wrote
  (`precis-agentlog-help`), and human sessions land as `conv`
  (`session-history-into-precis.md`).

What is missing is the procedure: no skill answers "how do I record an
experiment whose result rejects my hypothesis", and no test proves the
verbs compose without SQL.

## In scope

1. **One end-to-end test on the dev DB** using only MCP verbs: put a
   hypothesis finding with `testable_by` naming a measurand and a bound;
   put a todo that `tests` it; put a measure on the hub the todo produced;
   run the ruling; assert the finding reaches `STATUS:refuted` (or the
   confirming state), the `tests` edge carries the verdict in `links.meta`,
   and the agentlog of the run `touched` the measure's chunk.
2. **Gaps found by 1 become items**, not silent workarounds: any step that
   needs SQL, a hand tag, or a missing relation is filed as a gripe with
   the failing verb call.
3. **Runtime skill `precis-experiment-help`**: the loop in six verb calls,
   the fail signals (a refuted finding with no `tests` edge; a measure with
   no evidence edge), and how the logbook is reached from any node.
4. **`precis-finding-help` / `precis-quest-help`** each gain one line
   pointing at the skill.

## Explicitly NOT in scope

- A new `experiment` kind. The loop is findings + todos + measures +
  links; a kind would duplicate all four.
- Lab hardware, instrument drivers, or the extraction reader that pulls
  results from papers (`corpus-quantitative-extraction.md`).
- The measure record itself (`measures-substrate.md`).
- Argument-graph lemmas/inferences around the hypothesis
  (`precis-argument-help` already covers them).

## Acceptance criteria

1. The end-to-end test passes on the dev DB with zero direct SQL.
2. `search(kind='skill', q='record an experiment that rejects my
   hypothesis')` returns `precis-experiment-help` top.
3. From the refuted finding, `view='fisheye+1hop'` shows the `tests` todo,
   the measure's hub and the agentlog in one render.
4. Every gap the test surfaced is a filed gripe or a shipped fix before
   this item closes.

## Target + blast radius

- `tests/test_experiment_loop.py` (new)
- `src/precis/data/skills/precis-experiment-help.md` (new), one-line
  pointers in `precis-finding-help.md`, `precis-quest-help.md`
- no handler changes planned; any that turn out necessary are filed first

## Open questions / decisions log

- **[decided 2026-09-30]** Blocked on measures-substrate because the
  result node is a measure; a prose-only result (a finding body) would
  test nothing the argument graph does not already cover.
