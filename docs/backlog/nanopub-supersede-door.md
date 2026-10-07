---
status: in-progress
pillar: memory-graph
---

# Nanopub supersede door for our own anchored hubs

## Decision and scope (Reto, R14)

A correction is a new nanopublication: its pubinfo says
`<successor> npx:supersedes <predecessor trusty URI>`. The same signing
key must be used; retraction without a successor is separate.
Reto approved an explicit publish-row relationship and a NEW forward-only
migration. Existing migrations, artifact bytes and OTS proofs stay unchanged.
This migration requires the coordinator's gated `/go` path, never quick deploy.

Current-main premise checked at 4b7d64594fbf9ebd5bce5a31b3baf7239ed4b413:
no supersede CLI/write door, no local predecessor relation, no RDF emitter.
`mirror.index_bytes` already parses npx:supersedes. Ref links cannot distinguish
publish versions of the same hub and prohibit a ref-only self-link.

## First slice

`precis nanopub supersede FI` is interactive local staging, not signing or
publication. Only anchored, unpublished predecessors qualify. It locks and
rechecks the exact predecessor, changes its local state to superseded, creates
one candidate for the same hub, and links them in one transaction. Refuse
published rows (even if state was subsequently changed), pre-anchor states,
missing artifacts/anchors, and stale/concurrent invocations. Live publication
shares the hub lock through fresh preflight, POST and local bookkeeping;
otherwise an in-flight POST could race the unpublished-only check. A stubbed
POST concurrency regression demonstrates this race without network calls. No key access,
network, model calls or automatic approval. Ordinary human review uses current
evidence; this door does not copy the obsolete approved envelope.

New table `nanopub_supersessions`: predecessor publish-row PK/FK; successor
publish-row unique nullable FK. A candidate discard sets the successor to NULL
but retains the predecessor obligation. The normal candidate-creation path
reattaches that obligation atomically when restaging the same hub. Approval
and reopening never touch this table. This preserves existing stale-candidate
discard semantics rather than leaving stale candidates visible or calling them
human-rejected. A signed successor cannot be discarded (existing artifact FK).
A chain has one successor per predecessor and one predecessor per successor.
Restaging rejects multiple pending obligations or an obligation attached to a
rejected/retracted successor: it must not silently drop predecessor history.
Store-created edges always join the same hub and a fresh successor, preventing
self-links/cycles. Real proof validation checks artifact ownership and a matching
batch leaf/proof, not just non-null pointers.

Assembly resolves the predecessor artifact URI solely through this relationship,
never caller JSON. The successor pubinfo emits npx:supersedes from that URI.
Normal signing rejects a profile whose fingerprint differs from the predecessor
artifact's; tests use injected profiles and do not access keys or sign.

## Acceptance / focused validation

- Anchored unpublished predecessor → terminal superseded + one linked candidate;
  predecessor signed bytes, frozen fields, artifact/OTS pointers and proof intact.
- Real PostgreSQL transaction rollback leaves no partial transition; concurrent
  calls on the same predecessor have exactly one winner.
- Published, pre-anchor, missing-proof/artifact and noninteractive calls refuse.
- Approve/reopen/discard/restage preserve the predecessor obligation; fresh
  connection checks confirm persisted state. Ordinary unrelated candidates retain
  their current behavior.
- Assembly→TriG→existing mirror parser round-trip recovers the predecessor
  relation. Payload cannot forge it; existing non-successor RDF stays unchanged.
- Same-key mismatch fails before signing, using stub profiles only.
- New migration applies on fresh and preexisting schema and is Squawk-clean.
  Focused scripts/test, scoped container types and Ruff; root owns full release.

## Follow-ups

Published-row supersession / --live acknowledgement, registry propagation,
retraction, and native replay after coordinator deployment remain out of scope.
First motivating case fi191121/gr345628 is context only: no production writes.
