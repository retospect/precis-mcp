---
status: draft
title: "367 evidence edges record support:yes while anchoring no passage — a bounded July batch, and the reason a ZIF-8 paper 'supported' an HKUST-1 claim"
---

# A verdict with nothing under it

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

## Why it matters more than a missing pointer

Every gate passes on these edges. The edge exists, the source is primary, the
claim has evidence with `support: "yes"`. Nothing anywhere asks whether the
thing that produced the verdict ever read anything.

This is the mechanism behind `fi177486` — *"HKUST-1 has a Young's modulus of
approximately 9 GPa"* — grounded, with `support: "yes"`, to ref 4246, *Metal-
Organic Framework ZIF-8 Films As Low-κ Dielectrics in Microelectronics*. A
different material, a different property, no passage. The claim looked supported
to every automated check we have.

## It is bounded, and it is not ongoing

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

## Concentration

361 of the 367 are in the `dr42995` (boxel draft) cohort. That is not because
the draft is special — the July batch is simply what built that draft's
evidence. Any other draft built by the same batch would show it too.

## Repair — the tool is built; the run has not happened

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

### Blocker, found 2026-08-21: it cannot be run from an interactive SSH session

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

## Repair mechanics — three findings that change the obvious implementation

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

## Origin — one hypothesis refuted, one open

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

## The verdict is the part to distrust

A re-grounding pass that finds no supporting passage in a source whose edge says
`support: "yes"` has not failed — it has discovered that the verdict was empty.
Record `verify-rejected` and leave the claim alone. **Do not edit a claim to
match a source that a passage-less edge merely asserted.** The edge is the thing
that was wrong.
