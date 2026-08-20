---
status: draft
title: bring the 1,527-hub claim corpus up to a publishable standard before anything leaves the house
---

# Claim-corpus remediation

An audit on 2026-08-19 found the claim corpus is broadly unpublishable, in
ways notation normalization does not touch. This is the plan to fix it.

**The reframing that makes this tractable: nothing has been published.** Of
1,527 live claim hubs, 1 is `signed`, 138 are `reviewed`, and 1,388 have no
`nanopub_publish` row at all. Zero are `published`. So the freeze-at-review
semantics — which constrained every repair made during the audit, and which
produced the 77 stuck drift rows — **do not actually bind us**. We can reset
reviewed rows to candidate and re-approve under a corrected standard. Treat
this as pre-launch cleanup, not a live-corpus migration. That choice expires
the moment the first artifact is published.

## Position, verified against prod 2026-08-19 (read-only)

Re-measured directly rather than inherited from a transcript. Two repair
steps have already landed; the rest of the sequence is untouched.

| step | state |
|---|---|
| 1 — reset `reviewed`→`candidate`, unsign the 1 `signed` | **done**. `nanopub_publish` holds 139 rows, all `candidate`, with `approved_title` and `claim_sha` NULL on all 139 — a genuine reopen, not a state-only flip. The 77 drift rows and 5 truncations are dissolved. |
| 2 — fi191259/fi191268 merge (Decision 6) | **done**. 191259 soft-deleted, 191268 live. |
| 3 — title/body cohort repair | **not started**, and larger than the ~209 estimate: **297** live hubs have `btrim(chunk) <> btrim(title)`. **0** are missing the `ord=0` chunk, so `missing-body-chunk` is currently empty and the whole cohort is a divergence problem, not an absence problem. |
| 4 — notation normalization | **dry-run clean 2026-08-20, ready to apply.** 456 of 1,524 hubs (29.9%) change, idempotent, zero known-wrong fires. Getting here took a code fix (canon v3.1): four sibling rules shared the `ascii-minus-exponent` defect, one of them corrupting oxidation states (`Zn2+-sensing`→`Zn2±sensing`). See the 2026-08-20 re-run below. Not yet applied to prod. |
| 5 — `pub_id` re-hash + fresh duplicate scan | not started, and **re-scoped**: normalization creates zero new duplicates. Exactly two exact-sentence duplicate groups exist corpus-wide (fi191179/fi191260, fi191192/fi191262), both already live, both forked by free-text `scope` values rather than punctuation. A scan grouped on `pub_id` finds neither. |
| blocker — dedup-index coverage | **RESOLVED 2026-08-19.** Was 187 of 1,524 (12.3%). There was no broken card-forge pass to repair — no code path has ever written a hub's `card_combined`. `block()` now retrieves over the `ord=0` `finding_body` chunk, which all 1,524 hubs have and all 1,524 have embedded, so coverage is 100% with no backfill. See defect 4 below. Dedup verdicts are believable from here on; step 5's duplicate scan is unblocked. |

One consequence of step 1 worth stating, since it changes what is
recoverable: the reopen NULLed `approved_title` on all 139 rows, so that
column is no longer available as a repair source for the step-3 cohort.
The 200-char repair had already written its reviewer-authored rewordings
back into `refs.title` before the reset, so nothing known was lost — but
the "restore from `approved_title`" option that the frozen cohort needed
no longer exists, and step 3 must resolve `refs.title` vs chunk on those
two witnesses alone.

## What the audit found

| | count | of 1,527 |
|---|---|---|
| No evidence edges at all | 234 | 15% |
| Grounded only in non-papers (draft/todo/memory) | 44 | 3% |
| Groundable for minting (paper + `pdf_sha256`) | 1,188 | 77% |
| Exactly one evidence edge | 426 | 28% |
| Carries a `contradicts` edge | 2 | 0.1% |
| Carries a `refines` edge | 4 | 0.3% |
| No controlled evidence verb in title | 1,470 | 96% |
| No epistemic-mode token in title | 1,352 | 89% |
| Colon / em-dash label style | 172 | 11% |
| Author name in the sentence | 60 | 4% |
| Near-duplicate pairs (>0.75 / >0.9 / identical) | 19 / 6 / 3 | — |

Title length: min 27, median 147, p90 249, **max 506** characters.

Reading a random 45-title sample, the failures sort into classes that matter
more than any single count:

- **Many are not claims at all.** `Meir & Wingreen 1992 — Landauer formula
  for interacting electrons` is a bibliography entry. `Primary sources for
  freeze-seal reagent containment` is a to-do note. `NUPACK is a software
  suite for...` is a definition. `Surface interactions between graphene
  nanobuds and cerium(III) were investigated` states that a study happened
  and asserts no finding. These are unfalsifiable by construction — they can
  never be corroborated or contradicted, so they are inert mass in the graph.
  The orphan-hub examples *are* the bibliography stubs: something was minting
  reading-list entries as claims.
- **Some are not self-contained.** `The same group demonstrated ultra-
  sensitive detection of vitamins B9 and B12...` — "the same group" refers to
  nothing once the sentence stands alone. It is also half of a near-duplicate
  pair.
