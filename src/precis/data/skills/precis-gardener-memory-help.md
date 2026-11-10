---
id: precis-gardener-memory-help
family: memory
title: precis — the graph gardener's memory detectors
summary: opt-in memory anchor, restated-claim, review-debt, title, undated-absence, source-drift and entity-state checks that file review-ledger proposals
answers:
  - how does the gardener check memory anchors?
  - how does the gardener detect restated memory claims?
  - how does the gardener detect changed memory sources and entity state?
  - what do the memory and memory-review passes check?
  - can the gardener apply a memory rewrite proposal?
applies-to: get/search (kind='memory'/'job'), reviews ledger
tags: [workflow]
kinds: [memory, job]
status: active
---

# precis-gardener-memory-help — memory detectors

Gardener passes, review ledger and apply paths: [[precis-gardener-help]].

## Memory anchor checks

The opt-in `memory` pass includes `anchor_rot`: Python file/symbol anchors
and dotted qualnames, scripts/docs paths, Markdown sections and fragments,
and mixed letter-and-digit commit SHAs of at least seven characters.
It reads the configured Python `main` root's local `main` commit, using
committed blobs for the Python index and Markdown heading chunks. Every
proposal pins `base_sha` and `main_sha`, names each failed anchor, and
records the lookups tried. Working-tree edits do not affect that evidence.

Each memory checks at most 20 anchors; `matches_capped` says more remain.
Unavailable git, indexes, or incomplete history produce `not_checked`
entries alongside verified failures, never rot evidence. A body with only
unverifiable anchors produces no proposal. DREAMs and retired bodies are
excluded. The evaluator distinguishes rename, removal and already-recorded
history; a live SHA proves existence, not that the surrounding claim is
current. The reviewed rewrite door re-verifies the evidence before applying.

## Restated memory claims

The separate opt-in `memory-review` pass runs `restated_claim`; both `all`
and `memory` exclude it. It compares sentences across live memories inside
one `section:index` hub's `part-of` subtree, stopping at nested hubs. DREAMs,
retired nodes, title/heading lines, `Source:`, `Left:`, `Meta:` and sentences
shorter than eight words are excluded. Whole-body duplicates stay with `dup_pair`.

Each tick visits one hub window, embedding at most 128 sentences and taking
at most 20 per memory. The configured embedder must match the registry default;
there is no model fallback or stored-vector fill. Unavailable embedding records
`not_checked.restated_claim=embedder_unavailable` in the tick/job payload,
produces no candidate, and leaves the cursor unchanged for retry.

Cosine similarity of at least 0.9 or content-word overlap of at least 0.7
is a candidate signal. Both spans, cosine distance, overlap and both node SHAs
are pinned. Each pair carries at most 20 matches, marked when capped. The
evaluator checks identical meaning, scope, time and polarity, then proposes
one keeper: reference before gotcha before thread, lower ref id for ties.
The `rewrite` replaces only the redundant sentence with `See me<keeper>.`;
surrounding facts remain. These proposals have no apply or auto-apply path.
Windows resume by hub and memory; a filled proposal limit resumes pairs in
the same window. This is a bounded scan, not every sentence pair in a large hub.

## Memory review checks

`memory-review` is a separate opt-in pass: neither `all` nor `memory`
runs it. Alongside `restated_claim` and the source/entity checks below,
three read-only detectors pin evidence to the memory's content SHA and
emit proposals only; DREAMs remain
duplicate/merge-only.

- `review_due` sweeps expired, never-reviewed, SHA-changed,
  checklist-version-changed, then aged memories. A current review needs
  a `kg-review` trust act with matching `expected_sha` and
  `payload.prompt_version = MEMORY_PROMPT_VERSION`; unversioned stamps
  need review; demotion invalidates a stamp. Reference/gotchas age after
  30 days, runbooks after 60; workflow/rulings use `meta.review_by`
  until reviewed on or after that date. Threads have no age review.
- Expiry defaults use section tags plus title, computed at read time
  from `updated_at`: threads/handoff/resume/"state on"/"as of" get 14
  days; incident/campaign/date-range/"waves N-M" write-ups get 90.
  Ordinary reference/gotchas/runbooks have no expiry. `meta.expires`
  overrides the default (null disables it); malformed dates are
  `not_checked`. **Put-side expiry metadata and ack printing are not
  implemented by this pass.** Expiry never deletes or retires. Incident
  candidates suggest condensing to a dated cause, verified fix and open
  uncertainty after verification, never inventing a fix.
- `title_shape` flags slug/path titles, titles over sixteen words, and
  dated snapshots on thread nodes. A replacement must state the body's
  supported conclusion; no title is invented during detection.
