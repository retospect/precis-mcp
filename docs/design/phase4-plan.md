# Phase 4 — Cache-backed kinds

> Status: **active**. Phase 3 done. Phase 3.5 (navigation parity) is
> queued — see `phase3.5-plan.md`.

## Scope

Three kinds + the shared cache infrastructure they need:

1. **`math`** — Wolfram Alpha (paid, deterministic; cache-friendly)
2. **`youtube`** — transcript fetch (free; cache by video ID)
3. **`web`** — stored URL bookmarks + retrieval (mixed)

Plus four Perplexity-backed kinds (`websearch`, `think`, `research`)
deferred to phase 4b. Same Perplexity backend, three different model
profiles.

**Why these together**: they all share the same architectural piece
— a `cache_state` row keyed by query/canonicalized URL, with TTL and
provenance, plus a legally-mandated **attribution footer** on every
response. (See memory `14238a0c` re. legal compliance.)

## Schema additions (migration `0002`)

The schema already has a `cache_state` table (migration 0001) with
columns `(provider, key, fetched_at, ttl_seconds, body, meta)`. Phase
4 adds:

- A unique index on `(provider, key)` if not already present
- `attribution` column on `cache_state` (the literal text to render
  beneath responses; per-provider boilerplate)
- An optional `cost_estimate_usd` numeric column for `[cost: $X]`
  trailers

Aim for additive only — phase 1 sealed `0001_initial.sql`.

## Architecture: `CacheBackedHandler` mixin

A new abstract in `src/precis/handlers/_cache_base.py`:

```python
class CacheBackedHandler(Handler):
    provider: str          # cache_state.provider
    ttl_seconds: int        # default freshness window
    attribution: str        # legal footer; appended to every response

    def _get_or_fetch(self, key: str, fetch_fn) -> Cached:
        """Look up cache; on miss, call fetch_fn() and store the result."""
        ...
```

Every cache-backed kind subclasses this. The base class:
- Looks up `(provider, key)` in `cache_state`
- On hit within TTL → return cached body
- On miss / stale → call `fetch_fn()` (subclass-defined), store, return
- Always renders `attribution` as a footer, regardless of cache hit
- Emits a hint with cost estimate

## Per-kind specs

### `math` — Wolfram Alpha

- Endpoint: `https://api.wolframalpha.com/v2/query` (paid)
- Auth: `WOLFRAM_APP_ID` env var
- Cache key: SHA-256 of the canonical query string
- TTL: forever (Wolfram results are deterministic for a fixed query)
- Attribution: *"Computed by Wolfram|Alpha. https://www.wolframalpha.com"*
- Cost: ~$0.002/call → footer renders `[cost: ~$0.002 — cached]`
  on cache hit, `[cost: ~$0.002]` on miss

```python
get(kind='math', q='population of Ireland')
get(kind='math', q='orbital period of Jupiter')
```

Surfaces: `q=` (one-shot), `id=` (canonical query), pod ranges via
`view='pods/3..5'` (defer if too complex for v1 spike).

### `youtube` — transcript fetch

- Endpoint: `youtube_transcript_api` package (no API key needed; uses
  the public YouTube transcript endpoint)
- Cache key: video ID (the `dQw4w9WgXcQ` part)
- TTL: 30 days (transcripts very rarely change)
- Attribution: *"Transcript from YouTube. https://www.youtube.com/watch?v=<id>"*
- Cost: free → `[cost: free]`

```python
get(kind='youtube', id='dQw4w9WgXcQ')
```

Auto-strip URL form: `id='https://youtu.be/dQw4w9WgXcQ'` → extract id.

### `web` — bookmarks + page fetch

Two-mode kind:

**Bookmark mode** (stored): refs in `web` corpus, slugs minted from
canonicalized URLs.
```python
put(kind='web', text='https://example.com/article', tags=['topic-x'])
get(kind='web', id='example.com/article')   # by canonical
search(kind='web', q='something')           # by stored text
```

**Page-fetch mode** (cached): on `get` of an unbookmarked URL, fetch
+ extract readable content + cache it as a transient ref.
```python
get(kind='web', id='https://arxiv.org/abs/2207.09327')  # fetches if uncached
```

- Endpoint: `httpx` + `trafilatura` for extraction
- Cache key: URL after `url_canonical.canonicalize(...)` (port from v1)
- TTL: 7 days (web pages mutate often)
- Attribution: *"Source: https://example.com/article (fetched YYYY-MM-DD)"*
- Cost: free (just bandwidth) but rate-limit per domain

This is the biggest of the three because of the `web_archive.py` /
`url_canonical.py` baggage from v1. May get split into 4a (math +
youtube) and 4b (web).

## Suggested commit sequence

1. **0002_cache.sql** — schema additions (add `attribution`,
   `cost_estimate_usd`, indexes)
2. **`CacheBackedHandler` base + tests** — mock fetch_fn, verify cache
   hit/miss/expiry logic, attribution rendering
3. **`MathHandler` + tests** — first concrete kind; mock the Wolfram
   HTTP call
4. **`YouTubeHandler` + tests** — second concrete kind; same pattern
5. **`url_canonical.py` port** from v1 (~150 LOC, pure logic)
6. **`WebHandler` + tests** — both bookmark and page-fetch modes
7. **Skill drafts** — `precis-web-help.md`, `precis-math-help.md`,
   `precis-youtube-help.md`

## Done criteria

- `pytest -q` ≥ 280 tests green (228 → ~280 with phase 4)
- `precis serve` exposes `math`, `youtube`, `web` kinds
- Every cache-backed response carries an attribution footer
- Cost trailers emit on each response
- ~600-800 LOC added across handlers + base + tests

## Phase 4b — Perplexity (queued)

`websearch` (Sonar), `think` (Sonar Reasoning Pro), `research` (Sonar
Deep Research). All three share `_PerplexityBase` from v1. Likely
separate phase because:
- API key required (PERPLEXITY_API_KEY)
- Different attribution + cost profile per model
- Research is multi-step / async-ish (~minutes)

## Open questions for the user

1. **HTTP client**: stick with `httpx` (v1 used it) or try the stdlib
   `urllib`? V1 already has the patterns; reuse.
2. **Web extraction**: `trafilatura` (v1) or `readability-lxml`?
   Trafilatura is heavier but the v1 `web.py` is built around it.
3. **`web` storage corpus**: separate `web` corpus or stuff into
   `default`? V1 used a separate corpus; matches mental model better.