- **The 96%-no-verb figure has a caveat that is itself the finding.** The
  grammar rules live under "Claim-sentence grammar (**the approved title**)",
  so a candidate hub is not strictly bound by them. But approve syncs
  `refs.title` to the approved string — meaning the reviewer rewrites the
  sentence from scratch at approve time, every time. That is where the ~51
  pre-existing title drifts came from. **The grammar must be enforced at
  authoring, not discovered at approval.**

## Phase 0 — fix the standard first

Nothing else runs until this lands, or passes get re-run.

**Notation canon v2** (`precis-taproot-mint-help`). Reto's rulings, 2026-08-19:

- ASCII→UTF-8 fallback table, now explicit and closed: `+/-` → `±`,
  `ug`/`micro` → `µ`, `degrees C` → ` °C`, `x`/`*` (multiplication) → `×`,
  `Ohm` → `Ω` (so `kOhm` → `kΩ`), `Angstrom` → `Å`, `micrometer`/`micron` →
  `µm`. Symbol respelling is **not** unit conversion and is not blocked by
  the "never convert the paper's unit" carve-out — say so explicitly, since
  all three normalization agents were blocked by that ambiguity.
- **Letter sub/superscripts stay ASCII.** `K_d`, `E_g`, `ΔG_aq`, `R_Q`, `2^N`
  keep underscore/caret form. Unicode has no subscript `d`/`g`; the modifier
  superscripts (`ᴺ`, U+1D3A) are a different character class and render
  inconsistently. Only digits and `+`/`−` sub/superscript. All three agents
  improvised this rule independently — that is the signal it was missing.
- **`~` is overloaded.** Before a numeral it is approximation → `≈`. Between
  expressions it is proportionality (`E_g ~ 1/W`) → leave alone. One agent
  caught and correctly skipped this; the canon should not have relied on that.
- **`≈` spacing.** No space when it modifies a quantity (`≈1 Å`); space when
  it is a binary relation with a symbol on the left (`n ≈ 10²²`).
- **Terse is better, and it is a real rule, not a preference.** Prefer the
  shortest sentence that stays falsifiable and self-contained. A sentence
  joining two assertions with "and" is two atoms — split it. A 506-character
  claim is not an atomic claim. This connects to the known SMALL-tier failure
  where multi-clause claims collapse into one truncated atom: shorter atoms
  make that failure mode structurally impossible.

**Claim admissibility** (new section, `precis-taproot-mint-help`). The test a
sentence must pass *before* it earns a `TAPROOT:claim` tag:

1. **Falsifiable** — asserts a finding that some future measurement could
   contradict. Not a definition, not a topic label, not a bibliography entry,
   not "X was investigated", not historical narration.
2. **Self-contained** — no "the same group", "this work", "as above", no
   dangling comparative.
3. **Method-attributed** — the epistemic mode is readable from the sentence.
4. **Single assertion** — see terseness above.

**Grammar scope change** (`precis-nanopub-help`). Move claim-sentence grammar
from approved-title scope to **authoring** scope, and cross-reference it from
the taproot skill so hubs are born conforming. This is the structural fix for
the 96%, and it is what stops drift from being manufactured at approve time.

## Phase 1 — make the standard machine-checkable

- Extend `taproot/notation.py::lint_notation` with the v2 rules (`Ohm`, `Angstrom`,
  `micrometer`, `+/-`, `ug`, `1e3`, `degrees C`, proportionality-tilde exemption).
- New `lint_claim_sentence`: advisory codes for the admissibility and grammar
  rules — `not-falsifiable` (label style, `^[A-Z][a-z]+ (19|20)\d\d`,
  copula-definition, "was/were investigated"), `dangling-reference`,
  `no-evidence-verb`, `no-epistemic-mode`, `multi-assertion`, `no-terminal-period`.
  Heuristic and non-blocking by construction; it flags for judgment, never
  rewrites.
- A `precis taproot lint` CLI that runs both over an arbitrary cohort and
  emits per-code counts — this is the measurement instrument for every phase
  below, and the natural backing for the `taproot-health` view proposed in
  `mcp-aggregate-surface-gaps.md`.
- **Enforcement asymmetry:** lint *advises* at mint, *blocks* at approve.
  Authoring stays frictionless; nothing ungoverned reaches a publishable state.

## Phase 2 — triage deterministically, before touching anything

Partition all live hubs by SQL + regex, no LLM, and **report bucket sizes
before any repair**. The buckets are not equally worth fixing, and treating
them uniformly is how this becomes a 1,500-item rewrite it does not need to be.

- **A — orphan + bibliography-stub shape.** Not claims; they should lose the
  tag, not be repaired into claims.
- **B — orphan but genuinely claim-shaped.** Needs grounding (chase) or
  deletion.
- **C — near-duplicate clusters.** 3 identical pairs, 19 pairs > 0.75.
- **D — notation-dirty under v2.** Mechanical.
- **E — grammar-violating but substantively sound.** The large bucket.
- **F — clean.** Leave alone.

### Measured 2026-08-19, and what it falsifies

Hub definition that reproduces the audit (a first attempt got 1,710 by
row-multiplying the tag join, and 0 empty scopes by filtering on `meta ?
'scope'` — neither is real): `refs.kind='finding'`, `deleted_at IS NULL`,
joined to `tags` on `namespace='TAPROOT' AND value='claim'` with
`ref_tags.expires_at` null-or-future. Edges live in **`links`**
(`src_ref_id`/`dst_ref_id`) — there is no `ref_edges` table.

