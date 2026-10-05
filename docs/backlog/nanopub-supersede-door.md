---
status: in-progress
pillar: memory-graph
---

# Nanopub changes: signing is the boundary

## Reto ruling, 2026-10-05 / R14

Before signing (candidate/reviewed, internal), edit the claim in place: retain
its identity/links and mark every consumer for re-review. No supersede/version
row is created. Once signed (including anchored/published), signed bytes are
immutable. Correct by staging a new nanopublication whose pubinfo carries
`<successor> npx:supersedes <predecessor trusty URI>`, using the same key.
Retraction (`npx:retracts`) is the separate withdrawal-without-successor case.

The explicit successor→predecessor publish-row relationship is approved; a NEW
forward-only migration is authorized for that later slice. Never edit sealed
migrations. Any migration goes through coordinator gated `/go`, not quick deploy.

## Selected first slice: (b), no migration

(a)+(b) crosses separate publication concurrency and consumer-review paths.
Implement (b) first because existing `chunk_review`, `reviews` and `refs.meta`
provide the required review surfaces without a migration. The initial (a) WIP
is preserved separately and excluded; it also needs its publication-lock/pool
issue corrected under the newly widened signed/published policy.

Own `refine_claim_sentence` in-place title/scope edits, hypothesis
`testable_by`/`motivation` edits (preserving their existing prose history), and
the corresponding finding edit responses. Enumerate live direct graph users: citing/referencing
drafts; findings/claims via establishes/corroborates/contradicts/refines (also
conjunct-of/motivated-by/disputes dependencies); pathways/quests/todos referencing
the hub. Both endpoint directions are checked for graph relationships; retired
links/consumers are excluded. Because autolinking is best-effort, also parse
live consumer prose with the existing reference grammar and all retained hub
pub-id aliases. Chunk-addressed users cannot survive the existing DELETE+INSERT
body replacement: this slice refuses those edits atomically until a later
retargeting change, rather than silently dropping references. Ref-level users
and all existing citation pins are preserved. Return a deterministic de-duplicated list with
handles, kinds, titles and relationship reasons.

For a changed, unsigned claim: keep the same hub and publish row, reopen a
reviewed row in place for human reapproval, clear affected draft review watermarks,
append existing review-ledger `proposed` entries and persist a per-source
`meta.claim_review_required` marker on every consumer, containing source handle,
UTC change time and reason. Markers are queryable via the ordinary raw ref read;
the append-only review ledger retains the note. General historical approvals
remain intact (the marker is a re-review request, not a fabricated content change). The edit response prints the list.
Unchanged edits are no-ops. All claim writes and consumer invalidation share the
same transaction, and reciprocal claim edits lock impacted refs in id order.
After lock waits, refresh consumer discovery and retry the ordered lock set
if membership grew (bounded retry; no partial writes). The list represents this
post-lock discovery snapshot, not consumers created after it. Inspect historical
artifacts in a separate statement after publish-row locking so artifacts
committed during a lock wait are visible.
Signed/anchored/published claims (including a reopened row that still has a
historical signed artifact) refuse at this edit door; no supersede chain is
created by unsigned edits.

## Acceptance

- Candidate/unpublished-internal edits preserve ref/publish ids; no extra
  nanopub rows/artifacts. Reviewed unsigned edits return the same row to candidate.
- Ref-level/prose draft cites, supported finding relationships and
  pathway/quest/todo references all appear once in the edit response and have
  persisted review markers/ledger entries. Draft approval watermarks are revoked.
  Chunk-level citations, including generated `fbN` prose without a link, refuse
  title/scope replacement without side effects.
- No-op/dry-run/refused signed edits leave claim, review state and consumers intact.
- Failure during propagation rolls back claim changes and all flags. Reciprocal
  edits do not deadlock; concurrent committed artifacts freeze edits; an edit
  winning before artifact insertion defeats the stale reviewed→signed CAS.
  Tests use real PG but no keys, signing, network or spend.
- Focused scripts/test, scoped container types, Ruff and independent Codex review.

## Audit / remaining work

Current main4b7d64594: `refine_claim_sentence` freezes anchored/published only;
`nanopub_reopen` clears signed-row pointers and returns the same row to candidate;
`demote.plan_demotion` maps signed to reopen. Artifact bytes/OTS proof tables
remain append-only, but the mutable hub/publish envelope can diverge from a
signed artifact. No version rows are minted by those unsigned edit/reopen paths.

This slice guards the in-place edit door. Align all signed reopen/demotion,
dependency-dirty/re-stamp policy with Reto's boundary in (a), together with signed,
anchored AND published predecessor support, explicit relationship migration,
server-owned pubinfo emission, same-key enforcement, discard/restage persistence,
and publication/supersession concurrency using a pool-safe lock. Actual successor
approval/signing/publication remain normal human flow. Retraction and deployed
native replay are separate follow-ups. No production writes in this work.
