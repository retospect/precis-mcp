---
id: precis-web-help
title: precis — fetch, bookmark, and search web pages
summary: URL fetching and bookmarking — readable article extraction, cache-backed, link preservation
applies-to: get/search/tag/link (kind='web')
status: active
---

# precis-web-help — fetch, bookmark, search web pages

`web` is the cache-backed URL kind. Pass a URL as `id=` (or `q=` to
search) and get the readable article body back. Cache key is the
canonical URL.

## Fetch a URL and read it
## Open a web page by URL
## I have a link — how do I read it?

```python
get(kind='web', id='https://blog.langchain.com/agentic-rag/')
get(kind='web', q='https://blog.langchain.com/agentic-rag/')      # q= also works
get(kind='web', id='https://arxiv.org/abs/2207.09327')
```

Returns the article body as markdown — chrome (nav, sidebar, footer,
ads) is stripped. Links preserved as `[text](url)`; images dropped.
Footer carries source URL and fetch date for attribution. First call
fetches and caches; subsequent calls hit the cache.

Extraction needs the optional `trafilatura` dependency on the server.
If it's absent the call fails with `Upstream error: trafilatura not
installed` — the feature is unavailable on this deployment; contact
the admin to install it.

If extraction yields nothing the body is a stub
(`(no readable content extracted from URL — page may require JS,
login, or have non-article shape)`); the cache row is still written.

## What does a web id look like?
## URL canonicalisation rules
## How are URLs normalised before caching?

A web id is the canonical form of an http(s) URL. The cache key is
derived by:

- Lowercasing scheme and host.
- Dropping default ports (`:80`, `:443`).
- Stripping tracking params (`utm_*`, `fbclid`, `gclid`, `mc_cid`, …).
- Removing trailing `/` (except the bare root).
- Dropping `#fragment` — **except on SPA hosts** (see next section).

After canonicalisation the URL is the id and the slug. The same page
served from `HTTPS://Example.COM/x?utm_source=tw` and
`https://example.com/x` is one cache row.

## Why did the same URL produce different cache entries?
## What happens to #fragments and tracking params?
## Per-host fragment handling — the SPA gotcha

Fragments (`#anchor`) are dropped on most hosts but **preserved on
known SPA hosts** where the fragment is part of the route:

- `arxiv.org`
- `github.com`, `gist.github.com`
- `notion.so`

Consequence: on `example.com`, `…/page#a` and `…/page#b` collapse to
one cache entry (`…/page`). On `github.com`, `…/repo#readme` and
`…/repo#issues` are **two different cache entries**. If you fetched
the same URL twice and got different bodies, check whether the host
is in the SPA list and whether your URLs differ only by `#fragment`.

Tracking params (`utm_*`, `fbclid`, …) are always stripped. A URL
with and without `?utm_source=x` is always the same cache entry.

## Search across fetched pages
## Find a previously-cached page by content
## Where did I read about X?

```python
search(kind='web', q='retrieval-augmented generation')
search(kind='web', q='dopamine D1 D2', page_size=20)
search(kind='web', q='RAG', page=2)
```

Hybrid lexical + semantic over the bodies of pages already in the
cache. Results are `<slug>~N` handles; paste into `get` to drill in.
Only fetched pages are searchable — `search` does not crawl.

## Search across web pages and other kinds
## Cross-kind search including web
## Find something across papers and bookmarked pages

```python
search(q='Z-scheme photocatalysis')                # all kinds
search(kind='paper,web', q='retrieval augmentation')
```

Each hit is tagged with its source kind.

## Bookmark a page with tags
## Mark a cached page as topic:X
## Annotate a fetched URL

```python
get(kind='web', id='https://example.com/article')      # fetch to populate cache

tag(kind='web', id='https://example.com/article',
    add=['bookmark', 'topic:rag', 'read-later'])

tag(kind='web', id='https://example.com/article',
    add=['CACHE:pinned'])                              # never expire

tag(kind='web', id='https://example.com/article',
    remove=['read-later'])
```

Closed-prefix axes for web: `CACHE:` only. Open tags
(`bookmark`, `topic:x`, ...) always allowed.

## Cross-link a page to a memory, paper, or todo
## Tie a bookmarked URL to something else in the corpus
## Connect a web page to other refs

```python
put(kind='memory', text='Need this for the RAG review')
# → memory ref id=42

link(kind='web', id='https://example.com/article',
     target='memory:42')                              # rel defaults to related-to

link(kind='web', id='https://example.com/article',
     target='paper:wang2020state', rel='cites')

link(kind='web', id='https://example.com/article',
     target='todo:158', rel='supports')

link(kind='web', id='https://example.com/article',
     target='memory:42', mode='remove')
```

## See also

```python
get(kind='skill', id='precis-overview')        # verbs and kinds
get(kind='skill', id='precis-search-help')     # search mechanics
get(kind='skill', id='precis-tags')            # axis vocabulary
get(kind='skill', id='precis-relations')       # link relation slugs
get(kind='skill', id='precis-memory-help')     # capturing why you kept a page
```