| | count |
|---|---|
| Live hubs (after the 191259 merge) | 1,524 |
| Orphans (zero inbound links) | 234 |
| A — orphan **and** stub-shaped by regex | 68 |
| B — orphan, not stub-shaped by regex | 166 |
| D — notation-dirty under canon v2 | 41 |
| Title > 250 chars | 152 |
| No terminal period | 330 |
| No evidence verb (incl. past tense) | 1,222 (80%) |
| Grounded but stub-shaped | 60 |

Three corrections to earlier numbers:

1. **"Expect most of the 234" in bucket A was wrong** — only 68 match, and the
   easy mechanical win is a third the size assumed.
2. **The 96%-no-verb figure was an artefact of the audit regex**, which
   matched only present-tense verbs. Including *found/showed/measured/
   observed/demonstrated* gives **80%**. Still the dominant defect, but
   overstated by 16 points; the corpus is not as verbless as it looked.
3. **Bucket B is not "genuinely claim-shaped".** Sampling 12 shows most are
   *also* non-claims that the bucket-A regex simply failed to catch —
   colon-label style with no finite verb (`EDRR chloride-medium
   electrohydrometallurgy: Au recovery from refractory telluride ore`;
   `MOF/PPy + MnO2 hybrid CDI defluorination: 55.12 mg-F/g at 1.2 V`), topic
   labels (`PCR cartridge design with planar laminated card`), and
   **single-author** bibliography entries (`Dennard 1974: constant-field
   scaling rules for MOSFETs`) that the `Name & Name YYYY` pattern misses.
   Only a minority are real claims (`SWRO brine spreads up to 5 km along
   seabed; impairs benthic ecosystems`).

**Consequence:** the A/B split is not a stub-vs-claim boundary, it is an
artefact of one regex's coverage. Do not drive the untag pass off it. The
classifier must be `lint_claim_sentence` (Phase 1) applied to every hub, with
the A/B SQL split kept only as a cross-check on its recall. The stub
population is materially larger than 68 and the real-claim orphan population
materially smaller than 166.

### Lint triage over all 1,524 live hubs (canon v2)

Method: `COPY` the cohort to CSV, run `lint_notation` + `lint_claim_sentence` +
`lint_scope` locally over the dump. Pure string work, so it runs against
unshipped worktree code with no DB session — this is the repeatable
measurement loop, and it is what `precis taproot lint` wraps.

**21 of 1,524 hubs (1.4%) are lint-clean.** That is the headline: this is not
a corpus with a quality tail, it is a corpus with a quality *floor*.

| bucket | count | |
|---|---|---|
| E — grammar-violating, grounded | 1,044 | 68.5% |
| D — notation-dirty | 203 | 13.3% |
| A — stub, orphan | 117 | 7.7% |
| B — orphan, claim-shaped | 117 | 7.7% |
| A2 — stub, but grounded | 22 | 1.4% |
| F — clean | 21 | 1.4% |

A+B = 234, reconciling exactly with the SQL orphan count — the two independent
methods agree on corpus size and orphan population.

| sentence code | n | | notation code | n | | scope code | n |
|---|---|---|---|---|---|---|---|
| no-epistemic-mode | 1,419 | | two-denominator-solidus | 142 | | scope-empty | 1,130 |
| no-evidence-verb | 1,232 | | ascii-minus-exponent | 59 | | scope-unknown-key | 191 |
| no-terminal-period | 330 | | ascii-angstrom | 17 | | scope-free-text | 156 |
| author-name | 157 | | ascii-micro | 12 | | | |
| over-long | 152 | | ascii-ohm | 12 | | | |
| not-falsifiable | 138 | | ascii-micrometre | 10 | | | |
| multi-assertion | 33 | | ascii-degrees | 7 | | | |
| dangling-reference | 1 | | e-notation / plusminus | 4 / 4 | | | |

Two things this measurement bought that the SQL pass could not:

- **`two-denominator-solidus` at 142 is the largest notation defect**, and the
  earlier SQL sweep missed it entirely (41 "notation-dirty") because it tested
  only for ASCII spellings. It is also the one notation class that must **not**
  be auto-fixed — deciding which factors move under a negative exponent is
  judgment, not transcription.
- **`ascii-angstrom` (17), `ascii-ohm` (12), `ascii-micrometre` (10),
  `ascii-degrees` (7)** are pure canon-v2 yield: rules that did not exist
  during the first normalization pass, so those hubs could not have been
  cleaned then however careful the agent.

### Classifier recall — validated, then tightened

Probing the linter against hand-classified titles confirmed it flags
colon-label (`EDRR …: Au recovery …`), author-year (`Dennard 1974: …`),
copula-definition (`NUPACK is a software suite …`) and study-happened
(`… were investigated`) shapes, and correctly leaves real claims alone
(`DFT predicts …` lints clean; `SWRO brine spreads up to 5 km …` is not
flagged non-falsifiable).

It missed three, all genuine corpus titles:
`MOF/PPy + MnO2 hybrid CDI defluorination: 55.12 mg-F/g at 1.2 V` (colon-label
with a data right-hand side), `PCR cartridge design with planar laminated card`
(topic label, no structural marker), and `Landauer 1957/1970 - conductance as
transmission` (ASCII hyphen, not em-dash).

