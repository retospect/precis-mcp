---
id: precis-patent-power
title: precis — patent search, the power-user CQL surface
status: draft
tier: 2
floor: power-user
applies-to: search (kind='patent', q=<CQL>)
last-updated: 2026-04-29
---

# precis-patent-power — raw CQL for `kind='patent'`

This is the **power-user** companion to `precis-patent-help`. Read
that one first for the day-to-day shape (`q=` + `tags=`).

When you need surgical patent searches — Boolean combinations,
date windows, applicant exclusions, citation-class restrictions,
specific publication numbers — drop into **OPS CQL**. The
patent handler accepts CQL verbatim in `q=` and forwards it to EPO
OPS, then merges the hits with your local store the same way as
keyword searches.

## What you can express that simple `q=` can't

```python
# Title XOR abstract; date window; exclude a major applicant
search(kind='patent', q='''
    (ti="metal-organic framework" OR ab="MOF synthesis")
    and pd within "2020 2025"
    and not pa=basf
''')

# Specific CPC subclass plus inventor surname plus 5-year freshness
search(kind='patent', q='''
    cpc=B01J27/24 and in="smith j" and pd within "2020 2026"
''')

# Only granted EP patents (B-suffix kind codes), pulled from a specific family
search(kind='patent', q='ep=Y and pn=EP1234567 and kind="B*"')

# Hunt for prior art that cites a known publication
search(kind='patent', q='ct=EP1234567 and pd within "2024 2026"')

# Multi-applicant joint patents (single applicant queries don't catch these)
search(kind='patent', q='pa="university of limerick" and pa=intel')
```

These translate into one OPS round-trip plus the usual local-side
hybrid pass; the merged result still marks already-stored hits as
`[local]`.

## CQL field reference

OPS exposes a wider grammar than precis-tags can model. The most
useful fields:

| Field   | Meaning                                  | Example                  |
|---------|------------------------------------------|--------------------------|
| `ti=`   | title                                    | `ti=graphene`            |
| `ab=`   | abstract                                 | `ab=membrane`            |
| `ta=`   | title + abstract                         | `ta="MOF synthesis"`     |
| `txt=`  | title + abstract + description + claims  | `txt="single-atom catalyst"` |
| `pa=`   | applicant                                | `pa=siemens`             |
| `in=`   | inventor                                 | `in="smith j"`           |
| `cpc=`  | CPC class (or subclass)                  | `cpc=B01J27/24`          |
| `ipc=`  | IPC class                                | `ipc=H01M`               |
| `pn=`   | publication number                       | `pn=EP1234567`           |
| `ap=`   | application number                       | `ap=EP21712345`          |
| `pd=`   | publication date or range                | `pd=2024` / `pd within "2018 2025"` |
| `prd=`  | priority date                            | `prd within "2010 2015"` |
| `kind=` | kind code (publication type)             | `kind=B1` / `kind="B*"`  |
| `ct=`   | cited reference (any direction)          | `ct=EP1234567`           |
| `inct=` | inventor's country                       | `inct=US`                |
| `pact=` | applicant's country                      | `pact=DE`                |
| `pn=EP*`| publication-number wildcard              | `pn="EP*"` (EP only)     |

Boolean operators: `and`, `or`, `not`. Grouping with parentheses.
Phrase queries with double quotes. Wildcards with `*` (must be at
the end of a token).

Date forms: `pd=YYYY`, `pd=YYYY-MM-DD`, `pd within "YYYY YYYY"`,
`pd>=YYYY`, `pd<=YYYY`.

OPS's full CQL is documented at
[ops.epo.org → CQL guide](https://www.epo.org/en/searching-for-patents/data/web-services/ops);
the table above covers >95% of practical queries.

## Mixing `q=` with `tags=`

Tags and CQL **compose** — the handler appends translated tag
filters to whatever you pass in `q=` before calling OPS:

```python
search(kind='patent',
       q='ti=graphene and ab=membrane and pd within "2020 2025"',
       tags=['cpc:B01J27/24', 'country:ep'])
# becomes (effectively):
#   ti=graphene and ab=membrane and pd within "2020 2025"
#   and cpc=B01J27/24 and pact=EP
```

Use `tags=` for repeating filters you'd add to many searches
(your usual CPC class, your jurisdiction); use `q=` for the
variable, query-of-the-day part.

## Saved CQL via watches

The CLI watch runner takes the same CQL you'd use in `q=`:

```sh
precis jobs watch-patents 'cpc=B01J27/24 and pa="university of limerick"'
precis jobs watch-patents 'ti=nanobud or ab=nanobud' --every 1d
precis jobs watch-patents 'pa=basf and cpc=B01J' --auto-get
```

Each pass diffs against the publication numbers seen previously
and queues new hits into `quest:patents-pending-review` (or
ingests directly with `--auto-get`).

## OPS quirks worth knowing

- **Phrase tokenisation is field-specific.** `ti="metal-organic
  framework"` matches the bigram in titles; `ab="metal-organic
  framework"` matches in abstracts. There is no cross-field phrase.
- **Wildcards only post-token.** `pn=EP*` works; `pn=*1234567` does
  not. Use a different field if you need leading-wildcard.
- **No fuzziness.** OPS doesn't do edit-distance / typo-tolerant
  matching. The local leg's lexical+semantic fusion catches
  near-misses on already-ingested patents.
- **`pa=` is right-anchored phrase.** `pa=siemens` matches
  "Siemens AG", "Siemens Energy", etc. — fine for most uses.
  Pin with `pa="siemens ag"` (quoted) if needed.
- **OPS rate-limits separately by endpoint.** Search and biblio
  use different daily / weekly quotas; busy days can lock out
  search while individual `get(id=…)` keeps working.
- **Result paging tops out at 100 / page, 2000 total.** If you
  ask for more, the handler caps silently and notes it in the
  response trailer.

## Failure modes

- `BadInput: invalid CQL query` — OPS returned 400 with a parse
  error. The error trailer carries OPS's exact diagnostic and
  the canonicalised CQL the handler sent.
- `Upstream: HTTP 403` — quota exceeded or bad creds.
  Fair-use cap is 4 GB rolling / week.
- `Upstream: HTTP 429` — short-term rate limit. Back off and retry.

## See also

- `precis-patent-help` — entry-level skill (the friendly `q=` form)
- `precis-tags` — the tag conventions used here
- `docs/search-future-filters.md` — what's coming for non-CQL agents
- EPO OPS docs — `https://www.epo.org/en/searching-for-patents/data/web-services/ops`
