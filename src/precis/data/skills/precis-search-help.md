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
search(kind='paper', q='X', scope='pa5')            # search inside one ref (handle; slug still resolves)
search(kind='paper', q='X', exclude=['pa5', 'pa12'])    # skip refs by handle (slugs still resolve)
search(kind='patent', q='X', source='remote')       # patent-only knob
search(kind='paper', q='1.523 eV', mode='lexical')  # exact string, no embedding
search(kind='paper', q='X', queries=['rephrase 1','rephrase 2'],
       answers=['a passage an ideal source would contain'],
       per_paper=2, page_size=30)                   # broad / high-recall (see below)
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
| `queries` | list[str] | **Broad retrieval** (paper): extra question rephrasings, each fused as its own ranked leg. Up to 8. See below. |
| `answers` | list[str] | **Broad retrieval** (paper): hypothetical answer passages (HyDE) — short paragraphs you'd *expect* a relevant chunk to read like; embedded and fused. Up to 8. See below. |
| `per_paper` | int | **Broad retrieval** (paper): cap hits per paper to spread results across more sources (breadth triage). |

## Broad retrieval — when the gold hides behind the wording
## Find more, better, more-diverse chunks for one question
## High-recall paper search (multi-query + HyDE)

A single phrasing is fragile: the best chunk often loses just because it
words the idea differently than you did. Instead of firing 5 separate
searches and eyeballing each, hand `search` **several angles at once** and
let it fuse them. A chunk that surfaces across phrasings rises to the top.

Two knobs, both paper-side, both fused with `q` by reciprocal-rank fusion:

- `queries=[…]` — **rephrasings of the question** (synonyms, broader /
  narrower framings, the sub-questions hiding inside it). Up to 8.
- `answers=[…]` — **hypothetical answer passages** (HyDE): write 1–3
  short paragraphs the way you'd expect an *ideal source chunk* to read,
  and let their embeddings pull in real chunks that look like them. This
  is often the single biggest lever for technical queries — the fake
  answer lives in "chunk space", not "question space". Up to 8.

```python
search(
    kind='paper',
    q='does single-atom Cu help nitrate-to-ammonia selectivity?',
    queries=[
        'single-atom copper catalyst NO3RR selectivity',
        'Cu coordination environment ammonia faradaic efficiency',
        'isolated Cu sites suppress hydrogen evolution nitrate',
    ],
    answers=[
        'Isolating Cu as single atoms on an N-doped carbon support '
        'raises NH3 faradaic efficiency to ~90% by weakening *NO '
        'binding and suppressing the competing hydrogen-evolution '
        'reaction, shifting selectivity toward ammonia.',
    ],
    per_paper=2,        # at most 2 chunks per paper → broader spread
    page_size=30,       # widen the net so the fused set surfaces
)
```

Then **poke around** before you trust a hit: paste any returned handle
into `get(id='pc…')` to read it in full, and `search(kind='paper',
scope='pa…', q='…')` to read more of that paper around the chunk. Cite
or write a memory once you've confirmed the context — don't cite off the
keyword row alone.

Rules of thumb:
- Reach for this on **research / triage** questions ("what does the
  corpus say about X?"), not exact-string lookups — for an identifier or
  acronym use `mode='lexical'`.
- 3–5 `queries` + 1–2 `answers` is plenty; more legs ≠ better.
- `per_paper=2` is a good default when you want *breadth* (many papers);
  drop it when you want to mine one paper deeply.
- Honors `mode=` (a `'lexical'` broad search fuses only the text legs),
  `tags=`, `scope=`, `exclude=`, and the year filters like any search.

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
search(kind='paper', q='Z-scheme', scope='pa5')              # handle from get/search output
search(kind='paper', q='Z-scheme', scope='wang2020state')    # legacy slug, still resolves
search(kind='patent', q='heterojunction', scope='ep4123456a1')
```

`scope=` restricts to one ref's blocks. Useful for "where in this
paper does X come up?"

## Drop specific refs from results
## Hand-skip known-irrelevant papers
## Search but ignore these refs

```python
search(kind='paper', q='photocatalysis',
       exclude=['pa5', 'pa12'])                       # handles from output
search(kind='paper', q='photocatalysis',
       exclude=['wang2020state', 'kim2024electro'])   # legacy slugs, still resolve
```

Ref-level — a handle (`pa<id>`), slug, chunk selector, or DOI all resolve to
the underlying ref; unknown entries are silently ignored. `exclude=` is the skip-list for
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
