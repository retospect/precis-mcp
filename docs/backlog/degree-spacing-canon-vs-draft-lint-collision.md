---
status: draft
title: "°C spacing unified on SI — sweep existing drafts to the spaced form"
model: sonnet
---

# Decided and implemented; the corpus sweep is what remains

Found 2026-08-19: the claim notation canon mandated `25 °C` (spaced) while
`precis-draft-help` and `handlers/_draft_lint.py` mandated `63°C` (unspaced)
and flagged the spaced form as an offender. Two live rules, opposite
instructions, same characters — an agent drafting a paper *and* minting
claims from it received both.

**Reto's call: canon wins, spaced everywhere.** SI separates a value from a
unit symbol and `°C` is a unit symbol. The degree of an **angle** is not, so
angles stay tight (`85°`) — both surfaces now say this.

Shipped in the same pass:

- `_BAD_TEMP_PATTERNS` — dropped `\d\s+°` (the spaced form is now canonical),
  added `\d°[CF]\b` (the tight form is now the offender). Both remaining
  patterns require a trailing `C`/`F`, so a bare angle never fires.
- `temperature_form_hint` docstring + hint text now teach `63 °C` and name
  the angle carve-out.
- `precis-draft-help` "Units & temperatures" rewritten, cross-referencing
  `precis-notation-canon` so the two surfaces are visibly one rule.
- `tests/test_draft_handler.py::test_temperature_form_hint` inverted, with
  `85°` added as an explicit must-not-fire case.

## What remains — the sweep

Every draft already written carries `63°C`. Those chunks now trip
`⚠ temperature/unit formatting` on their next write. The hint is advisory
and no write is refused, so nothing is broken — but until the sweep runs the
hint is noisy in the opposite direction, which is exactly how a linter
teaches people to ignore it.

- Size it first: count `chunks` whose `text` matches `\d°[CF]` across live
  drafts. This was never measured — do not assume it is small.
- The rewrite is mechanical (insert one space) and content-preserving, so it
  fits the `normalize_notation` discipline in
  `docs/conventions/corpus-normalization.md`: dry-run over a CSV dump, assert
  idempotence, and diff before writing.
- **Not** the same pass as the claim-hub notation normalization
  (`nanopub-corpus-remediation.md` Phase 3 step 4) — different table,
  different surface. But the two should use the same dry-run harness, and
  the claim pass now emits the spaced form, so running them in either order
  converges.

## Check for siblings

Nothing enforces that `precis-draft-help` and `precis-notation-canon` keep
agreeing on the rows they share — `±`, en-dash ranges, and the ASCII→UTF-8
fallbacks all appear in both and happen to agree today. A test asserting the
shared rows match, or folding both onto one table, would stop the next
divergence from being found by accident.
