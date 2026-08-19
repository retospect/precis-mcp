# Corpus normalization — measure before you ship

**A normalizer or lint rule is not correct until it has been dry-run over
the whole live corpus and its wrong:right ratio measured.** A rule that
looks obviously right against a handful of examples in your head finds
counterexamples somewhere in 1,500+ rows of real text — chemistry
nomenclature especially. This governs any rule that rewrites (`normalize_*`)
or lints (`lint_*`) text at corpus scale — the pattern below is drawn from
the 2026-08-19 pass over 1,524 live claim hubs
(`src/precis/taproot/notation.py`, `src/precis/taproot/sentence_lint.py`).

## 1. Character classes lie; closed token sets don't

`ascii-minus-exponent` (`src/precis/taproot/notation.py`) was written to
turn `s^-1` / `s-1` into `s⁻¹`. Its first regex matched a *character
class* to the left of the hyphen (`[A-Za-z]-\d`). Over the corpus: **146
wrong rewrites against 3 right ones** — `Fe-ZSM-5` matched the substring
`M-5` and misread `M` as a unit, corrupting material names (`MOF-74` →
`MOF⁻⁷⁴`, `UiO-66` → `UiO⁻⁶⁶`, and more). Caught by a corpus dry run
before any production write; see `docs/backlog/nanopub-corpus-remediation.md`.

The fix: require the whole standalone token left of the hyphen to be a
member of a **closed accepted-denominator set**
(`_ACCEPTED_DENOMINATORS` in the same module), with a negative lookbehind
so a longer word can't end in a unit letter. Affected hubs dropped from
108 to 1.

**Rule: a rewrite must match a whole token drawn from a closed set, never
a character class.** A character class always finds a substring somewhere
in a large corpus of real chemistry/materials nomenclature.

## 2. Mandatory pre-ship procedure

Before any normalizer or auto-fix rule ships (or before `--fix` is
extended to a new lint code):

1. Dry-run over a dump of the full live corpus.
2. Print every proposed rewrite as a before/after pair.
3. Count them.
4. Eyeball a sample.
5. State the wrong:right ratio explicitly.

Ship the auto-fix only if wrong is zero. If a rule is valuable but can't
reach zero false positives, ship it **detector-only** — it reports a code
but is structurally excluded from `--fix`. Detector-only is a legitimate
outcome, not a failure: `title-body-divergence` and `missing-body-chunk`
(`src/precis/cli/taproot.py`) shipped that way in this pass because the
correct repair differs per cohort — there's no single mechanical fix.

## 3. Two invariants every rewrite rule must hold

- **Idempotent** — running it twice equals running it once.
- **Content-preserving** — no assertion, quantity, unit or qualifier is
  lost. Every rewrite is a respelling of something already there.

The corpus already carries scars from a truncation incident (200-character
titles); a normalizer that can drop content is a live hazard, not a
theoretical one.

**Character length is not the test, and asserting `len(out) >= len(in)` is
a bug.** Measured over the live cohort, `normalize_notation` shortens 63
hubs — every one of them a legitimate ASCII→UTF-8 respelling:
`microsiemens`→`µS`, `micromolar`→`µM`, `microvolts`→`µV`,
`Angstrom`→`Å`, `Ohm`→`Ω`, `+/-`→`±`, plus digit-grouping and caret
exponents. The largest single shrink is 19 characters and loses nothing.
Shortening is in fact one of the goals — a claim sentence is read far more
often by machines than by people, and `µS` costs fewer tokens than
`microsiemens` while being *less* ambiguous.

So the invariant has to be checked as content, not length: assert that the
rule's own regex matched and that the substitution is a member of a closed
spelling table (§1), rather than comparing string lengths. A length assert
would have blocked all 63 of those correct rewrites while still failing to
catch a truncation that replaced text with text of equal length.

## 4. A code that co-fires with a high-frequency code is not redundant

Worked example (`src/precis/taproot/sentence_lint.py`): the advisory code
`past-tense` fired on 588 of 1,524 hubs, and exactly **1** fired alone.
The tempting read — "587/588 co-fire, so it adds no signal, drop it" — is
wrong. Its top co-firing partners, `no-epistemic-mode` (556) and
`no-evidence-verb` (432), each fire on roughly 93% of the corpus;
*everything* co-fires with those. Repairing an epistemic mode or an
evidence verb does not change a sentence's tense, so those 588 hubs
become the visible next layer once the louder repairs land.

**Judge a code's independence against the base rate of its co-firing
partners, not against a raw co-occurrence count.** Queued signal is not
noise.

## 5. When two measurements disagree, probe the interface first

A hand-rolled corpus script reported 0 hits for a code the owning agent
measured at 588. Neither number was wrong: `sentence_lint.py::lint_claim_sentence`
returns `list[str]` of `"code: detail"` strings, and the script had
tested set membership against the full strings instead of splitting on
`:` and matching the code prefix.

**Procedural lesson: when two counts of the same corpus disagree, print
the actual return value of the function on a handful of pinned inputs
before arguing about which number is right.**
