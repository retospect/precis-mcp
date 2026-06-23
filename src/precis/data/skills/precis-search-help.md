---
id: precis-search-help
title: precis — the search verb (mechanics, pagination, filters)
summary: hybrid lexical and semantic search — pagination, tag filters, scope, exclude, cross-kind fan-out
applies-to: search (every kind that supports it)
status: active
---

# precis-search-help — search across kinds

Hybrid lexical + semantic search. Returns ranked handles (`pc<chunk_id>`,
e.g. `pc40`) you paste straight into `get(id=…)` to drill in — the handle's
prefix infers the kind. Order is the relevance signal — there is no honest
numeric score.

## What knobs does search have?
## Quick reference for search arguments
## How do I call search?

```python
search(q='photocatalysis')                          # fan out across all kinds
search(kind='paper', q='photocatalysis')            # one kind
search(kind='paper,patent', q='photocatalysis')     # several kinds
search(kind='paper', q='X', page=2, page_size=20)       # paginate
search(kind='paper', q='X', tags=['topic:noxrr'])   # tag-filter
search(kind='paper', q='X', scope='wang2020state')  # search inside one ref
search(kind='paper', q='X', exclude=['wang2020state', 'kim2024electro'])
search(kind='patent', q='X', source='remote')       # patent-only knob
search(kind='paper', q='1.523 eV', mode='lexical')  # exact string, no embedding
```

## Ranking mode — hybrid (default), lexical, or semantic

By default `search` is **hybrid**: it fuses a lexical pass (Postgres
full-text) with a semantic pass (embeddings) by reciprocal-rank fusion.
That's the right default for "find me things about X". But you can pin
the ranking with `mode=`:

| `mode` | What it does | Reach for it when |
|---|---|---|
| `'hybrid'` *(default)* | RRF of lexical + semantic. | General recall — concepts *and* keywords. |
| `'lexical'` | Postgres FTS only; no embedding. | You know the **exact string** — an identifier, acronym, surname, code token, a numeric like `1.523 eV`, or an exact phrase. Embeddings blur these; lexical is precise and deterministic. Also the honest tool when the embedder is down (hybrid silently degrades to this anyway). |
| `'semantic'` | Embedding cosine only. | Pure conceptual / paraphrase recall where the wording won't match but the meaning does, and keyword noise is hurting precision. Degrades to lexical if the embedder is unavailable. |

```python
search(kind='paper', q='MoS2 monolayer', mode='lexical')   # exact term recall
search(q='ways to stop catalyst poisoning', mode='semantic') # paraphrase recall
```

`mode=` works on a single kind **and** across the cross-kind fan-out.
Scores are never comparable *across* modes (RRF score vs. cosine
distance vs. lexical rank) — within a result list, more-relevant is
always first.

| Arg | Type | Meaning |
|---|---|---|
| `q` | str | Free-text query. |
| `mode` | str | Ranking strategy: `'hybrid'` (default) / `'lexical'` / `'semantic'`. See below. |
| `kind` | str | One kind, comma-list, or `'*'` / `'all'` / `'any'` / `''` for fan-out. |
| `page` | int | Page number (default 1). |
| `page_size` | int | **Page size** (default 10, max 100). Not a match-quality cutoff despite the name. |
| `tags` | list[str] | Per-kind tag filters; AND semantics. |
| `scope` | str | Restrict to one ref's blocks. |
| `exclude` | list[str] | Skip-list (specific slugs to drop). `page=` is the normal pagination. |
| `source` | str | Patent only: `'both'` (default) / `'local'` / `'remote'`. |
| `view` | str | Alternate result shape. `view='dreamable'` returns a salience-focus-region pick from the most-due seed (cross-kind only; `q=` not required for this view). `view='stubs'` returns the paper-acquisition backlog — paper refs with an external id but no PDF yet (`q=` ignored; see `precis-stubs-help`). |
| `angle` | float | Salience-rotation search; pairs with `like=` (or `q=` for a seed). See `precis-dreaming-help`. |
| `like` | str | Seed ref handle for `angle=` search; e.g. `like='pc40'` (a handle also works) or the legacy `like='paper:wang2020state~5'`. |
| `status` | str | Finding-only shorthand for `tags=['STATUS:<value>']`. Default is `'established'` (the "what evidence do we have?" cohort); pass `'tracing'`/`'multi_candidate'`/`'dead_chain'` for a specific cohort, or `'*'` for all findings regardless. Ignored on every other kind. |

