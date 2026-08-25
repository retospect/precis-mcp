---
status: idea
title: "LLM judges as instruments — measure reliability before trusting verdicts at scale"
---

# Judges are instruments; nothing reports their error bars

Prompted by mapping precis against Zyphur's "AI as configurable research
method" framework (Instats seminar, 2026-08): precis has the qualitative
governance half (role separation, evidence boundaries, human sign-off,
claim ladder) but not the psychometric half. Every LLM judge — the
grounding verifier, `extract_claim`, the classifiers, hub_refine's strict
judge — is treated as a one-shot oracle. No inter-run agreement, no
cross-rung stability, no held-out gold set. `grounding-verification-rubric.md`
says it outright: *"Verdicts are LLM judgments, advisory and unreviewed …
treat output as leads."* The nanobud audit that preceded it had three
errors found on verification, two of them contradictions wrong in opposite
directions — that is a reliability problem wearing an accuracy costume.

## What to measure (all data already flows through `llm_call_log`)

1. **Test-retest**: re-run a fixed sample of grounding verdicts N times at
   temperature-as-deployed. Report per-label agreement (a kappa, not raw
   percent — SUPPORTED base rate is ~35%, and this corpus has already been
   burned twice by ignoring base rates; see §2 of
   `grounding-verification-rubric.md` and `ingest-strips-greek-glyphs.md`).
2. **Cross-rung sensitivity**: same sample through two ladder rungs
   (e.g. glm-flash vs opus). Where the cheap rung disagrees with the
   expensive one tells the router where downshifting is actually free vs
   where it silently changes verdicts — direct input to
   `router-coverage-and-downshift.md`.
3. **Prompt sensitivity**: the rubric has been revised twice (pilot →
   wave-1). Re-score a fixed sample under both rubric versions; the verdict
   churn is the rubric's own error bar, and it says whether wave-1 numbers
   are comparable to pilot numbers at all.
4. **A small human gold set**: ~30 hubs hand-labeled once, held out, never
   used for rubric development. Turns "the rubric seems right" into a
   calibration number, and gives every future rubric revision a fixed
   yardstick (dev/test separation — today the 18-hub pilot is both).

## Cheap first slice — measured 2026-08-25 (grounding verifier); gold set + drift axis remain open

Ran on 52 hub-edge pairs from the `dr42995` cohort (22 edges from 20
exemplar hubs the audit had flagged + 30 seeded-random, `setseed(0.17)`),
scored independently by 3× opus (test-retest) and 1× sonnet (cross-rung)
under a frozen instrument. Raw verdicts, roster, and the analysis script:
`llm-judge-reliability-data/`; the instrument (reconstructed and now
durable — the original shard prompt was lost with the `verdicts_*.jsonl`
files): `docs/runbooks/grounding-verifier-instrument.md`.

| level | test-retest (Fleiss, 3× opus) | cross-rung (Cohen, sonnet vs opus modal) |
|---|---|---|
| repair lane (benign / claim-edit / edge-repair) | **0.88** (47/52 unanimous) | **0.83** |
| disposition (8 labels) | **0.86** (46/52 unanimous, zero 3-way splits) | 0.83 |
| passage verdict (8 labels) | 0.79 | 0.65 |

Per-label (disposition): WRONG_SOURCE 0.90, CLAIM_DEFECT 0.89, NONE 0.86,
NEEDS_SECOND_EDGE 0.79. Singletons (FRONT_MATTER_ANCHOR, PARTIAL_MINOR)
are unmeasurable at n=1.

**Reading**: the verifier is reliable where decisions are made — the
repair-lane call is near-ceiling on the deployed rung, and the audit's
headline rates carry error bars small enough to act on. The cross-rung
0.65 on passage verdict is almost entirely the SUPPORTED↔ADJACENT_CHUNK
boundary (6 of 14 disagreements), which is benign↔benign; sonnet's
disposition disagreements skew toward *more* findings (3× NONE→
CLAIM_DEFECT), i.e. false alarms, not misses — relevant to
`router-coverage-and-downshift.md`: downshifting this judge costs review
time, not detection.

**Caveats**: same-day replicates (no drift axis); exemplar hubs have been
partially repaired since the audit, so no comparison against the audit's
own labels was attempted (replicates were blind to them); n=52.

## Related

- `grounding-verification-rubric.md` — the rubric this would calibrate;
  its "method caveat" section is this item's justification.
- `context-quality-eval.md` — evaluates the *input* contexts; this item
  evaluates the *judges*. Complementary, don't merge.
- `plan-tick-remeasure.md` — same disease elsewhere ("the performance
  claim rests on one baseline").
- `quest-rubric-unproducible-objectives-warning.md` — rubric changed
  mid-flight with no warning; version-freeze + dated amendments is the
  preregistration analog of the same idea.
