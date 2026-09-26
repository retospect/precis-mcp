---
status: draft
---

# Evidence edge verification

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## A verdict with nothing under it

_Grouped 2026-09-26; was `evidence-edges-assert-support-with-no-passage`, status draft._

An evidence edge is supposed to say *this passage, in this source, supports this
claim*. 367 of the corpus's 1,498 evidence edges say only the first and last
parts. Their `meta` reads, verbatim:

```json
{"caveats": [], "support": "yes", "source_handle": null}
```

`src_chunk_id IS NULL` and `meta->'source_handle'` is jsonb **null** — the key is
present, deliberately written, and empty. So the row asserts an affirmative
support verdict for a passage that was never identified.

That is **30% of every affirmative support verdict in the corpus** (367 of
1,235).

### Why it matters more than a missing pointer

Every gate passes on these edges. The edge exists, the source is primary, the
claim has evidence with `support: "yes"`. Nothing anywhere asks whether the
thing that produced the verdict ever read anything.

This is the mechanism behind `fi177486` — *"HKUST-1 has a Young's modulus of
approximately 9 GPa"* — grounded, with `support: "yes"`, to ref 4246, *Metal-
Organic Framework ZIF-8 Films As Low-κ Dielectrics in Microelectronics*. A
different material, a different property, no passage. The claim looked supported
to every automated check we have.

### It is bounded, and it is not ongoing

Evidence edges by creation day, split on whether they anchor a passage:

| day | no passage | with passage |
|---|---|---|
| 2026-07-30 | **230** | 345 |
| 2026-07-31 | **133** | 270 |
| 2026-08-02 … 08-17 | 9 total | 194 |
| 2026-08-12 | 0 | 132 |
| 2026-08-19 | 0 | 178 |

**363 of the 367 came from a two-day batch.** The current edge-writing path
anchors correctly — the two busiest recent days produced 310 edges with zero
defects. Whatever wrote the July batch is fixed or retired.

So this is a **bounded backfill over a known cohort**, not an incident. Do not
treat it as a live bug; do not go looking for a regression in today's code
before reading this.

### Concentration

361 of the 367 are in the `dr42995` (boxel draft) cohort. That is not because
the draft is special — the July batch is simply what built that draft's
evidence. Any other draft built by the same batch would show it too.

### Repair — the tool is built; the run has not happened

`src/precis/taproot/repair_evidence.py` + `precis taproot repair-evidence`
implement everything below. **Dry-run is the default and the only mode that
needs no flag**; `--apply` writes, `--draft` scopes to a draft's cited hubs,
`--limit` caps the batch, `--tier` re-verifies above MEDIUM. Proposal rows
(`link_id`, `hub`, `source_ref`, `chunk_id`, `quote`, `reason`) go to `--out`
as JSONL; the summary goes to stderr. What remains open is the **run**: a
dry-run over the `dr42995` cohort, a read of its proposals, then a small
`--apply` batch.

```
precis taproot repair-evidence --draft dr42995 --limit 20 --out /tmp/repair-dr42995.jsonl
```

#### Blocker, found 2026-08-21: it cannot be run from an interactive SSH session

Deployed and attempted on melchior. **All 20 edges errored**, every one with
`claude -p … "result":"Not logged in · Please run /login"`. Four escalating
attempts all failed the same way:

