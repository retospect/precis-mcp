---
status: idea
pillar: memory-graph
title: Existing dangling [fi] cites in draft prose need a data repair
---

# Dangling `[fi]` cites: prod data repair (gr265228)

A draft chunk citing `[fi<id>]` kept the cite when the hub was deleted or
merged away, so grounded prose reverted to ungrounded while still looking
grounded (and every conversion census counts it as done; see the dr42995
sections in [dr42995-boxel-draft.md](dr42995-boxel-draft.md)). The code side
is done: `retire_ref` refuses a cited finding (70bca5d9), draft lint flags
tombstone cites, and `merge_hubs` rewrites live `[fi<loser>]` cites to the
winner in the same transaction (collapsing a doubled winner cite), listing
them in `plan.cite_rewrites` on a dry run.

## Prod data repairs

Existing dangling cites: find them with the detection query in gr265228
(live chunks only, `retired_at IS NULL`). Known examples: dc2618395 cites
retired fi191261 / fi191265; dc3345999 cites fi176919. Repoint each to the
live successor hub (`taproot merge` records `loser --collapse--> winner`, so
the successor is usually the collapse edge's target), or reground when none
exists.

Who: ops.