**All three share one signal: no finite verb.** An assertion needs a verb, so
verblessness is the general form of the rule that the label/marker patterns
were only approximating. Folded into `not-falsifiable`, with the explicit
bias that **wrongly flagging a real claim is the expensive error** — the untag
pass acts on this code, so it must prefer false negatives.

## Phase 3 — repair, cheapest first

**The cost argument: do not rewrite ~1,400 sentences.** Corpus-wide LLM
rewriting is the expensive path and the one most likely to introduce
assertion drift. Instead: run the mechanical passes corpus-wide, and let the
Phase-1 approve-time block handle bucket E **on demand** — a hub gets its
grammar rewritten when someone actually wants to publish it. Most hubs will
never be published, and rewriting them speculatively buys nothing.

1. **D (notation)** — mechanical, small model, no judgment. Re-run the
   proven 3-slice partition with canon v2 over the *whole* corpus, not just
   previously-detected hubs; v2 adds rules the first pass could not apply.
2. **A (untag)** — mechanical once the stub pattern is confirmed by sampling.
   Reversible: remove the `TAPROOT:claim` tag, do not delete the ref.
3. **C (dedup/merge)** — judgment, but see "Why dedup never fired" below:
   the cascade already exists and the fix is wiring, not construction. Merge
   still means picking a survivor, moving evidence edges, and aliasing the
   retired `pub_id`. Pilot on the identical pairs.
4. **B (orphans that are real claims)** — chase for sources or park as
   hanging.
5. **E** — on demand at approve, per the cost argument.

Every mutating pass carries the round-trip assertion from
`hub-title-200-truncation-via-stale-mcp.md`: persisted title must equal the
intended sentence, or fail loudly.

### Ordering constraint: repair before re-approval, not after

Once a hub is re-approved its `claim_sha` is frozen and gate #14
(`nanopub/gates.py::check_drift`) goes live for it. Any repair pass that then
touches `refs.title` re-triggers drift and forces another reopen — which is
precisely how 77 of 139 rows got stuck in the first place (the regrounding
pass rewrote titles under approvals that had already frozen). So Phase 3 runs
to completion while the whole corpus is `candidate`, and re-approval starts
only afterwards. Approving early is not merely wasteful, it recreates the
original defect.

### Phase 3.1 dry run, 2026-08-19 — one blocker found before any write

`normalize_notation` run over all 1,524 hubs from the prod dump (pure string
work, no DB). 474 hubs (31.1%) would change. Two checks fired:

**`ascii-minus-exponent` is unusable as written — 146 wrong, 3 right.** It
rewrites nomenclature hyphens as negative exponents, destroying compound
names: `Fe-ZSM-5`→`Fe-ZSM⁻⁵`, `MOF-74`→`MOF⁻⁷⁴`, `UiO-66`→`UiO⁻⁶⁶`,
`MIL-101(Fe)`, `PCN-222-MBA`, `TJU-21`, `HKUST-1`, `VOTCPP-PIF-1`, and even
the non-chemical `sub-10-nm`→`sub⁻¹⁰-nm`. A real negative exponent follows a
**unit symbol** (`s-1`, `cm-2`, `M-1`); nomenclature follows a series name.
Fix is to gate both the rewriter and the detector on the existing
`_ACCEPTED_DENOMINATORS` allowlist, requiring a standalone unit token so
`ZSM-5` cannot match on its trailing `M`. **The detector shares the defect**,
so the ~108 hubs this code contributed to bucket D above are mis-bucketed;
those counts need re-deriving after the fix.

This is the argument for dry-running every mechanical pass against the dump
before it touches prod. The rule looked correct, passed its unit tests, and
would have silently corrupted the material names in ~108 hubs.

**The 200-char canary is clean.** 7 hubs have a title of exactly 200
characters, but `length(title) = length(finding_body)` for all 7 and none is
severed mid-word — genuine sentences that happen to land on 200, not
truncation residue. The repair in
`hub-title-200-truncation-via-stale-mcp.md` held.

