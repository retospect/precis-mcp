---
status: draft
title: claim hubs — mandatory pre-mint dedup search + a notation canon for claim sentences
model: sonnet
---

# Two skill rules the claim-mint path is missing

Both surfaced operating the mint path against smartdrafts dr43004 /
dr43012 / dr43029 (2026-08-19). Both are *skill* gaps first — agents mint
correctly-grounded hubs that are nonetheless near-duplicates of each
other, because nothing in `precis-taproot-help` tells them to look first
or to write numbers the same way twice.

## 1 — Search before minting; strengthen rather than duplicate

`precis-taproot-help` currently sells `pub_id` convergence as if it were
dedup: mint "converges onto an existing one for identical claim content".
True but narrow — convergence is a **content hash**, so it catches only
byte-identical (post-NFKD) sentences. Two agents phrasing one claim two
ways mint two hubs, each with half the evidence.

Live evidence: `fi191132` and `fi211518` are both "a pentagon–heptagon
defect pair joins incompatible nanotube lattices" geometric-construction
claims, minted independently and never merged.

Proposed addition to `precis-taproot-help`, as a **hard gate** in the
mintable-claim rubric (not a soft flag), immediately before "Mint a claim
hub from a claim I've already sourced":

> **Search before you mint.** `pub_id` convergence is byte-level, not
> semantic — two wordings of one claim mint two hubs, splitting the
> evidence that should have stacked on one. Before every mint, search the
> claim sentence you are about to write:
>
> ```python
> search(kind='finding', q='<the claim sentence>', status='*', mode='semantic')
> ```
>
> `status='*'` is **required** — the default filter is
> `status='established'` and silently hides most hubs.
>
> Then judge each near hit:
>
> - **Same claim, same scope** → *don't mint*. Attach your evidence to the
>   hub that exists: `link(kind='finding', id='fi<existing>',
>   rel='corroborates', target='pc<your chunk>')`. One hub with three
>   independent groundings outweighs three hubs with one each — this is
>   the strengthening move, and it is the default.
> - **Same claim, your wording is better** → reword in place
>   (`edit(kind='finding', id='fi<existing>', title=…)`; keeps the old
>   `pub_id` as an alias, evidence untouched), then attach.
> - **Different scope or a different quantity bound** → mint, then record
>   the relation: `link(rel='refines')`.
> - **Your source disagrees with the existing hub's number** → mint, and
>   `link(rel='contradicts')`. Never silently restate someone else's
>   quantity.
> - **Two existing hubs are near-duplicates of each other** → merge them
>   (recipe below) rather than adding a third.

## 2 — A notation canon for claim sentences

The skill has one soft "Notation" flag (UTF-8, never TeX). That leaves
every other choice open, and the corpus shows the drift — three spellings
of one unit, two of them minted the same day:

| form | where |
|---|---|
| `≈10,000 cm²/Vs` | `precis-taproot-help`'s own example |
| `100,000 cm²V⁻¹s⁻¹` | `fi218293` |
| `4,600 cm² V⁻¹ s⁻¹` | `fi218337` |

### This is load-bearing, not cosmetic

`pub_id` = SHA-256 over `normalize_text_for_hash(sentence)` =
NFKD-fold + lowercase + whitespace-collapse
(`identity.py::normalize_text_for_hash`, via
`make_taproot_hub_paper_id`). So NFKD already forgives some of it —
**verified**:

- `cm²` ≡ `cm2`, `C₆₀` ≡ `C60`, `10⁵` ≡ `105` (super/subscript digits
  decompose)
- `µ` U+00B5 ≡ `μ` U+03BC

and does **not** forgive the rest — each of these mints a second hub:

- **spacing inside a unit**: `cm²V⁻¹s⁻¹` → `cm2v−1s−1` vs
  `cm² V⁻¹ s⁻¹` → `cm2 v−1 s−1`
- **solidus vs negative exponent**: `cm²/Vs` → `cm2/vs`
- **superscript minus**: `⁻` U+207B folds to `−` U+2212, *not* to ASCII
  `-` — so `cm²V⁻¹s⁻¹` ≠ `cm2V-1s-1`
- **digit grouping**: `4,600` vs `4600`; `10⁵` vs `100,000`

### Proposed canon

