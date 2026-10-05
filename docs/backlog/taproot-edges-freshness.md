---
status: ready
title: Taproot edge freshness follows overdue chase work, not output silence
pillar: memory-graph
---

# Taproot edge freshness (gr346342)

Reto authorized this bounded alert fix via the orchestrator. Adjudication
is separate (`taproot-adjudicate-reopened-claims.md`); its open decisions
are untouched. The gripe comment correcting its old ownership pointer
also carries the production audit below.

## Checked premise

Main650232a82 already replaced the flat6h edge clock with a proxy:
a claim hub created after the latest edge. Chase actually claims live
`STATUS:tracing`/`acquiring` findings, excluding real claim hubs without
`TAPROOTCASCADE`, and suppresses recent waiting events via exponential
backoff (60min base,1440min cap). A hub timestamp is not this queue.

Read-only prod audit2026-10-05: current exact eligible queue=0. Six
edge lulls >24h over45days (337.6,47.1,39.1,30.8,67.7,66.3h) all had chase
events (344,9,4,2,6,8); longest observed overdue inter-event interval
after backoff was0.66h, none >6h. Historical tags are not archived:
this is evidence against a continuous stall, not proof of every past
queue size. No demonstrated separate stall to file. Full SQL/metrics
retained in task `.scratch/taproot-prod-*`; numbers posted to gr346342.

## Slice

- Mirror the chase claim predicate read-only in health_digest, including
  tracing/acquiring, real-hub exclusion, and capped exponential backoff.
  No worker imports/model hooks, locking, or production worker execution.
- Eligible age starts at the latest of ref creation, current queue-status
  tag creation, and last chase event (plus backoff for waiting). This
  avoids aging cooled-down work or newly requeued old findings as stalled.
- Alert iff at least one eligible finding has waited >6h AND the latest
  existing taproot-role edge is older than6h (or absent). Recent edge
  progress is healthy even when an older candidate remains queued.
- An empty eligible queue renders exactly `idle: 0 eligible`; a nonempty
  young/progressing queue reports its count and oldest eligible age.
- Keep the existing fingerprint/severity/6h budget and edge relation set.
  No service gating, remediation policy, schema or adjudication changes.

## Acceptance / validation

Real DB tests: idle empty (including old claim hubs/edges) gives no alert;
overdue eligible backlog without an edge or with stale output alerts;
bursty/recent edge progress does not alert; young/requeued work does not
alert; waiting backoff is excluded until due and ages from expiry.
Check queue membership against claim_tracing_findings on fixtures,
including acquiring, canonical/retired exclusions and cascade claims.
Focused scripts/test, scoped container types, Ruff. Full suite remains
coordinator scheduled. Push isolated work/knowledge-mesh/taproot-edges-freshness
and request independent Codex review; no deploy or adjudication start.
