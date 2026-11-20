---
id: precis-gardener-memory-help
family: memory
title: precis — the graph gardener's memory detectors
summary: what the gardener's memory checks flag (anchor rot, restated claims, review debt, titles, undated absence, source drift, entity state, host facts), the actions a reviewer may take, and notation flags
answers:
  - how does the gardener check memory anchors?
  - how does the gardener detect restated memory claims?
  - how does the gardener detect changed memory sources and entity state?
  - what do the memory and memory-review checks flag?
  - how do I audit memory claim coverage?
  - what does a reviewed memory supersede or retire do?
  - how do I fix a memory flagged AUDIT:notation?
applies-to: get/search (kind='memory'/'job'), reviews ledger
tags: [workflow]
kinds: [memory, job]
status: active
---

# precis-gardener-memory-help — memory detectors

Gardener proposals and the review ledger: [[precis-gardener-help]]. Every
memory detector reads DREAM memories as duplicate/merge-only and never
proposes a rewrite for them.

## Memory anchor checks

`anchor_rot` checks Python file/symbol anchors and dotted qualnames,
scripts/docs paths, Markdown sections and fragments, and mixed
letter-and-digit commit SHAs of at least seven characters, against the
committed `main`. Every proposal pins the memory's SHA and the main commit,
names each failed anchor, and records the lookups tried. Working-tree edits
do not affect that evidence.

Each memory checks at most 20 anchors. Anchors that cannot be verified are
recorded `not_checked`, never as rot evidence; a body with only unverifiable
anchors produces no proposal. The evaluator distinguishes rename, removal
and already-recorded history; a live SHA proves existence, not that the
surrounding claim is current.

## Restated memory claims

`restated_claim` compares sentences across live memories inside one
`section:index` hub's `part-of` subtree. Title/heading lines, `Source:`,
`Left:`, `Meta:` and sentences shorter than eight words are excluded.
Whole-body duplicates are merge candidates instead. Both spans and both
node SHAs are pinned. The evaluator checks identical meaning, scope, time
and polarity, then proposes one keeper: reference before gotcha before
thread, lower ref id for ties. The `rewrite` replaces only the redundant
sentence with `See me<keeper>.`; surrounding facts remain. These proposals
are never applied automatically. The scan is bounded: not every sentence
pair in a large hub is compared.

## Memory review checks

Four read-only detectors pin evidence to the memory's content SHA and emit
proposals only:

- `review_due` flags expired, never-reviewed, SHA-changed,
  checklist-version-changed, then aged memories. A current review is a
  `kg-review` trust act with matching `expected_sha` and the current
  prompt version; unversioned stamps need review; demotion invalidates a
  stamp. Reference/gotchas age after 30 days, runbooks after 60;
  workflow/rulings use `meta.review_by` until reviewed on or after that
  date. Threads have no age review.
- Expiry defaults use section tags plus title, computed at read time from
  `updated_at`: threads/handoff/resume/"state on"/"as of" get 14 days;
  incident/campaign/date-range/"waves N-M" write-ups get 90. Ordinary
  reference/gotchas/runbooks have no expiry. `meta.expires` overrides the
  default (null disables it); malformed dates are `not_checked`. Expiry
  never deletes or retires. Incident candidates suggest condensing to a
  dated cause, verified fix and open uncertainty, never inventing a fix.
- `title_shape` flags slug/path titles, titles over sixteen words, and
  dated snapshots on thread nodes. A replacement must state the body's
  supported conclusion.
- `negative_undated` flags absence wording ("no X exists") without a date on
  that line. No search has verified the claim; `not_checked` records that
  limit.
- `notation` flags the first authored-prose span that breaks
  [[precis-notation-canon]] — one anchored fix per memory per pass,
  skipping quoted spans, code spans and `$$…$$`/multi-line math (the
  `Notation flags` section below). The evaluator supplies the
  canon-correct replacement for the flagged token or asks for human review
  on TeX residue and verbatim LaTeX sections. The detector skips
  `formula-ascii-subscript`.

`restated_claim`, `source_drift` and `entity_claim_stale` belong to the same
review group.

## Claim-level grounding audit

Run an audit tick when the gardener is enabled and outside cooling.
Like every hand-submitted job it needs a parent — the todo it runs for:

```python
put(kind='job', parent_id=<todo ref id>,
    args={'job_type': 'graph_garden',
    'params': {'pass': 'memory-grounding'}})
```

`memory-grounding` is opt-in and excluded from `all`. Audit-only jobs skip
the proposal consumer. The audit skips DREAMs and human-origin holds.
A tick scans up to ten memories within the shared proposal budget — the
audit row and its repair proposals all spend `max_proposals`, and repairs
are capped at what the budget still holds.
Each memory gets one classifier call with
at most 60 body spans, 16,000 span characters, and 12 candidate citation
links. A router pause defers the tick with its audit window available for retry.
Failed or incomplete classifier replies leave the same SHA eligible on a later scan.

