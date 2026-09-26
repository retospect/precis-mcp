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
| 3 — title/body cohort repair | **DONE — all 45.** 42 on 2026-08-21, the final 3 on 2026-08-22 after adjudicating each against its corroborating paper (all already in-corpus; two titles were amended first — fi190976 gained the 145 ppm H₂O condition, fi191134 `DFT`→`DFTB`). **Strict divergence is now 0 of 1,254.** All 45 also re-derived `pub_id` from the repaired sentence — old ids kept as aliases — so step 5 is already done for them. Awaiting re-embedding. Detail: `hub-title-body-chunk-divergence.md`. Originally scoped as, and *smaller* than, both earlier estimates: **45**, not 297 — the 297 was measured on `TAPROOT:claim` alone, which includes ~280 chase-tree findings that never mint. Strict (`+ STATUS:canonical`, n=1,249): 45 diverged, 42 title-longer, 3 body-longer, all August, **zero June**. So it is one cohort in one direction (chunk ← title) and the opposite-repair hazard does not apply to hubs. **0** are missing the `ord=0` chunk. Detail + the 3 to eyeball: `hub-title-body-chunk-divergence.md`. |
| 4 — notation normalization | **dry-run clean 2026-08-20, ready to apply.** 456 of 1,524 hubs (29.9%) change, idempotent, zero known-wrong fires. ⚠ **that 1,524 is the contaminated predicate — re-run the dry run strict before applying**, or the "456 change" figure counts chase-tree rows the mint never sees. Spot-measured strict 2026-08-21 over titles (n=1,249): 5 unspaced `°C` (12 already spaced), 11 ASCII `kohm`, 4 ASCII `±`, 17 spelled-out `Angstrom`, 4 `micron`, **0** wrongly-spaced angles — so the per-rule residual on real hubs is tens, not hundreds. (An earlier `u[mM]\b` count of 4 was a false positive: it matches `vacuum`/`spectrum`; digit-anchored it is 0. Anchor notation regexes on a numeral.) Canon v3.1 fixed four ASCII→UTF-8 rules that lacked a numeral-left guard — worst case, `ascii-plusminus` was corrupting oxidation states (`Zn2+-sensing`→`Zn2±sensing`); all four now share `taproot/notation.py::_NUMERAL_LEFT`. Not yet applied to prod. |
| 5 — `pub_id` re-hash + fresh duplicate scan | not started, and **re-scoped**: normalization creates zero new duplicates. Exactly two exact-sentence duplicate groups exist corpus-wide (fi191179/fi191260, fi191192/fi191262), both already live, both forked by free-text `scope` values rather than punctuation. A scan grouped on `pub_id` finds neither — group on the sentence hash *ignoring* scope, then adjudicate scope separately. **Durable fix, not yet done:** route `src/precis/identity.py::make_taproot_hub_paper_id`'s sentence input through `taproot/migrate.py::_normalize_number_text` (already used by reground) before hashing — `normalize_text_for_hash` folds NFKD/case/whitespace but not notation, so `10^4` and `10⁴` still mint distinct hubs. Re-hashes every existing `pub_id`; same alias-path caveat as the rest of this step (Phase 3 item 3 aliases the retired id on merge). |
| blocker — dedup-index coverage | **RESOLVED 2026-08-19.** Was 187 of 1,524 (12.3%) — no code path had ever written a hub's `card_combined`. `block()` now retrieves over the `ord=0` `finding_body` chunk instead (`taproot/canon.py::block`, which carries the full root-cause writeup), which all 1,524 hubs have and all 1,524 have embedded, so coverage is 100% with no backfill. Dedup verdicts are believable from here on; step 5's duplicate scan is unblocked. |

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
- New `lint_claim_sentence`: heuristic codes for the admissibility and
  grammar rules — `not-falsifiable` (label style, `^[A-Z][a-z]+ (19|20)\d\d`,
  copula-definition, "was/were investigated"), `dangling-reference`,
  `no-evidence-verb`, `no-epistemic-mode`, `multi-assertion`, `no-terminal-period`.
