---
id: precis-gardener-memory-help
family: memory
title: precis — the graph gardener's memory detectors
summary: opt-in memory anchor checks and restated-claim detection that file review-ledger proposals
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
