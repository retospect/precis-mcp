---
id: precis-get-help
title: precis — the get verb (read or compute)
summary: the get verb — read existing refs by id, or compute fresh results via q=
applies-to: get (every kind that supports it)
status: active
---

# precis-get-help — read a ref or compute a value

`get` is the read verb. Two shapes under one call:

- **Read** — fetch an existing ref by its **handle** (`<2-char type
  code><decimal id>`, e.g. `pa5` a paper, `me47` a memory) — the canonical
  address shown in search/get output, copy it straight back **including the
  2-char prefix** (never strip it: `pa5`, not `5`). Legacy forms still resolve
  *for the kinds that have them*: a slug for slug-keyed kinds (`paper`,
  `patent`, `draft` — e.g. `wang2020state`), a bare number **only** for
  int-keyed kinds (`memory`, `todo`, `job`, …). A bare number is **not** a
  paper address — `get(kind='paper', id=5)` is read as a cite_key and fails;
  use its `pa5` handle.
- **Compute** — pass `q=` (or `id=` for some kinds) and the handler
  computes a fresh result. Used by `calc`, `math`, `web`, `youtube`,
  `perplexity-research`, `perplexity-reasoning`, `websearch`.

```python
get(id='pa5')                                            # read by handle (prefix infers kind)
get(kind='paper', id='pa5', view='abstract')             # read + view
get(kind='paper', id='wang2020state')                    # legacy slug, still resolves
get(kind='math', q='population of Ireland')              # compute
```

## What knobs does get have?
## Quick reference for get arguments
## How do I call get?

| Arg | Type | Meaning |
|---|---|---|
| `kind` | str | Required. Which kind to read from. |
| `id` | str | Identifier — the **handle** (`<2-char code><id>`, e.g. `pa5`, `me47`) is canonical; copy it with its prefix. A legacy slug resolves for slug-keyed kinds (paper/patent/draft); a bare number resolves **only** for int-keyed kinds (memory/todo/…), never for a paper. Some kinds accept `id` *or* `q`. |
| `view` | str | Display variant. Kind-specific (`'abstract'`, `'toc'`, `'bibtex'`, `'cite/bib'`, …). |
| `q` | str | Free-text query for compute-style kinds. |
| `args` | dict | Typed extras for views that need them. Reserved keys (`kind`, `id`, `view`, `q`) are rejected. |

## Pass typed extras to a view
## Some views need more than a slug and a name
## How do I supply structured arguments?

```python
get(kind='python', view='callgraph',
    args={'entry': 'pkg.mod:func', 'depth': 3})

get(kind='python', view='runtrace',
    args={'entry': 'pkg.mod:main',
          'argv': ['--flag', 'value'],
          'timeout': 10})
```

Reserved keys (`kind`, `id`, `view`, `q`) inside `args=` raise
`BadInput` — pass them as explicit kwargs.

## Browse what's in a kind without knowing any id
## List the default page for a kind
## What if I don't have a slug yet?

```python
get(kind='skill')      # every active skill — discovery starts here
get(kind='paper')      # recent papers (one page)
get(kind='patent')     # recent patents
get(kind='memory')     # recent memories
```

Default pages return a slice of recent refs. On a large corpus this is
not "all of them" — use `search` for content-driven discovery.

## Filter a listing by kind-specific view
## How do I list only open todos / due flashcards / upcoming crons?
## What's the `/<filter>` shape in id=?

```python
get(kind='todo', id='/open')        # open + doing + blocked
get(kind='todo', id='/doing')       # by literal STATUS
get(kind='todo', id='/done')
get(kind='flashcard', id='/due')    # SM-2: due now or within 3 days
get(kind='gripe', id='/wontfix')    # STATUS:wontfix retrospect view
get(kind='memory', id='/sticky')    # sticky:thread ∪ sticky:global
get(kind='cron', id='/upcoming')    # next-fire-ordered queue
get(kind='<any>', id='/recent')     # universal — most-recent-N
```

`id='/<filter>'` is a kind-specific virtual selector. `recent` works
on every numeric-ref kind. Other filters are per-kind — see the
relevant `precis-<kind>-help` for the full list (an unknown filter
errors with the supported set as `options`).

## Page through a listing
## See more than the first page
## What if there are more refs than fit on one page?

```python
search(kind='paper', q='perovskite', offset=20)   # next page of hits
```

`get(kind='paper')` does not paginate — only `search` does, via
`offset=N`. `exclude=[…]` is for hand-skipping known slugs, not paging.

## Address a chunk or sub-range inside a ref
## Read just block 38, or blocks 38..42
## What does slug~N mean?

```python
get(id='pc38')                            # one block by handle (prefix infers kind)
get(kind='paper', id='<slug>~38')         # legacy single-block form, still resolves
get(kind='paper', id='<slug>~38..42')     # block range (ranges keep the slug form)
get(kind='paper', id='<slug>', view='toc')
get(kind='paper', id='<slug>~38..42', view='toc')   # sub-TOC of a range
```

A single chunk is addressed by its handle `pc<chunk_id>` (e.g. `pc38`) — what
search and TOC output now show; the legacy `slug~38` still resolves on input.
Ranges stay `slug~A..B`. The grammar is shared across TOC-capable kinds — see
`precis-overview` and `precis-addressing-help`.

## Pick a view by path or by kwarg
## slug/view and view= are equivalent
## What's the difference between id='slug/abstract' and view='abstract'?

```python
get(kind='paper', id='wang2020state/abstract')
get(kind='paper', id='wang2020state', view='abstract')   # equivalent
get(kind='paper', id='wang2020state/cite/bib')           # nested view path
```

Exception: bare-DOI ids don't take a view path (DOI suffixes contain
`/`). Use the kwarg:

```python
get(kind='paper', id='10.1038/nature10352', view='toc')
```

## Address a paper by DOI instead of slug
## I have a DOI, not a slug
## Skip the slug lookup when I already know the identifier

```python
get(kind='paper', id='10.1038/nature10352')
get(kind='paper', id='10.1038/nature10352', view='bibtex')
```

The DOI resolves to its slug transparently. If the DOI isn't ingested,
the call raises — register a finding via `precis-finding-help`.

## Compute a fresh answer instead of reading a ref
## Use get as a calculator or fact lookup
## When do I pass q= instead of id=?

```python
get(kind='calc', q='42 * 365')                # local arithmetic, free
get(kind='math', q='speed of light in km/h')  # Wolfram, paid
get(kind='web', q='https://example.com/page') # fetch + extract a URL
get(kind='youtube', q='dQw4w9WgXcQ')          # transcript
```

Results cache durably per kind. See `precis-cache` for TTLs and the
cost trailer.

## Find the help skill for a specific kind
## Where do I read about kind X?

```python
search(kind='skill', q='<kind>')
```

`paper` → `precis-paper-help` (views, DOI shortcuts, selectors).
`patent` → `precis-patent-help` (OPS, `view='biblio'` / `'claims'`).
`python` → `precis-python-help` (callgraph, runtrace, `args=`).
`markdown` / `plaintext` / `tex` → `precis-files-help` (file address
grammar).

## See also

```python
get(kind='skill', id='precis-overview')      # verbs and kinds
get(kind='skill', id='precis-search-help')   # the discovery verb
get(kind='skill', id='precis-edit-help')     # region edits
get(kind='skill', id='precis-files-help')    # file-backed address grammar
get(kind='skill', id='precis-cache')         # paid-tool caching, TTLs
get(kind='skill', id='precis-finding-help')  # chasing un-ingested DOIs
```
