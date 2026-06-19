---
id: precis-wikipedia-help
title: precis — on-demand Wikipedia lookup
summary: Resolve a query to the best Wikipedia article and fetch its plain-text extract — cache-backed, fenced out of default search
applies-to: get/search/tag/link (kind='wikipedia')
status: active
---

# precis-wikipedia-help — on-demand Wikipedia lookup

`wikipedia` is the **on-demand** encyclopedic kind. It is the deliberate
alternative to bulk-embedding a Wikipedia dump (~30M chunks, ~200 GB
resident HNSW, a permanent precision tax on every search). Instead, you
fetch exactly the article you need, when you need it.

A `get(kind='wikipedia', id='<query>')`:

1. runs the MediaWiki **search** API (CirrusSearch/Elasticsearch) to
   resolve your query to the single best-matching article, then
2. fetches that article's **plain-text extract** (clean prose, no
   wikitext markup) and caches it 7 days.

The body is block-split + embedded by the standard pipeline, so
`search(kind='wikipedia', q=...)` lands hits inside articles you've
already pulled. No API key; free (bandwidth only); always current.

## Look up a topic / definition
## What is X — fetch the Wikipedia article
## Resolve a query to one article

```python
get(kind='wikipedia', id='CRISPR gene editing')
get(kind='wikipedia', q='attention mechanism transformer')   # q= also works
get(kind='wikipedia', id='Claude Shannon')
```

You pass a **query**, not an exact title — CirrusSearch picks the
article (`"attention mechanism transformer"` → *Transformer (deep
learning)*). The ref title is the resolved article name; the response
footer carries the canonical article URL. The cache key is your
lower-cased, whitespace-collapsed query, so `"CRISPR Gene Editing"` and
`"crispr   gene editing"` share one row.

No match → a stub body naming the query (the cache row is still
written). Long articles are capped at 60 KB with a `[…truncated]`
marker.

## Other languages

```python
# default is en.wikipedia.org; override per-process via env:
#   PRECIS_WIKIPEDIA_LANG=de
```

## The `ORIGIN:wikipedia` fence — why wiki hits don't show up by default
## Why isn't my Wikipedia article in cross-kind search?

Every `wikipedia` ref is auto-stamped with the closed-vocab provenance
tag **`ORIGIN:wikipedia`**, and that tag is **fenced out of default and
cross-kind search** — the same mechanism that hides `DREAM:speculative`
inspirations. This is the whole point of on-demand fetching: tertiary
encyclopedic prose never competes with your curated paper library for
top-k slots.

The fence **lifts** in exactly two cases:

```python
search(kind='wikipedia', q='soft attention weights')   # explicit scope → wiki hits
search(q='soft attention', tags=['ORIGIN:wikipedia'])  # explicit opt-in → wiki included
```

By contrast these **exclude** Wikipedia content:

```python
search(q='soft attention weights')                     # default → fenced
search(kind='*', q='soft attention weights')           # cross-kind fan → fenced
search(kind='paper,wikipedia', q='...')                # multi-kind incl. wikipedia → fenced*
```

\* a multi-kind fan that *names* `wikipedia` still fences unless you also
pass `tags=['ORIGIN:wikipedia']`; use `kind='wikipedia'` alone to browse
the wiki corpus.

You cannot un-stamp the fence tag on this kind — `untags=['ORIGIN:wikipedia']`
is ignored. It is provenance, not a user label.

## Search across fetched articles

```python
search(kind='wikipedia', q='retrieval augmented generation')
search(kind='wikipedia', q='dopamine receptor', page_size=20)
```

Hybrid lexical + semantic over the bodies of articles already in the
cache. Results are `<slug>~N` handles; paste into `get` to drill in.
`search` does not crawl — only fetched articles are searchable. To pull
a *new* article, use `get` (which runs the live search + extract).

## Keep / annotate an article

```python
get(kind='wikipedia', id='Photosynthesis')             # populate cache first

tag(kind='wikipedia', id='photosynthesis', add=['bookmark', 'topic:co2'])
tag(kind='wikipedia', id='photosynthesis', add=['CACHE:pinned'])   # never expire
```

Open tags (`bookmark`, `topic:x`, …) and `CACHE:` always allowed. The
`ORIGIN:wikipedia` stamp is automatic and survives re-fetches.

## Cross-link an article to a paper / memory / todo

```python
link(kind='wikipedia', id='photosynthesis',
     target='paper:wang2020state', rel='related-to')

link(kind='wikipedia', id='photosynthesis',
     target='memory:42')                               # rel defaults to related-to
```

Use Wikipedia as a **grounding / definition layer** the curated corpus
can point at — not as a citation. Cite the primary source, not this
fetch (text is CC BY-SA, a tertiary summary).

## See also

```python
get(kind='skill', id='precis-overview')        # verbs and kinds
get(kind='skill', id='precis-web-help')        # arbitrary URL fetch (kind='web')
get(kind='skill', id='precis-search-help')     # search mechanics, fencing
get(kind='skill', id='precis-tags')            # axis vocabulary (ORIGIN, …)
```
