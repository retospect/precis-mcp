---
id: precis-web-help
title: precis — fetch and read web pages
status: phase-4
tier: 1
floor: any
applies-to: get (kind='web')
last-updated: 2026-04-26
---

# precis-web-help — fetch and read web pages

`web` fetches a URL, extracts the readable article body, and caches
the markdown for **7 days**. Free (bandwidth only).

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

## What's *not* in phase 4

- **Bookmark mode** (durable `put` of a URL with tags + notes) —
  phase 4b.
- **Wayback / archive integration** — phase 4b.
- **Search across cached pages** — comes free once block embeddings
  are wired through the existing fused-search path; phase 5.

## See also

- `precis-overview` — verbs and kinds
- `precis-cache` — TTL, freshness, attribution, cost trailers
