---
id: precis-perplexity-help
title: precis — Perplexity (websearch / think / research)
status: phase-5
tier: 1
floor: any
applies-to: get/search/put/tag/link (kind='websearch' | 'think' | 'research'); put accepts mode='import'
last-updated: 2026-05-02
---

# precis-perplexity-help — Perplexity Sonar tiers

Three cache-backed kinds wrap the Perplexity Sonar API at different
price/latency points. All three also accept `put(mode='import')` so
that Perplexity Pro subscribers can paste the result of a free
web-UI query and have it cached at $0.

| kind         | model                  | typical latency | cost/call |
|--------------|------------------------|-----------------|-----------|
| `websearch`  | `sonar`                | 2–5s            | ~$0.001   |
| `think`      | `sonar-reasoning-pro`  | 5–30s           | ~$0.005   |
| `research`   | `sonar-deep-research`  | 2–10 min        | ~$0.50    |

Cache TTLs: `websearch` 7 days, `think` 30 days, `research` pinned.

## Get — paid API path

```python
get(kind='websearch', id='who is the CEO of Anthropic')
get(kind='think',     id='compare DAC and BECCS for net-negative emissions')
get(kind='research',  id='landscape of post-quantum signature schemes')
```

The response body carries the answer with inline `[N]` citations and
a trailing `Sources:` block of underlying URLs. Cache hits return
the same body for free; the cost trailer reads `— cached`. Imported
entries (see below) read `— imported` so you can distinguish
user-supplied bodies from API-cached ones at a glance.

## Recent — list cached refs per kind

```python
get(kind='research')              # same as id='/recent'
get(kind='research', id='/recent')
get(kind='research', id='/')
```

Returns up to 20 refs of this kind, newest first, with slug, title,
provenance (`imported` vs `fetched`), and last-update date. The
listing path never calls the Perplexity API — it works with no
`PERPLEXITY_API_KEY` set, so pure importers can use it.

## Put (mode='import') — free Pro-subscriber path

If you have Perplexity Pro, deep research is **free in the web UI**.
Paste the result here and the same cache row a paid `get` would have
created is populated at $0:

```python
put(kind='research',
    id='landscape of post-quantum signature schemes',
    text='<paste the report markdown>',
    mode='import')
```

Subsequent `get(kind='research', id='landscape of post-quantum
signature schemes')` then hits the cache for $0 — no API call.

The pasted body is parsed as Markdown and split into one block per
heading / paragraph / list / table / fenced code, so per-block
citation handles work and `search(kind='research', q='...')` finds
granular hits rather than the whole report.

The `id=` you pass becomes part of the canonical cache key (combined
with the handler's model). It must match the query you would later
pass to `get`. Trailing/leading whitespace is trimmed, but
otherwise the strings are compared verbatim.

Imported entries:

- are pinned (no expiry — they don't depend on web freshness),
- record `meta.source = 'imported'` on both the ref and the cache row,
- carry `cost_usd = 0` so the cost trailer reads `[cost: free]`.

Re-importing the same query replaces the previous import atomically.

## Choosing kinds across imports

Match the model that produced the report:

- A "Quick answer" or short factual lookup → `websearch`.
- A "Reasoning Pro" / "Reasoning" output → `think`.
- A long "Deep Research" report (most common for imports) → `research`.

The cache key includes the model, so the same `id=` imported under
two different kinds creates two distinct cache rows. That's by design
— a `think` summary and a `research` deep dive on the same question
are different artefacts.

## Failure modes

- `BadInput: <kind> requires a non-empty query` — empty `id=`.
- `BadInput: <kind> only supports mode='import' for put` — `put` is
  scoped to imports today; other modes will land in later phases.
- `BadInput: import requires text=` — empty body.
- `Upstream: PERPLEXITY_API_KEY not set` — raised only when a
  cache-miss `get` actually needs to call the API. Imports,
  `/recent`, and cache hits never trigger this.
- `Upstream: HTTP 401 / 429 / 5xx` — paid API path only.

## Required env

`PERPLEXITY_API_KEY` is **only** required for the paid API path
(cache-miss `get`). Imports, `/recent` listings, cache hits, and
bulk CLI imports all work without a key — the kind is always
available, and the fetch path raises `Upstream` with a clear
error only when it actually needs the key.

## Bulk CLI import

```
precis jobs import-perplexity ./reports/ --kind research
precis jobs import-perplexity ./reports/ --kind research --dry-run
precis jobs import-perplexity ./reports/ --query-from filename
```

Walks the directory for `*.md` files, derives the `id=` query from
the first H1 heading (falling back to the filename), and bulk-calls
`put(mode='import')` for each. `--dry-run` prints the derived query
per file without touching the DB.

## See also

- `precis-overview` — verbs and kinds
- `precis-cache` — TTL, freshness, attribution, cost trailers
- `precis-markdown-help` — block parser + slug rules used by imports
