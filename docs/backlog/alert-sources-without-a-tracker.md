---
status: idea
title: Alert sources outside watchdog/nursery are never auto-filed, auto-resolved, or bound to a live tracker
pillar: platform
---

# Alert sources without a tracker

Bundled in the 2026-10-10 gripe triage. The four gripes are symptoms of one
gap. The health_digest router files and resolves gripes only for
`watchdog:*` and nursery-backlog sources. Every other alert source
(`nanopub_ots`, `quest_tick`, nursery `child-failed-parked`) either
re-raises against a soft-deleted tracker or gets hand-filed as a
"no live tracker" gripe.

- **gr472019:** OTS batch 7 is stuck pending (al471919) and has no live
  tracker. `al468011` was orphaned when its quest went dormant.
- **gr474610:** an autocatpath `child-failed-parked` leaf cites the
  soft-deleted gr168886. There were 4 instances in 2 days, each unparked by
  hand.
- **gr475405:** a quest_tick dry-rest alert (al462681) was filed only as a
  dedup anchor. Backoff works as designed.
- **gr476100:** the quest_tick narrated a search dispatch, but its
  structured `searches` field did not fire. A self-diagnosed gap was logged
  and never filed. Check the code and logs before treating it as part of
  this item; it may be a separate bug.

## Direction

Make the router's source→tracker binding generic: any alert source gets
find-or-file of a live tracker gripe, re-binding when the cited tracker is
soft-deleted, and auto-resolve when the alert clears. Add a registry entry
per source in [alert-failure-id-registry.md](alert-failure-id-registry.md),
not a per-source branch.

## Acceptance

- A `nanopub_ots` or `quest_tick` alert with no tracker files one, and a
  repeat raise comments on that tracker instead of filing a new one.
- An alert citing a soft-deleted gripe re-binds to a live one.
- A cleared alert closes its auto-filed tracker.