- A `precis taproot lint` CLI that runs both over an arbitrary cohort and
  emits per-code counts — this is the measurement instrument for every phase
  below, and the natural backing for the `taproot-health` view proposed in
  `mcp-aggregate-surface-gaps.md`.
- **Enforcement asymmetry, current statement:** `precis-taproot-mint-help`'s
  "Enforcement asymmetry" paragraph (`nanopub/gates.py::_BLOCKING_LINT_CODES`
  — ten sentence codes plus every deterministically-fixable notation code
  block at approve; judgment-only codes stay advisory even there).

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
'scope'` — neither is real): `refs.kind='finding'`, `retired_at IS NULL`,
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

**Re-measured 2026-08-23, strict predicate (n=1,267): 83 clean (6.6%).** Two
lexicon growths that day (`sentence_lint.py`: spelled-out technique names +
new tokens, seven new evidence verbs, then generic way-of-knowing head nouns
— dated comments in the module carry the dry-run verdicts) account for the
move; `no-epistemic-mode` 1,052, `no-evidence-verb` 903, and 788 hubs are
blocked *solely* by that pair. A 50-hub Opus rewrite pilot over the
pair-blocked pool (grounding passages supplied, invent-nothing rule): 27
rewrote honestly, 23 had to SKIP — capability/definitional/historical claims,
or evidence naming no method — so scaling the rewrite pass graduates roughly
half the pool and the other half needs claim-type-aware judgment
(`taproot-claim-type-v2.md`), not grammar.

**Two tranches later that day the strict cohort stands at 299 clean (23.6%)**
— 215 Opus rewrites applied through the `edit(kind='finding', title=…)`
reword door, pair-blocked pool 963 → 577. Two corrections the tranches
forced, both worth carrying forward:

- **`multi-assertion` was over-firing on serial commas.** `A, B, and C` is
  one assertion, but the detector split on the Oxford comma and counted the
  stranded subject and verb as two clauses (gr245400). Fixed in
  `sentence_lint.py::_clause_fragments`; corpus dry run flipped 8 sentences,
  every one a genuine noun/adjective/number list, with true coordinations
  and `; ` joins untouched.
- **Evidence windows must reach the methods section.** Chunk-level
  `establishes`/`corroborates` links point at *result* paragraphs, which
  routinely state the finding without naming the technique — so a rewriter
  given only those correctly refuses to name one, and the claim loses
  specificity it had (`fi218658`/`fi218661` lost "density functional
  theory" to a bare "Calculations" this way, since the paper declares DFT in
  its methods chunk, `ord=12`). Pull the paper's method-bearing chunks
  alongside the grounding passage.

**Tranche 3 acted on both and reached 411 of 1,280 clean (32.1%)** — 111 more
rewrites, pair-blocked pool 578 → 469, 328 applied in all. The widened window
did what it promised (rewrite rate 47% → 55.5%, and the specificity loss did
not recur), but it introduced a failure the gate cannot see and the next
tranche must plan around:

- **A methods window buys yield and costs precision.** The method block offers
  every instrument the paper used, not the one that produced *this* result,
  and a rewriter will attach the nearest plausible name: `fi176401` said "AFM
  shows" where the evidence plainly says the rotation was recorded by optical
  profilometry. Both sentences are gate-clean, so no lint distinguishes them.
  Rank the sources **original technique → evidence passage → method block**,
  and draw the method block only from the best-tier source paper
  (`establishes` > `corroborates` > `cites`) — a paper that merely cites a
  hub has methods that belong to different work entirely.
- **Budget an attribution audit per tranche.** Re-showing accepted rewrites
  with their evidence and asking only "does this technique match what produced
  this observation?" found 5 corrections and 1 SKIP in 57 — ~11%, all
  invisible to the lint. A half-wave potential credited to NMR, an SEM present
  in no source, DFT credited for a result it only seeded with parameters.

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
| no-epistemic-mode | 1,419 | | two-denominator-solidus | 173 | | scope-empty | 1,130 |
| no-evidence-verb | 1,232 | | ascii-minus-exponent | 59 | | scope-unknown-key | 191 |
| no-terminal-period | 330 | | ascii-angstrom | 17 | | scope-free-text | 156 |
| author-name | 157 | | ascii-micro | 12 | | | |
| over-long | 152 | | ascii-ohm | 12 | | | |
| not-falsifiable | 138 | | ascii-micrometre | 10 | | | |
| multi-assertion | 33 | | ascii-degrees | 7 | | | |
| dangling-reference | 1 | | e-notation / plusminus | 4 / 4 | | | |

(`two-denominator-solidus` corrected from an initial 142 to 173 once the
shared `_ACCEPTED_DENOMINATORS` allowlist landed — the same gate that fixed
`ascii-minus-exponent`'s nomenclature-hyphen false positives, e.g.
`Fe-ZSM-5`→`Fe-ZSM⁻⁵`, `taproot/notation.py::_ACCEPTED_DENOMINATORS`.)

Two things this measurement bought that the SQL pass could not:

- **`two-denominator-solidus` is the largest notation defect**, and the
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

1. **D (notation)** — mechanical, small model, no judgment. Apply canon v3.1
   (position table step 4, dry-run clean) over the *whole* corpus, not just
   previously-detected hubs.
2. **A (untag)** — mechanical once the stub pattern is confirmed by sampling.
   Reversible: remove the `TAPROOT:claim` tag, do not delete the ref.
3. **C (dedup/merge)** — judgment, but the fix is wiring, not construction:
   `taproot/canon.py::place()` (`dedup_judge` → `merge_confirm` → `place`)
   already exists, with callers in every *automated* path
   (`apply_migrate.py`, `backfill.py`, `directed.py::directed_mint`,
   `workers/chase.py::_taproot_bridge`) but not the interactive
   `put(kind='finding')` → `taproot/hub.py::mint_hub` door, which converges
   on exact `pub_id` identity only. Merge means picking a survivor, moving
   evidence edges, and aliasing the retired `pub_id`. Pilot on the identical
   pairs.
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

## Dedup design: one authored sentence, reassembly on export

`refs.title`-vs-`ord=0`-chunk divergence — which of the sentence's several
storage sites is real duplication vs. an independent witness drift
detection needs — is owned by `hub-title-body-chunk-divergence.md`.
`refs.title` is the sole authored copy of a claim sentence. The `ord=0`
chunk survives — it anchors `chunk_id`, which `chunk_embeddings` FKs — but
its *text* is a verbatim derivation of `refs.title`, never independently
authored: `nanopub/evidence.py::HubBundle.sentence` reads `hub_ref.title`,
and publication, signing and every gate read the title, never the body
chunk. Divergence between the two therefore stops being a defect to detect
and becomes a state that cannot be represented; `title-body-divergence`
degrades from a permanent check to a migration-era canary. Full
root-cause writeup for why the semantic dedup index (`taproot/canon.py::block`)
missed 88% of the corpus for months: the function's own docstring.

`pub_id` is trustworthy: `normalize_text_for_hash` folds NFKD/case/whitespace
but not punctuation, so two titles differing only by a trailing period hash
to different `pub_id`s legitimately (the 191259/191268 forensic item —
resolved, no mutation occurred). **Punctuation hides duplicates from
`pub_id`** — recurred twice, is why the duplicate rescan (step 5) must run
after notation normalization, not before.

## Phase 4 — adversarial review and adjudication

Design, census and the five-verdict adjudication taxonomy moved to
`docs/backlog/disputes-edge-nonblocking-disagreement.md` — canonical home.

## Phase 5 — the frozen cohort

Executed as step 1 of the position table: reset, not backfill (Decisions
#1–2 below) — every `reviewed`/`signed` row reopens to `candidate` and
re-approves later under canon v3.1, rather than repairing titles to satisfy
a gate that was protecting an artifact nothing had published yet.

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

1. **Reset, don't backfill.** All 138 `reviewed` rows flip to `candidate`
   (executed — position table step 1); re-approval happens later under
   canon v3.1. This dissolves the 77 drift rows and the 5 pre-existing
   truncations rather than papering over them — the drift signal was
   false, since the gate protects a published artifact that does not
   exist.
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
   `precis-taproot-mint-help` remains the only mint-time dedup control
   (Phase 3 item 3's wiring gap accepted for now).
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
- Re-approval of the reset cohort under canon v3.1 — deliberately later.

---

# Absorbed 2026-09-26

## The claim-review mechanism

_Grouped 2026-09-26; was `claim-review-mechanism`, status draft._

Procedure for turning fi claim hubs into sign-off-ready nanopubs,
mechanically and repeatably — written down so it doesn't require an Opus
session driving it. Target state (Reto, 2026-08-20): automatic claim
management producing consistent, easy-to-sign-off results. **approve / sign
/ signoff / publish --live are human-only doors** — nothing below crosses
them.

### The invariant

Derive, never duplicate — every expensive incident here traces to a second
copy of a fact allowed to drift from the first. `block()` retrieved over
`card_combined`, a chunk kind nothing writes — 88% of hubs invisible to
dedup; fixed by retrieving over `finding_body`, which already exists and
re-embeds on every reword. `nanopub_publish.approved_title` vs `refs.title`
is *deliberately* a second copy — that pair is the drift sensor;
collapsing it deletes the sensor. Before building a pass, ask what already
holds the fact.

### Landed

- **Dry-run-first discipline** for every mechanical pass — harness reports
  fires/before-after/wrong:right ratio/idempotence/lint-delta;
  `docs/conventions/corpus-normalization.md` owns the harness and the
  pattern-vs-role lesson (three corpus-corrupting rules caught only by it:
  `ascii-minus-exponent`, `ascii-plusminus`, `ascii-micrometre`).
- **Duplicate scan**, scoped correctly (sentence-hash *ignoring* `scope`,
  scope adjudicated separately — grouping on `pub_id` finds nothing since
  `scope` is inside the hash): exactly two exact-sentence-hash duplicate
  groups corpus-wide (fi191179/fi191260, fi191192/fi191262), both forked
  by paraphrased `scope` values, neither a real regime distinction.
  Normalization creates zero new ones. (Denominator: sentence-hash
  groups — distinct from the byte-identical-*title* count in
  `scope-key-vocabulary-registry.md`.)
- **`contradicts` recount** with the `block()` fix in: 6 rows total, 0
  hub↔hub — full census in `disputes-edge-nonblocking-disagreement.md`.
- **Independent-supporter count** (`view='evidence'`, union-find over
  shared authors) — read-side, no schema change.
- **Scope-value lint** (`sentence_lint.py::lint_scope`) wired into both
  mint paths (MCP + CLI); previously computed, surfaced nowhere.
- **Skill sync** — `precis-taproot-mint-help`, `precis-nanopub-help`,
  `precis-notation-canon` (dropped the unguarded v3.0 ASCII fallback
  table).

### Reto's ruling 2026-08-20 — the source is the authority, the text is adjustable

The draft is machine-written and carries no authority of its own. **Truth flows
source → claim → draft**, and both the claim sentence and the draft prose may be
rewritten to match what the underlying passage actually supports. A
claim-versus-passage mismatch is therefore normally a **repair**, not a
retraction: adjust the sentence to what the source says.

**Reality is the arbiter; our wishes and assumptions do very little.** The
draft's early assertions carry no weight. When claim and text disagree, refine
rapidly along the whole stack — source, claim, and prose — and let the assertion
die if that is where the evidence lands. Deleting a paragraph is a legitimate,
expected outcome.

Three limits, which are the difference between repair and drift:

1. **Do not hunt for the one source that props up an early assumption.** This is
   the dangerous failure mode, and a ~10× corpus makes it easy: search hard
   enough and *something* supports almost any proposition. The order is read
   what the literature says → write the claim that follows → then see whether
   the draft's assertion survives. Never: keep the assertion, hunt for support.
   A claim that had to be searched for is weaker evidence than one that fell out
   of the reading.
2. **Equally, do not let retrieval order set the claim.** Silently shrinking a
   claim to fit whichever passage happened to get attached first shapes the
   corpus by search accident rather than by the literature. The rule is not
   "prefer the stronger claim" nor "prefer the weaker" — it is *state what the
   best available evidence supports, in whichever direction that moves*. If the
   honest answer is weaker, weaker is correct.
3. **A mismatch has three causes and only one is "the claim is wrong"** — see
   `nanobud-grounding-audit-2026-08-20.md`. The passage can be corrupt
   (`ingest-strips-greek-glyphs.md`) and the reader can misjudge the study type.
   Both were live today, and both would have been "repaired" into errors under a
   rewrite-first reflex. Read the source before adjusting the sentence.

**Design consequence — widening is motivated retrieval by construction.**
`hub_refine` searches for evidence *supporting an existing claim*. Run alone
against a large corpus that is a confirmation engine: it will find support for
whatever the claim already says, including claims that are wrong. It must be
paired with a disconfirmation search over the same neighbourhood — the
`disputes`/`contradicts` work in
`disputes-edge-nonblocking-disagreement.md`, which is built but dark (stage 7
*Oppose* preserves a verdict and never writes the edge). Enabling widening
without opposition would systematically harden the corpus's existing errors.
Symmetric search is not a refinement of the widening plan; it is a precondition
for it being honest.

### The corpus grew ~10× — what that unlocks

Reto added roughly ten times the original papers (2026-08-20). This changes the
disposition of the weak sets rather than just their size:

- The **331 `dr42995` hubs with no grounding text** move from "probably exclude"
  to "reground first" — support that did not exist in the corpus may now.
- The **313 hubs grounded *exclusively* by Greek-strip-exposed sources** may now
  have a clean second witness, which repairs them without waiting on the ingest
  fix. (It does not retire the ingest fix — new scarred papers keep arriving —
  but it decouples the two.)
- The **HKUST-1 pair** (`fi176432`/`fi177486`) becomes regroundable against a
  source that actually measures the modulus, instead of a retract-both.
- **New claims** can be mined from the added papers and from `dr42995`'s 9,175
  uncited chunks.

This is the moment the **widening** stage earns its keep. `hub_refine` /
`chase_trigger` exist precisely to find more support for claims that already
exist, and they have been built-but-dark; a 10× corpus growth is exactly the
condition under which a re-run pays. Enabling them is now the highest-value next
move rather than a deferred item. Constraints unchanged: land `exclude_ref_ids`
first, `claim_embeddings` is EMPTY so `chase_trigger`'s first pass is a cold
build of ~1,244 vectors, run `chase_trigger` alone until its sweep drains, and
then `hub_refine` on a **single host** — the memo write is a read-modify-write on
`meta` with no CAS (`store/_refs_ops.py::update_ref`).

### Open — disputes / adversarial review

Owned by `docs/backlog/disputes-edge-nonblocking-disagreement.md`: the
five-verdict taxonomy, the `disputes`-vs-`contradicts` edge split, the
incentive-bug retraction (cause was `block()`'s `card_combined` blindness,
not agent self-censorship — do not cite the incentive story), and the
asymmetry (`"same"` at low confidence gets a second LLM confirm;
`"contradicts"` gets none at any confidence). Seed cases: fi191120 vs
fi218681; pa1992's GPa/TPa error (~10³ off). Run the dense neighbourhoods
first (MOF conduction, DNA bricks, molecular switches) — conflicts hide
where coverage is thickest.

### Open — mechanical passes not yet applied

1. **Notation apply** — 456 hubs, canon v3.1, dry-run clean, not yet
   written to prod.
2. **`pub_id` re-hash + duplicate rescan** — normalizing changes the hash
   input; stored `pub_id`s don't collapse on their own. Must follow #1.
3. **156 hubs** carry a lint-flagged `scope` value, accumulated while the lint
   was live but unsurfaced. Not mechanical — a scope value is a term choice,
   rewriting one changes `pub_id` and may collapse hubs.
4. **Near-duplicate hub sweep** — spec in `claim-hub-dedup-sweep.md`. Its
   cohort was measured over the permissive hub predicate and **must be
   re-measured** before it runs. Must precede re-approval for the same reason
   as #2: merging changes `pub_id`.
5. **Title/body reconciliation residue.** 297 hubs diverge under the permissive
   predicate, **45** under the strict one — and 43 of those 45 are inside the
   dr173020 cohort. Raw and whitespace-normalized counts are identical at every
   level, so **not one divergence is cosmetic**: every case is a wording
   difference needing a call about which text is right. This is curation, not a
   pass; do not plan it as a mechanical sweep.

**Which denominator.** `mint_hub` writes both `TAPROOT:claim` and
`STATUS:canonical`; `block()` and the nanopub overview read only the former and
so count 1,524, while `hub_refine`/`chase_trigger` require both and count 1,244.
The strict number is the real one. Every count above should say which predicate
it used — `claim-hub-definition-divergence.md` owns the fix.

**Neither the notation (456) nor the scope (156) verdict is stored anywhere** —
not on `refs.meta`, not on `nanopub_publish`. Both come from running
`precis taproot lint`, so both go stale silently and must be re-derived at use.

**Boxel document is `dr42995`** (confirmed by Reto 2026-08-20) — *"Molecular
Computing from Self-Assembling Nanoscale Cubes"*. Found by tracing the HKUST-1
groundings rather than supplied; it cites claim hubs directly, so its cohort
scopes exactly like `dr173020`'s (`links.src_chunk_id → chunks.ref_id = 42995`,
`relation='cites'`). No longer blocked — the per-document cohort pass can start.
Its first known defect: it cites both misgrounded HKUST-1 hubs, so the modulus
figures in its interface section rest on nothing.

### Open — re-approval sequencing (load-bearing order)

Acquisition-markers→tags, title/body reconciliation, notation
normalization, `pub_id` re-hash+rescan, bucket-A stub untag, adversarial
review, **re-approval last**. Approving a hub freezes `claim_sha` and arms
drift gate #14; any later pass touching `refs.title` forces a reopen. The
title/body reconciliation pass already did exactly this: **all 139
`nanopub_publish` rows are `candidate`, 0 `reviewed`/`signed`** (verified
2026-08-20 — supersedes any earlier "N of 139 stuck" count). Re-approving
early doesn't waste effort, it recreates the defect.

### Open — confidence signals

Every gate is admissibility (well-formed, sourced, traceable), not truth.
Computable, defeasible, exposed with lineage — never a scalar:
independent-supporter count (shipped), hearsay distance (already gated),
source rank (available, not wired), dispute status (needs the item
above), `testableBy` resolution (not tracked).

**Patent-assignee independence gap, unresolved.** Patents carry zero
author data in prod (0/101) — shared-author collapse never fires for
them, so a hub backed by three patents from one assignee reads as three
independent supporters. Fix by collapsing on assignee once assignee-field
coverage is checked.

### Open — external critique, page 1 only

Two `perplexity-research` entries reviewed the design 2026-08-20
(`id='critique-the-design-of-a-scientific-claim-publication-pipeli'`,
`id='critique-a-concrete-machine-readable-scientific-claim-artifa'`);
~150 KB per report past page 1 is undrained, cursors expired. Agrees with
the internal diagnosis: *"impeccably traced but epistemically flat"* — no
claim-to-claim argumentation edges (Micropublications' `supports`/
`challenges`); assertion/evidence-mode/identity conflated into one
sentence where ECO/GRADE separate them; CiTO, schema.org/Claim, PROV-O
under-used; provenance weak on paywalls/preprint-versioning/retraction.

**Reto's ruling (2026-08-20): identity and admissibility are not
over-engineered — do not simplify, stop adding.** `pub_id` over the
normalized sentence is why the notation canon exists and it correctly
held the two live duplicate pairs apart; primary-source-only is the
system's most valuable truth proxy. The one genuine fragility is `scope`
free-text inside the identity hash — the 156-hub item above, not identity
depth.

### The dr173020 worked example

Scope the cohort via `links.src_chunk_id → chunks.ref_id = 173020`
(`relation='cites'`), run the passes above over it rather than the whole
corpus. Live-count re-verified 2026-08-20: **126** distinct live findings
cited (permissive predicate), 116 with a `nanopub_publish` row, **all
`candidate`** — 0 reviewed, 0 signed, 0 published.

The cohort is a far larger share of some passes than of others, which is the
argument for running it cohort-scoped rather than waiting for corpus-wide
sweeps: title/body is **43 of 45** strict-predicate divergences (96%), scope is
123 of 394 hubs-with-a-scope (31%), and notation is a small minority — the
notation backlog is overwhelmingly *not* nanobuds. Only the second of each
known duplicate pair (fi191260, fi191262) is cited here; fi191179 and fi191192
sit outside the cohort entirely, so a merge touches a hub the draft does not
cite. `nanobud-nanopub-batch3.md` carries this
cohort's per-hub sign-off/adjudication queue and points here for the
count.

Caveat for any re-approval: the title/body reopen NULLed `grounding` on
all 139 touched publish rows — evidence edges survive, hand-trimmed
quotes/snips don't and must be redone.

## The convergences that never happened

_Grouped 2026-09-26; was `claim-hub-dedup-sweep`, status draft._

`block()` retrieved candidates over `card_combined` (`ord=-1`) until
2026-08-20, and only 187 of ~1,524 hubs had one — 12.3% coverage. So `place()`
almost never saw a candidate, and the merge path it exists to run has
effectively never run at scale. The index now reads `finding_body` (`ord=0`),
which every hub has. This item is the one-time backward sweep over the
duplicates that accumulated while the index was blind; `place()` handles the
forward direction from here.

### The cohort, measured 2026-08-20 — **contaminated, re-measure first**

The measurement below used `TAPROOT:claim` alone as the hub predicate, which is
`block()`'s definition and includes **280 rows that are not hubs** — see
`claim-hub-definition-divergence.md`. The strict population is 1,244, not 1,527.
Re-run the all-pairs scan against `TAPROOT:claim` **+ `STATUS:canonical`** before
acting on any band; pairs whose either side is a chase-tree finding are not
duplicates and must not reach the merge path. The table stands only as evidence
that a real cohort exists.

Cosine distance between the `finding_body` chunk embeddings of every live
`TAPROOT:claim` hub pair (1,527 embedded, permissive predicate, all-pairs):

| band | pairs |
|---|---|
| < 0.02 | 4 |
| < 0.05 | 9 |
| < 0.10 | 24 |

The nine under 0.05 — the actionable set:

| dist | pair | shape |
|---|---|---|
| 0.0000 | `fi191179` / `fi191260` | identical sentence, **forked on `scope`** |
| 0.0000 | `fi191192` / `fi191262` | identical sentence, **forked on `scope`** |
| 0.0008 | `fi191256` / `fi191263` | same result, one framed "Ravi et al. synthesized", one "Fluorescence measurements on" |
| 0.0088 | `fi176714` / `fi178555` | same light-gated STOP-GO shuttle claim |
| 0.0205 | `fi177386` / `fi177399` | Top7 — "designed via Rosetta fragment assembly and energy minimisation" vs "designed entirely by energy minimisation with Rosetta" |
| 0.0325 | `fi176432` / `fi177486` | HKUST-1 Young's modulus **9–12 GPa vs ~9 GPa** |
| 0.0333 | `fi176861` / `fi178714` | two-stage kinetic-proofreading — "can eliminate" vs "eliminates" (`gripe #180306`) |
| 0.0396 | `fi176667` / `fi176669` | largest spiroligomer **macrocycles** vs **structures** |
| 0.0445 | `fi176919` / `fi177522` | contact resistance "at a single metal-molecule-metal interface" vs "per molecular interface" |