- `negative_undated` flags absence wording without a date on that line.
  Up to 20 lines carry SHA-pinned spans and content terms as a grep brief.
  No search has verified the claim: `not_checked` records that limit.

Each detector has its own cursor and the normal deadline. `review_due`
also saves a priority cursor, walking each reason before the next so
limits cannot skip lower-ID memories. `scan_cap` bounds inspected IDs
across those priority walks. Its version/expiry/type checks supersede the
generic `considered_days` skip. These candidates have distinct proposal
keys and use the memory prompt version; the legacy prompt is unchanged.

## Memory source and entity checks

The opt-in `memory-review` pass also runs `source_drift` and
`entity_claim_stale`; neither `all` nor `memory` includes them.
`source_drift` compares a `Source:` line or `cites` edge's
`docs/... § heading` section before the memory's last touch with committed
main. Evidence carries both section-content SHAs, the two commit pins,
and an excerpt of the new section, including its child blocks.

`entity_claim_stale` checks entity handles in state sentences (`is retired`,
`is the only`, `runs`, `has no`, `is live`). It compares recorded content,
machine role or STATUS changes after the memory touch. Markdown sections,
Python symbols and file-backed skill contracts use committed Git history.
Stored entities need recorded history: many kinds do not retain revisions,
and a newer timestamp alone does not establish a change. A missing prior
SHA, role or STATUS is `not_checked`, never evidence of staleness.

Each memory checks at most 20 matches; the separate `source_drift:memory`
and `entity_claim_stale:memory` cursors, limits and dispatcher deadline
bound the scan. DREAMs are excluded. A candidate pins `base_sha`
and its source evidence; missing-history entries accompany verified changes
in `not_checked`. Unverifiable-only bodies produce no proposal. The evaluator
compares the claim with the new text and rejects unchanged meaning or dated
history. Both detectors propose rewrites only; neither adds an apply path.

## Host facts

