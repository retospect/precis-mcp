---
status: ready
title: taproot numeral cross-check advisory (report-only)
pillar: memory-graph
prio: normal
---

# taproot numeral cross-check advisory (report-only)

From gr250034. Spec drafted 2026-09-09; Reto accepted the spec defaults
2026-10-11.

Who: agent-code. Base-rate caveat applies: per the td249939 sample, content-error
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

## Shipped (slice 1)

`taproot/numeral_audit.py::absent_numerals` surfaces as the advisory
`numeral-absent` code in `lint_claim_sentence` (needs `source_text`, so it
appears on the approve/review page beside `unsupported-term`; never in
`_BLOCKING_LINT_CODES`). Normalises thousands separators, unicode minus,
`1.0`/`1`, `x10^n`/`e`-notation, bare `10^n` (incl. PDF-flattened `106`),
ranges, `%`, SI prefixes, source-side spelled one-ten. The report CLI
`precis taproot numeral-audit` (`--hub`, `--sample N --seed`, `--format
text|json`) ranks live claim hubs by unmatched-numeral count with the
grounding chunk ids; read-only, separate from `run_mint_gates`.

## Measured false-positive rate (2026-10-12, 300 random prod claim hubs)

166 of 300 hubs had a checkable claim numeral and grounding chunks; 20 (12%)
were flagged. In 17 of the 20 the numeral's digit string occurs nowhere in
the grounding text (candidate fabrication or a value on another page; true
positives not verified against the papers). Tokenizer-attributable false
positives: 2 of 20 (10%) — PDF-spaced exponent `8.4 × 1 0 4 cm−1`
(fi349979) and a name that carries digits, `GMTKN55` (fi453546); 1 more
(fi349605, `3 h` vs a `CNP-3` label) is unclear. Other shapes seen: range
endpoints where only one end is stated (fi176696 `300–365 nm`), derived
values. Treat the flag count as an upper bound on tokenizer FPs of ~10–15%
and a lower bound on real misses only after paper reading.

Folded 2026-10-12 (gr250034): space-separated exponent digits (`1 0 4`,
a flattened `x10^4`) now widen the source side only — the spaced digits
still count as themselves. Open: decide whether hub_refine consumes the
signal.

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

## Target + blast radius

New `taproot/numeral_audit.py` + CLI wiring; read-only against
refs/chunks. No schema change, no worker.

## Decisions log

- Slice 1 normalises %, ×10^n notation, spelled-out one–ten and SI
  prefixes; everything else is reported raw (Reto accepted, 2026-10-11).
- Whether hub_refine ever consumes this signal is decided after the
  false-positive measurement.
