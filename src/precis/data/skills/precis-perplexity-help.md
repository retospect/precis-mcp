---
id: precis-perplexity-help
title: precis — Perplexity (websearch / perplexity-reasoning / perplexity-research)
summary: Perplexity Sonar tiers — websearch, perplexity-reasoning, perplexity-research; latency/cost trade-offs, import mode
applies-to: get/search/put/tag/link (kind='websearch' | 'perplexity-reasoning' | 'perplexity-research')
status: active
renamed-kinds: kind 'think' → 'perplexity-reasoning'; kind 'research' → 'perplexity-research' (renamed 2026-06-15)
---

# precis-perplexity-help — Perplexity Sonar, three tiers

Three paid, cache-backed kinds wrap Perplexity Sonar at different
price/latency points. All three accept `put(mode='import')` so Pro
subscribers can paste a free web-UI answer at $0.

| kind                   | use for                              | latency  | cost/call |
|------------------------|--------------------------------------|----------|-----------|
| `websearch`            | fast factual lookup, single question | 2–5s     | ~$0.001   |
| `perplexity-reasoning` | comparison, reasoning, trade-offs    | 5–30s    | ~$0.005   |
| `perplexity-research`  | long report, landscape survey        | 2–10 min | ~$0.50    |

## Ask Perplexity a question
## Run a websearch / perplexity-reasoning / perplexity-research call
## I need an answer from Perplexity

```python
get(kind='websearch',            q='who is the CEO of Anthropic')
get(kind='perplexity-reasoning', q='compare DAC and BECCS for net-negative emissions')
get(kind='perplexity-research',  q='landscape of post-quantum signature schemes')
```

`id=` and `q=` are equivalent. The response body carries the answer
with inline `[N]` citations and a trailing `Sources:` block. Cache
hits return the same body for free.

## Pick the right kind
## Which model do I want — websearch, perplexity-reasoning, or perplexity-research?
## When to use which tier

- One fact, one URL would do → `websearch`.
- Needs reasoning across several sources, or a comparison → `perplexity-reasoning`.
- Multi-section report, broad landscape, deep dive → `perplexity-research`.

Switching kinds mid-investigation re-spends — see the cache-key
warning below.

## Avoid re-spending on the same query
## How do I not pay twice for Perplexity?
## The cache key includes the model — what does that mean?

Cache keys are `<model>:<query>`. The same `q=` under `websearch`,
`perplexity-reasoning`, and `perplexity-research` are three distinct cache
rows. Switching kinds on the same question issues a fresh paid call each time.

```python
get(kind='websearch',            q='post-quantum signature schemes')   # cached as websearch:...
get(kind='perplexity-reasoning', q='post-quantum signature schemes')   # NEW paid call (perplexity-reasoning:...)
get(kind='perplexity-research',  q='post-quantum signature schemes')   # NEW paid call (perplexity-research:...)
```

Pick the tier first, then call. If you must escalate, accept the
new spend — there is no upgrade path that reuses a cheaper row.

## Import a free Pro web-UI answer
## Paste a Perplexity answer I ran in the browser
## How do I cache a free web-UI result at $0?

Pro subscribers run answers free in the browser. Paste the result
to populate the same cache row a paid `get` would create:

```python
put(kind='perplexity-research',
    id='landscape of post-quantum signature schemes',
    text='<paste the report markdown>',
    mode='import')
```

`get(kind='perplexity-research', id='landscape of post-quantum signature
schemes')` then hits the cache for $0.

The `id=` you import under must match the query you later `get` —
trim only; comparison is otherwise verbatim. Re-importing the same
`id=` replaces the previous body atomically. Imported entries are
pinned and carry `meta.source = 'imported'`; the cost trailer
reads `— imported` so you can tell them apart from API-fetched rows.

The pasted Markdown is split per heading / paragraph / list /
table / code fence, so `search(kind='perplexity-research', q='...')` returns
granular block handles, not the whole report.

## List recent calls under a kind
## See what's already cached for websearch / perplexity-reasoning / perplexity-research
## What have I asked Perplexity lately?

```python
get(kind='perplexity-research')                # same as id='/recent'
get(kind='perplexity-research', id='/recent')
```

Returns up to 20 refs newest-first with slug, title, provenance
(`imported` vs `fetched`), and date. Never calls the API — works
without `PERPLEXITY_API_KEY`.

## Force a fresh call
## Bypass the Perplexity cache for one query
## How do I re-run a stale answer?

```python
delete(kind='perplexity-reasoning', id='<canonical-query>')
get(kind='perplexity-reasoning', q='<query>')
```

`perplexity-research` is pinned by default; `perplexity-reasoning` has
a 30-day TTL; `websearch` has 7 days. See `precis-cache` for the full
TTL table and the `CACHE:fresh` / `CACHE:stale` / `CACHE:pinned` axis.

## Bulk-import a directory of reports

```text
precis jobs import-perplexity ./reports/ --kind perplexity-research
precis jobs import-perplexity ./reports/ --kind perplexity-research --dry-run
precis jobs import-perplexity ./reports/ --query-from filename
```

Walks `*.md`, derives `id=` from the first H1 (or filename), and
calls `put(mode='import')` per file. `--dry-run` prints derived
queries without writing.

## When Perplexity fails

- `BadInput: <kind> requires a non-empty query` — empty `id=`.
- `BadInput: <kind> only supports mode='import' for put` — `put` is
  scoped to imports.
- `BadInput: import requires text=` — empty body.
- `Upstream: PERPLEXITY_API_KEY not set` — raised only on cache-miss
  `get`. Imports, `/recent`, and cache hits work without a key.
- `Upstream: HTTP 401 / 429 / 5xx` — paid API path only.

## Required env

`PERPLEXITY_API_KEY` is required only for paid `get` cache misses.
Imports, `/recent`, and cache hits never need it.

## See also

```python
get(kind='skill', id='precis-overview')        # verbs and kinds
get(kind='skill', id='precis-cache')           # TTLs, force-refresh, CACHE:* axis
get(kind='skill', id='precis-math-help')       # facts and world data (Wolfram)
get(kind='skill', id='precis-web-help')        # direct page fetch
get(kind='skill', id='precis-markdown-help')   # block parser used by imports
```
