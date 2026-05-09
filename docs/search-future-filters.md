# Future search-filter affordances

> Status: **deferred** — captured here so the simple `q=` / `tags=` /
> `scope=` / `top_k=` surface stays the canonical agent contract
> across kinds. Revisit per item once usage demands it.

## Shipped (was on this list)

- **`exclude=` for ref-level pagination** — coarse skip-list of slugs
  pushed down to both lex and sem CTEs in `search_blocks_fused`, so
  `LIMIT` runs after exclusion and `top_k=10, exclude=[5 slugs]`
  returns the next ten hits. Wired on `kind='paper'` (paper.search +
  paper.search_hits); cross-kind dispatch forwards it via the new
  `_cross_kind_invoke_search_hits` retry chain so handlers can opt
  in by adding the kwarg. Documented in `precis-paper-help.md`,
  surfaced in the Next: trailer with the continuation list pre-
  filled. Closes the "I already saw the top 5, give me 6..N" gap.

## Still deferred

The current cross-kind `Handler.search` signature is uniform:

```python
search(*, q, scope, tags, top_k, exclude, **_kw) -> Response
```

Per-kind specialisation lives **inside** that surface — typically by
auto-applying closed-prefix tags at ingest (`cpc:`, `topic:`,
`STATUS:`, …) and translating those tags to whatever the upstream
backend expects (CQL fields for `patent`, lexical fragments for
`paper`/`markdown`, etc.).

This file lists filter axes that aren't expressible today, kept here
so we don't keep re-deriving them when a new kind lands.

## 1. Date / year ranges

**Symptom**: papers, patents, web pages, and Perplexity reports all
carry publication dates in `refs.meta`, but no agent-facing filter
exposes them. The agent has to fall back to:

- backend-specific `q=` syntax (`pd within "2020 2025"` for OPS
  patents), which leaks CQL into the convention; or
- post-filtering hits client-side on the rendered date string.

**Possible designs** (in order of effort):

1. **Year tags at ingest.** Apply `year:2024` (or `pd:2024`) as a
   closed-prefix single-valued tag on every patent/paper. Exact-year
   filtering becomes `tags=['year:2024']`. Range filtering still
   needs union-of-N-calls.
2. **Month/quarter tags.** Same idea, finer grain (`pd-q:2024-q1`,
   `pd-m:2024-03`). Useful for legal-status freshness on patents.
   Adds vocabulary churn.
3. **Protocol kwarg.** Add `since=` / `until=` (or `year_range=`) to
   `Handler.search`. Cleanest surface; biggest blast radius — every
   handler signature changes; tests update; docs update. Worth doing
   only when a second kind needs it (papers, very likely).

**Recommendation** when work begins: start with (1) for both
`paper` and `patent` (one closed-prefix tag, two ingest-time
hooks); upgrade to (3) only if range queries dominate.

## 2. Search state markers beyond `[local]`

`patent` search merges local + remote results and marks the
already-stored ones with `[local]`. Other states surfaced
informally during the spec discussion but **not yet shipped**:

- `[queued]` — a watch runner has placed this id into a `quest`
  inbox (e.g. `patents-pending-review`); the agent has implicitly
  expressed interest. Reading the quest table per search call is
  one extra index probe.
- `[stale]` — local ref's `meta.ops_etag` differs from the remote
  hit's etag → a re-`get(id=…)` would refresh. Important for
  legal-status freshness on patents.
- `[partial]` — ref exists but `blocks_with_embedding < blocks`
  (post-reembed sweep, interrupted ingest). Explains why
  semantic search missed it.
- `[ingesting]` — another process is actively fetching this id.
  Requires a real lock; defer.

**Recommendation**: add these state markers when the patent kind
sees real traffic and an agent confuses a stale legal status for
a fresh one. The implementations are small once the data is
already in `meta`.

## 3. Family-aware deduplication

