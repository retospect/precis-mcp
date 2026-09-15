---
status: draft
title: mutate-diff reports false SURVIVED because it samples only ~5 covering tests
---

# mutate-diff reports false SURVIVED because it samples only ~5 covering tests

Found 2026-09-15 on the d5e1d342 ship (quote-contiguity + paper-context
sentence). The advisory mutation pass reported 6 survivors; at least two
were false, and the killing test existed the whole time.

`scripts/mutate-diff` runs each mutant against "just its covering tests"
from the recorded coverage contexts, but the per-mutant list it prints —
and, it appears, runs — is capped at about 5 tests. When a changed line is
covered by more tests than the cap, the test that actually kills the
mutant can fall outside the sample, and the mutant is reported SURVIVED.

Verified, not inferred. For
`src/precis/workers/context_sentence.py:126  boolop or -> and`, the
printed covering set listed five tests, none of them
`TestLint::test_empty_is_a_violation`. Applying that exact mutation by
hand and running the whole `TestLint` class gives
`1 failed, 6 passed` — `test_empty_is_a_violation` fails, so the mutant
is killed by a test that already exists. Same shape for
`src/precis/export/latex.py:1217  unary: remove not`, whose sampled set
omits `test_footnote_prefers_frozen_contiguity_flag_over_live_recompute`
— the one test written specifically to distinguish that branch.

Why it matters: `/go` tells the operator to treat every survivor as a
residual to fix or file. False survivors send that effort at tests that
are already adequate, and — worse — train the reader to discount
survivors generally, which is exactly when a real one gets waved through.

Fix direction: either run every covering test per mutant and cap only
what is *printed*, or, if the cap is a deliberate budget control, make
the output say so (`SURVIVED (sampled 5 of N covering tests — may be a
false survivor)`) so the signal is honest about its own confidence.
Check whether `PRECIS_MUTATE_MAX`/`_BUDGET` is what imposes the cap
before changing behavior.

test: a line covered by >5 tests, where only the 6th kills the mutant,
is reported KILLED (or explicitly marked as sampled).
