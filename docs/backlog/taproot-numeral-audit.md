---
status: draft
title: taproot numeral cross-check advisory (report-only)
prio: normal
---

# taproot numeral cross-check advisory (report-only)

From gr250034. Spec drafted 2026-09-09; awaiting Reto's review before
build. Base-rate caveat applies: per the td249939 sample, content-error
rates cluster by source (6%–62% by paper cluster) — do not treat the 12%
sample rate as uniform, and measure the audit's own false-positive rate on
a real slice before acting on its output.

## Motivation / why

Some claims are content-wrong against their own sources — fabricated or
misattributed numerals (fi178176: "quantum efficiencies of 1–5%" where the
source reports none; fi176615: "approximately 0.3 W/m·K" where the source
states only a trend). `gates.py::check_claim_sentence` audits sentence
*form* only; nothing anywhere extracts numerals from a claim and checks
them against the linked chunks.

## In scope

- New module (`taproot/numeral_audit.py`) that per hub: (1) tokenizes
  numerals from the claim title and from each linked chunk
  (`establishes`/`corroborates`/`cites`), (2) normalizes obvious variants
  (percent vs fraction, spelled-out small numerals, unit prefixes),
  (3) reports any claim numeral absent from every linked chunk.
- CLI surface `precis taproot numeral-audit` alongside `taproot lint`,
  ranked per hub. Report-only — never blocking, and kept fully separate
  from `run_mint_gates` (that gate is "blocking or nothing" by design).
- Tokenizer reuses the sub/superscript boundary lesson from td244964
  (insert a boundary at every sub/superscript run before translating).

## Explicitly NOT in scope

- Blocking anything: not a mint gate, not a lint error class.
- Semantic/unit-conversion equivalence beyond the cheap normalizations
  above (0.16 vs 16% is normalized; derived ratios, range endpoints, and
  arithmetic consequences are accepted false positives, documented in the
  report header).
- Image-only values (unfixable at this layer; noted as a known miss).

## Acceptance criteria

- Flags fi178176 and fi176615 (fixture reproductions).
- Does not fire on the gate-clean hubs of td244964's graduated cohort
  (fixture slice).
- Report ranks hubs by unmatched-numeral count and prints the unmatched
  numerals with their claim context.
- A measured false-positive estimate on a real corpus slice is recorded on
  the gripe before the tool's output is used to drive any remediation.

## Target + blast radius

New `taproot/numeral_audit.py` + CLI wiring; read-only against
refs/chunks. No schema change, no worker.

## Open questions / decisions log

- Normalization table scope for slice 1 (proposal: %, ×10^n notation,
  spelled-out one–ten, SI prefixes; everything else reported raw).
- Should hub_refine ever consume this signal? (out of scope here; decide
  after the false-positive measurement.)
