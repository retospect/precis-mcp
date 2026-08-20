---
status: draft
title: "the claim-review mechanism — how to churn fi hubs + quotes to sign-off-ready, repeatably"
---

# The claim-review mechanism

Written down so it can be run without an Opus session driving it — the
procedure is the asset, not the session that discovered it. The target
state Reto named on 2026-08-20: *"we are happy with our automatic claim
management system and it is creating consistent high quality results
that are easy to sign off on."* Sign-off stays human; everything up to
it should be mechanical, measured, and boring.

Standing constraint: **approve / sign / signoff / publish --live are
human-only doors.** Nothing below crosses them.

## The invariant that makes this cheap

Derive, never duplicate. Every expensive incident in this corpus so far
traces to a second copy of a fact that was allowed to drift from the
first:

- `block()` retrieved over `card_combined`, a chunk kind nothing writes
  — 88% of hubs were invisible to dedup. Fixed by retrieving over the
  `finding_body` chunk that already exists and already re-embeds on
  every reword. One join, no backfill, no worker.
- `nanopub_publish.approved_title` vs `refs.title` is *deliberately* a
  second copy — that pair IS the drift sensor, and collapsing it would
  delete the sensor. Know which duplications are load-bearing.

Before building a pass, ask what already holds the fact. The answer has
twice been "the thing you were about to copy from."

## Step 0 — never mutate prod from an unverified rule

`normalize_notation` and `lint_notation` are pure and DB-free precisely
so they can be run over a CSV dump. **Every mechanical pass dry-runs
first**, per `docs/conventions/corpus-normalization.md`.

```
PRECIS_PROD_PSQL_OPTS="--csv -t" scripts/prod-psql "
  SELECT r.ref_id, r.title FROM refs r
    JOIN ref_tags rt ON rt.ref_id=r.ref_id
                    AND (rt.expires_at IS NULL OR rt.expires_at > now())
    JOIN tags t ON t.tag_id=rt.tag_id
               AND t.namespace='TAPROOT' AND t.value='claim'
   WHERE r.kind='finding' AND r.deleted_at IS NULL
   ORDER BY r.ref_id;" > hubs.csv
```

The harness then reports, per rule: fires, before/after pairs, the
**wrong:right ratio by inspection**, idempotence, and a before→after
lint delta. Ship the auto-fix only when wrong is zero; otherwise demote
that rule to detector-only and leave the corpus alone.

Three separate corpus-corrupting rules have been caught by exactly this
and by nothing else:

| rule | what it did | why no test caught it |
|---|---|---|
| `ascii-minus-exponent` | `Fe-ZSM-5`→`Fe-ZSM⁻⁵`, 146 of 149 fires wrong | unit tests used units, the corpus used nomenclature |
| `ascii-plusminus` | `Zn2+-sensing`→`Zn2±sensing`, destroying an oxidation state | the test sentence had digits on both sides |
| `ascii-micrometre` | `micron-scale`→`µm-scale`, 5 of 10 wrong | the test sentence had a numeral |

The pattern is identical in all three: **a rule that reads a pattern
where it must read a role.** `-` between a unit and a digit is a
negative exponent; between a series name and a digit it is nomenclature.
A unit *symbol* takes a numerical value; a unit *name* takes words. When
adding any rewrite rule, write down which role it depends on, then
assert the role — not the characters.

Two harness properties earn their keep and should never be dropped:

- **`len(out) >= len(in)` is a bug, not an assertion.** 51 hubs
  legitimately shorten.
- **The before→after lint delta catches corruption independently.** The
  `Zn2±` bug surfaced as `formula-ascii-subscript` going 77→78 — a rule
  creating work for another detector is a corruption signal.

## Step 1 — order of passes, and why it cannot be reordered

1. **Acquisition markers → tags** (blocker 3). Before the body chunk is
   derived from the title, or the markers get baked in.
2. **Title/body reconciliation** — 297 hubs where the `ord=0`
   `finding_body` chunk ≠ `refs.title`. Resolution: one authored
   sentence, the chunk a verbatim derivation of the title, reassembly on
   export. Re-embeds 297 chunks.
