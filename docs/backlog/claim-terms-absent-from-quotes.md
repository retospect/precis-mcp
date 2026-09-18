---
status: draft
title: flag claim terms that appear in no quoted passage, and teach the minter to quote the definition
prio: normal
---

# flag claim terms that appear in no quoted passage, and teach the minter to quote the definition

## Motivation / why

A claim can assert something in vocabulary the evidence never uses, and
nothing between extraction and signature notices.

fi191121 (signed 2026-09-17, publish row 15) reads "…a C60 nanobud on a
semiconducting (10,0) single-walled carbon nanotube…". Both frozen
grounding quotes say only **CNB100** — the label paper 498 coins for that
system. The equivalence is real and the claim is correct, but a third
party reading the signed artifact has no way to reach it: the definition
lives at ords 12 and 14 of the same paper, unquoted, and the artifact
carries only the two quotes. Reto, 2026-09-17: *"How does the LLM know
this, how does the nanobuds paper reader know this equivalency between C60
and CNB100?"*

This is one instance of a shape that recurs whenever a paper coins its own
label, which is most papers. Three sub-shapes, only one of which has an
external authority to defer to:

| shape | example | where the mapping lives |
|---|---|---|
| paper-local coinage | CNB100 → C60 on (10,0) SWCNT | in the paper, at first use |
| community synonym | SWCNT / SWNT / single-walled nanotube | nowhere in particular; usage |
| registry identity | C60 → InChIKey / CAS | an external authority |

**Decision (Reto, 2026-09-18): the bridge is a quoted passage, not a
table.** A house translation table would assert "CNB100 = C60 on (10,0)
SWCNT" on *our* authority — unsigned, unsourced, silently wrong the first
time a second paper uses the label differently. The definition sentence in
paper 498 *is* the evidence for the equivalence; quoting it makes the claim
carry its own translation, signed and scoped to the paper that coined it.
Normalizing the papers instead was rejected outright: it would break the
byte-identical-quote property the whole provenance story rests on.

## In scope

1. **A new advisory lint code** — `unsupported-term` — in
   `taproot.sentence_lint.lint_claim_sentence`, reached through
   `nanopub.gates.advisory_lint`. Flags a notation-shaped term in the claim
   sentence that appears in neither `source_text` nor the source's title.
   **`source_text` must carry only chunks of real evidence kinds**
   (paper/patent/datasheet/edgar, per `nanopub_evidence_source_kinds`). A
   sibling finding's prose is not a source: measured 2026-09-18, letting
   finding chunks in cleared `C60` for fi191121 out of *another finding's*
   text, which is precisely the failure this rule exists to catch.
   Advisory, never blocking: a claim that generalizes past a quote's literal
   wording is sometimes exactly right, so the reviewer must see the flag,
   not be stopped by it.

2. **A section in the `precis-taproot-mint-help` skill** — when your claim
   uses a term the passages don't, search the paper for where it coins that
   term and quote *that* passage too, as additional grounding. (Not
   `precis-claim-fidelity-help`: that one governs draft prose restating an
   existing hub, the downstream direction.)

No new plumbing is needed for (1). `advisory_lint(sentence, *,
artifact_type, source_text)` already exists and the review page's
pre-approve dry-run already threads `bundle.grounding_chunks` text in as
`source_text` for the `all-caps-artifact` rule (gr245768).
`all-caps-artifact` is also the shape precedent: extract a token class from
the claim → clear it against an allowlist → clear it against `source_text`.
This rule is the same three steps over a different token class.

## Explicitly NOT in scope

- **No translation table, synonym registry, or canonical-vocabulary
  store.** The lint says a term is unsupported; the skill says go quote the
  definition. Neither asserts an equivalence on our authority. That
  property is the point, not an omission.
- **No `defines` link relation.** An index over something the corpus
  already contains. Worth building only if "find where the paper coins this
  term" turns out to be unreliable in practice — premature now.
- **No registry identity work** (InChIKey, CAS, DOI). Different project,
  and the only one of the three shapes with a real authority behind it;
  keeping it separate is what stops the folklore from accumulating.
- **Not blocking.** This never joins `_BLOCKING_LINT_CODES`.
- **No re-mint of existing signed hubs.** fi191121's own remedy is Reto's
  open decision in gr345628, tracked there, not here.

## Acceptance criteria

- `advisory_lint("…a C60 nanobud on a semiconducting (10,0) single-walled
  carbon nanotube…", source_text=<the two fi191121 passages>)` returns an
  `unsupported-term` warning naming **both `(10,0)` and `C60`** — the real
  regression case, measured 2026-09-18 against the actual paper-498 grounding
  chunks. `C60` is the term Reto asked about; an earlier draft of this
  criterion named only `(10,0)`.
- The same call with paper 498's definition passage appended to
  `source_text` returns no `unsupported-term` warning.
- Ordinary prose does not flag: a claim whose only unmatched words are
  common nouns/adjectives ("semiconducting", "measured") produces nothing.
  The token class is notation-shaped only.
