---
id: precis-gardener-memory-help
family: memory
title: precis — the graph gardener's memory detectors
summary: opt-in memory anchor, restated-claim, review-debt, title and undated-absence checks that file review-ledger proposals
answers:
  - how does the gardener check memory anchors?
  - how does the gardener detect restated memory claims?
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
current. Rewrites remain proposal-only, with no apply path.

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
runs it. Alongside `restated_claim`, three read-only detectors pin evidence
to the memory's content SHA and emit proposals only; DREAMs remain
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
