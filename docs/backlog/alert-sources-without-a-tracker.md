---
status: idea
title: quest_tick inquiry search step narrates instead of dispatching (gr476100); OTS batch 7 re-stamp
pillar: platform
---

# Remainder of the alert-tracker binding

The generic router binding shipped (`_TRACKED_SOURCES` in
`workers/health_digest.py`: find-or-file per `(source, fingerprint)`,
re-bind when the tracker is soft-deleted, auto-close on clear; a new source
is one registry entry). Left:

- **gr472019 (ops):** re-stamp OTS batch 7; al471919 stays open until the
  batch confirms.
- **Registry doc:** add the `quest_tick` / `nanopub_ots` budgets to
  [alert-failure-id-registry.md](alert-failure-id-registry.md) if that item
  grows a per-source column.

## quest_tick inquiry search step narrates instead of dispatching (gr476100)

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
