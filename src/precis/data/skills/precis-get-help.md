---
id: precis-get-help
title: precis — the get verb (read or compute)
applies-to: get (every kind that supports it)
status: active
---

# precis-get-help — read a ref or compute a value

`get` is the read verb. Two shapes under one call:

- **Read** — fetch an existing ref by `id=` (slug or numeric). Used by
  `paper`, `patent`, `memory`, `markdown`, …
- **Compute** — pass `q=` (or `id=` for some kinds) and the handler
  computes a fresh result. Used by `calc`, `math`, `web`, `youtube`,
  `research`, `think`, `websearch`.

```python
get(kind='paper', id='wang2020state')                    # read
get(kind='paper', id='wang2020state', view='abstract')   # read + view
get(kind='math', q='population of Ireland')              # compute
```

## What knobs does get have?
## Quick reference for get arguments
## How do I call get?

| Arg | Type | Meaning |
|---|---|---|
| `kind` | str | Required. Which kind to read from. |
| `id` | str / int | Identifier — slug for slug kinds, int for numeric. Some kinds accept `id` *or* `q`. |
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

## Page through a listing
## See more than the first page
## What if there are more refs than fit on one page?

```python
get(kind='paper', page=2)
get(kind='paper', page=3, top_k=20)   # top_k = page size (default 10, max 100)
```

`page=1` is the default. `top_k=` is the page size, not a quality
cutoff. `exclude=[…]` is for hand-skipping known slugs, not paging.

## Address a chunk or sub-range inside a ref
## Read just block 38, or blocks 38..42
## What does slug~N mean?

```python
get(kind='paper', id='<slug>~38')         # one block
get(kind='paper', id='<slug>~38..42')     # block range
get(kind='paper', id='<slug>', view='toc')
get(kind='paper', id='<slug>~38..42', view='toc')   # sub-TOC of a range
```

Paste a TOC handle (`slug~A..B`) as `id=` to drill in. The address
grammar is shared across TOC-capable kinds — see `precis-overview`.

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
