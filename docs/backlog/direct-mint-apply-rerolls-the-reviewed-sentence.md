---
status: draft
title: "taproot direct-mint --apply re-runs the qualify LLM, so the sentence a human reviewed in the dry-run is not the sentence that gets written"
---

# `direct-mint --apply` writes a different sentence than the dry-run showed

Found 2026-08-22 minting fi237847 (the neck-length claim, backlog item 2b of
`hub-title-body-chunk-divergence`).

## What happened

`precis taproot direct-mint` is dry-run by default, and its help sells that as
the safety property: *"Default (omitted) is a read-only dry-run that makes ZERO
claim-data writes (the qualify LLM call still runs, budget-metered)."* The
implied workflow is review-then-apply.

But `--apply` **re-runs the qualify LLM from the proposed claim**, rather than
applying the qualified sentence the dry-run produced. Same input, same passage,
two different outputs minutes apart:

Dry-run (reviewed, and fine — one assertion, defining parenthetical):

> In carbon nanobuds, the width of the transmission plateau below the Fermi
> energy E_F (where transmission matches that of the pristine single-walled
> nanotube) depends on the length of the neck region connecting the fullerene
> to the nanotube.

Applied (actually written, 328 chars):

> In carbon nanobuds, calculated for three different CNBs, the transmission
> spectra show a plateau region below the Fermi energy E_F where the
> transmission equals that of the pristine single-walled nanotube (SWNT), **and**
> the width of this plateau region depends on the length of the neck region
> connecting the fullerene to the nanotube.

The applied version violates two standing rules at once:

- **Atomicity** — `precis-notation-canon`: *"A sentence joining two assertions
  with 'and' is two atoms — split it."* It joins exactly that way.
- **Don't duplicate, strengthen** — its first assertion restates the parent hub
  fi191129 ("below it the transmission retains a plateau matching that of the
  pristine nanotube"). The dedup-before-mint rule exists to stop precisely this,
  and the dedup check passed because it ran against the *proposed* sentence,
  which did not contain the duplicated assertion.

Repaired by hand via `refine_claim_sentence` (fi237847 → `pub_id` `34ongq`, old
`j7xs3h` kept as alias), but the next mint has the same exposure.

## Why it matters

A review gate that shows you A and writes B is not a review gate. Everything
downstream of the dry-run — the admissibility read, the notation check, the
duplication judgment, a human's eyeball — is spent on a sentence the system
then discards. The failure is silent and the output is *plausible*, which is
the worst combination: nothing errors, and the written claim reads fine until
you diff it against what you approved.

It also makes the dedup gate structurally unsound. Dedup runs on the proposed
sentence; the qualifier can then add clauses that collide with an existing hub,
and nothing re-checks.

## Fix

Make apply write what the dry-run showed. Either:

1. **Carry the qualified sentence forward** — dry-run emits it (it already
   prints it, and `--out` writes a report); `--apply` accepts it back and skips
   re-qualifying. This also halves the LLM spend on the review path.
2. **Or make apply a two-phase call** that qualifies once and writes in the
   same invocation, with `--apply` gated behind showing the qualified sentence
   — no separate dry-run to diverge from.

Either way, re-run the admissibility and dedup checks against the **final**
sentence, not the proposed one. If the qualifier is left free to add clauses,
those checks belong after it, not before.

## Verification

Run `direct-mint` twice on one claim+chunk, once dry once applied, and assert
the written `refs.title` equals the dry-run's reported qualified claim. Cheaper
regression: assert the qualify call happens exactly once per `--apply`.
