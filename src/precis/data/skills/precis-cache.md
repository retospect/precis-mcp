---
id: precis-cache
title: precis — paid tools cache automatically
summary: cache mechanics for paid tools — TTLs, freshness, force-refresh, cost control
applies-to: get (kind in math/web/websearch/perplexity-reasoning/perplexity-research/youtube)
status: active
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
| `perplexity-reasoning` | 30 days | Sonar Reasoning Pro | ~$0.005   |
| `perplexity-research`  | pinned  | Sonar Deep Research | ~$0.50    |

`pinned` means never expires automatically. TTLs are stamped on the
row at write time — changing a handler constant only affects rows
written after.

## How is a cache key composed?
## What makes two queries hit the same cache row?
## Cache key gotcha across kinds

Cache keys are `<kind>:<canonical-query>`. The kind is part of the
key, so the same `q=` under different kinds is a different row and a
fresh paid call:

```python
get(kind='websearch', q='post-quantum signature schemes')               # websearch:...
get(kind='perplexity-reasoning', q='post-quantum signature schemes')    # NEW paid call
get(kind='perplexity-research',  q='post-quantum signature schemes')    # NEW paid call
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

The `CACHE:` axis is system-applied, closed-vocab:

- `CACHE:fresh` — age within 50% of the kind's TTL.
- `CACHE:stale` — age between 50% and 100% of TTL.
- `CACHE:pinned` — never expires (kind is pinned, or the row carries
  the `pinned` flag).

Filter on it:

```python
search(kind='perplexity-reasoning', q='photocatalysis', tags=['CACHE:fresh'])
search(kind='web',   q='reactor design',  tags=['CACHE:stale'])
```

The response footer also reports it inline:

```text
(perplexity-research cache · age 12d · pinned)
(perplexity-reasoning cache · age 22d · stale)
```

## How do I force a refetch?
## Bypass the cache for one query
## Re-run a stale answer

Soft-delete the row, then re-query. The next `get` misses and
refetches:

```python
delete(kind='perplexity-reasoning', id='<canonical-query>')
get(kind='perplexity-reasoning', q='<query>')
```

Works on every cache kind. For `web` and `youtube`, `id=` is the
canonical URL / video ID.

## Pin a row so it never expires
## Keep a cached answer indefinitely
## Stop a useful answer from going stale

```python
tag(kind='perplexity-reasoning', id='photocat-mechanism', add=['pinned'])
tag(kind='perplexity-reasoning', id='photocat-mechanism', remove=['pinned'])
```

`pinned` is a system-recognised flag — it suppresses `CACHE:*` decay
and the row reports as `CACHE:pinned`. Imported entries (e.g.
`put(kind='perplexity-research', ..., mode='import')`) are pinned automatically.

## See also

```python
get(kind='skill', id='precis-overview')          # verbs and kinds
get(kind='skill', id='precis-tags')              # CACHE:* axis, pinned flag
get(kind='skill', id='precis-perplexity-help')   # websearch / perplexity-reasoning / perplexity-research
get(kind='skill', id='precis-math-help')         # Wolfram Alpha
get(kind='skill', id='precis-web-help')          # direct page fetch
get(kind='skill', id='precis-youtube-help')      # transcript fetch
```
