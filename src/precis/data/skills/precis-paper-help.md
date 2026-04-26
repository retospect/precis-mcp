---
id: precis-paper-help
title: precis — find, read, cite, annotate papers
status: draft
tier: 1
floor: any
applies-to: get/search (kind='paper'), put (kind='paper')
last-updated: 2026-04-26
---

# precis-paper-help — find, read, cite, annotate papers

## Find

```python
search(kind='paper', q='photocatalytic NOx reduction', limit=5)
get(id='doi:10.1002/aenm.202400065')      # by DOI
get(id='arxiv:2207.09327')                # by arXiv id
```

Restrict to one paper:

```python
search(kind='paper', q='Z-scheme', scope='wang2020state')
```

## Read

```python
get(kind='paper', id='wang2020state', view='abstract')          # skim
get(kind='paper', id='wang2020state', view='representatives')   # compressed
get(kind='paper', id='wang2020state', view='toc')               # navigate
get(kind='paper', id='wang2020state', view='methods')           # one section
get(kind='paper', id='wang2020state~38..42')                    # chunk range
```

Section views also work for `results`, `discussion`, `conclusions`,
`figures`, `tables`.

Slug pattern: `wang2020state` (whole), `wang2020state~38` (chunk),
`wang2020state~38..42` (range).

## Cite

```python
get(kind='paper', id='wang2020state', view='bibtex')
get(kind='paper', id='wang2020state', view='ris')
get(kind='paper', id='wang2020state', view='endnote')
```

## Annotate

```python
put(kind='paper', id='wang2020state', tags=['topic:noxrr', 'star'])

put(kind='memory',
    text='Z-scheme idea from §3 looks transferable to NOxRR.',
    tags=['kind:idea', 'topic:noxrr', 'CONFIDENCE:tentative'],
    link='wang2020state~38')
```

## See also

- `precis-overview` — verbs and kinds
- `precis-relations` — `related-to`, `contradicts` between papers
- `precis-tags` — `topic:`, `SRC:`, `DENSITY:*`
- `precis-density` — `representatives`, `echoes`, `coverage`
- `precis-memory-help` — capturing thoughts from a paper