**One new duplicate surfaced, as predicted.** Adding terminal periods makes
fi191179 and fi191260 byte-identical ("Carbon NanoBud material has been
engineered into printed touch sensors."). This is the 191259/191268 pattern
again: punctuation was hiding a real duplicate from `pub_id`. Confirms that
the duplicate rescan must run *after* normalization, not before.

Minor gap: `plus/minus 2 V` appears spelled out and `ascii-plusminus` does
not catch it (only 4 hits corpus-wide, which is suspiciously low).

### Phase 3.1 RE-RUN, 2026-08-20 — the exponent fix held; four more rules fail the same way

Re-ran `normalize_notation` over all 1,524 live hubs from a fresh prod dump,
now that the `_ACCEPTED_DENOMINATORS` gate has landed (`2480c172`). Pure
string work, no DB session.

**The `ascii-minus-exponent` fix is confirmed.** It now fires **once**
corpus-wide, down from 149 fires of which 146 were wrong. No `Fe-ZSM-5`
class rewrite survives. Totals: **466 hubs (30.6%) would change**, 63 of
them shortening (so the `len(out) >= len(in)` assertion remains a bug, as
the convention doc says), and the pass is **idempotent** — 0 hubs change on
a second application.

**But the same defect shape is present in four more rules, and it was
masked by the exponent rule's noise.** Every ASCII→UTF-8 unit-symbol
rewrite in the canon v2 table converts a *unit name* to a *unit symbol*
without checking that a numeral precedes it. SI is explicit that symbols
are used with numerical values and names with words, so each of these
fires on adjectival and spelled-out prose where the name is correct:

| rule | fires | right | wrong | the wrong shape |
|---|---|---|---|---|
| `ascii-plusminus` | 4 | 2 | **2** | ionic charge + hyphenated adjective: `Zn2+-sensing`→`Zn2±sensing` (fi176493), `Ni2+-binding`→`Ni2±binding` (fi177426) |
| `ascii-micrometre` | 10 | 5 | **5** | `micron-scale`→`µm-scale` (fi34754, fi178208), `micrometre-dimension`→`µm-dimension` (fi176821), `nanometer to micrometer scale`→`nanometer to µm scale` (fi176900), `ten micrometres`→`ten µm` (fi218294) |
| `ascii-micro` | 12 | ~9 | **≥3** | `Microsecond quantum coherence times`→`µs quantum…` (fi176479 — rewrites the sentence's opening word into a symbol), `nanoseconds to microseconds`→`nanoseconds to µs` (fi176550), `at microsecond timescales` (fi176762) |
| `ascii-degrees` | 7 | 6 | **1 partial** | fi176552 `80 to 60 degrees C at 1 degree C/min` → converts the first, leaves the second: `80 to 60 °C at 1 degree C/min` — mixed forms in one sentence |

`ascii-plusminus`'s wrong cases are the most damaging: they destroy an
oxidation state. `Zn2±sensing` is not a notation variant of `Zn2+-sensing`,
it is a different (meaningless) string, and it would have been written to
2 hub titles and their `pub_id`s.

**Two shared root causes**, both one guard away:

1. **No numeral-left guard.** A unit *symbol* rewrite must require a
   numeric value immediately to its left (optionally through an SI prefix).
   `ascii-minus-exponent` got a unit-token guard; its four siblings never
   did. `ascii-plusminus` additionally needs a numeral on **both** sides —
   `±` means tolerance, and `<digit>+-<letter>` is a charge followed by a
   compound adjective.
2. **Partial application inside one sentence.** fi176552 and fi176900 come
   out with two spellings of the same unit side by side, which is worse
   than either input. A rule that rewrites some occurrences must rewrite
   all of them or none.

**FIXED the same day (canon v3.1).** All four rules now share a
`_NUMERAL_LEFT` guard — a unit *symbol* rewrite requires a numeral
immediately left of the unit name, optionally through a space or hyphen
(`1-µm colloids` is a compound adjective and is preserved).
`ascii-plusminus` additionally requires a numeral on **both** sides.
`_OHM_RE` and `_ANGSTROM_RE` deliberately do not take the guard — an SI
prefix intervenes (`40 kOhm`) and `per angstrom` is a correct
unqualified use; both were inspected at 12/12 and 17/17 correct.

One correction to the diagnosis above: `ascii-degrees`'s partial
application was **not** the missing numeral guard. `deg(?:rees)?` never
matched the *singular* — `1 degree C/min` failed on `deg` + `ree`. Now
`deg(?:rees?)?`.

**Post-fix re-run, same harness, directly comparable:** 456 hubs change
(29.9%), still idempotent, and every false-positive class is gone —
`ascii-plusminus` 4→2, `ascii-micrometre` 10→4, `ascii-micro` 12→7,
each dropped fire inspected and confirmed wrong. `formula-ascii-subscript`
now holds at 77→77 (was 77→78): normalization no longer creates a lint
hit, because the `Zn2±` corruption that created it is gone. Detector and
rewriter counts now agree exactly per rule, which is the invariant the
module docstring claims and previously did not hold.

**Step 4's auto-fix set is now shippable in full** — all 456, no
detector-only demotion needed.

**Re-derived lint counts after normalization** (the bucket-D re-derivation
the broken detector owed us). Everything the rewriter claims to fix goes to
zero. What remains is advisory-only by design:
`two-denominator-solidus` 173 (unchanged — judgment call),
`formula-ascii-subscript` 77, `tex-residue` 2, `approx-spacing` 1,
`tilde-approximation` 1.

**Normalization creates one lint hit** — `formula-ascii-subscript`
77 → 78 — and it is the `ascii-plusminus` corruption above: `Zn2+-sensing`
→ `Zn2±sensing` makes `Zn2` newly visible as an element+digits token. The
counter caught the corruption independently, which is a small argument for
keeping before/after lint deltas in the harness permanently.

### The duplicate pair the plan predicted is not the pair we have

The prediction was that adding terminal periods would make fi191179 and
fi191260 byte-identical. **Measured: they are already byte-identical** —
both 70 chars, *neither* carries a terminal period. The "differ only by a
terminal period" reading was an artifact of the earlier broken run.

Normalization creates **zero** new duplicate groups. There are exactly
**two** exact-sentence duplicate groups corpus-wide, and both are live now:

- **fi191179 / fi191260** — identical sentences; `scope.method` differs as
  `"engineered into printed"` vs `"engineered into printed touch sensors"`.
- **fi191192 / fi191262** — identical 192-char sentences; `scope.quantity`
  differs only in prose framing (`"Pt binding energies of -3.34 and -3.78 eV
  (…) and -2.12 eV binding on pristine graphene"` vs `"-3.34 and -3.78 eV
  (…), -2.12 eV binding on pristine graphene"`).

This is the **first confirmed live instance of the mechanism Decision 4
predicted**: `pub_id` is behaving exactly as designed (scope is in the
hash), and free-text scope *values* are what forked these hubs. Neither
pair encodes a real regime distinction — both are the same claim written
twice with the scope field paraphrased. It is direct evidence for the
half of Decision 4 that was kept.

Note for step 5: an exact-sentence duplicate scan must group on the
sentence hash **ignoring scope**, then adjudicate scope separately —
grouping on `pub_id` finds nothing, because scope is what is splitting
them. fi191262 was approved during the nanobud batch-3 sweep, so this
pair is inside the nanobud claim tree.

### The claim sentence is stored three times — which copies are real duplication

Asked, and worth settling permanently because the next reader will ask again.
The sentence lives in `refs.title`, the `ord=0` `finding_body` chunk,
`nanopub_publish.approved_title` (+ its `claim_sha`), the content-derived
`pub_id`, and the signed artifact's `trig_bytes`. Only the **first pair** is
duplication — same fact at the same instant, a denormalization so that list
and search surfaces need not join the chunk table per row.

The rest are **witnesses, not copies**: the same fact recorded at a different
instant and deliberately not maintained. Drift detection is *defined* as the
comparison of two independently-stored observations, so replacing
`approved_title` with a pointer to `refs.title` would not deduplicate the
data — it would make `check_drift` return `None` unconditionally, deleting the
sensor. `trig_bytes` must likewise be self-contained because it is verified by
readers who cannot query this database. And `approved_title` is not even a
copy in practice: when the 26 frozen truncated hubs were repaired, restoring
from the chunk would have been *wrong* for 21 of them, because a reviewer had
deliberately reworded the claim at approval. That content existed in exactly
one place.

What made the truncation recoverable was not redundancy as such but that the
two copies had **different writers** — `refs.title` came through the capped
handler, the chunk through `replace_body_chunk`. Copies written by one code
path buy nothing. So the copy is kept, under two conditions:

1. **Single write door.** Satisfied: `store/_refs_ops.py::replace_ref_text`
   (the generic in-place retitle) is reachable only from `handlers/quest.py`
   and `handlers/todo.py`; hubs are written solely by `taproot/hub.py::mint_hub`
   and `::refine_claim_sentence`, both transactional across all three sites.
2. **Continuous divergence check.** Was missing — nothing compared them, which
   is why the bug survived three weeks and was caught by a human noticing the
   number 200. Now a `title-body-divergence` (and `missing-body-chunk`) code in
   the `precis taproot lint` cohort sweep, reporting only: choosing which side
   is authoritative is a judgment call, and the frozen-vs-non-frozen repair
   needed *opposite* sources.

## Why dedup never fired — four separate defects

A semantic dedup cascade already exists in `taproot/canon.py`:
`dedup_judge` (cheap tier) → `merge_confirm` (BIG, only on a risky "same") →
`place()` (deterministic branching), with the verdict vocabulary
`same | different | contradicts` already in place. It did not fail. **It was
never called.**

1. **The cascade is not wired to the door agents use.** `place()` has callers
   in `apply_migrate.py`, `backfill.py`, `directed.py::directed_mint`, and
   `workers/chase.py::_taproot_bridge` — every *automated* path. The
   interactive path is `put(kind='finding', supporters=…)` →
   `handlers/finding.py::_put_claim_hub` → `taproot/authoring.py::seed_claim_hub`
   → `taproot/hub.py::mint_hub`, and it calls `place()` nowhere. `mint_hub`
   converges on **exact content identity only** (the `pub_id` hash). Two
   sentences that mean the same thing in different words sail straight
   through. This is why the "Search before you mint" rule had to be written
   as *prose in a skill* — it is a human-language workaround for a missing
   code gate, and prose gates get skipped.
2. **`scope` is free text, and mostly empty.** Across 1,527 hubs there are
   **372 distinct scope values**; **1,130 (74%) are `{}`**; the most common
   non-empty scope covers 17 hubs and nearly all the rest are singletons.
   Agents are stuffing sentence fragments into it — two of the three
   byte-identical title pairs differ *only* by prose drift inside scope
   (`"engineered into printed"` vs `"engineered into printed touch sensors"`;
   a `quantity` value rephrased). Since `pub_id = hash(sentence, scope)`,
   free-text scope manufactures spurious non-convergence: the same claim
   mints twice because someone described its scope slightly differently.
   Scope needs a controlled vocabulary (material / method / regime as short
   enumerated keys), or it should be dropped from the identity hash.
3. **Hash-only convergence is notation-sensitive.** `10^4` and `10⁴` minted
   as distinct hubs. Canon v2 largely closes this, but it is the same root
   cause as (2): identity is a string hash over fields nobody normalizes.
4. **The ANN index `block()` queries is 88% empty** (found 2026-08-19). The
   retrieval step joins `chunks` at **`ord = -1`** — the `card_combined`
   card, *not* the `finding_body` at `ord = 0`. Card coverage across the live
   cohort:

   | month | hubs | with card | |
   |---|---|---|---|
   | 2026-06 | 229 | 161 | 70.3% |
   | 2026-07 | 942 | 5 | **0.5%** |
   | 2026-08 | 353 | 21 | **5.9%** |

   **187 of 1,524.** So even wired to the right door and called at the right
   moment, `block()` would retrieve over 12% of the corpus and miss ~88% of
   candidate duplicates. This is the most mechanical of the four defects and
   has to be fixed before any dedup result can be believed — a "no duplicates
   found" verdict from the current index means nothing.

   Cause: `refine_claim_sentence` **deletes** every `ord < 0` card on rewrite
   (correct — a stale card must not keep matching the old wording) and relies
   on an async card-forge pass to re-emit. That pass is not running for hubs.
   A derived artifact was deleted and never regenerated, silently, for months.
   Note the 1,524 `finding_body` chunks are all embedded — it is specifically
   the card layer that is missing, so general semantic search still works and
   only dedup is blind. That asymmetry is why nobody noticed.

   **RESOLVED 2026-08-19 — and the cause was worse than "a pass stopped
   running": there was never a pass.** `Store.blocks.upsert_card_combined`
   has exactly one caller, `NumericRefHandler._create`, behind the
   `emits_card` class flag — which `FindingHandler` does not set. `mint_hub`
   writes the ref and the `ord=0` body chunk and nothing else. So no path,
   agent-facing or system, ever wrote a hub's `card_combined`; the "async
   card-forge path" asserted in `hub.py`'s docstrings and in
   `precis-taproot-help` never existed. The 187 hubs that *do* carry a card
   got it from `workers/chase.py::_snapshot_chain`, which fires at chain
   termination for `STATUS:tracing` findings — so that text is a chain
   snapshot, not the claim sentence. The index was both 88% empty and
   off-content where populated.

   Fix: `block()` now retrieves over the `ord=0` `finding_body` chunk
   (`+ ce.status = 'ok'`). No backfill, no new worker, no derived copy —
   coverage went 12.3% → **100%** (1,524/1,524, all `status='ok'`, verified
   against prod) the moment the join changed. It also closes the sync hazard
   rather than managing it: `refine_claim_sentence` already DELETE+INSERTs
   the body chunk, so the embed cascade re-runs and the dedup index tracks
   every reword by construction. A hub's `card_combined` would only ever
   have been a verbatim second copy of the sentence whose one distinguishing
   property was that it could drift.

   Standing invariant added: `health_digest.py::_check_claim_hub_dedup_index`
   counts live hubs with no embedded body chunk and reports coverage, so a
   future shortfall surfaces instead of being found by accident. Regression
   test: `test_block_finds_a_minted_hub_that_has_no_card_combined`.

**Design rule this establishes: derived is right, fire-and-forget is not.**
Both this and the title/body divergence
(`hub-title-body-chunk-divergence.md`) are the same failure: a value that
should have exactly one author acquired two, or acquired zero. Anything
derived needs synchronous regeneration in the same transaction, or a
queue-backed pass **with a coverage invariant that alarms**. A 0.5% coverage
month should have paged, not waited to be found by accident.

Applied to the sentence itself (Reto's framing, and it is the right one):
**one authored sentence, reassembly on export.** `refs.title` is the sole
authored copy; the `ord=0` chunk row survives — it anchors `chunk_id`, which
`chunk_embeddings` FKs, and all 1,524 hubs have an embedding there — but its
*text* becomes a verbatim derivation of `refs.title`, never independently
authored. Nothing longer is stored: the published nanopub and the draft
renderings are assembled at export from the sentence plus its evidence links.

The export path already works this way, which settles it:
`nanopub/evidence.py::HubBundle.sentence` is `hub_ref.title`, so publication,
signing and every gate read the title and **never read the body chunk**. The
chunk's only real job is to be embedder input. The short paraphrase currently
sitting in it is a third artifact serving nothing.

Divergence then stops being a defect to detect and becomes a state that
cannot be represented; `title-body-divergence` degrades from a permanent
check to a migration-era canary.

**Consequence for sequencing: normalization can create new duplicates.**
Collapsing `10^4` → `10⁴` makes two previously-distinct titles identical
*without* collapsing their `pub_id`s. So the dedup pass must run **after**
notation normalization, over the normalized text, and Phase 3.1 must be
followed by a fresh duplicate scan rather than reusing the audit's pair list.

**Forensic item — resolved, and `pub_id` is trustworthy.** Hubs 191259 and
191268 appeared to have identical title *and* scope but different `pub_id`,
which should be impossible for a deterministic hash and would have meant a
post-mint mutation. Explained: the titles differ by exactly one **trailing
period** (`…applications.` vs `…applications`). `normalize_text_for_hash`
folds NFKD, lowercases, and collapses whitespace, but does **not** normalize
punctuation — so the hashes diverged legitimately. No mutation occurred and
`pub_id` remains reliable as identity. 191259 was the survivor-loser and is
soft-deleted; its one inbound link came from a different chunk than 191268's,
so it was repointed rather than dropped, preserving unique grounding.

The general lesson stands and has now recurred twice: **punctuation hides
duplicates from `pub_id`.** Adding terminal periods in Phase 3.1 collapses
fi191179/fi191260 into a byte-identical pair the same way.

## Phase 4 — adversarial review and adjudication

**2 `contradicts` and 4 `refines` across 1,527 hubs** drawn from overlapping
literature. That is not a corpus without disagreement; it is a corpus where
nobody looks for it. Every pass is additive — each mints, none challenges.

The mechanism is nameable: **`contradicts` is under-filed because it is
punitive.** A live contradicts edge makes the other hub unpublishable, so an
agent that suspects a conflict chooses between filing nothing and detonating
someone's claim. There is no middle path, so you get 2 out of 1,527. The gate
design suppresses exactly the signal we want.

Therefore adjudication ships *with* adversarial review, not after it. Proposal:
the reviewer emits one of five verdicts, and only the last files `contradicts`:

- `same-claim` → attach evidence to the existing hub, retire the duplicate
- `refines` → typed `refines` edge
- `scope-mismatch` → different functional / cell size / measurement regime;
  annotate scope on both, no edge. **This is the expected majority.**
- `unit-error` → one side is arithmetically wrong; retract it
- `genuine-conflict` → `contradicts`, plus a hunt for a third adjudicating source

`precis-adversarial-reviewer` already exists but is a **paper-draft** persona
(`applies-to: paper review via scripts/review-paper/run.sh`). Its categories
(`unsupported-claim`, `overgeneralisation`, `internal-inconsistency`) transfer
almost directly to hub review — adapt it, do not write a new one.

Run it first over the dense topic neighbourhoods where near-duplicates
clustered (MOF conduction, DNA bricks, molecular switches) — conflicts hide
where coverage is thickest.

Two findings already in hand that no automated gate caught, as seed cases: a
possible genuine contradiction (fi191120 vs fi218681) and pa1992's GPa/TPa
unit error, off by ~10³.

## Phase 5 — the frozen cohort

138 `reviewed` rows, of which 77 are drifted (`refs.title != approved_title`)
and 5 carry pre-existing 200-char truncation (fi176566, fi176758, fi177385,
fi211522, fi176896 at 217). Under the reframing at the top, the clean answer
is **reset them to candidate and re-approve under canon v2** rather than
backfilling titles to satisfy a gate whose premise (a published artifact to
protect) does not yet hold. Re-approval re-runs every gate against the
corrected standard, which is what we want anyway.

The 1 `signed` hub needs its own decision — nothing was published, so
retract-and-redo is available and probably simplest.

## Workflow change

Extraction agents currently mint freely and discover problems at approve. The
order should be: **admissibility test → mint → notation autofix → dedup
check → park for review**. The admissibility test at the front is what stops
the next 234 bibliography stubs from entering.

## Standard / definition of done

- A ref carries `TAPROOT:claim` only if it passes the four admissibility tests.
- Every claim hub has >=1 paper edge with `pdf_sha256`, or is explicitly
  tagged hanging.
- No two hubs above the similarity threshold without an explicit typed edge
  between them.
- Notation lint clean; sentence lint clean at approve.

## Decisions taken (Reto, 2026-08-19)

1. **Reset, don't backfill.** All 138 `reviewed` rows flip to `candidate`;
   re-approval happens later under canon v2. This dissolves the 77 drift rows
   and the 5 pre-existing truncations rather than papering over them — the
   drift signal was false, since the gate protects a published artifact that
   does not exist.
2. **The 1 `signed` hub is unsigned** — same reopen path. `nanopub_reopen`
   already accepts `signed`, discards the frozen fields, and leaves the
   append-only artifact row in place; nothing was anchored, so nothing is
   irreversible.
3. **Similarity threshold is split.** ≥0.90 **mandates** a typed edge between
   the two hubs (`same-claim` / `refines` / `scope-mismatch` / `contradicts`);
   0.75–0.90 raises an **advisory** for review. One threshold could not serve
   both jobs: 0.75 is where the audit's tail starts and is too noisy to block
   on, 0.9 is where pairs are unambiguously the same claim.
4. **`scope` stays in the identity hash, with a controlled vocabulary.**
   Free-text *values* are a lint violation. Keeping scope preserves "same
   sentence, different regime" as two distinct claims, which dropping it
   would destroy.
   **Superseded 2026-08-19 on the key half.** The enumerated seven
   (`material`, `method`, `regime`, `system`, `quantity`, `substrate`,
   `temperature`) were invented without consulting usage: `catalyst` (52
   hubs) outranks four of them, and 191 hubs lint `scope-unknown-key`
   mostly for sensible domain keys the spec failed to anticipate. Keys move
   to a frequency-ordered registry — `scope-key-vocabulary-registry.md`.
   The *value* ruling is unchanged and is the half that actually protects
   dedup.
5. **`place()` is NOT wired into `seed_claim_hub`** — explicitly deferred.
   Dedup therefore ships as an **offline pass** over the corpus, not as a
   change to the interactive `put(kind='finding')` door. The prose gate in
   `precis-taproot-mint-help` remains the only mint-time dedup control, with its
   known weakness (see "Why dedup never fired" above) accepted for now.
6. **One of the 191259/191268 pair is deleted** rather than merged — the
   forensic anomaly is most likely a truncation artefact, so the pair is one
   claim, not two. Survivor is chosen on evidence-edge count, then earliest
   creation, then untruncated title; the retired hub's evidence edges move to
   the survivor first.

## Still open

- The `scope` backfill itself (372 values) now depends on
  `scope-key-vocabulary-registry.md`, which specifies the frequency-ordered
  key registry that replaces the enumerated seven. Values-to-shortform is
  still unscheduled; keys are blocked on that spec landing.
- Re-approval of the reset cohort under canon v2 — deliberately later.