| question | rule |
|---|---|
| exponents | UTF-8 superscript — `cm²`. Never `cm^2`, never TeX `$\mathrm{cm}^2$`. |
| compound units | negative exponents, space-separated: `cm² V⁻¹ s⁻¹`. |
| single denominator | solidus is fine and preferred: `mA/µm`, `mV/dec`. Never a solidus with two denominators (`cm²/Vs` is ambiguous). |
| minus | `⁻` U+207B inside superscripts, `−` U+2212 standalone. Never ASCII hyphen as a minus. |
| digit grouping | **none** — `4600`, not `4,600`. Grouping is locale-dependent and is the single largest hash-splitter. |
| order of magnitude | SI prefix when one exists and the mantissa lands in 0.1–1000 (`4.6 nm`, `250 µm`); power of ten otherwise (`10¹¹ cm⁻²`, `1.0×10⁻⁶ S`). Never both (`10⁻⁶ µm`). |
| unit system | SI, except a field's own conventional unit where it is unambiguous (eV, Å, bar, °C, ppm). Convert customary units (inch, psi, kcal); record the original in `scope`, not in the sentence. |
| multiplication | `×` U+00D7. Never `x`, never `*`. |
| ranges | en dash, unspaced: `19–39°`. |
| approximation | `≈`. Not `~` (ambiguous between "about" and "of order"). |
| percent / degrees | `50%`, `85°` unspaced; `300 K`, `25 °C` spaced. |

### One tension to resolve while doing this

`precis-nanopub-help`'s claim-sentence grammar says "numbers with units
matching the source **exactly**". Read literally that forbids
normalization, and it collides with the canon. Amend to:

> numbers matching the source in **value and bound**; notation normalized
> per the notation canon.

The boundary that must be stated explicitly in both skills: **quotes are
verbatim and are never normalized** (the mint gate refuses a rewritten
quote). The canon governs the *authored claim sentence* only.

## Durable fix behind the skill rule

The skill rule is the interim. The structural fix is to fold notation
before hashing: `taproot/migrate.py::_normalize_number_text` already does
most of this (it is used by `reground`), but the `pub_id` path does not
call it. Routing `make_taproot_hub_paper_id` through it would make
grouping, spacing and solidus-vs-exponent stop splitting identities —
after which the skill rule is about *legibility* rather than correctness.
Note this changes existing pub_ids, so it needs the alias path.

Cheap assist meanwhile: a non-blocking lint in
`taproot/hub.py::seed_claim_hub` warning on `^`, `$`, a comma between
digits, and a two-denominator solidus.

## Follow-up

Survey minted hub titles for how far the drift already reaches, to size a
retro-normalization pass (`edit(kind='finding', title=…)` rewords in
place and keeps the old pub_id as an alias, so a retrofit is safe).

## Detector gaps found at pre-ship review, 2026-08-19

Both are in `taproot/notation.py`, both advisory-severity — filed rather
than fixed because neither can corrupt data.

1. **`formula-ascii-subscript` misses the commonest two-element formulas.**
   `_FORMULA_ASCII_SUBSCRIPT_RE` carries a `(?<![-A-Za-z])` lookbehind to
   stop it firing mid-acronym (the `S` in `ZSM-5`). That same guard kills
   detection whenever an element symbol directly abuts a preceding letter:
   `TiO2`, `SiO2`, `CaCO3` never fire, because the `O`/`C` is preceded by
   `i`/`a`. `Fe3O4` *does* fire — its `O` follows a digit. So the detector
   is weakest exactly where a materials corpus is densest. It is
   detector-only and never auto-fixed, so the cost is a missed hint, not a
   wrong rewrite. Fixing it means distinguishing "letter that ends an
   element symbol" from "letter mid-acronym", which likely needs a real
   formula tokenizer rather than a wider lookbehind — size it before
   attempting.

2. **`ascii-x-multiplier` has no closed-token guard.** `(?<=\d)x\b` fires on
   any digit-then-`x` at a word boundary, with none of the closed
   accepted-token discipline that `_ASCII_MINUS_EXP_RE` gained after the
   `Fe-ZSM-5` incident (`docs/conventions/corpus-normalization.md` §1). A
   composition variable written bare at a word boundary (`…Sr2x.`) would be
   rewritten to `Sr2×`. **Not currently a defect:** the corpus dry run
   changed 19 hubs with zero false positives, and the usual shapes are
   excluded structurally — `2x2` by the trailing `\b`, `AlxGa1-xAs` and
   `Cu2-xS` because the `x` follows a letter or hyphen, not a digit. This is
   a watch item for corpora with different nomenclature habits (bio,
   geology), not a known bug. Re-run the dry run before trusting it on a new
   corpus.
