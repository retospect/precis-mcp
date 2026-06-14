---
id: precis-patent-power
title: precis — raw CQL for patent search (power-user)
summary: raw CQL patent search — Boolean queries, field-scoped lookups, date windows, citation-graph filters
applies-to: search (kind='patent', q=<CQL>)
status: active
---

# precis-patent-power — raw CQL for `kind='patent'`

CQL is passed verbatim as `q=`. Use it when you need Boolean
combinations, field-scoped queries, date windows, or
citation-graph filters. For the friendly `q=` form see
`precis-patent-search-help`.

## Pass a CQL string to search
## How do I run a raw CQL query?
## Send EPO CQL through the patent verb

```python
search(kind='patent', q='cpc=B01J27/24 and pd within "2020 2025"')
search(kind='patent', q='ti=graphene and ab=membrane')
search(kind='patent', q='pa=siemens and not pa=basf')
```

The string is forwarded to OPS verbatim and merged with local
hits the same way as bare keywords. `[local]` marks hits already
in the store.

## CQL field reference
## What fields can I scope to?
## Which CQL operators does OPS accept?

| Field   | Meaning                                  | Example                  |
|---------|------------------------------------------|--------------------------|
| `ti=`   | title                                    | `ti=graphene`            |
| `ab=`   | abstract                                 | `ab=membrane`            |
| `ta=`   | title + abstract                         | `ta="MOF synthesis"`     |
| `txt=`  | title + abstract + description + claims  | `txt="single-atom catalyst"` |
| `pa=`   | applicant                                | `pa=siemens`             |
| `in=`   | inventor                                 | `in="smith j"`           |
| `cpc=`  | CPC class or subclass                    | `cpc=B01J27/24`          |
| `ipc=`  | IPC class                                | `ipc=H01M`               |
| `pn=`   | publication number                       | `pn=EP1234567`           |
| `ap=`   | application number                       | `ap=EP21712345`          |
| `pd=`   | publication date or range                | `pd=2024` / `pd within "2018 2025"` |
| `prd=`  | priority date                            | `prd within "2010 2015"` |
| `kind=` | kind code                                | `kind=B1` / `kind="B*"`  |
| `ct=`   | cited reference (either direction)       | `ct=EP1234567`           |
| `inct=` | inventor country                         | `inct=US`                |
| `pact=` | applicant country                        | `pact=DE`                |

Booleans: `and`, `or`, `not`. Parentheses group. Phrases use
double quotes. `*` wildcards are post-token only (`pn=EP*` works;
`pn=*1234567` does not).

Dates: `pd=YYYY`, `pd=YYYY-MM-DD`, `pd within "YYYY YYYY"`,
`pd>=YYYY`, `pd<=YYYY`.

## Worked CQL examples
## Show me real CQL queries I can adapt
## Recipes for common patent searches

```python
# Title-or-abstract topic, 5-year window, exclude one applicant
search(kind='patent', q='''
    (ti="metal-organic framework" OR ab="MOF synthesis")
    and pd within "2020 2025"
    and not pa=basf
''')

# CPC subclass + inventor + freshness
search(kind='patent',
       q='cpc=B01J27/24 and in="smith j" and pd within "2020 2026"')

# Granted EP only (B-suffix kind codes)
search(kind='patent', q='pn=EP* and kind="B*"')

# Citation-graph: prior art that cites a known publication
search(kind='patent', q='ct=EP1234567 and pd within "2024 2026"')

# Joint patents — single-applicant queries miss these
search(kind='patent', q='pa="university of limerick" and pa=intel')

# Specific family member
search(kind='patent', q='pn=EP1234567 and kind="B*"')
```

## Combine CQL with tag filters

```python
search(kind='patent',
       q='ti=graphene and pd within "2020 2025"',
       tags=['cpc:B01J27/24', 'country:ep'])
# effective CQL sent to OPS:
#   ti=graphene and pd within "2020 2025"
#   and cpc=B01J27/24 and pact=EP
```

Tag filters append to whatever you pass in `q=`. Use `tags=` for
the filters you'd repeat across many searches (your CPC class,
jurisdiction); use `q=` for the variable part.

## Save a CQL query as a watch
## Run a CQL search on a schedule
## How do I get notified when new patents match?

```sh
precis jobs watch-patents 'cpc=B01J27/24 and pa="university of limerick"' --name limerick-cat
precis jobs watch-patents 'ti=nanobud or ab=nanobud' --name nanobud --every 1d
precis jobs watch-patents 'pa=basf and cpc=B01J' --name basf-b01j
precis jobs watch-patents 'cpc=Y02E60/13' --name h2 --max-per-pass 5
```

Watches require strict CQL — bare keywords are rejected at create
time. `--every` accepts hours (`1h`), days (`7d`, default), or
weeks (`2w`). `--max-per-pass` caps ingest per pass.

New hits are ingested directly into the patent kind. Overflow past
`--max-per-pass` is dropped and resurfaces next pass
(oldest-publication-date first). Triage afterwards with
`search(kind='patent', q='…')` against the freshly ingested rows.

Manage:

```sh
precis jobs list-patent-watches              # NAME · EVERY · LAST RUN · SEEN
precis jobs list-patent-watches --show-cql
precis jobs run-patent-watches               # one-shot pass over due watches
precis jobs run-patent-watches --name limerick-cat --dry-run
precis jobs watch-patents --name limerick-cat --delete
```

## OPS quirks worth knowing

- **Phrases are field-local.** `ti="metal-organic framework"`
  matches the bigram in titles only; no cross-field phrase.
- **Wildcards are post-token.** `pn=EP*` yes, `pn=*1234567` no.
- **No fuzziness.** OPS doesn't do edit-distance. The local leg
  catches near-misses on already-ingested patents.
- **`pa=` is right-anchored phrase.** `pa=siemens` matches
  "Siemens AG", "Siemens Energy". Pin with `pa="siemens ag"`.
- **Endpoint-level rate limits.** Search and biblio have separate
  quotas — search can lock out while `get(id=...)` still works.
- **Paging caps at 100/page, 2000 total.** Over-asks are silently
  capped and noted in the response trailer.

## Failure modes

- `BadInput: invalid CQL query` — OPS returned 400. The trailer
  carries OPS's exact diagnostic and the canonicalised CQL sent.
- `Upstream: HTTP 403` — quota exceeded or bad creds. Fair-use
  cap is 4 GB rolling / week.
- `Upstream: HTTP 429` — short-term rate limit; back off and retry.

## See also

```python
get(kind='skill', id='precis-patent-search-help')   # friendly q= form, source= matrix
get(kind='skill', id='precis-patent-help')          # read patents (get, views, slugs)
get(kind='skill', id='precis-search-help')          # cross-kind search mechanics
get(kind='skill', id='precis-tags')                 # tag axes that lift to CQL
```