1. plain `ssh melchior` as the operator → not logged in
2. `+ HOME=/Users/deploy` (the daemon's home, `.claude` present) → not logged in
3. `--tier small` with the daemon's full `EnvironmentVariables` exported → **HTTP
   401, "Missing…"** — the OpenRouter key is not in that plist either
4. `sudo -n -E -u deploy -H` → not logged in

**Cause:** the `claude` credential is bound to the **macOS login keychain**,
which is per-user and requires a GUI login session. `sudo` does not unlock it,
and no reconstruction of `HOME`/env from an SSH session reaches it.

**This is not an outage.** `llm_call_log` shows `big`/`claude_p` at **16 calls,
0 errors** on the same day, most recent 01:59, and `small`/`openai_compat` at
23k calls/day — the daemons' own context has working credentials. Only
interactive one-offs are affected. (`medium`/`claude_p` last ran 2026-08-17,
which is idleness, not failure.)

**Therefore run it one of two ways:**
- as a **worker job**, in the process context that already dispatches
  successfully — the architecturally right answer, but `repair-evidence` is not
  a registered job type yet; or
- **by a human on melchior in a logged-in session**, which is the cheap answer
  for a one-off backfill.

**The tool itself behaved correctly under total dispatch failure** and this is
worth keeping: it emitted `error` rows with `reason: null`, never recorded a
dead dispatch as `verify-rejected` or `no-passage`, exited non-zero, and wrote
nothing. A pass that silently converted infrastructure failure into "no
grounding found" would have looked like a successful audit of 20 edges.

The machinery it reuses: `src/precis/taproot/reground.py`. It takes a claim
and a candidate source paper, ranks that paper's body chunks by content-word
overlap (with notation folding, so `10^4` and `10⁴` match), excludes hearsay
sections (references/related-work/prior-art) so a claim cannot ground in someone
else's citation, verifies support with an LLM, and then **post-validates the
quote in code** — the returned quote must appear verbatim as a substring of the
claimed chunk *and* be unique across the paper's non-hearsay chunks. A
hallucinated quote is rejected mechanically rather than trusted.

Its four named ungrounded reasons (`no-passage`, `hearsay-only`,
`verify-rejected`, `quote-validation-failed`) are exactly the taxonomy this
backfill needs.

Split the population before running it (measured against the `dr42995` cohort,
920 hubs):

| bucket | count | action |
|---|---|---|
| source paper **has** live body chunks | **271** | re-ground — the text is right there |
| source paper has **no** live body chunks | **59** | acquire + ingest first |

### Repair mechanics — three findings that change the obvious implementation

**1. `reground.py` IS reusable.** The "built for migration atoms" worry was
wrong at the library layer — an atom is just
`canon.CanonicalClaim(sentence, scope)`, exactly what
`hub_refine._fetch_hub_info` already returns for a hub. Only the *CLI*
(`taproot-migrate reground`, strictly file-in/file-out over a dry-run artifact)
is migration-shaped. `verify_atoms(..., collect_papers_fn=lambda _s,_h:
[source_ref_id])` is a first-class seam for searching only the known source, and
`verify_batch_fn` makes the hardcoded `Tier.MEDIUM` injectable per call.

**2. The repair must `UPDATE` in place — never `attach_evidence`.**
`Store.add_link`'s conflict key is
`(src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation)`. Since the
broken row's `src_chunk_id` is NULL, attaching a grounded edge **inserts a second
row and leaves the broken one live** — doubling the defect while appearing to fix
it. Repair is `UPDATE links SET src_chunk_id=…, meta = meta || …
WHERE link_id=…`, catching `UniqueViolation` for the case where a grounded twin
already exists.

**3. The existing repair path excludes exactly this population.**
`cli/taproot.py::_backfill_grounding` Part B already does the right in-place
`UPDATE links SET src_chunk_id`, but its candidate SQL
(`_PAPER_EVIDENCE_CANDIDATE_SQL`) requires
`meta->>'source_handle' IS NOT NULL AND <> 'null'`. These rows carry
`source_handle: null`, so the one tool built for this filters them out. That
is why `repair-evidence` is a second verb rather than a widened filter: Part
B *resolves a stored handle*, this pass has to *find the passage* — different
machinery behind the same UPDATE.

### Origin — one hypothesis refuted, one open

Suspected cause: `hub_refine`'s discovery attach passes
`handle_registry.try_format(ref.kind, block.id, chunk=True)` into
`attach_evidence`; when that returns `None` the meta is byte-for-byte the shape
above, and `_grounding_chunk_ord` then yields no `src_pos`.

**The "unsupported kind" version of that is refuted.** Source `kind` is `paper`
for both the 369 broken edges and the 1,124 healthy ones, so `try_format` plainly
works for `paper`. (Also noted: 3 broken edges have source kind `finding`, which
is not a valid evidence-source kind at all — a separate, tiny anomaly.)

What remains open is whether `block.id` was a **ref-level** id rather than a
chunk id, which would make `chunk=True` formatting fail. If so these edges were
formed by matching the claim against the paper as a whole rather than any
passage — which would explain an affirmative `support` verdict with no anchor.
**Not established.** Read the call site before repeating it as fact.

Knowing the origin is not a prerequisite for the repair, and the path currently
writes clean edges.

### The verdict is the part to distrust

A re-grounding pass that finds no supporting passage in a source whose edge says
`support: "yes"` has not failed — it has discovered that the verdict was empty.
Record `verify-rejected` and leave the claim alone. **Do not edit a claim to
match a source that a passage-less edge merely asserted.** The edge is the thing
that was wrong.

## What remains (the write-path fix shipped)

_Grouped 2026-09-26; was `evidence-edges-born-released`, status draft._

The code half is done: no attach path writes `support` without
`support_reason` + `verified_by` (+ `verified_at` / `verified_claim_sha`);
mechanical mints are born withheld; `hub_refine` re-verifies a hub's
attached-but-unverified edges per pass; the preflight withholds a
sha-stale verdict. What's left is **operational**, against prod:

1. **Run the sweep over the standing corpus.** `precis taproot
   verify-edges` (default withheld cohort — 264 edges / 248 hubs measured
   2026-08-27), then `--unverified-stamped` (the born-released cohort:
   `support` set, no `verified_by` — 1252 edges measured 2026-08-21; a
   non-corroborating verdict STRIPS the stamp and returns the edge to
   withheld). Dry-run first; `--apply` **must be coordinated with active
   minting sessions** — other sessions are minting against these hubs
   right now, and stripping mid-flight blocks their publishes without
   warning. Run it as its own announced pass, per-claim-set if needed
   (the nanobuds set was already pushed back 2026-08-21; ~1208 edges
   outside it remain). `hub_refine` chips at the same debt
   (`_REVERIFY_PER_PASS` per hub per pass) but never strips — the sweep
   is the completion mechanism.
2. **The 209 legacy verified edges carry no `verified_claim_sha`.**
   Verdicts from the 2026-08-21 retro-verify pass predate the sha stamp;
   invalidation is forward-only by design, so an edit to those claims
   will NOT withhold their (now possibly stale) verdicts. Optional
   tightening once the sweep above has run clean: re-stamp them (any
   verify pass over them adds the sha) or accept the exposure for the
   fixed historical cohort.

Acceptance for closing this file: both sweeps run `--apply` against prod
with the strip counts recorded, and
`support IS NOT NULL AND NOT meta ? 'verified_by'` returns 0 corpus-wide
(the write-path guarantee already holds it at 0 for new edges).

## The pass works; its rubric does not

_Grouped 2026-09-26; was `grounding-verification-rubric`, status draft._

A pilot ran 2026-08-20 over 18 claim hubs cited by `dr42995` (3 mandated
calibration hubs + 15 drawn with `setseed(0.42)`), reading each hub's sentence
against the full text of its grounding passages. **It passed its calibration
bar**: it independently caught `fi176432` and `fi177486`, the two hubs whose
detection I had declared the go/no-go condition for scaling to 922.

It also broke its own rubric in four places. Fix these before the full run —
each one changes verdicts, not just labels.

### 1. `WRONG_CHUNK` is missing, and it is the failure that matters at scale

Six of 18 hubs are cases where **the paper supports the sentence but the
attached chunk does not**:

- `fi176432` — sentence claims HKUST-1 Young's modulus 9–12 GPa; the attached
  chunk is about methane storage capacity. Another chunk of the *same ref 1698*
  reads *"the indentation modulus (I) of mono HKUST-1 is 11.5 ± 0.4 GPa … its
  Young's modulus (E) to be 9.3 ± 0.3 GPa"*. The claim is **true of its source**.
- `fi177412` — "12 diverse targets" is absent from the chunk, present in the
  paper's extended-data table (*"Number of binders against the 12 targets"*).

Verdicting strictly on the passage, as the pilot was instructed, labels these
UNSUPPORTED/PARTIAL. **If downstream repair edits claims rather than re-grounds
edges, this pass actively destroys correct work** — the exact inversion Reto's
ruling warns about, arriving from the opposite direction (there the corpus
looked wrong and the claim was right; here the *edge* is wrong and both claim
and paper are right).

Make it a second axis, not a label: every verdict carries
`passage_verdict` × `paper_verdict`. `passage=fail, paper=pass` ⇒ re-ground the
edge, never touch the sentence.

### 2. The corruption tell is wrong, and this is the second base-rate error

The pilot was told: zero Greek + non-ASCII present ⇒ extraction damage. Seven of
17 checkable sources (**41%**) matched it, and **every one** carried LaTeX
`\mu` / `\pi` / `\tau` macros. Mathpix/marker-style extraction escapes Greek by
design; those papers are not damaged.

This is the same species of error as the earlier 24%-of-corpus detector recorded
in `ingest-strips-greek-glyphs.md`. Two independent detectors, two base-rate
failures, same root cause: **a signal was read as evidence of damage without
first measuring how often it occurs in undamaged documents.**

Required discriminator, in order:
1. zero Greek codepoints (U+0370–U+03FF, U+00B5); **and**
2. other non-ASCII present; **and**
3. **no LaTeX Greek macro anywhere in the doc's chunks** — if `\mu`/`\pi`/`\tau`
   appear, Greek is escaped, not lost. Not corrupt. Stop.

Consequence: the `325 Greek-exposed / 313 exclusively` figure recorded for
`dr42995` was produced by the pre-(3) detector and must be re-derived before
anything is decided on it.

### 3. `NO_GROUNDING` conflates two defects with opposite remediations

Five hubs had no readable passage. They split cleanly:

| shape | example | source has live chunks? | fix |
|---|---|---|---|
| edge lost its chunk pointer | `fi177486`, `fi176638`, `fi176729`, `fi177479` | **yes** (51–126) | re-ground the edge — mechanical, no acquisition |
| source never ingested | `fi176753` (Stoddart, *The Nature of the Mechanical Bond*) | **no** (zero) | acquire and ingest the text |

Same label today, completely different cost. Split into `EDGE_UNGROUNDED` and
`SOURCE_UNINGESTED`, and partition **mechanically in SQL before spending any LLM
tokens** — a pilot rate of 5/18 means ~28% of a 922-hub run would be spent
discovering that there is nothing to read.

### 4. Technique/quantity misattribution has no label

`fi177394` claims *"validated by cryo-EM at 2.7 Å"*. The source's 2.7 Å is an
**X-ray crystal structure**; its cryo-EM reconstruction is **5.1 Å** (the 2.7 Å
that appears near cryo-EM is an RMSD, not a resolution). Neither reading is in
the source. This is not partial support, not off-topic, and
`STUDY_TYPE_MISREAD` is defined as the *reader's* error, not the claim's. It
landed in PARTIAL, which badly undersells it. Add `MISATTRIBUTED`.

### 5. The search boundary must be stated

For every PARTIAL the pilot had to decide whether to read beyond the passage.
Doing so changed the verdict's *meaning* three times and cost ~⅓ of its
queries. Once (`fi177394`) the wider check made the finding **worse**. Policy:
verdict strictly on the passage; run a bounded whole-paper keyword probe and
record it in a separate field. Both facts are needed — the passage verdict
drives the edge repair, the paper verdict protects the claim.

### The failure class the PARTIALs share

Nine of 18 were PARTIAL, and they fail the same way: **the sentence asserts more
structure than the passage carries** — a superlative, a cause, a comparison, a
priority claim, or a unit upgrade.

- `fi176409` — "*the primary* driver of research"; source lists it as one
  application among several, never ranks it.
- `fi176800` — source says "~200- to **3500**-fold"; sentence reports the top of
  a 17× range as the value.
- `fi177720` — source says 5 nm **process node**; sentence says "sub-5 nm **gate
  lengths**". Real gate lengths at that node are ~16–20 nm. A marketing label
  silently upgraded into a physical dimension.
- `fi177597` — quantitative core is verbatim-supported; "**the first** direct
  experimental evidence" is a priority claim the source never makes.
- `fi176612` — source's objection is "large size and mass"; sentence renders it
  "lower bandwidth". Mass→slowness is the reader's inference.
- `fi177646` — passage covers rotaxanes; sentence also asserts catenanes.

This class is invisible to every existing gate: the edge is real, the source is
primary, the quote verifies. **These are repairable by weakening the sentence to
what the passage carries** — cheap, safe, and it makes the draft more true. This
is the highest-yield repair lane in the corpus.

### One structural fact to verify before scaling

Every hub in the 18-hub sample had **exactly one** evidence edge. If that holds
across all 922, then `EDGE_UNGROUNDED` and `WRONG_CHUNK` are *unrecoverable*
failures rather than degradations — there is no second witness to fall back on.
That changes repair design, so measure it first.

### Wave-1 revisions (2026-08-20, 118 hubs across two shards)

Two opus shards ran the rubric above. It held — the two-axis split did its job
and `source_corrupt` correctly never fired (no disputed quantity in either shard
hinged on a Greek-prefixed unit). Rates are stable across shards and match the
pilot: **SUPPORTED ~35%, PARTIAL ~48%, WRONG_CHUNK ~9%**. Six changes before
scaling.

#### 1. Split `PARTIAL` by severity — it is carrying 48% of all verdicts

It currently spans a dropped adjective and a wholly invented subject. Replace
with:

- `PARTIAL_MINOR` — a qualifier is dropped; meaning survives intact. Often no
  repair needed.
- `PARTIAL_MATERIAL` — the sentence adds structure the source does not carry
  (superlative, cause, comparison, priority, unit upgrade, extra subject).
  Repair = weaken the sentence to what the passage supports.
- `PARTIAL_FABRICATED` — an element has **zero** support anywhere in the source.
  Repair = delete the element or retire the claim. Example: `fi176460`'s
  "5–10-fold enhanced cascade efficiency", where the source gives a detection
  limit and no multiple at all.

#### 2. `WRONG_SOURCE` ≠ `WRONG_CHUNK` — add it

`WRONG_CHUNK` means the paper supports the claim and the edge points at the
wrong passage — re-grounding fixes it. `WRONG_SOURCE` means **the paper does not
contain the result at all**, so re-grounding within it is guaranteed to fail:

- `fi176620` — "20 pW/K" grounded on Lee 2013; `pW` has 0 hits there (the result
  is Cui 2019, a different paper).
- `fi176623` — Ni₃(HITP)₂ claim on a ref that never says `HITP`.
- `fi176594` — "multi-kilogram scale" on a milligram-scale solid-phase paper.
- `fi176638` — grounded on ref 5267, whose real title (recovered from its own
  first chunk, 2026-08-20) is *Limits of economy and fidelity for programmable
  assembly of size-controlled triply periodic polyhedra* — geometric assembly,
  not conductive MOFs.

The repair pass must report `WRONG_SOURCE` and stop, not grind.

#### 3. `ADJACENT_CHUNK` — check `ord ± 1` before ever scoring PARTIAL

A chunk boundary is not a grounding defect. `fi176643` scored PARTIAL only
because an abstract split across two chunks: 428885 ends at *"superior to that of
metals"* while **428886** carries *"five times stronger and half the weight …
retaining 80%"* verbatim. Every element was correct. The verifier nearly
mis-scored it from a truncated dump.

**Always read the neighbouring chunks of the same ref before scoring anything
below SUPPORTED.** This is a false-PARTIAL generator and it is cheap to rule out.

#### 4. `NEEDS_SECOND_EDGE` for comparative claims on one-sided sources

`fi176659`, `fi176660`: the source measures the subject but never the baseline,
so the comparison is unsupported. Folding these into UNSUPPORTED hides that the
repair is *adding an edge*, not fixing one.

#### 5. Widen `misattributed` from techniques/quantities to **named entities**

`fi176448` swaps a reagent — source says TMB/DAB/OPD, sentence says TMB/**ABTS**/
OPD, and `ABTS` returns zero rows across the paper. Caught only because the
verifier stretched the definition. Materials, molecules, reagents and instruments
all belong in scope.

#### 6. Two traps to warn verifiers about explicitly

- **The number belongs to the comparison device, not the subject.** `fi176436`'s
  "30 ms" is the MEMS comparator's, not azobenzene's. Mis-scores in both
  directions.
- **A second corruption mode exists**, unrelated to Greek: chunk 395239 renders
  `±` as `(`. Do not assume Greek-drop is the only extraction scar.

#### Also surfaced: nothing checks claims against each other

`fi176623` says single-crystal Ni₃(HITP)₂ is metallic; `fi176640` says it is a
bulk semiconductor. Both live, both in the same draft's cohort. Every check we
have compares a claim to a *source*; none compares claims to **each other**.
That is a distinct detection lane and it is cheap — the hubs are already
embedded. Worth building after this pass.

#### Distinguish "no support found" from "source not fully ingested"

`fi176401`'s NOT_FOUND is an ingest limit: all 21 chunks of that ref are
supplementary material and the main text is absent. That is `UNDECIDED`, not
counter-evidence. Verifiers must check whether the source is *completely*
ingested before reading silence as absence.

### Gate wiring — the rubric's approve-time consumer (2026-08-27)

An external review of the staged candidate queue independently re-derived
this rubric's taxonomy (PARTIAL severity split, `WRONG_CHUNK`/`WRONG_SOURCE`/
`MISATTRIBUTED`/`NEEDS_SECOND_EDGE`) and added the requirement the rubric so
far only implies: **approval must check claim-level coverage, not edge-level
support.** Its 30-hub sample: 20 had a `partial` edge, only 2 were
all-`yes`; ~7 material mis-groundings (fabricated range, wrong source,
inference-as-result). The failure the field-containment gate cannot see: a
single `partial` quote releasing an unsupported conjunction.

Wire, at approve time (`nanopub/gates.py`, payload-dependent — it has the
quote envelope the mint-time gates lack):

- Every atomic proposition of the sentence must be covered by the **union**
  of selected passages; multiple complementary quotes are allowed
  (fi191120/fi191293 shape: different passages cover different clauses).
- `PARTIAL_MINOR` passes; `PARTIAL_MATERIAL`/`PARTIAL_FABRICATED` block.
- Verdicts stamp `verified_claim_sha` so a claim edit re-opens coverage
  (the edge-level plumbing shipped 2026-08; this extends it to the
  approval payload).

Run the review's pilot before scaling: 10–20 claims, human-reviewed,
including complementary-partial cases.

### Method caveat, unchanged

Verdicts are LLM judgments, advisory and unreviewed. The nanobud audit that
preceded this had **three** errors found on verification, including two
"contradictions" that were both wrong in opposite directions. Treat output as
leads. Nothing is written from this file without reading the source.