3. **Notation normalization** — 456 hubs, canon v3.1, dry-run clean.
4. **`pub_id` re-hash, then duplicate rescan.** Normalizing changes the
   hash input; the *stored* `pub_id`s do not collapse on their own.
5. **Bucket-A stub untag** — reversible (drop the tag, keep the ref),
   biased to false negatives.
6. **Adversarial review** (below).
7. **Re-approval** — human, and only now.

**Re-approval last is load-bearing.** Once a hub is approved its
`claim_sha` freezes and drift gate #14 goes live; any later pass
touching `refs.title` forces a reopen. That is exactly how 77 of 139
rows got stuck. Approving early does not merely waste effort, it
recreates the original defect.

## Step 2 — the duplicate scan, scoped correctly

Group on the **sentence hash ignoring `scope`**, then adjudicate scope
separately. Grouping on `pub_id` finds nothing, because `scope` is in
the hash and free-text scope values are what fork the pair.

Measured 2026-08-20: exactly two exact-sentence duplicate groups
corpus-wide (fi191179/fi191260, fi191192/fi191262), both forked by
paraphrased scope values, neither encoding a real regime distinction.
Normalization creates **zero** new ones. Near-duplicates (0.75–0.90
advisory, ≥0.90 mandating a typed edge) are the larger bucket and need
the ANN pass, which the `block()` fix unblocked.

## Step 3 — adversarial review, and the incentive bug

This is the step that turns an archive into an instrument, and it is the
one currently missing.

2 `contradicts` and 4 `refines` across 1,527 hubs drawn from deliberately
overlapping literature.

**The cause is not what it looks like.** This document first blamed an
incentive bug — a `contradicts` edge blocks the other hub, so agents
self-censor. A readiness review checked the code and disproved it: no
agent files a hub↔hub `contradicts` at all. It is written automatically
from an unreviewed MEDIUM-tier LLM verdict during ingest
(`canon.place()` → `hub._mint_for_placement`). The real cause is that
`place()` judges only against candidates `block()` retrieved, and
`block()` was retrieving over `card_combined` — a chunk kind nothing
writes — so the candidate set was empty for 88% of hubs. **The judge was
almost never asked.** Same root cause as the dedup blindness in the
invariant section above; we fixed it without noticing what else it
explained.

Consequence for this step: **measure before building.** Re-run placement
with the `block()` fix in and re-count. That is read-only and it sizes
everything downstream.

**Proposed fix — split the edge.** `disputes` records disagreement and
blocks nothing; `contradicts` is the *adjudicated outcome* and blocks.
The ingest judge emits `disputes`, so an unreviewed LLM call raises a
question instead of silently blocking a stranger's publication. A dense
`disputes` graph is then the map of where inquiry should go, not corpus
damage.

**The asymmetry to fix regardless of counts:** in `place()`, a `"same"`
verdict at low confidence triggers a second `merge-confirm` LLM call;
a `"contradicts"` verdict triggers no confirmation at any confidence.
We double-check the reversible, low-harm decision and single-shot the
irreversible, high-harm one.

Reviewer emits one of five verdicts; only the last blocks:

- `same-claim` → attach evidence to the survivor, retire the duplicate
- `refines` → typed `refines` edge
- `scope-mismatch` → annotate scope on both, no edge — **the expected
  majority**
- `unit-error` → one side is arithmetically wrong; retract it
- `genuine-conflict` → `contradicts` + hunt a third adjudicating source

Run it first over the dense neighbourhoods (MOF conduction, DNA bricks,
molecular switches) — conflicts hide where coverage is thickest. Seed
cases already in hand: fi191120 vs fi218681; pa1992's GPa/TPa error, off
by ~10³. `precis-adversarial-reviewer` exists as a paper-draft persona;
adapt it, don't write a new one.

## Step 4 — confidence, since truth is not available

We cannot check whether a claim is true, and should stop implying we
can. Every existing gate is an **admissibility** gate — well-formed,
sourced, traceable. Admissible is not true.

What *is* computable, and each is defeasible on its own:

