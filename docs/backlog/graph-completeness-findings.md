# Graph completeness — open audit findings

Four still-open findings from the 2026-07-23 graph audit.

1. `MemoryHandler.supersede()` never fires on near-duplicate memories — prod
   superseded_by was 0 at audit; ~80% of memories are DREAM-tagged synthetic
   insight with near-duplicate clusters. Wire the dream/review pass to call
   supersede, or surface a candidate-duplicate nudge. Re-confirm
   superseded_by=0 before building.
2. No isolated-memory nursery check (~10% have zero links either direction) —
   widen autolink_mentions adoption and/or add an "isolated memory" finding.
3. No two-ref intersection query ("does A share links/tags with B") —
   view='shared'? a compare verb? The SQL is trivial once the verb shape is
   picked.
4. No aggregate/fan-in graph view (`links_for` is one-ref/one-hop) — biggest
   lift.
