---
status: draft
title: "the claim-review mechanism — how to churn fi hubs + quotes to sign-off-ready, repeatably"
---

# The claim-review mechanism

Procedure for turning fi claim hubs into sign-off-ready nanopubs,
mechanically and repeatably — written down so it doesn't require an Opus
session driving it. Target state (Reto, 2026-08-20): automatic claim
management producing consistent, easy-to-sign-off results. **approve / sign
/ signoff / publish --live are human-only doors** — nothing below crosses
them.

## The invariant

Derive, never duplicate — every expensive incident here traces to a second
copy of a fact allowed to drift from the first. `block()` retrieved over
`card_combined`, a chunk kind nothing writes — 88% of hubs invisible to
dedup; fixed by retrieving over `finding_body`, which already exists and
re-embeds on every reword. `nanopub_publish.approved_title` vs `refs.title`
is *deliberately* a second copy — that pair is the drift sensor;
collapsing it deletes the sensor. Before building a pass, ask what already
holds the fact.

## Landed

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

## Reto's ruling 2026-08-20 — the source is the authority, the text is adjustable

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

## The corpus grew ~10× — what that unlocks

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

## Open — disputes / adversarial review

Owned by `docs/backlog/disputes-edge-nonblocking-disagreement.md`: the
five-verdict taxonomy, the `disputes`-vs-`contradicts` edge split, the
incentive-bug retraction (cause was `block()`'s `card_combined` blindness,
not agent self-censorship — do not cite the incentive story), and the
asymmetry (`"same"` at low confidence gets a second LLM confirm;
`"contradicts"` gets none at any confidence). Seed cases: fi191120 vs
fi218681; pa1992's GPa/TPa error (~10³ off). Run the dense neighbourhoods
first (MOF conduction, DNA bricks, molecular switches) — conflicts hide
where coverage is thickest.

## Open — mechanical passes not yet applied

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

## Open — re-approval sequencing (load-bearing order)

Acquisition-markers→tags, title/body reconciliation, notation
normalization, `pub_id` re-hash+rescan, bucket-A stub untag, adversarial
review, **re-approval last**. Approving a hub freezes `claim_sha` and arms
drift gate #14; any later pass touching `refs.title` forces a reopen. The
title/body reconciliation pass already did exactly this: **all 139
`nanopub_publish` rows are `candidate`, 0 `reviewed`/`signed`** (verified
2026-08-20 — supersedes any earlier "N of 139 stuck" count). Re-approving
early doesn't waste effort, it recreates the defect.

## Open — confidence signals

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

## Open — external critique, page 1 only

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

## The dr173020 worked example

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