| signal | status | note |
|---|---|---|
| independent-supporter count | **shipped 2026-08-20**, `view='evidence'` | distinct supporting papers collapsed by shared authors (union-find, transitive); derived on read, no column or worker |
| hearsay distance | **already gated** | primary-source-only is a real truth proxy — keep it |
| source rank | **newly available** | the paper ranking system, not yet lit up — this is the missing multiplier |
| dispute status | needs step 3 | unchallenged ≠ true, but challenged-and-survived means something |
| `testableBy` resolution | not tracked | for `predicts` claims: was the discriminating experiment ever run? |

Expose the confidence **with its lineage**, never as a scalar verdict.
A reader must be able to see which term carried it.

**Author coverage, measured 2026-08-20 (read-only, prod).** Corpus-wide
only ~33% of `paper` refs carry authors — but restricted to refs that
actually hold a taproot evidence edge, **436/521 (84%)** do. The
evidence-linked slice is the well-curated one, which is why the shared-
author collapse is worth computing at all rather than falling back to a
raw distinct-paper count.

**Known over-report, and it runs the wrong way.** Patents carry *zero*
author data (0/101 in prod), so every patent is its own singleton and a
hub supported by three patents reads as three independent supporters.
Three patents from one assignee are not independent evidence — they are
one party filing three times. The failure mode is therefore
**over-stating** independence exactly where a reader would most want it
under-stated, and a patent-heavy hub's number should not be trusted as
printed. Fix when it matters: collapse on shared **assignee** for
patents, the way authors collapse for papers. Filed here rather than
built because it needs the assignee field checked for coverage first —
the same measure-before-building rule this document keeps re-learning.

## Step 5 — what an outside review says we are missing

`perplexity-research` was asked to attack the design on 2026-08-20. Both
reports are cached in prod (read-only, free):

- `get(kind='perplexity-research', id='critique-the-design-of-a-scientific-claim-publication-pipeli')`
- `get(kind='perplexity-research', id='critique-a-concrete-machine-readable-scientific-claim-artifa')`

**Only page 1 of each has been read** — roughly 150 KB across both
remains undrained, and the pagination cursors expired. Do not treat the
notes below as the full review.

What page 1 established, and it agrees with the internal diagnosis:

- The corpus is *"impeccably traced but epistemically flat"* — claims
  that cannot disagree productively cannot support discovery. Same
  conclusion, reached independently.
- **ECO** separates evidence *type* from assertion *method* and leaves
  the assertion to other vocabularies. **GRADE** separates the study
  outcome from the certainty appraisal. Our design conflates assertion,
  evidence mode and identity into one canonical sentence — likely too
  rigid.
- **Micropublications** model `supports` / `challenges` between claims
  explicitly, enabling citation-distortion detection. We emit no
  claim-to-claim argumentation edges at all.
- The provenance design is strong on cryptographic and textual anchoring
  and weak on paywalls, preprint versioning, and retraction.
- We under-use existing vocabularies: **CiTO** for citation semantics,
  **schema.org/Claim** for web-scale interoperability, **PROV-O** at
  full expressive power.
- Suspected over-engineering: low-level identity and admissibility,
  relative to the value they return.

Open question the review sharpens: a single `rdfs:label` AIDA sentence
plus an AIDA URI, rather than decomposed triples over domain ontologies
— when is that the right call and when is it a cop-out? Real
nanopublication corpora do both.

## The verdict on identity and admissibility (Reto asked, 2026-08-20)

An external review suggested we over-engineered low-level identity and
admissibility. **Do not simplify either. Stop adding to them.**

- **Identity earns its keep.** `pub_id` over the normalized sentence is
  *why* the notation canon exists, and it correctly held two claim pairs
  apart today. The alternative — opaque minted ids — loses content-based
  dedup entirely, which is the property doing the work.
- **Admissibility is a real truth proxy.** Primary-source-only is the
  single most valuable rule in the system; weakening it enables hearsay
  laundering. Keep it.
- **The imbalance is the defect, not the depth.** Essentially all
  engineering went into "is this claim well-formed"; none into "does
  anything disagree with it." The fix is to build the missing half.

