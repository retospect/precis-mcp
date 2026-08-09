# Source-backfill — unbuilt follow-ups

The backfill core shipped (see the `precis.backfill` package docstring —
recall lenses, Tier-0 dedup, `view='backfill'`). Three declared follow-ups
remain unbuilt: a HyDE query lens, a Tier-1 relevance cull for candidate
lists, and an `integrate` planner coroutine that walks accepted candidates
into the draft. Owner `src/precis/backfill/candidates.py`. Needs design
per piece; each is independently shippable.
