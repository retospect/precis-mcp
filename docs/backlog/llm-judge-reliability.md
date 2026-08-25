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

**Reading**: the verifier is *reliable* where decisions are made — the
repair-lane call is near-ceiling on the deployed rung. The cross-rung
0.65 on passage verdict is almost entirely the SUPPORTED↔ADJACENT_CHUNK
boundary (6 of 14 disagreements), which is benign↔benign.

**Caveats**: same-day replicates (no drift axis); exemplar hubs have been
partially repaired since the audit, so no comparison against the audit's
own labels was attempted (replicates were blind to them); n=52.

## Second slice — human gold set, 2026-08-25 (n=30). Reliability ≠ accuracy

30 of the 52 edges were adjudicated by the operator (disagreements first,
then unanimous fill): `llm-judge-reliability-data/gold.jsonl`, scored by
`score_vs_gold.py`. 19 labels carry an explicit operator ruling; 11 are
assistant-proposed and marked `blessed: false`.

**Read the accuracy numbers as a floor, not a corpus rate.** The gold set
was deliberately enriched: 19 of its 30 edges are ones the judges
disagreed on, and the unanimous fill drew the *rarest* labels first, so
22 of 30 are defects against a corpus base rate nearer 27%. These are
accuracy figures on hard cases. A random-sample accuracy would be higher
and is not yet measured — that is the next slice, and it is the one that
matters, because it sizes the `NONE` bucket (see finding 2).

| judge | accuracy (all 30) | accuracy (blessed 19) |
|---|---|---|
| replicate B (opus) | **87%** | **79%** |
| replicate C (opus) | 80% | 68% |
| replicate A (opus) | 77% | 63% |
| replicate D (sonnet) | 77% | 68% |
| **opus modal (majority of 3)** | 80% | 68% |

**Three findings, each of which changes how the 922-hub run should be
read:**

1. **High agreement did not mean high accuracy.** The same judges that
   agreed at Fleiss κ=0.88 on the repair lane are 20% wrong against
   gold. Reliability bounds accuracy from above; it does not
   estimate it. Every headline rate in
   `dr42995-grounding-audit-results.md` should be read with a ~20%
   per-edge error bar, and the audit's `NONE` share (73%) is the
   number most affected — see (2).

2. **The judges never over-flag; they under-flag.** Zero false alarms
   from the opus modal across 30 edges (one across all four judges).
   Every error was a missed defect (3) or the wrong repair lane (3).
   **Operationally: a defect verdict can be routed straight to its
   repair lane, but a `NONE` verdict is the untrustworthy one** and
   needs sampled human review. That is the opposite of the usual
   assumption about LLM judges.

   Sharpened: the opus modal returned `NONE` on 11 of the 30 edges, and
   **3 of those 11 (27%) hid a real defect**. Every one of its 22
   defect verdicts was a true defect (the lane was sometimes wrong, the
   detection never was). So the verdict distribution is trustworthy in
   one direction only.

3. **Majority voting made it worse.** A single replicate (B) beat the
   3-way modal, 87% vs 80%, because A and C missed defects B caught.
   Consensus suppressed correct minority findings. Do not add voting to
   the pipeline on the theory that it improves quality — on this
   sample it cost 2 detections.

**A conclusion from the first slice is hereby falsified.** That slice
recorded sonnet's extra findings (3× NONE→CLAIM_DEFECT vs the opus
modal) as false alarms. Against gold, **2 of those 3 were correct** —
sonnet caught real defects (`fi176612`, `fi176710`) that all three opus
replicates missed. Cross-rung disagreement was signal, not noise, and
the downshift note now reads the other way: the cheaper rung is not
strictly worse, and `router-coverage-and-downshift.md` should not treat
opus as ground truth when calibrating.

### Modality: a missing claim axis, found while adjudicating

Of the 29 distinct claims adjudicated, **10 carry no statement of
whether the result is experimental, computational, or theoretical**, and
**2 carry one that is wrong** (`fi176677` names PXRD where the source ran
in-situ EDXRD and SANS; `fi177518` generalises "two-probe" across a value
measured by van der Pauw). The sharpest case is `fi176612`, whose source
is titled *Simulation of* reversible molecular mechanical logic and whose
claim reads as settled engineering.

Marker presence does **not** predict defect rate (unmarked 7/10 defective,
marked 14/19 — statistically the same), so this is an honesty axis, not a
lint signal. Filed separately: `taproot-claim-modality-axis.md`.

### Still open

- **Drift axis**: re-run the frozen instrument over the same roster in a
  month. Roster and instrument are pinned, so this is one command.
- The 11 `blessed: false` gold labels want an operator pass.
- Repair follow-through for the adjudicated defects (stubs 254907 /
  254908 were minted for `fi176620`'s comparator half).

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