## Search the whole corpus
## Find something but I don't know which kind
## Cross-kind search — let the runtime pick

```python
search(q='Z-scheme photocatalysis')             # all kinds
search(kind='*', q='topic:x')                   # explicit wildcard
search(kind='paper,patent', q='Z-scheme')       # subset via comma-list
```

When `kind=` is omitted (or `'*'` / `'all'` / `'any'` / `''`), search
fans out across every kind whose handler supports it. Each hit is
tagged with its source kind. Streams merge by rank, so a strong hit
in `memory` can out-rank a weaker hit in `paper`.

## See more results
## Page through search hits beyond the first page
## What if there are more hits than I see?

```python
search(kind='paper', q='photocatalysis', page=2)
search(kind='paper', q='photocatalysis', page=3, page_size=20)
```

`page=1` is the default. Bump `page=` to walk results. `page_size=` sets
the page size (default 10, max 100) — *not* a quality cutoff despite
the name.

## Filter search results by tag
## Find refs tagged with topic:X
## Combine search with a tag axis

```python
search(kind='paper', q='photocatalysis', tags=['topic:noxrr'])

search(kind='patent', tags=['cpc:B01J27/24', 'country:ep'])

search(kind='todo', tags=['STATUS:open', 'PRIO:high'])

search(kind='memory', q='', tags=['pinned'])
```

AND semantics — `tags=['A', 'B']` matches refs carrying *both* tags.
Closed-vocab axes (`STATUS:`, `PRIO:`, `SRC:`, `CACHE:`) are kind-gated;
open tags (`topic:`, `project:`, `pinned`, ...) are universal. See
`precis-tags` for the axis matrix.

## Search inside a specific paper or ref
## Where does this paper mention X?
## Scope a search to one ref's contents

```python
search(kind='paper', q='Z-scheme', scope='wang2020state')
search(kind='patent', q='heterojunction', scope='ep4123456a1')
```

`scope=` restricts to one ref's blocks. Useful for "where in this
paper does X come up?"

## Drop specific refs from results
## Hand-skip known-irrelevant papers
## Search but ignore these slugs

```python
search(kind='paper', q='photocatalysis',
       exclude=['wang2020state', 'kim2024electro'])
```

Paper-level — chunk selectors and DOIs both resolve to the bare slug;
unknown slugs are silently ignored. `exclude=` is the skip-list for
known-irrelevant refs, not a paging mechanism — use `page=` for that.

## Find the right skill for a task
## Which skill explains how to do X?
## Discover a skill by topic

```python
search(kind='skill', q='how do I edit a markdown file')
search(kind='skill', q='paginate paper search')
search(kind='skill', q='patent prior art')
```

Natural-language queries work — phrase your query the way you'd ask
it. Skill hits whose subject kind isn't loaded in the current build
are prefixed `[unwired]` so you don't follow them to no-op verbs.
This is the standard first move on any non-trivial task.

## Find patents not yet in the local store
## Search EPO directly via OPS
## How do I find a patent that isn't ingested yet?

```python
search(kind='patent', q='photocatalysis', source='remote')
search(kind='patent', tags=['cpc:B01J27/24'], source='remote')
```

`source=` is patent-only. `'both'` (default) merges local + remote;
`'local'` skips OPS; `'remote'` returns only patents *not* already
in the local store. CQL details in `precis-patent-search-help`.

## See also

```python
get(kind='skill', id='precis-overview')             # verbs and kinds
get(kind='skill', id='precis-paper-help')           # paper-specific search shape
get(kind='skill', id='precis-patent-search-help')   # CQL + source= matrix
get(kind='skill', id='precis-tags')                 # axis vocabulary
get(kind='skill', id='precis-relations')            # link vocabulary
get(kind='skill', id='precis-toc-help')             # drilling into hits via /toc
```