- A term present in the source's **title** but not in any quoted passage
  does not flag.
- `unsupported-term` is absent from `_BLOCKING_LINT_CODES`; a hub carrying
  one still mints.
- The `/nanopub/fi<id>` approve page shows the warning beside a passing
  claim-sentence gate, in the existing "passed, but look at this" channel.
- `precis-taproot-mint-help` gains the step, and its `answers:` front-matter
  gains a question in the shape of "the paper calls it X but my claim says
  Y — what do I quote?".

## Target + blast radius

- `src/precis/taproot/sentence_lint.py` — the new rule + its token
  extractor.
- `src/precis/nanopub/gates.py` — advisory-set registration only.
- `src/precis/data/skills/precis-taproot-mint-help.md` — the new step.
- Read-only effect on `precis_web/routes/nanopub.py` (the approve page
  renders whatever `advisory_lint` returns; no route change expected).
- No migration, no schema change, no worker.

## Open questions / decisions log

- **Term extraction is the fuzzy part, and there is no ready-made
  extractor.** Checked 2026-09-18: `taproot.notation.lint_notation` is
  about notation *formatting* (carets, TeX residue, digit grouping, ASCII
  multiplication) — it does not tokenize terms, so it cannot be reused
  here. `sentence_lint`'s `all-caps-artifact` gives the *shape* but its
  token class (ALL-CAPS, ≥4 letters) is far narrower than what is needed.
  The class to define: mixed alphanumeric labels (`CNB100`, `CNB55`),
  parenthesized chirality/index pairs (`(10,0)`, `(5,5)`), chemical
  formulae with subscripts (`C₆₀`). Deliberately excludes ordinary words.
  **DECIDED 2026-09-18 against corpus evidence** — measured on all 2373
  prod `finding` claim sentences, 1463 of them grounded by a real evidence
  kind. Four arms, union:

  | arm | shape | corpus hits / distinct | top |
  |---|---|---|---|
  | index pair | `\(\s*\d+\s*,\s*-?\d+\s*\)` | 26 / 13 | `(10,0)`×5 `(6,6)`×5 |
  | subscript formula | `[A-Z][a-z]?[₀-₉]+` | 124 / 29 | `C₆₀`×41 `C₅₀`×13 |
  | hyphen-number label | `[A-Za-z]{2,}-\d+[A-Za-z0-9]*` | 236 / 73 | `UiO-66`×43 `ZIF-8`×29 |
  | mixed alphanumeric | letters+digits, ≥2 chars | 578 / 289 | `C60`×24 `CO2`×21 |

  Exclusions are corpus-evidenced, not invented: `0D 1D 2D 3D 4D` (`3D`×30
  and `2D`×23 are the two loudest false positives) and a leading
  `sub-<digit>` (`sub-10 nm`). **72% of claim sentences (1717/2373) extract
  zero tokens** — the rule is silent on most claims, and flags **107 of 1463
  grounded findings (7.3%)** under the normalization below.
  Residual false positives worth weighing for an allowlist (the
  `_CAPS_ARTIFACT_ALLOWLIST` analogue, same bar): `CO2`, `O₂`, `Cl₂`, `NH3`,
  `group-13`, and the DFT functionals `BP86` / `B3LYP` / `M06` — the last
  arguably flagged correctly, since the claim names a method the passage
  never names.
- **Normalization — DECIDED 2026-09-18: reuse `nanopub.snip.normalize_text`,
  then add one step.** It is soft-hyphen strip + ligature unfold + NFKD +
  casefold + whitespace-collapse, and its **NFKD already folds `C₆₀` → `c60`**
  — the biggest single win, free. Measured flag rates: exact substring 10.0%
  → subscripts folded 8.2% → whitespace collapsed 8.1% → **TeX residue
  stripped 7.3%**.
  - **Stripping TeX residue (`$ { } \ _ ^`) is required, not optional.** It
    removes ~50 false positives. Paper 498 writes the fullerene `$C_{60}$` at
    ord 12 — the very passage that defines CNB100 — so without folding, every
    TeX-bearing passage reads as omitting the term.
  - Also remove intra-token whitespace: `(8, 8)` really does occur beside
    `(8,8)` in this corpus, so that question was not hypothetical.
  - Reuse means accepting casefold. Measured cost: 2 tokens out of 188. Take
    the reuse; do not grow a second normalizer.
- Should the warning name the candidate definition passage when the term
  *is* found elsewhere in the same paper? That is most of the value for the
  reviewer (one click to add it as grounding) but needs a paper-body search
  at mint time. **First slice is flag-only** — 7.3% is already an actionable
  rate, and the skill step tells the minter how to find the passage by hand.
  If slice 2 happens, it must search on the *normalized* form: a literal
  `C60` scan of paper 498 hits ords 27 and 28, which define nothing, and
  misses the actual definition at ord 12 because that one writes `$C_{60}$`.
