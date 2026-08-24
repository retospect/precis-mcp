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

## Cheap first slice

Pick the grounding verifier only (highest-stakes judge, richest existing
data). One script: sample ~50 already-verdicted hubs, re-run ×3 on the
deployed rung + ×1 on the adjacent cheaper rung, emit a one-page agreement
table. No schema, no worker — a read-mostly batch like the retro-verify
passes. The decision rule for scaling the 922-hub run should cite this
number.

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