Note the corpus is not only nanobuds — most of this cohort is the
molecular-machines material, so the sweep is not a nanobud-campaign task and
should not wait on it.

### These are four different problems wearing one shape

Distance does not tell you which. Each band needs a different verdict, and
three of the four are **not** merges:

1. **Scope forks** (the two 0.0000 pairs). Same sentence, different `scope`,
   so they are distinct `pub_id`s but collide on AIDA URI at publication —
   see `aida-uri-ignores-scope.md`. Either they are one claim (merge) or the
   scope is load-bearing and the *sentences* are inadmissible as written.
2. **True paraphrases** (`fi176861`/`fi178714`, `fi176714`/`fi178555`,
   `fi191256`/`fi191263`). Merge: keep one hub, move evidence, retire the
   other. This is what `place()` would have done.
3. **Neither claim is grounded** (`fi176432`/`fi177486`: 9–12 GPa vs ~9 GPa).
   Read as a point-vs-range disagreement until the sources were checked
   2026-08-20; it is not one. `fi176432` grounds to pa1698, whose passage
   reports *hardness* "at least 130% greater than … conventional MOF
   counterparts" and gives no modulus figure at all. `fi177486` grounds to
   pa4246 — *ZIF-8 films as low-κ dielectrics*, a different MOF — with a NULL
   grounding chunk. **Do not merge and do not harmonise into a range**: two
   unsupported claims averaged together are still unsupported. Reground against
   a source that measures HKUST-1's modulus, or retract both. The only other
   edge each carries is a `cites` from draft 42995, which is the drafting
   document, not evidence.
