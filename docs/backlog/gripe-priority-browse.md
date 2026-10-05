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

## gr468293 — Reto-expanded live browse contract

Native open, stored priority2, linked gr468266; claimed wip. Queryless
status/tag or unfiltered browse defaults to nonterminal for todo/gripe/quest/alert.
Explicit status or lifecycle tag (including terminal), or status='*', overrides
that default. Preserve ranked-search defaults. Todo terminals done/won't-do/
abandoned; gripe done/wontfix; quest abandoned (dormant stays visible); alert
alert-state:resolved. Alerts use open tags, not STATUS; status='closed' is a
read shorthand for resolved. Show actual per-row lifecycle in one bulk tag
query, and exact hidden terminal count for the requested tag scope, before
SQL LIMIT/OFFSET. No close/tag cleanup mutations; housekeeping24 owns those.
Negative tag filters compose internally in list/count SQL; no schema/endpoint
changes. Regression each lifecycle/opt-out/explicit-state/empty page/priority
composition, real synthetic DB, and memory/ranked/source behavior preserved.
