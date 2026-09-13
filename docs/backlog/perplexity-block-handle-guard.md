# Guard `get(kind='perplexity-*', id=…)` against a search block handle

**Found 2026-09-13, cost a real ~$0.50 report.** `search(kind='perplexity-research', q=…)`
returns block handles shaped `<query-slug-prefix>~<section-slug>-<hash>`.
Passing one back as `id=`/`q=` does two bad things at once:

1. It is sent to Perplexity as a **fresh paid query** (that kind is addressed
   by query text — there is no fetch-ref-by-handle path through `get`).
2. The resulting row's slug is derived from the same truncated
   `<query-slug-prefix>`, so it **collides with the original report's slug and
   replaces it**. The expensive report is destroyed, mid-paging, and
   `id='/recent'` then shows one row where there were two — holding a
   *different* report's body under the original query's slug.

Observed: an EWOD-in-oil deep-research report was lost at page 3 of ~5 this
way; the surviving row held an unrelated "chemical synthesis" report.

## Why the existing guard misses it

`PerplexityHandler` already rejects a **digits-only** `id=` (the misrouted
ref-id footgun, documented in `precis-perplexity-help`). That guard reads —
to an agent and to a human skimming the skill — as though the
handle-as-`id=` class is covered. It is not: the check is
`str.isdigit()`-shaped, and a block handle is neither a number nor a query.

## Fix (two parts, the first is cheap)

1. **Reject a block-handle-shaped `id=`/`q=`** the same way the bare number is
   rejected, with a hint naming the right call. The shape is recognisable
   without a DB lookup: contains `~`, and the segment after the last `-` is
   hex. Same `args={'literal': True}` escape as the numeric guard, and the
   `mode='import'` path stays exempt. A stricter and simpler variant: reject
   any `id=` containing `~`, since a genuine natural-language query
   essentially never does.
2. **Stop a new row from silently replacing an unrelated report.** The slug
   collision is the part that destroyed data — a distinct query truncating to
   the same prefix should get a distinct slug (include a hash of the *full*
   query, not just the truncated prefix), or the write should refuse to
   overwrite a row whose stored full query differs from the incoming one.
   Part 1 prevents this specific trigger; part 2 is what makes the loss
   impossible from any trigger.

## Also worth doing

`precis-perplexity-help` should say out loud that a long report must be
**fully drained through `more(cursor=…)` before any other call on that
kind** — the row can vanish underneath an in-progress paging session. Today
the skill implies the cache is stable once written.

Related: auto-memory `perplexity-block-handle-clobbers-row`.