**One genuine fragility**: `scope` is in the identity hash with
free-text values, and that forked *both* live duplicate pairs. That is
not "simplify identity" — it is finishing Decision 4's value ruling.

## Next moves, ranked by leverage per unit of work

0. **Re-count `contradicts` with the `block()` fix in.** Read-only, cheap,
   and it sizes every item below — the 2-edge figure was measured through
   a broken retrieval path, so *no* estimate here is trustworthy until
   this runs. Do it before writing code.
1. **`disputes` edge** — `docs/backlog/disputes-edge-nonblocking-disagreement.md`,
   now split into Part 1 (write path) / Part 2 (review workflow). The
   highest value-to-risk line in the whole plan is **Part 1 item 1a**:
   repoint `canon.place()`'s `"contradicts"` verdict at `disputes`, so an
   unreviewed ingest LLM call raises a question instead of silently
   blocking a stranger's publication. Small, local, and it fixes the
   confirm-asymmetry regardless of what step 0 measures.
2. **Independent-supporter count** — derivable from evidence edges
   today, derived nowhere. Read-side only, no schema change.
3. ~~**Scope-value lint**~~ — **already shipped in `2480c172`**
   (`sentence_lint.py::lint_scope`), and the two duplicate pairs were
   already its test fixtures. The real gap was that it was surfaced to
   **nobody**: `seed_claim_hub` returned only `notation`, and neither the
   MCP `finding` handler nor the CLI's default text output printed even
   that — the warnings existed solely under `--format json`. Wired into
   both mint paths 2026-08-20.

   **A detector nobody sees is not a detector.** 156 of 1,525 hubs
   (~10.2%) carry a lint-flagged scope value, accumulated while the rule
   was live and silent. When adding any advisory lint, name its surfacing
   path in the same change — the rule is the cheap half.

   Remaining corpus work: those 156 hubs. Not mechanical — a scope value
   is a *term choice*, and rewriting one changes `pub_id`, so it must run
   before re-approval (step 1 ordering) and may collapse hubs.
4. **Skill updates** — cheapest high-leverage item, because skills are
   the runtime channel every minting agent reads. Must carry:
   admissible ≠ true; scope values are terms, not paraphrases; canon
   v3.1.
5. **Apply step-4 normalization** — dry-run clean at 456 hubs, not yet
   written to prod.
6. **Adjudicate the two duplicate pairs** — small, and it exercises the
   verdict vocabulary end-to-end before it is used at scale.
7. **Re-approve the nanobud cohort** — human door, last, and only after
   1–6.

**Landed 2026-08-20**: item 4 (skills — `precis-taproot-mint-help`,
`precis-nanopub-help`, and `precis-notation-canon`, whose ASCII fallback
table was telling agents to apply the *unguarded* v3.0 rules); item 3
(the lint existed; its surfacing did not — now wired into both mint
paths with 6 tests).

**The methodological lesson of the day, worth more than any single item:**
three of the four dispatched tasks came back reporting that the task
itself was wrong — the lint already existed, the incentive diagnosis was
false, the reviewer persona can't be adapted. Each correction was worth
more than the task would have been. **When a plan item is cheap to check
against the code, check it before building it**; this document asserted
three things about its own codebase that the codebase disagreed with.

## Step 6 — applying this to a specific document

The nanobud draft dr173020 is the worked example: 180 chunks, 126 cited
findings, 118 with a publish row. The same procedure applies to the
boxel document and any successor — scope the cohort by
`links.src_chunk_id → chunks.ref_id = <draft>`, then run steps 0–4 over
that cohort rather than the whole corpus. Per-document scoping is what
makes this affordable; the corpus-wide pass is only for the mechanical
rules that are already dry-run clean.

Known caveat for any re-approval: **the step-1 reopen NULLed
`grounding` on all 139 publish rows.** Evidence edges survive, so
passages regenerate from the prefill, but hand-trimmed quotes and snips
(de-bracketed `[60]fullerene`, a snip extended out of a chunk overlap)
are gone and must be redone. Budget for it.