The review ledger stores a `proposed` row per audited memory revision with
actor `garden-grounding`, version `grounding.v6`, content SHA and a
`grounding_audit` note. The
row is audit-only and outside the pending action queue —
`get(kind='job', id='/garden/proposals')` intentionally excludes it. Read
it back with `precis.graph.garden.grounding.latest_audit(store, ref_id)`
in Python, or on prod:

```
scripts/prod-psql --ro "SELECT review_id, content_sha, at
  FROM reviews WHERE actor = 'garden-grounding'
    AND target_kind = 'ref' AND target_id = <memory ref_id>
  ORDER BY review_id DESC LIMIT 5"
```

Each claim gives body offsets, subject, scope, kind, evidence or
premises, and term flags. The classifier records meaning and existence
as distinct claims. Chat — a `chat:`/`conv:` reference or a `co`/`cc`/`ag`
handle — can support attribution or a decision, never an empirical claim.
Read `classifier_reported_claims` / `inspected` as anchored-entry counts,
`provisional` as suggested support awaiting verification, `unresolved` as
claims with open support or term questions, and `spans_unchecked` /
`unaccounted_ranges` (with `truncated`, `spans_omitted_by_limit`,
`candidate_links_truncated`) for exactly which text the bounds left
unchecked. Exact claim totals and unchecked claim counts stay null — a
classifier can miss assertions even inside an excerpt. `all_grounded`
stays false in this audit-only pass.

## Grounding verdicts, budgets and repairs

Evidence-bound judgments: for empirical / source_report claims with
resolvable citations, the shared contextual evidence reviewer verifies the
claim against the cited passages (batched, at most 5 claims and 8 passages
per memory) and records `claim.support` — source fidelity separate from
evidential strength, study type, caveats, quantity/condition checks and
the identity of the passage the judgment anchored at. A faithful report of
a weak or simulated paper is never `supported`; a claim that outruns its
passage is `partial` (single-hop overreach); a passage that merely
restates another work's result is flagged as source copying; a reviewer
outage is `unresolved`, never `unsupported`. Definitions and decisions
never support existence.

Premise lineage (`rollup`, with `lineage` counts) then rolls each claim up
to `supported` / `partial` / `unsupported` / `contradicted` / `unresolved`.
Exact assertion quotes identify premises within the same memory. Missing
or ambiguous quotes remain unresolved. External memory premises use their
current audit judgments. Other citation handles without a premise judgment
remain unresolved. A supported inference needs a separate warrant judgment
and support for every necessary premise. The audit records
`inference_depth` as inference steps and flags deep derivations for review.
Local cycles and cross-memory cycles detected within four audit hops ground
nothing. Evidence handles compare by root ref, so two chunks of one paper
count as one source. `stale_sources` lists evidence whose SHA moved since
the previous audit pinned it in `evidence_shas`. A stale source caps the
claim at `partial` and sets `needs_source_review`.

