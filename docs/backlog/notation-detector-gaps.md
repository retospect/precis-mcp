---
status: idea
title: two advisory-severity detector gaps in taproot/notation.py
pillar: memory-graph
---

# Notation detector gaps

Both in `src/precis/taproot/notation.py`, both advisory-only (never
auto-rewrite), found at pre-ship review 2026-08-19.

1. **`formula-ascii-subscript` misses the commonest two-element
   formulas.** `_FORMULA_ASCII_SUBSCRIPT_RE`'s `(?<![-A-Za-z])` lookbehind
   (added to stop it firing mid-acronym, e.g. the `S` in `ZSM-5`) also
   kills detection whenever an element symbol directly abuts a preceding
   letter: `TiO2`, `SiO2`, `CaCO3` never fire (`i`/`a` precedes `O`/`C`).
   `Fe3O4` does fire (its `O` follows a digit). Weakest exactly where a
   materials corpus is densest. Fix needs a real formula tokenizer, not a
   wider lookbehind — size before attempting.
2. **`ascii-x-multiplier` has no closed-token guard.** `_ASCII_X_MULT_RE`
   (`(?<=\d)x\b`) fires on any digit-then-`x` at a word boundary, unlike
   `_ASCII_MINUS_EXP_RE`, which gained closed accepted-token discipline
   after the `Fe-ZSM-5` incident (`docs/conventions/corpus-normalization.md`
   §1). A bare composition variable at a word boundary (`…Sr2x.`) would
   rewrite to `Sr2×`. Not currently a defect: the corpus dry run changed 19
   hubs, zero false positives — `2x2` excluded by the trailing `\b`,
   `AlxGa1-xAs`/`Cu2-xS` excluded because `x` follows a letter or hyphen,
   not a digit. Watch item for a corpus with different nomenclature habits
   (bio, geology); re-run the dry run before trusting it there.

3. **`ascii-angstrom` fires on the word "A" (gr249075).** The rule in
   `_ANGSTROM_RE` in `src/precis/taproot/notation.py` fires on the bare
   capital word `A` as if it were an ångström. Measured base rate over the corpus: 0 of 104 hits real, and the
   recommendation was to hold the rule. Who: needs-Reto: hold the rule
   (leave it advisory and unshipped to rewrite) or close the gripe as
   not worth fixing; if kept, tighten `_ANGSTROM_RE` to require a preceding number.
