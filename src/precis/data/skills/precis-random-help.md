---
id: precis-random-help
title: precis — random corpus pick
summary: corpus serendipity — random block pick for discovery, warm-up, sanity checks
applies-to: get(kind='random')
status: active
---

# precis-random-help — stumble into something you didn't know to ask for

`random` is a discovery kind. One call, one pick: a canonical handle
into the corpus plus a drill-down hint. Useful for warm-up,
inspiration, sanity-checking a fresh corpus.

## Stumble into a random block of the corpus
## Pick something I didn't know to ask for
## Warm up by sampling whatever's in here

```python
get(kind='random')
```

Returns the handle of one undeleted embedded block, a short preview,
and a `get(...)` call that fetches the block in full. Repeat the call
to keep spinning.

```text
# random
`paper:miller2000food~5`

Food security depends on resilient supply chains that …

Next:
  get(kind='paper', id='miller2000food~5') — read this block
  get(kind='random')                       — another random pick
```

The handle is copy-pasteable into any `link=` target or `id=`. Numeric
kinds (memory / todo / …) drop the `~pos`; slug kinds keep it.

## Sanity-check a fresh corpus

If `get(kind='random')` raises `NotFound: corpus has no embedded
blocks`, ingest hasn't run yet — ask the user to run the embed worker,
or wait for the background job to land. A successful pick means the
store is alive and at least one block has an embedding.

## Mint a random short identifier
## Get a unique opaque handle for a tag or correlation id

```python
get(kind='random', view='slug')                              # 4-char default
get(kind='random', view='slug', args={'len': 8})             # longer
get(kind='random', view='slug', args={'alphabet': 'alnum'})  # a-z + 0-9
```

Returns a fresh short string — no markdown wrapper. Use it as a tag
value (`session:k7m3`), a correlation id, or any opaque handle where
uniqueness matters more than meaning. Don't use it as a human-readable
slug (encode meaning instead) or as a cryptographic key (too short).

Length clamps to `[1, 64]`. Alphabets: `crockford` (default, 32 chars,
no 0/o/1/l), `lower` (a-z), `alnum` (a-z + 0-9), or a literal string
of ≥ 2 distinct characters.

## See also

```python
get(kind='skill', id='precis-overview')      # verbs and kinds
get(kind='skill', id='precis-paper-help')    # where most picks land
get(kind='skill', id='precis-oracle-help')   # tradition-scoped pick
```