The opt-in `host-facts` pass (gr560767) runs `host_facts` after the
operator refreshes the machine census — `put(kind='machine', mode='sync')`
plus hand-added hosts that never heartbeat, with `role`/`gpu` set by hand;
see [[precis-machine-help]]. Only live `machine` refs are matched: a
hostname matches as a whole word (`ma_spark` and `sparkle` never match
`spark`), and a sentence already carrying a machine handle or an ISO date
is a dated event, not a current-state claim — the scan never re-flags its
own output. One proposal per memory carries at most 20 anchored rewrite
ops (`text=null`; the evaluator writes the dated-event-plus-handle
replacement, verified against the machine ref's current role fact).

The one-fact-one-ref rule: a host's current role/status lives on the
`machine` ref; a memory keeps dated events plus the `ma_<host>` handle.
So each proposal also names, per host whose memory is newer than the
ref's fact, the `put(kind='machine', id='<host>', args={'role': …})` that
moves the verified fact onto the ref — the reviewer fills the role from
the verified current truth, never from the stale sentence. The reviewed
rewrite door re-runs this detector's predicate before applying; a census
fact change produces a new `evidence_sha` and a fresh proposal. The spark
case (a retired claim versus the ref's compute role) is the fixture.

## The memory-body evaluator

Memory-body candidates are judged as bodies, not structural pairs
(gr560762). The prompt carries the target node's `fisheye+1hop`, the
detector's evidence, and the memory-body checklist pinned verbatim from
[[precis-fix-memory-gripe-help]] — the per-type checks, the disagreement
shapes and the standard of done, the same sections the hand repair path
reads. Prompt code holds only the one-action output contract.

The evaluator answers with exactly one action: `rewrite` (anchored
find/text ops plus reason), `qualify` (a caveat line, or a `qualifies`
edge from a named gotcha), `supersede` (by a named live node), `retire`,
`no-change` (with why; closes the key under the current memory prompt
version, as a proposer reject does), or `ask` — the only fallback when
the fact needs a human. An `ask` files a `memory-review` todo linked to
the memory (`raises-concern-about`), its `meta.llm_tier` set by smell
class: content smells that need repo/entity archaeology dispatch on
`opus`, shape smells on `sonnet`. The rejection row closes the key under
the current prompt version, so an asked candidate is not re-billed each
tick. `qualify` and `ask` proposals stay outside the consumer; an `ask`
writes no proposal at all.

## Reviewed memory actions

The memory apply gate defaults **off**. While off, the consumer
leaves `rewrite`, `supersede` and `retire` proposals pending without a
reviewer call, write or row-state change. Enabling the flag admits only
detectors with an apply-time re-check (`dangling_mention`, `anchor_rot`,
`thread_shape`, `compact`, `grab_bag`, `griped`, `host_facts`) to the
existing bounded Opus review pass; it does not bypass review. The sampled
reviewer and Fable cross-check remain non-terminal. One cell needs no
review (gr564554): a `dangling_mention` rewrite whose every op is a bare
handle swap onto a verified duplicate successor — a merge-made redirect
whose resolved successor is the tombstone's recorded merge winner, or a
`supersedes` edge to a node with the same normalised title — applies with
no model call, recorded `garden.applied='mention'` with the before/after
spans and the undo `rewrite_id`. A redirect chain that walked past the
merge winner, and a supersedes to different content, stay reviewed.

Before a memory action applies, its door checks that the proposal is
still pending and current, the node's pinned SHA holds, and the detector
predicate still holds. Its action must match the fresh candidate's action:
a proposer cannot replace a supersede with a retire. An exact-evidence
mismatch closes as `apply_refused`; changed evidence or target content closes
as retryable `stale`, with the same check at apply. `md:` successors are
refused: their source content cannot yet be shown and pinned for review.
A `trust_origin='human'` or currently `considered`
node is held for a human (`garden_review`, `held='human'`), including
when protection changed after review.

Unresolved rewrite ops (`text=null`) get no reviewer call. After one day,
mixed proposals close retryably and re-propose only resolved ops for review;
wholly unresolved proposals hold as `unresolved` for evidence repair.
Legacy proposals lacking supporting ref SHA pins close before review so a
fresh detector run can replace them. Stale evidence permits a fresh proposal;
deterministic apply refusals close the unchanged key. Operational database
errors reuse the saved approval within the shared review budget. Five failed
apply attempts, recorded across ticks, hold as `apply_error` with the last error.
If saving approval itself fails, nothing applies; a later tick may pay again,
at most once per proposal per tick and within the consecutive-error cap.
Other unexpected apply errors hold the
proposal for a human. The proposal listing shows the hold reason;
after fixing an `apply_error` or `unresolved` hold, rejecting the proposal reopens
the unchanged key for a fresh proposal. Failed hold writes count as errors
without aborting the rest of the tick.

The six `memory-review` detectors remain proposal-only: even with the gate
on, the consumer leaves them pending, unreviewed and unchanged because
they have no apply-time detector checks. `qualify` and `ask` stay outside
consumer review.

## Compaction before retirement

The `compact` detector fires on expiry, 90-day-cold reads, a
half-history body or any pending retire proposal. The evaluator's plan
accounts for every detector sentence: keep on a kept shell, move into a
related node (hub family, qualifies targets, `+recall` neighbours,
restated_claim matches), mint an atomic child, or drop only as
`history`/`restatement`. A reviewed plan applies only through
`precis garden apply <review_id>` — the consumer never auto-applies it.
Both retirement doors refuse while a compact proposal is pending or
while the detector fires on content no applied compaction covers.

## Memory undo

An approved rewrite applies anchored edits with
`reason='garden:<detector>:<proposal id>'`, recording the
`kg-review:<model>:<UTC date>:fixed` trust stamp in the same transaction.
Its `meta.garden_rewrite` records the proposal, prior SHA and body chunk,
body bytes, outgoing links and incoming chunk links. Each applied `find` and
`span` must match a freshly detected op; the model supplies replacement text.
Mention synchronization cannot silently drop provenance or route links:
link removals are refused because no rewrite detector justifies them.
Undoing the rewrite restores the prior body byte-for-byte and
the saved links, then rejects the key on the restored content so the next
tick cannot apply it again. Anchor-rot undo remains blocked across main commits
while the restored target content is unchanged.

An approved supersede writes the winner → loser `supersedes` edge,
retires the loser and stamps `meta.superseded_by` so ordinary `get`
redirects to the survivor. Retire uses the existing memory delete door.
Both save `meta.garden_retirement`: prior metadata, every incident link
(including chunk ends, metadata and setter), successor links and endpoint pins.
Undoing a supersede or retire restores the node and
links, reverses moved edges and removes the newly written supersedes trail.
Changed successors or affected link ends refuse undo with a repair next step;
unrelated successor links are left alone. An undone removal blocks every removal
route of that node, including a merge losing it, until its content changes.
In strict mode, restoring a legacy link stamps it from version zero to one while
preserving its original origin and confidence; the result reports `links_restamped`.
Both actions require review that still-true claims and needed
figures are already preserved elsewhere; a payload instruction to preserve them later is not
enough. Applied rows remain `approved` plus `garden.applied`, with no
new verdict or table.