Passage-verified `supported` empirical claims get a bounded conflict probe
(`conflict`, at most 2 claims per memory, 6 verify calls each, ranked by
the conflict sweep's `read_first`/distance selection): `contradicted`,
`none_found`, `unknown`, or `not_probed` (no embedder wired, ineligible claim or
probe budget spent). `none_found` records scope (method version,
kinds, queries, UTC date), the checked passages' identities and unknowns —
never proof of support, and never reported after a retrieval or verify
outage (those are `unknown`).

Pending repairs: `rollup` of `unsupported`/`contradicted` mints a `rewrite`
proposal, `partial` with verifier flags or a partial warrant mints `qualify`, and `unresolved`
with unresolvable premises mints `ask` — at most 3 per memory, each pinned
to its claim span and the audited revision (`payload.audit_sha`), through
the shared garden ledger with no apply door registered. They stay pending
for a human or reviewer; a rollup of `supported` is withheld (capped
`partial`) when the pipeline itself supplied or certified the evidence
(audit or garden-ledger actor on the evidence ref's reviews or revisions).

An `unresolved` claim carries `missing_check`. The coordinator can
commission three handoffs per tick and resumes deferred or failed writes
from the saved audit. It reuses a research todo when its claim, source
and check pins match. A new `grounding-handoff` todo records the exact
question, pins, stopping condition and return destination. Three
cumulative audit attempts exhaust the research handoff. Ambiguous terms,
identity or authority questions, and substantive decisions go to an
`ask-user` todo.

Return research through an ordinary todo comment starting with
`candidate-evidence:`. Report each completed check on a line starting
with `Checks performed:`. Close the research todo when work ends,
including when evidence is unavailable or disputed. A return prompts
re-audit of the same memory SHA as candidate input. The audit can remain
unresolved and every suggested finding stays provisional.

## Notation flags

Memory text follows [[precis-notation-canon]]. The `memory-review` pass's
`notation` detector (above) surfaces existing violations: one anchored
proposal per memory per pass, fixing one spot at a time. The writer-side
flag is the system-owned `AUDIT:notation` tag that `put`/`edit(kind=
'memory')` set on a title/body that breaks the canon.
`search(kind='memory', tags=['AUDIT:notation'])` lists the flagged nodes.
Apply a reviewed correction at the flagged spot with
`edit(kind='memory', id='me…', find='<old>', text='<new>')`. Keep quoted
spans, code spans and `$$…$$`/multi-line derivations byte-identical.
A clean rewrite clears the tag. File a gripe if the canon does not fit
the text.

## Memory source and entity checks

`source_drift` compares a `Source:` line or `cites` edge's `docs/... §
heading` section before the memory's last touch with committed main.
Evidence carries both section-content SHAs, the two commit pins, and an
excerpt of the new section, including its child blocks.

`entity_claim_stale` checks entity handles in state sentences (`is retired`,
`is the only`, `runs`, `has no`, `is live`). It compares recorded content,
machine role or STATUS changes after the memory touch. Stored entities need
recorded history: many kinds do not retain revisions, and a newer timestamp
alone does not establish a change. A missing prior SHA, role or STATUS is
`not_checked`, never evidence of staleness.

Each memory checks at most 20 matches. A candidate pins the memory's SHA and
its source evidence; missing-history entries accompany verified changes in
`not_checked`. Unverifiable-only bodies produce no proposal. The evaluator
compares the claim with the new text and rejects unchanged meaning or dated
history. Both detectors propose rewrites only.

## Host facts

`host_facts` scans live memory bodies for sentences naming a machine in the
census with a state verb and neither an ISO date nor a machine handle:
current-state claims. The one-fact-one-ref rule: a host's current
role/status lives on the `machine` ref ([[precis-machine-help]]); a memory
keeps dated events plus the `ma_<host>` handle. A hostname matches as a
whole word, and a sentence already carrying a machine handle or an ISO date
is a dated event, not a current-state claim. One proposal per memory carries
anchored rewrite ops (`text=null`; the evaluator writes the dated-event-plus-
handle replacement, verified against the machine ref's current role fact)
and, for each host whose memory is newer than the ref's fact, the
`put(kind='machine', id='<host>', args={'role': …})` that moves the verified
fact onto the ref — the reviewer fills the role from the verified current
truth, never from the stale sentence.

## The memory-body evaluator

Memory-body candidates are judged as bodies, not structural pairs. The
evaluator reads the target node's `fisheye+1hop`, the detector's evidence,
and the memory-body checklist of [[precis-fix-memory-gripe-help]] — the
per-type checks, the disagreement shapes and the standard of done, the same
sections the hand repair path reads.

It answers with exactly one action: `rewrite` (anchored find/text ops plus
reason), `qualify` (a caveat line, or a `qualifies` edge from a named
gotcha), `supersede` (by a named live node), `retire`, `no-change` (with
why), or `ask` — the only fallback when the fact needs a human. An `ask`
files a `memory-review` todo linked to the memory (`raises-concern-about`)
and writes no proposal.

## Reviewed memory actions

Before a memory action applies, the proposal must still be pending and
current, the node's pinned SHA must hold, and the detector predicate must
still hold. A stale proposal closes and a fresh detector run may replace
it. `md:` successors are refused. A `trust_origin='human'` or currently
`considered` node is held for a human, including when protection changed
after review. Unresolved rewrite ops (`text=null`) get no reviewer call.
The `memory-review` detectors stay proposal-only.

A `dangling_mention` rewrite whose every op is a bare handle swap onto a
verified duplicate successor (a merge-made redirect whose resolved
successor is the tombstone's recorded merge winner, or a `supersedes` edge
to a node with the same normalised title) applies without review.

An approved rewrite applies anchored edits with
`reason='garden:<detector>:<proposal id>'`, recording the
`kg-review:<model>:<UTC date>:fixed` trust stamp. Each applied `find` and
`span` must match a freshly detected op; the model supplies replacement
text. Link removals are refused because no rewrite detector justifies them.

An approved supersede writes the winner → loser `supersedes` edge, retires
the loser and stamps `meta.superseded_by` so ordinary `get` redirects to
the survivor. Retire uses the existing memory delete door. Both actions
require review that still-true claims and needed figures are already
preserved elsewhere; a payload instruction to preserve them later is not
enough.

## Compaction before retirement

The `compact` detector fires on expiry, 90-day-cold reads, a half-history
body or any pending retire proposal. Expiry is shielded, not blind: an
expired node tagged `ruling`, `reto-ruling` or `spec:*` (or in
section `rulings`/`specs`), or one a
live, non-terminal todo/gripe/quest links to — either direction — gets
no `expired` reason; when another trigger still fires, its evidence
notes what shielded it. `review_due` has no shield — the node still owes
its review. The evaluator's plan accounts for every
detector sentence: keep on a kept shell, move into a related node (hub
family, qualifies targets, `+recall` neighbours, restated_claim matches),
mint an atomic child, or drop only as `history`/`restatement`. Both
retirement doors refuse while a compact proposal is pending or while the
detector fires on content no applied compaction covers.
