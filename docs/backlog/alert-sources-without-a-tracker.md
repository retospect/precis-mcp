---
status: idea
title: OTS batch 7 re-stamp; alert-failure-id registry budgets
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
