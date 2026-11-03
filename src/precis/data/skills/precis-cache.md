---
id: precis-cache
family: tools
title: precis — paid tools cache automatically
summary: cache mechanics for paid tools — TTLs, freshness, force-refresh, cost control
answers:
  - how long does a cached paid-tool answer stay fresh before it expires?
  - how do I force a refetch instead of using the cached answer?
  - how do I pin a cached row so it never goes stale?
  - what makes two queries hit the same cache row?
applies-to: get (kind in math/web/websearch/perplexity-reasoning/perplexity-research/youtube)
kinds: [math, web, websearch, perplexity-reasoning, perplexity-research, youtube]
status: active
tags: [troubleshooting, external-sources]
---

# precis-cache — TTLs, freshness, force-refresh

Paid and fetched kinds cache every result. Re-calling the same query
hits the cache and skips the upstream cost.

## What TTL does a kind have?
## How long does cached data stick around?
## When does a cached answer expire?

| kind        | TTL     | provider               | cost/call    |
|-------------|---------|------------------------|--------------|
| `math`      | pinned  | Wolfram Alpha          | free tier    |
| `web`       | 7 days  | direct fetch           | free         |
| `youtube`   | 30 days | youtube-transcript-api | free         |
| `websearch` | 7 days  | Perplexity Sonar       | ~$0.001      |
| `perplexity-reasoning` | pinned  | Sonar Reasoning Pro | ~$0.005   |
| `perplexity-research`  | pinned  | Sonar Deep Research | ~$0.50    |

`pinned` means never expires automatically. `perplexity-reasoning`
is pinned by default (a wrong cached answer is bounded — citations
trace to real papers via the chase/finding pipeline, not to
Perplexity — so the cost of staying wrong is low next to the cost
of re-running a $0.005 call); override per call with
`args={'ttl_days': N}` if you want that one row to decay. TTLs are
stamped on the row at write time — changing a handler constant only
affects rows written after.

## How is a cache key composed?
## What makes two queries hit the same cache row?
## Cache key gotcha across kinds

Cache keys are `<kind>:<canonical-query>`. The kind is part of the
key, so the same `q=` under different kinds is a different row and a
fresh paid call:

```python
get(kind="websearch", q="post-quantum signature schemes")  # websearch:...
get(kind="perplexity-reasoning", q="post-quantum signature schemes")  # NEW paid call
get(kind="perplexity-research", q="post-quantum signature schemes")  # NEW paid call
```

Per-kind canonicalisation:

- `websearch`, `perplexity-reasoning`, `perplexity-research` — query text, trimmed, verbatim.
- `math` — query text, trimmed.
- `youtube` — canonical video ID extracted from any URL form.
- `web` — canonical URL (scheme + host + path + sorted query).

Pick the tier first. There is no upgrade path that reuses a cheaper
row.

## How do I check freshness?
## Is this cached answer still fresh?
## Tell me if a ref is fresh, stale, or pinned

The `CACHE:` axis is system-applied, closed-vocab — a rendered state
of the row's `fresh_until`, not a flag you write:

- `CACHE:fresh` — `fresh_until` still in the future.
- `CACHE:stale` — past `fresh_until` (the next `get` re-fetches).
- `CACHE:pinned` — `fresh_until` NULL: the row was written without a
  TTL (a pinned kind, `mode='import'`, or `args={'ttl_days': 0}`).

Filter on it:

```python
search(kind="websearch", q="photocatalysis", tags=["CACHE:fresh"])
search(kind="web", q="reactor design", tags=["CACHE:stale"])
```

The response footer also reports it inline:

```text
(perplexity-research cache · age 12d · pinned)
(perplexity-reasoning cache · age 22d · pinned)
(websearch cache · age 9d · stale)
```

## How do I force a refetch?
## Bypass the cache for one query
## Re-run a stale answer

Soft-delete the row, then re-query. The next `get` misses and
refetches:

```python
delete(kind="perplexity-reasoning", id="<canonical-query>")
get(kind="perplexity-reasoning", q="<query>")
```

Works on every cache kind. For `web` and `youtube`, `id=` is the
canonical URL / video ID.

## Pin a row so it never expires
## Keep a cached answer indefinitely
## Stop a useful answer from going stale

Pinning is a write-time property, not a tag: pass
`args={'ttl_days': 0}` on the `get` — the row's `fresh_until` lands
NULL and it reports `CACHE:pinned` forever:

```python
get(kind="perplexity-reasoning", q="<query>", args={"ttl_days": 0})
# same on a refresh — pins the stored row in place:
get(kind="perplexity-reasoning", q="<query>", mode="refresh", args={"ttl_days": 0})
```

The bare `pinned` flag is *not* the mechanism — on a cache kind it is
refused outright (it collides with the closed `CACHE:pinned` value),
and a `CACHE:pinned` tag row is inert: freshness reads the cache
row's `fresh_until`, never the tag. Rows on a TTL-less kind (`math`,
`perplexity-*`) are pinned from the start, and `put(mode='import')`
writes `ttl_seconds=None` — pinned — explicitly.

<!--
Fact-checked (archdocs3): bare `pinned` refused on cache kinds —
store/types.py Tag.parse_strict kind-scoped collision (cache kinds
allow the CACHE axis, or like math are unlisted); pinned-from-start —
handlers/math.py ttl_seconds=None, perplexity.py ThinkHandler /
ResearchHandler TIER ttl_seconds=None (websearch is 7d);
import-mode put — perplexity.py put writes ttl_seconds=None;
args ttl_days ≤0 → handlers/_cache_base.py CacheBackedHandler.get
leaves ttl_seconds_override None with ttl_override_active →
put_cache_entry/_refetch_in_place write fresh_until NULL.
-->

## See also

- [[precis-overview]] — verbs and kinds.
- [[precis-tags]] — `CACHE:*` axis, pinned flag.
- [[precis-perplexity-help]] — websearch / perplexity-reasoning / perplexity-research.
- [[precis-math-help]] — Wolfram Alpha.
- [[precis-web-help]] — direct page fetch.
- [[precis-youtube-help]] — transcript fetch.
