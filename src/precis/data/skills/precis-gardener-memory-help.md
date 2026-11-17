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

Three read-only detectors pin evidence to the memory's content SHA and emit
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
A tick scans up to ten memories within the shared proposal budget.
Each memory gets one classifier call with
at most 60 body spans, 16,000 span characters, and 12 candidate citation
links. A router pause defers the tick with its audit window available for retry.
Failed or incomplete classifier replies leave the same SHA eligible on a later scan.

The review ledger stores a `proposed` row per audited memory revision with
actor `garden-grounding`, content SHA and a `grounding_audit` note. The
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

Read `classifier_reported_claims` and `inspected` as counts of anchored
classifier entries and
`provisional` as suggested support awaiting verification. `unresolved`
counts identified claims with open support or term questions.
`spans_unchecked` counts omitted or unclassified inputs. `unaccounted_ranges`
gives body offsets of scanned text outside the reported excerpts and
structural spans, plus the ranges of claim sentences the scan bounds
omitted — exactly which text went unchecked. Exact claim totals and unchecked claim counts remain null because a
classifier can miss assertions even inside an excerpt. Read
`truncated`, `spans_omitted_by_limit`, and `candidate_links_truncated` for
the bounds reached. Missing material leaves support unknown.
`all_grounded` stays false in this audit-only pass.

## Notation flags

Memory text follows [[precis-notation-canon]]. There is no `notation`
proposal: the flag is the system-owned `AUDIT:notation` tag that
`put`/`edit(kind='memory')` set on a title/body that breaks the canon.
`search(kind='memory', tags=['AUDIT:notation'])` lists the flagged nodes.
A pass that touches a flagged node normalizes it by anchored edit —
`edit(kind='memory', id='me…', find='<old>', text='<new>')`, one spot at a
time — never a whole-body rewrite and never a script. Quoted spans, code
spans and `$$…$$`/multi-line derivations are exempt and stay
byte-identical; `C60`-style formulas are never flagged (the detector folds
`C60` to `C₆₀`, and many matches are nomenclature like `B3LYP`). A clean
rewrite clears the tag; a canon that does not fit the text is a gripe, not
a spelling improvised in the node.

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
body or any pending retire proposal. The evaluator's plan accounts for every
detector sentence: keep on a kept shell, move into a related node (hub
family, qualifies targets, `+recall` neighbours, restated_claim matches),
mint an atomic child, or drop only as `history`/`restatement`. Both
retirement doors refuse while a compact proposal is pending or while the
detector fires on content no applied compaction covers.