4. **Scope-differing near-synonyms** (`fi176667`/`fi176669`: macrocycles vs
   structures; `fi176919`/`fi177522`: per-interface vs single-interface).
   These may be a `refines` relationship, not a merge — one is strictly
   narrower than the other.

### Constraints on the pass

- **Merging changes `pub_id`** (identity is sentence + scope), so it must run
  while hubs are `candidate` and **before** re-approval — the same ordering
  constraint the notation and scope backfills carry.
- **Dry-run first, reviewed by a human before any write.** This is the first
  large run of a merge path that has never executed at scale; the standing
  rule is a confidence floor, an idempotency story and a dry-run before any
  corpus-wide automated writer commits.
- **Over-merge is the one dangerous direction** — `eval_canon`'s live gate
  requires zero false `same`. Under-merge is tolerated. Bands 3 and 4 above
  are exactly where an automated judge would over-merge, so they should route
  to a human rather than to `merge_confirm`.
- `gripe #180306` filed pair 7 independently; closing it is part of done.
- **`fi176485` / `fi176490`** — near-identical molecular NAND-tree claims,
  spotted by eye during the 2026-08-23 graduation campaign rather than by the
  cosine sweep. Add them to the candidate list; they also serve as a check on
  whether the sweep's floor caught them.

### Why 24 and not more

All-pairs cosine over 1,527 hubs is a floor, not a census. Embedding
proximity measures topical similarity, so it finds paraphrases and misses
duplicates phrased in different vocabularies — the same structural bias
documented for the opposition finder in
`disputes-edge-nonblocking-disagreement.md`. Do not report the post-sweep
count as "the corpus is now deduplicated".
