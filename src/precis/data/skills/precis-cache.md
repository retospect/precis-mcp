---
id: precis-cache
title: precis — paid tools cache automatically
status: active
tier: 1
floor: any
applies-to: get (kind in math/web/websearch/think/research/youtube)
last-updated: 2026-04-28
---

# precis-cache — paid tools cache automatically

The cache-backed kinds store every fetch as a ref. Re-calling
`get(kind=K, q=...)` with the same canonical query hits the cache
and skips the upstream call (and its cost).

## TTL table

| Kind | TTL | Provider | Cost/call |
|---|---|---|---|
| `math` | pinned (results are deterministic) | Wolfram Alpha | free tier |
| `web` | 7 days | direct fetch | free (bandwidth) |
| `websearch` | 7 days | Perplexity Sonar | ~$0.001 |
| `think` | 30 days | Sonar Reasoning Pro | ~$0.005 |
| `research` | pinned (too expensive to expire) | Sonar Deep Research | ~$0.50 |
| `youtube` | 30 days | youtube-transcript-api | free |

`ask` is **not** a real kind on this build; older docs that
reference it should be read as `research` (or `websearch` /
`think`, depending on cost tier).

## Call a tool

```python
get(kind='research', q='mechanism of NOxRR')
# → answer text + footer:
#   (research cache · age 12d · pinned)
```

Same `q=` next time → cache hit, no upstream call.

## Check freshness

The `CACHE:` axis is closed-vocab with three values:

- `CACHE:fresh` — within 50% of the kind's TTL.
- `CACHE:stale` — between 50% and 100% of TTL.
- `CACHE:pinned` — never expires (math, research, anything tagged
  `pinned`).

Filter on it:

```python
search(kind='think', q='photocatalysis', tags=['CACHE:fresh'])
```

## Force a re-fetch

Soft-delete the cache ref, then re-query. The next `get` will miss
and refetch:

```python
put(kind='research', id='mechanism-of-noxrr', mode='delete')
get(kind='research', q='mechanism of NOxRR')
```

(`mode='delete'` is the only mode supported on numeric-ref kinds;
file kinds and cache kinds also accept `delete`. See per-kind help.)

## Preserve indefinitely

```python
put(kind='think', id='photocat-mechanism', tags=['pinned'])
# the 'pinned' flag suppresses CACHE:* decay; never expires
# until unpinned with put(..., untags=['pinned'])
```

## Notes

- TTLs are stored on the cache row at write time. Changing the
  handler's `ttl_seconds` constant only affects rows written
  *after* the change.
- The TTL table above is hand-mirrored from the live handler
  classes. If the two ever drift, the **handler is canonical**
  and this skill is wrong.

## See also

- `precis-overview` — verbs and kinds
- `precis-tags` — `CACHE:*` and the `pinned` flag
- `precis-perplexity-help` — websearch / think / research detail
- `precis-math-help` — math (Wolfram Alpha)
- `precis-youtube-help` — youtube transcript fetch
- `precis-web-help` — direct page fetch
