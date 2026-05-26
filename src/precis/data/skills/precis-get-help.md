---
id: precis-get-help
title: precis — the get verb (read or compute)
status: active
tier: 1
floor: any
applies-to: get (every kind that supports it)
last-updated: 2026-05-24
---

# precis-get-help — read a ref or compute a value

`get` is the read verb. Two flavours under one shape:

- **Read** — fetch an existing ref by id (and optional view).
  Used by `paper`, `patent`, `memory`, `markdown`, …
- **Compute** — pass a query as `q=` (or as `id=` for some kinds);
  the handler computes a result and returns it. Used by `calc`,
  `math` (Wolfram), `python` (callgraph / runtrace).

```python
get(kind='paper', id='wang2020state')             # read
get(kind='paper', id='wang2020state', view='abstract')
get(kind='math', q='population of Ireland')      # compute
get(kind='python', view='callgraph',
    args={'entry': 'pkg.mod:func', 'depth': 3})  # structured args
```

## Arguments

| Arg | Type | Default | Meaning |
|---|---|---|---|
| `kind` | str | required | Which kind to read from. |
| `id` | str / int | None | Identifier — slug for slug kinds, int for numeric kinds. Some kinds accept `id` *or* `q`. |
| `view` | str | None | Display variant. Kind-specific (`'abstract'`, `'bibtex'`, `'biblio'`, `'toc'`, `'cite/bib'`, …). |
| `q` | str | None | Free-text query. Used by compute-style kinds in lieu of `id`. |
| `args` | dict | None | Typed extra parameters. Reserved keys (`kind`, `id`, `view`, `q`) are rejected. |

## `args=` — typed extras for views that need them

Some views need more than a single id / view string. Pass a dict via
`args=`. The handler's help skill documents the accepted shape per
view:

```python
# python callgraph
get(kind='python', view='callgraph',
    args={'entry': 'pkg.mod:func', 'depth': 3})

# python runtrace
get(kind='python', view='runtrace',
    args={'entry': 'pkg.mod:main',
          'argv': ['--flag', 'value'],
          'timeout': 10})
```

Reserved keys (`kind`, `id`, `view`, `q`) inside `args=` are
rejected with `BadInput`. Pass them as the explicit positional
kwargs instead — keeps the call shape unambiguous.

## Listing pages — `id` omitted

Many kinds let you omit `id` to get a default page (typically the
N most recently created or accessed):

```python
get(kind='paper')            # latest 50 papers
get(kind='patent')           # latest patents (page by ingest time)
get(kind='memory')           # latest memories
get(kind='skill')            # skill index — start here for discovery
```

## Cross-kind addressing tricks

- **Bare DOI as `id=`** for `kind='paper'` — the parser resolves
  the DOI to its slug transparently. Example:
  `get(kind='paper', id='10.1038/nature10352')`.
- **Selectors with `~`** — `get(kind='paper', id='wang2020~38')`
  reads block 38; `~38..42` reads a range.
- **Special `id` paths** — `id='/recent'` (cross-kind list of
  recent refs), `id='/published'` (patent-specific ingest-by-pub-
  date list), etc. Per-kind; see each kind's help skill.

## View paths — `id='slug/view'`

Slug-kinds also accept the view as a path suffix. The kwarg `view=`
and the path `id='slug/<view>'` accept the same vocabulary:

```python
get(kind='paper', id='wang2020state/abstract')
get(kind='paper', id='wang2020state', view='abstract')   # equivalent
get(kind='paper', id='wang2020state/cite/bib')           # nested view
```

Exception: **DOI form does not accept view paths** (DOI suffixes can
contain `/`; the parser can't disambiguate). With a DOI, use the
kwarg: `get(kind='paper', id='10.1038/nature10352', view='toc')`.

## Compute-style kinds

For these, `q=` (or `id=`) is the query and the handler computes
a fresh result, caching it durably:

- **`calc`** — local sympy-backed calculator. Pass an expression as
  `q=` or `id=`.
- **`math`** — Wolfram Alpha. Same shape; results pinned (no TTL).
- **`web` / `wolfram` / `youtube` / `research` / `think` /
  `websearch`** — cache-backed kinds; results have per-kind TTL.

See `precis-cache` for the cache lifecycle and cost trailers.

## Per-kind notes

Discover them via `search(kind='skill', q='<kind>')`. Examples:

- **paper** — see `precis-paper-help` for views, DOI shortcuts,
  selector grammar.
- **patent** — see `precis-patent-help` for OPS integration and
  the `view='biblio'` / `'claims'` / `'description'` matrix.
- **python** — see `precis-python-help` for callgraph, runtrace,
  and the `args=` shape.
- **markdown / plaintext / tex** — see `precis-files-help` for
  the universal address grammar (`id='slug~selector'`).

## See also

- `precis-overview` — kinds and verbs at a glance
- `precis-search-help` — the discovery verb
- `precis-edit-help` — the region-edit verb
- `precis-files-help` — universal address grammar
