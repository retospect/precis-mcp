---
status: in-progress
pillar: platform
title: priority-sorted gripe, todo and quest browse
prio: high
---

# gr468266 — checked R14 contract

Confirmed open and claimed wip via native Precis. Tools reject prio globally;
recency intercepts source search before kind resolution and requires a query.
Numeric status/tag browse already has SQL LIMIT/OFFSET but no order selector.
Accept recency (existing modification-first order) and prio for queryless
gripe/todo/quest status/tag or unfiltered browse. Sort priority in SQL before
pagination: coalesce(prio,5), updated_at descending, ref_id descending.
No inferred stored priority: unset sorts at existing default 5.
Keep ranked/source search, dated searches, linked searches and special views
on their existing paths. Invalid-sort recovery must name applicable sorts
and provide a working call for the current shape. No schema changes.
Tests through tools + runtime: recency recovery, stable priority pages incl
unset/default/ties, status filters across three kinds; source-search regressions.
Coordinator owns full release/version gate; no deployment/closure claim.
