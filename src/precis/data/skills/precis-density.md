---
id: precis-density
title: precis — find novel content, compress a paper, check coverage
status: draft
tier: 1
floor: any
applies-to: search (tags=['DENSITY:*']), get (view='representatives'/'echoes'/'coverage')
last-updated: 2026-04-26
---

# precis-density — find novel content, compress a paper, check coverage

Every chunk has a `DENSITY:` tag based on how many close neighbours it
has in embedding space.

| Tag | Means |
|---|---|
| `DENSITY:sparse` | distinctive, novel, idiosyncratic |
| `DENSITY:medium` | common, not saturated |
| `DENSITY:dense`  | saturated, echoed across many refs |

## Find novel content

```python
search(kind='paper', q='photocatalysis', tags=['DENSITY:sparse'])
# distinctive chunks; skips the commonplace echo
```

## Compress a long paper

```python
get(kind='paper', id='wang2020state', view='representatives')
# ~5–10 chunks spanning the paper, one per cluster
```

## Trace what a chunk echoes

```python
get(kind='paper', id='wang2020state~38', view='echoes')
# chunks across the corpus similar to chunk 38
```

## Diagnose corpus thinness

```python
get(kind='paper', view='coverage', q='NOxRR mechanism')
# → thin / moderate / thick + counts per DENSITY: bucket
```

If thin, reach for `ask` or flag a gap with a `kind:question` memory.

## Compose filters

```python
search(kind='paper', q='catalysis',
       tags=['DENSITY:sparse', 'SRC:primary'])
# novel chunks from primary sources
```

## See also

- `precis-overview` — verbs and kinds
- `precis-tags` — `DENSITY:*`
- `precis-paper-help` — `representatives`, `echoes`, `coverage` views
