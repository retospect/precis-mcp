# Grounding eval — opt-in proposal-only trial protocol

Owner: Reto. Filing: gr634496. Harness: `src/precis/mesh_eval/grounding.py`,
CLI `precis mesh grounding`. Nothing here authorises automatic writes, a
new model choice or service activation. The grounding auditor
(`precis.graph.garden.grounding`) stays audit/proposal-only: no apply door
is registered for `grounding_audit` proposals.

## What the harness measures

`precis mesh grounding [--criteria F] [--cases F] [--repairs] [--json]`
replays a JSONL corpus (`src/precis/mesh_eval/data/grounding_cases.jsonl`)
through the production pipeline pieces with stubbed model replies and
stubbed source passages (no database, no model call). Per gold claim it
records the confusion bucket and, for every miss, the faulty stage:

| stage | an error here means |
|---|---|
| extraction | the claim was not reported, mis-kinded, lost its evidence handle, or sat past a scan bound |
| retrieval | the claim cites evidence but no passage came back |
| verification | the passage or warrant judgment disagrees with gold |
| policy | the judgments were right; the rollup (lineage, terms, freshness) was wrong |

Reported with 95% Wilson intervals and the actual `k/n`: false approvals
(audit `supported`, gold not), false alarms, missed substantive claims,
appropriate and unnecessary abstentions, evidence-retrieval failures,
source-independence agreement, research-handoff check accuracy, span
coverage, truncation-flag agreement, and cost (model calls, prompt
chars). Repair proposals are scored by `--repairs` in a separate suite;
they never feed the audit-judgment rates. A fluent classifier judgment or
a resolved reference cannot pass alone: support needs a retrieved passage
and a verifier verdict.

`--criteria F` is the only source of pass/fail. Keys left `null` are not
judged; without the flag the command reports and exits 0. No default
threshold exists in code.

## Corpus rules

- One case per JSONL line; every case names a `category` and a gold
  `status` per claim (`supported`, `partial`, `unsupported`,
  `contradicted`, `abstain`, `not_applicable`). The loader rejects a case
  missing either. At least two cases per category (`CATEGORIES`).
- The shipped corpus is **synthetic and author-drafted**: its
  `adjudication.independent` is `false` on every case and the report says
  so. It exercises the harness and the pipeline policy. It is not evidence
  that the auditor is good. A case counts as independently adjudicated
  only when someone other than its author set the gold with the source
  text in hand and flipped `adjudication.independent` to `true`.
- Recorded model replies marked `PLANTED FAULT` in `note` exist to prove
  stage attribution; do not read their errors as auditor quality.
- Held-out means gold is fixed before any prompt or policy change is
  tuned against it. A case used to tune is retired from the held-out set.

## Trial protocol (opt-in, proposal-only)

1. **Opt in.** Reto names the memories (or a tag) the trial may audit. The
   auditor already skips human-origin, considered and DREAM memories.
2. **Run proposal-only.** The `memory-grounding` pass writes audit rows and
   pending repair proposals; nothing is applied. Record the audit and
   prompt versions (`grounding.v*`, `grounding-classify.v*`) and the model
   route used.
3. **Sample for independent review.** Draw the sample by a rule fixed
   before looking at results, stratified by rollup verdict and claim kind,
   including a share of `supported` claims (to estimate false approvals)
   and of claims the audit never extracted (to estimate missed claims).
   A reviewer who did not run the audit judges each sampled claim against
   the source text and records gold status and, for a miss, the stage.
4. **Score.** Load the reviewed sample as cases (live replies in place of
   `recorded`), run the same scorers, report `k/n` with intervals. State
   the sample size and what it was drawn from; a small `n` gives a wide
   interval and the report must show it.
5. **Preservation and rails.** `tests/test_garden_grounding_safety.py`
   must pass: untouched sentences preserved, stale sha refused at apply,
   human-origin and DREAM memories held, apply/undo restores the body,
   repeat runs add no proposals.
6. **Decide.** The owner reads the report against the criteria below.
   Any move beyond proposal-only (automatic repairs, wider scope) is a
   separate decision recorded here, not implied by a green report.

## Criteria — to be set by the owner

Left blank on purpose. Copy to a JSON file and fill values; `null` means
"not judged". Do not invent numbers on the owner's behalf.

```json
{
  "false_approval_upper_max": null,
  "false_alarm_upper_max": null,
  "missed_substantive_upper_max": null,
  "retrieval_failure_upper_max": null,
  "status_accuracy_lower_min": null,
  "span_coverage_lower_min": null,
  "prompt_chars_per_case_max": null,
  "model_calls_per_case_max": null
}
```

`*_upper_max` compares the Wilson upper bound, `*_lower_min` the lower
bound, so a small sample cannot pass by luck. Budget keys compare per-case
means.

| field | value |
|---|---|
| Minimum sampled claims per stratum | _owner to set_ |
| Reviewer independence rule | _owner to set_ |
| Budget ceiling for the trial | _owner to set_ |
| Sign-off for any step beyond proposal-only | _owner to set_ |
