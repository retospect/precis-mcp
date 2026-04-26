---
id: precis-cache
title: precis — paid tools cache automatically
status: draft
tier: 1
floor: any
applies-to: get (kind='ask'/'math'/'websearch'/'youtube')
last-updated: 2026-04-26
---

# precis-cache — paid tools cache automatically

`ask`, `math`, `websearch`, `youtube` cache their output as a ref.
Re-call with the same `q=` hits the cache.

| Kind | TTL |
|---|---|
| `ask` | 180d |
| `math` | 180d |
| `websearch` | 90d |
| `youtube` | 365d |

## Call a tool

```python
get(kind='ask', q='mechanism of NOxRR')
# → answer text + footer:
#   (ask cache · age 12d / ttl 180d · CACHE:fresh)
```

Same `q=` next time → cache hit.

## Check freshness

`CACHE:fresh` (< 50% TTL), `CACHE:stale` (50–100%), `CACHE:expired` (> 100%).
Filter:

```python
search(kind='ask', q='photocatalysis', tags=['CACHE:fresh'])
```

## Force re-fetch

Delete and re-query:

```python
put(kind='ask', id='mechanism-of-noxrr', mode='delete')
get(kind='ask', q='mechanism of NOxRR')
```

## Preserve indefinitely

```python
put(kind='ask', id='mechanism-of-noxrr', tags=['pinned'])
# suppresses CACHE:* decay; never expires until unpinned
```

## Exclude caches from a broad search

```python
search(q='photocatalysis', cache=False)
# skips ask/math/websearch/youtube cache refs
```

Default is `cache=True` — caches surface alongside curated content.
Use `cache=False` when you want primary literature and notes only.

## See also

- `precis-overview` — verbs and kinds
- `precis-tags` — `CACHE:*` and the `pinned` flag
