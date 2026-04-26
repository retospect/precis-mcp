---
id: precis-paper-help
title: precis — find, read, cite papers
status: phase-3
tier: 1
floor: any
applies-to: get/search (kind='paper')
last-updated: 2026-04-26
---

# precis-paper-help — find, read, cite papers

Papers are slug-addressed (`wang2020state`, `kim2024electrocatalytic`).
Slugs are deterministic on author-surname + year + first content word
of the title; collisions get a `-2`/`-3` suffix.

## Find

```python
search(kind='paper', q='photocatalytic NOx reduction')
search(kind='paper', q='photocatalytic NOx reduction', top_k=20)
get(kind='paper')                              # list all papers (limit 50)
```

Block-level hybrid search (lexical tsvector + semantic pgvector, RRF
fused). Returns hits as `<slug>~<pos>` with the matching block excerpt
and a fused score.

Scope to one paper:

```python
search(kind='paper', q='Z-scheme', scope='wang2020state')
```

## Read

```python
get(kind='paper', id='wang2020state')                  # overview
get(kind='paper', id='wang2020state', view='abstract') # abstract only
get(kind='paper', id='wang2020state', view='toc')      # hierarchical TOC
get(kind='paper', id='wang2020state~38')               # block 38
get(kind='paper', id='wang2020state~38..42')           # block range
```

The id syntax also supports view paths:

```python
get(kind='paper', id='wang2020state/abstract')
get(kind='paper', id='wang2020state/toc')
get(kind='paper', id='wang2020state/cite/bib')
```

Slug pattern: `wang2020state` (whole), `wang2020state~38` (chunk),
`wang2020state~38..42` (range).

## Navigate

The `view='toc'` output is **hierarchical** — section/subsection
ranges are detected from heading patterns and laid out as a jump
table:

```
# acheson2026automated — TOC (177 blocks, 20 sections)

  ~0..7     (8)   <untitled>  (preview from first block)
  ~8..20    (13)  ■ INTRODUCTION
  ~21..40   (20)  ■ THEORY
  ~41..73   (33)  ■ METHODS
    ~43..53   (11)  Physics-Informed Program Synthesis [PIPS]
    ~54..58   (5)   Calculation Details
    ~59..63   (5)   Heterodiatomic Molecules
    ~64..73   (10)  Alkanes
  ~74..116  (43)  ■ RESULTS & DISCUSSION
    …
```

To **drill into a section**, use the combined chunk-range + view path
form:

```python
get(kind='paper', id='wang2020state~74..116/toc')   # TOC of just this range
get(kind='paper', id='wang2020state~74..116')       # read this range
```

Each response ends with a column-aligned "Next:" block that suggests
the next likely call (next/previous range, parent TOC, citation, …)
so the agent can keep navigating without re-reading the help.

## Cite

```python
get(kind='paper', id='wang2020state', view='bibtex')
get(kind='paper', id='wang2020state', view='ris')
get(kind='paper', id='wang2020state', view='endnote')
```

## Coming in later phases

- `put(kind='paper', ...)` — paper edits and tags. Phase 5+.
- `get(id='doi:10.1002/...')` and `get(id='arxiv:2207.09327')` — URI
  scheme prefixes. Phase 4 acatome-quest-mcp territory.
- `view='representatives'`, `view='methods'`, etc. — semantic section
  views. Phase 5+.

## See also

- `precis-overview` — verbs and kinds
- `precis-relations` — `related-to`, `contradicts` between papers
- `precis-tags` — `topic:`, `SRC:`, `DENSITY:*`
- `precis-memory-help` — capturing thoughts from a paper