Patent families group national variants of the same invention
(EP-prosecuted, US-filed, JP-filed, …) under a `family_id`. Today
the spec stores one ref per DOCDB id and lets the agent navigate
family members via `view='family'`.

**Open question**: should `search` deduplicate across families and
return one representative member per family by default, with a
`view='family'`-style flag (or `tags=['family:any']`) to expand?
This is a real prior-art workflow ("show me one of each invention
in this CPC class") but the right default is unclear — sometimes
you want every variant, sometimes you don't.

Consider when: someone runs a real FTO search and hand-collapses
duplicates.

## 4. Cited / cited-by graph

Patents reference other patents and (less often) papers. EPO OPS
exposes both directions. This data should land in `links` rows
(not tags), keyed `linked-via='cites'` and `linked-via='cited-by'`.
That makes "show me the prior art for ep1234567b1" expressible
as a `links`-table walk:

```python
get(kind='patent', id='ep1234567b1', view='cites')      # what this cites
get(kind='patent', id='ep1234567b1', view='cited-by')   # what cites this
```

Defer to v2; it's a separate ingest pass and a real graph problem.

## 5. Cross-kind search

Today `kind=` is required and a single value. Patent prior art
naturally crosses into `paper` (peer-reviewed prior art) and `web`
(non-patent prior art). The `precis-overview` skill already notes:

> cross-kind search (`kind='all'` or comma-lists like
> `kind='paper,memory'`) is not yet implemented; use
> `get(kind='skill', id='precis-help')` to discover the kinds that
> support `search` and call them one at a time.

Patent search makes this gap a bit more painful (the natural
"prior art" question spans patent + paper + web), but the fix is
cross-cutting, not patent-specific. Tracked at the protocol level.

## 6. CQL parity for `paper`

If we add structured field filters at the protocol level (option 3
above), `paper` can opt into them by reading `meta.year`,
`meta.journal`, `meta.authors` at ingest and applying matching
closed-prefix tags. The generic infrastructure should make this a
~50-LOC addition per kind.

## 7. Force local-only / remote-only on `search`

**Patent-specific** today, but the question generalises to any
kind that merges a local store with a live backend (`patent`,
maybe `web` once the bookmark mode lands).

The patent handler currently always merges local + remote (with
the remote leg auto-skipped when OPS creds are unset). That's the
right default for "did anyone patent this?", but two real cases
ask for an opinion-knob:

- **Local-only** — "what have I curated on this topic?" Useful
  for offline curation review and for FTO double-checks where the
  agent wants to lean only on its triaged set.
- **Remote-only** — "what is the world saying right now?" Useful
  for fresh prior-art sweeps where the agent worries that local
  curation biases ranking. Marginal at our scale; documented for
  symmetry.

**Proposal**: a single optional kwarg

```python
search(kind='patent', q='...', source='local')   # skip OPS leg
search(kind='patent', q='...', source='remote')  # skip local leg
search(kind='patent', q='...', source='both')    # explicit default
```

One branch in `PatentHandler.search` (skip the corresponding leg
+ skip the merge). Tests for both skips. **Defer until merged
output proves insufficient** — `[local]` markers may already give
the agent enough to disambiguate without a knob.

When implemented, the same `source=` kwarg should land on any
other hybrid handler (`web` bookmarks, future `news`, etc.) so
the convention is uniform.

## 8. Block-level (positional) tag filtering

`precis-tags` already mentions:

> Block-level (positional) tag filtering — the schema supports
> `pos=N` tags on a specific block, but no handler currently
> writes them and the search filter only matches ref-level tags.

Patent claims are an obvious use case (tag a block as
`claim:independent` vs `claim:dependent`). Defer until a second
kind also needs it.

## See also

- `src/precis/data/skills/precis-patent-help.md` — agent skill
- `src/precis/data/skills/precis-patent-power.md` — power-user CQL surface
- `src/precis/data/skills/precis-tags.md` — tag conventions
- `docs/patent-kind-spec.md` — patent implementation spec
