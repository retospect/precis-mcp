---
id: precis-web-help
title: precis — fetch, bookmark, and search web pages
status: shipped
tier: 1
floor: any
applies-to: get / search / tag / link (kind='web')
last-updated: 2026-05-02
---

# precis-web-help — fetch, bookmark, and search web pages

`web` fetches a URL, extracts the readable article body, embeds
it paragraph-by-paragraph, and caches the result for **7 days**.
Free (bandwidth only). The surface is four verbs:

| Verb | Use |
|---|---|
| `get` | Fetch and read a URL (cache-backed) |
| `search` | Full-text + semantic search across previously-fetched pages |
| `tag` | Bookmark, pin, or classify a cached page |
| `link` | Cross-reference to `memory:` / `paper:` / `todo:` / … |

```python
get(kind='web', id='https://example.com/article')
get(kind='web', id='https://blog.langchain.com/agentic-rag/')
get(kind='web', id='https://arxiv.org/abs/2207.09327')
```

## Canonicalization

URL variants of the same page collapse to one cache row:

- Scheme/host case (`HTTPS://Example.COM/x` → `https://example.com/x`)
- Default ports (`:80` for http, `:443` for https are dropped)
- Tracking params (`utm_*`, `fbclid`, `gclid`, `mc_cid`, …)
- Trailing `/` (except the bare root)
- Fragments (`#anchor`) — *unless* the host is a known SPA
  (`arxiv.org`, `github.com`, `gist.github.com`, `notion.so`)

## What you get back

A markdown rendering of the **article body only** — chrome (nav,
sidebar, footer, ads) is stripped by trafilatura. Links are preserved
as `[text](url)`; images are dropped.

The response footer carries the source URL and fetched-on date for
attribution.

## Failure modes

- `BadInput: not a valid URL` — input wasn't an http(s) URL
- `Upstream: HTTP 4xx / 5xx` — the page returned an error
- `Upstream: fetch failed` — network/DNS/TLS error
- *Stub body* `(no readable content extracted from URL — page may
  require JS, login, or have non-article shape)` — trafilatura
  couldn't find article-shaped content. The cache row is still
  written so we don't pay the network round-trip again for 7 days.

## Caching

7-day TTL; free upstream so cost is always `[cost: free]`. The agent
can force a refresh by deleting the ref (phase 5+) or simply waiting
out the TTL.

## Required env

Optional: set `WEB_USER_AGENT` to override the default User-Agent
header. Some sites have stricter anti-bot middleware that may need
this.

## Search across fetched pages

Fetched pages are block-parsed (paragraph / heading / list / code)
and embedded per-block, so `search` runs the same hybrid lexical +
semantic leg that `paper` / `memory` / `oracle` get.

```python
search(kind='web', q='retrieval-augmented generation')
search(kind='web', q='dopamine D1 D2', top_k=20)
```

Result body format matches every other searchable kind: a
`## slug~pos  (score=0.xxxx)` block per hit, with a short
excerpt. Slugs come from the canonical URL so the same page
always shows up under the same handle.

Cross-kind search works too — pass `kind='*'` or
`kind='paper,web'` to the top-level `search` tool.

## Bookmark with tags

Tag a fetched slug to flag it for later. Slugs appear in
`/recent` listings and in search hit headings:

```python
# Fetch first (populates the cache + gives you a slug)
get(kind='web', id='https://example.com/article')

# Bookmark it — open tag, free vocabulary
tag(kind='web', id='example-com-article', add=['bookmark'])

# Topic classification — any open tag works
tag(kind='web', id='example-com-article',
    add=['topic-rag', 'read-later'])

# Pin the cache so it never expires
tag(kind='web', id='example-com-article', add=['CACHE:pinned'])

# Remove a tag
tag(kind='web', id='example-com-article', remove=['read-later'])
```

**Closed prefixes** on `web`: only `CACHE:` (pinned / fresh /
stale / expired) is allowed — cache provenance tracking. Use
open tags for everything else (`bookmark`, `topic-...`,
`read-later`, project labels, …).

## Link to memory / papers / todos

Cross-reference a fetched page to anything else in the corpus.
Canonical form: `kind:identifier[~selector]`.

```python
# Capture a memory about why you kept this page
put(kind='memory', text="Need this for the RAG architecture review")
# → memory ref id=42

# Link the web page to that memory
link(kind='web', id='example-com-article', target='memory:42')

# Or link to a paper for supplementary reading
link(kind='web', id='example-com-article',
     target='paper:wang2020state')

# Or to a todo that this page informs
link(kind='web', id='example-com-article',
     target='todo:158', rel='supports')

# Remove
link(kind='web', id='example-com-article',
     target='memory:42', mode='remove')
```

The relation defaults to `related-to`. See `precis-relations` for
the full vocabulary (`cites`, `contradicts`, `supports`,
`derived-from`, …).

## Typical workflow

1. `get(kind='web', id='<url>')` — fetch and read.
2. `tag(kind='web', id='<slug>', add=['bookmark', 'topic-x'])` —
   mark for recall.
3. `link(kind='web', id='<slug>', target='memory:N')` — tie to
   the reason you kept it.
4. Weeks later:
   - `search(kind='web', q='...')` — find it by content.
   - `get(kind='web', id='/recent')` — browse all cached pages.
   - `get(kind='memory', id=N)` to land on the memory; its links
     will surface the bookmarked URL.

## See also

- `precis-overview` — verbs and kinds
- `precis-cache` — TTL, freshness, attribution, cost trailers
- `precis-tags` — closed vs. open tag vocabulary
- `precis-relations` — link relation slugs (`cites`, `supports`, …)
