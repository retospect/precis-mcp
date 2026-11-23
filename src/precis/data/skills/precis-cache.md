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
| `math`      | pinned  | Wolfram Alpha          | ~$0.002      |
| `web`       | 7 days  | direct fetch           | free         |
| `wikipedia` | 7 days  | Wikipedia              | free         |
| `youtube`   | 30 days | youtube-transcript-api | free         |
| `chem`      | 30 days | PubChem                | free         |
| `news`      | pinned  | direct fetch           | free         |
| `websearch` | pinned  | Perplexity Sonar       | ~$0.001      |
| `perplexity-reasoning` | pinned  | Sonar Reasoning Pro | ~$0.005   |
| `perplexity-research`  | pinned  | Sonar Deep Research | ~$0.50    |

`pinned` means the result stays fresh indefinitely. Use
`args={'ttl_days': N}` on a fetch or refresh to set a finite TTL.
Freshness is stamped at write time. The migration pins existing
`websearch` rows with a 7-day TTL and preserves other TTLs.

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
- `CACHE:stale` — past `fresh_until`. A request lookup can re-fetch.
- `CACHE:pinned` — `fresh_until` NULL: the row was written without a
  TTL (a pinned kind, `mode='import'`, or `args={'ttl_days': 0}`).

Filter on it — `tags=` rides the cross-kind fan-out, so name a
comma-list of the cache kinds (or omit `kind=` to sweep all):

```python
search(kind="websearch,perplexity-reasoning,perplexity-research", q="photocatalysis", tags=["CACHE:fresh"])
search(kind="web,youtube", q="reactor design", tags=["CACHE:stale"])
```

The response footer also reports it inline:

```text
(perplexity-research cache · age 12d · pinned)
(perplexity-reasoning cache · age 22d · pinned)
(websearch cache · age 9d · pinned)
```

## How do I force a refetch?
## Bypass the cache for one query
## Re-run a stale answer

Pass `refresh=True` on the `get` to force an upstream call:

```python
get(kind="perplexity-reasoning", q="<query>", args={"refresh": True})
```

Works on every cache kind. For `web` and `youtube`, pass the canonical
URL / video ID in `id=` instead of `q=`.

For `web`, `wikipedia`, `youtube` and all three Sonar tiers, a refresh
publishes a new ref and slug. When the fetch supplies a
`content_sha256` that matches the current snapshot's hash, the refresh
reuses that ref. Lookups by request hash resolve the current snapshot.
Read a cited snapshot by its stored ref ID or slug with `no_fetch=True`.
Each snapshot retains its chunks and their links.

## Pin a row so it never expires
## Keep a cached answer indefinitely
## Stop a useful answer from going stale

Pinning is a write-time property, not a tag: pass
`args={'ttl_days': 0}` on the `get` — the row's `fresh_until` lands
NULL and it reports `CACHE:pinned` forever:

```python
get(kind="perplexity-reasoning", q="<query>", args={"ttl_days": 0})
# Pin the result of a refresh.
get(kind="perplexity-reasoning", q="<query>", args={"mode": "refresh", "ttl_days": 0})
```

The bare `pinned` flag is *not* the mechanism — on a cache kind it is
refused outright (it collides with the closed `CACHE:pinned` value),
and a `CACHE:pinned` tag row is inert: freshness reads the cache
row's `fresh_until`, never the tag. Rows on a TTL-less kind (`math`,
`news`, `websearch`, `perplexity-*`) are pinned from the start.
`put(mode='import')` also writes a pinned entry.

<!--
Fact-checked (archdocs3): bare `pinned` refused on cache kinds —
store/types.py Tag.parse_strict kind-scoped collision (cache kinds
allow the CACHE axis, or like math are unlisted); pinned-from-start —
handlers/math.py ttl_seconds=None, handlers/perplexity.py WebsearchHandler /
ThinkHandler / ResearchHandler TIER ttl_seconds=None;
import-mode put — perplexity.py put writes ttl_seconds=None;
args ttl_days ≤0 → handlers/_cache_base.py CacheBackedHandler.get
leaves ttl_seconds_override None with ttl_override_active →
put_cache_entry/update_cache_entry write fresh_until NULL.
-->

## See also

- [[precis-overview]] — verbs and kinds.
- [[precis-tags]] — `CACHE:*` axis, pinned flag.
- [[precis-perplexity-help]] — websearch / perplexity-reasoning / perplexity-research.
- [[precis-math-help]] — Wolfram Alpha.
- [[precis-web-help]] — direct page fetch.
- [[precis-youtube-help]] — transcript fetch.
