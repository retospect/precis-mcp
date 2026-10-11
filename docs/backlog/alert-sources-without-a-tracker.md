---
status: idea
title: Alert sources outside watchdog/nursery are never auto-filed, auto-resolved, or bound to a live tracker
pillar: platform
---

# Alert sources without a tracker

Bundled in the 2026-10-10 gripe triage. The three gripes below are symptoms of one
gap (gr476100, first listed here, is a separate bug: §2). The health_digest
router files and resolves gripes only for
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

## 1. Direction and acceptance

Make the router's source→tracker binding generic: any alert source gets
find-or-file of a live tracker gripe, re-binding when the cited tracker is
soft-deleted, and auto-resolve when the alert clears. Add a registry entry
per source in [alert-failure-id-registry.md](alert-failure-id-registry.md),
not a per-source branch.

### Acceptance

- A `nanopub_ots` or `quest_tick` alert with no tracker files one, and a
  repeat raise comments on that tracker instead of filing a new one.
- An alert citing a soft-deleted gripe re-binds to a live one.
- A cleared alert closes its auto-filed tracker.

Who: agent-code, M, for the router binding. gr472019 also needs ops: re-stamp
OTS batch 7 (al471919 stays open until the batch confirms).

## 2. quest_tick inquiry search step narrates instead of dispatching (gr476100)

A quest_tick narrated a literature-search dispatch, but no search ran: its
structured `searches` field did not fire. The tick logged a self-diagnosed gap
and nobody filed it. This is a separate bug from the missing tracker. Check
`src/precis/quest/tick.py::_TickRun._stage_search`, which runs only when the
tick is in acting (`compute`) mode and only when `payload()["searches"]` is a
non-empty list, then calls `precis.quest.search.run_search_step`:

- In inquiry mode, does the model emit `searches` while the stage is skipped
  because `self.compute` is false? If so, either run the step in inquiry mode
  or tell the model not to promise a dispatch there.
- When the model's prose mentions a search but `searches` is empty or missing,
  log it (a counter in the tick outcome next to `searches_run`) so the gap is
  visible without reading the transcript.

Reproduce from the logged tick payload before changing anything. Who:
agent-code, S.
