# Running a claim-rewrite pass over the hub corpus

**When.** Any batch pass that rewrites live `TAPROOT:claim` sentences —
attributing a technique, regrounding an unlinked hub, repairing content
against sources. Written from the 2026-08-24 graduation campaign (td244964),
whose measured failure modes are what each rule below exists to stop.

## Preflight — the two checks that invalidate a whole run

1. **Prove semantic search works before dispatching anything.**

   ```python
   search(kind='paper', q='<a full natural-language sentence on a topic the corpus covers>', mode='semantic')
   ```

   Zero or incoherent hits means the embedder is mocked or down. The campaign
   ran ~700 rewrites and two audit layers against a `MockEmbedder` (hashed
   noise, valid vectors, random directions) because `PRECIS_EMBEDDER` was unset
   and defaults to `"mock"` — `embed_query` degrades to `None` on any embedder
   failure and logs at WARNING, so from the agent side a broken search is
   indistinguishable from an empty corpus (gr249198).

2. **Read live titles from prod, not from the proposal files.** Proposals get
   rejected by the validator or superseded by a correction; auditing a
   proposal audits text that is not in the database. Verify applied results by
   **equality against a fresh dump**, never by re-running the gate — the gate
   answers a different question.

## The output contract

Three columns, always:

```
fi<ID><TAB><rewritten sentence><TAB>pc<id>
```

The `pc<id>` is the chunk the agent actually read and judged from. A pass that
emits two columns throws away the only evidence that its own output is sound
(gr249569): the next audit must re-derive every claim's best passage from
scratch, and the hub ends up with an attributed technique and no way to ask
which passage said so.

Apply both halves together:

```python
edit(kind='finding', id='fi<id>', title='<sentence>')       # old pub_id kept as alias
link(kind='finding', id='fi<id>', rel='establishes', target='pc<id>')
```

A refusal takes the same shape and must be falsifiable:

```
fi<ID><TAB>SKIP: <reason><TAB><the queries you ran>
```

**Validate every proposal mechanically before applying it.** Run the corrected
sentence through `gates.py::check_claim_sentence` and discard anything that
fails: a repair that un-graduates a hub which currently passes is a net loss,
and agents produce these routinely. Also discard a proposal whose `pc<id>` is
missing, or whose sentence equals what is already live.

## Two constraints agents violate unless told

- **250 characters, hard** (corpus median 147). This is the most common
  failure in a *repair* pass specifically, because the natural way to fix a
  claim is to append the qualifying detail — which lands at 300+ and trips
  `over-long`. Repair by replacing the wrong element, not by accreting
  caveats. A claim that cannot be made correct inside 250 characters is
  carrying more than one assertion; propose the single-assertion core.
- **Ground on the primary, not a review.** A passage that attributes the
  finding onward ("…has been implemented for X [37,38]") is testimony, and a
  quote carrying citation markers fails the hearsay gate whatever section it
  sits in (`_check_passage`). When the corpus holds only reviews citing the
  work, there is no repair to make — the honest output is "the primary is not
  held", filed as a paper to acquire. An agent told only "find a passage" will
  cheerfully ground on the review.

## Rules for the brief

- **Search the corpus, not the linked set.** Every pass can only read what is
  already attached unless you tell it otherwise, and it will inherit that as
  the boundary of the knowable. Measured: of hubs no pass could repair, 83%
  had a usable passage sitting in the corpus, unlinked; none failed for lack
  of a source (td249196).
- **Fetch the chunk; never judge from a search excerpt.** Excerpts clip
  mid-table. The dominant content error is not invention but a real number
  attached to the wrong thing — wrong phase, wrong species, wrong device,
  wrong end of a range, typical read as worst-case (td249939).
- **Never invent a technique**, and never stretch a topically-related hit into
  a grounding. A wrong link is worse than none.
- **One axis per pass.** Say explicitly what is out of scope. Agents asked
  about technique will volunteer content defects, which reads like data but
  has no denominator — the campaign's "~13% content errors" was exactly this,
  and only became a real number when asked directly with a fixed sample.

## Correction layers

A layer that overrides a first opinion needs a higher bar than the thing it
overrides — otherwise it degrades the corpus. Measured over the campaign's 12
overrides, judged blind: 4 wrong, 2 right, 6 no-op. **Twice as likely to break
a claim as to fix one**, because auditors with a broken search converted
"found nothing" into "no source supports this" and had unilateral authority to
act on it (td249766).

Therefore:

- A `SKIP:` on "no source names a technique" is **not accepted** without the
  queries that were run, from a run that passed preflight check 1.
- Before any correction ships, **blind A/B it**: show an agent both versions
  without saying which is live, ask which the corpus supports, keep the key
  out of band. Twelve pairs cost one agent. Told which is live, agents defer
  to the status quo and the check is worthless.
- **The blind pass has veto power, and a clean wave does not earn skipping it
  next time.** Wave 1 of the repair pass scored 23 right / 0 wrong on 24 pairs;
  wave 2, same brief and same model, vetoed 4 of 24 — corrections that would
  have replaced a correct live sentence. Drop the vetoed ids from the apply
  file and keep them in a sidecar so the decision stays auditable.
- Blind A/B controls for status-quo deference. It does **not** control for two
  passes misreading the same source the same way, and agents do converge:
  four separate batches independently over-corrected "rotaxane" to
  "pseudorotaxane" from one paper's caveat about a single device, and the
  judges rejected it.

## A propose-only pass is not propose-only until you diff the edge table

An investigation pass told twice — in its brief and in its dispatch prompt —
to propose only and write nothing reported "No database edits were made" and
had written five evidence edges, to claims that were being held for author
review. The written edges did not match the proposals it reported.

So: snapshot `links` for the target refs before dispatch and after, and diff.
The agent's own account of what it did is not evidence, for the same reason
its account of what a source says is not evidence.

When stray writes turn up, **judge them on merit before reverting**. Four of
those five were good and two were better targeted than what the agent had
reported proposing; one was not, and is the shape to watch for — an
unsourced number given a grounding edge to a passage that does not contain
it. A wrong claim carrying a citation is harder to catch than a wrong claim
carrying none, because every downstream check reads it as verified.

## Sampling

Draw the audit sample from claims the previous audit did **not** read — an
agent that has seen a claim is not a fresh judge of it — with a fixed seed
recorded in the builder script. Report the denominator with the rate.

## What the gates do and do not cover

`nanopub/gates.py::check_claim_sentence` blocks on sentence *form*: epistemic
mode, evidence verb, falsifiability, notation, length. It reads no numbers and
checks nothing against a source. Graduation means "safe to cite," not
"correct" — approve, sign, and signoff stay human, and a gate-clean corpus can
still carry a 10⁶ unit error (fi176705, live and lint-passing).

The one deterministic content check worth adding: flag a claim whose numerals
appear in **no** linked chunk. It is cheap, and catches both the fabricated
quantity and the value that exists only as a trend in the source.
