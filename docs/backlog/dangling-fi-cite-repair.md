---
status: idea
pillar: memory-graph
title: Deleting or merging a claim hub leaves dangling [fi] cites in draft prose
---

# Dangling `[fi]` cites after hub delete or merge (gr265228)

Bundled in the 2026-10-11 gripe triage. A draft chunk citing `[fi<id>]` keeps
the cite when the hub is deleted or merged away, so grounded prose reverts to
ungrounded while still looking grounded (and every conversion census counts
it as done; see the dr42995 sections in
[dr42995-boxel-draft.md](dr42995-boxel-draft.md)). Asks 1 and 3 of the gripe
shipped in 70bca5d9.

## Code

Auto-repoint cites when a hub is merged: the merge path
`_apply_merge_plan` in `src/precis/taproot/hub.py` should rewrite live draft
chunks citing the absorbed hub to the survivor (a chunk edit through the draft
door, so reading order and embeddings follow the normal cascade). Deletion
without a survivor has no target: refuse or report the citing chunks rather
than delete silently.

Who: agent-code.

## Prod data repairs

Existing dangling cites: find them with the detection query in gr265228
(live chunks only, `retired_at IS NULL`). Known examples: dc2618395 cites
retired fi191261 / fi191265; dc3345999 cites fi176919. Repoint each to the
live successor hub, or reground when none exists.

Who: ops after the code lands, so the repair does not re-create the drift.
