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
3. **156 of ~1,525 hubs (~10.2%)** carry a lint-flagged `scope` value,
   accumulated while the lint was live but unsurfaced. Not mechanical — a
   scope value is a term choice, rewriting one changes `pub_id` and may
   collapse hubs.
4. **Near-duplicate hub sweep** — 24 pairs under 0.10 cosine, 9 under 0.05,
   spec in `claim-hub-dedup-sweep.md`. Must precede re-approval for the same
   reason as #2: merging changes `pub_id`.

**Blocked on an input we do not have:** the boxel document's `dr` id was never
obtained, so its per-document cohort pass cannot start. Every other document
in the campaign has one. This is a question for Reto, not a task — until it is
answered the boxel cohort is simply out of scope, and should not be counted as
outstanding work against the campaign.

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
cited, 118 with a `nanopub_publish` row (all `candidate` per the
re-approval note above). `nanobud-nanopub-batch3.md` carries this
cohort's per-hub sign-off/adjudication queue and points here for the
count.

Caveat for any re-approval: the title/body reopen NULLed `grounding` on
all 139 touched publish rows — evidence edges survive, hand-trimmed
quotes/snips don't and must be redone.
