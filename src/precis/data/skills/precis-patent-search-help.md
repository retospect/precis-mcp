---
id: precis-patent-search-help
title: precis — search patents via EPO OPS (search × patent)
status: active
tier: 2
floor: any
applies-to: search (kind='patent')
last-updated: 2026-05-24
---

# precis-patent-search-help — `search(kind='patent', ...)`

This skill covers the patent-specific search nuances: the
`source=` matrix, the local + remote merge, prior-art sweep mode,
OPS CQL lift, and saved watches. For general patent reading
(`get`, views, slugs, OPS quotas) see `precis-patent-help`. For
the cross-kind `search` mechanics (`top_k`, `exclude=`, `tags=`)
see `precis-search-help`.

> **Availability**: this skill and the `kind='patent'` registration
> only appear when **`EPO_OPS_CLIENT_KEY`**,
> **`EPO_OPS_CLIENT_SECRET`**, and **`PRECIS_PATENT_RAW_ROOT`**
> are all set in the server's environment. Free credentials at
> [developers.epo.org](https://developers.epo.org).

## Shape

```python
search(kind='patent', q='photocatalytic NOx reduction')
search(kind='patent', q='Z-scheme photocatalysis', top_k=20)

# Filter by closed-prefix tags (auto-applied at ingest from biblio)
search(kind='patent', q='photocatalysis',
       tags=['cpc:B01J27/24', 'country:ep'])
search(kind='patent', q='MOF synthesis',
       tags=['applicant:siemens-ag', 'kind:b1'])

# Scope to one already-ingested patent
search(kind='patent', q='Z-scheme', scope='ep4123456a1')
```

## The `source=` matrix

Patent search has one knob beyond the cross-kind shape: which leg
runs.

| `source=` | What it returns | Use when |
|---|---|---|
| `'both'` (default) | Local hits ∪ OPS hits, merged by DOCDB id | General-purpose discovery |
| `'local'` | Only patents you've already fetched | Offline; reviewing curation |
| `'remote'` | Only OPS hits **not yet** in your local store | **Prior-art sweep** mode |

```python
search(kind='patent', q='amine carbon capture', source='local')
#                                                       → only patents I've curated
search(kind='patent', q='amine carbon capture', source='remote')
#                                                       → only OPS hits NOT yet
#                                                         in my local store
search(kind='patent', q='amine carbon capture', source='both')
#                                                       → default: merged
```

`source='remote'` is the natural prior-art sweep — it dedupes the
OPS results against the local store so the agent only sees
patents it hasn't fetched yet.

## How the merge works

The handler does two passes and merges them by DOCDB id:

1. **Local** — block-level hybrid (lexical tsvector + semantic
   pgvector, RRF fused) over patents already in the store.
2. **Remote** — OPS keyword search; results cached for 7 days so
   re-issuing the same query is free. Tag filters lift to OPS CQL
   automatically (see below).

Hits are interleaved by relevance; locally-stored patents are
tagged `[local]`:

```
1. ep4123456a1  [local]  Z-scheme photocatalyst for NOx abatement
   Siemens AG · 2024 · cpc=B01J27/24
   > "…visible-light driven Z-scheme heterojunction…"     ← matched block

2. wo2023123456a1         Method for selective NOxRR over MOF surfaces
   Univ. Limerick · 2023 · cpc=B01J27/24
   Abstract preview: …
```

`[local]` rows return instantly on `get(id=...)`. Untagged rows
are remote-only — calling `get(id=...)` will fetch and persist
them.

## Tag filters: how they lift to CQL

Tag filters work on **both** legs of search:

- The **local leg** uses precis's regular SQL tag filter.
- The **remote leg** translates supported tags back to OPS CQL.

| `tags=` entry | OPS CQL equivalent |
|---|---|
| `cpc:B01J27/24` | `cpc=B01J27/24` |
| `ipc:H01M` | `ic=H01M` |
| `applicant:siemens-ag` | `pa="siemens ag"` |
| `country:ep` | `cc=ep` |
| `kind:b1` | `kc=b1` |

Tags that don't have a CQL equivalent (e.g. `topic:my-project`)
are ignored on the remote leg — they only narrow the local hits.

## Bare-keyword vs. raw CQL

The default `q=` accepts a bare keyword string (lexical search):

```python
search(kind='patent', q='photocatalysis')
```

If you need Boolean operators, date windows, citation-graph
filters, applicant exclusions, or wildcard publication numbers,
the handler accepts raw OPS CQL in `q=`:

```python
search(kind='patent',
       q='cpc=B01J27/24 and pd within "2020 2025" and not pa=basf')
```

See `precis-patent-power` for the full CQL grammar.

## Saved watches (background search)

Saved CQL watches let you set up "tell me when something new
matches this query" without polling by hand:

```sh
# Default mode: open a quest for new hits, you triage manually.
precis jobs watch-patents 'cpc=B01J27/24' --name catalysts
precis jobs watch-patents 'ti=nanobud or ab=nanobud' --name nanobud --every 1d

# Auto-get: ingest new hits directly. Use --max-per-pass to cap.
precis jobs watch-patents 'pa=basf and cpc=B01J' --name basf-b01j --auto-get --max-per-pass 5

# Manage them.
precis jobs list-patent-watches
precis jobs run-patent-watches                       # one-shot pass over due watches
precis jobs watch-patents --name catalysts --delete
```

**Watches require strict CQL.** Bare keywords like
`'photocatalysis'` are rejected at create time so meaning doesn't
drift. Always use explicit fields: `ti="..."`, `ab="..."`, `cpc=`,
`pa=`, etc.

The runner is fair-use aware: when the rolling 7-day OPS bytes
total exceeds `PRECIS_PATENT_FAIR_USE_LIMIT_GB` (default 3 GiB),
it pauses without mutating any watch row. The next hourly tick
retries.

See `precis-patent-power` for usage notes and CQL recipes.

## Failure modes specific to search

- `BadInput: invalid CQL query` — OPS rejected the search; raw
  `q=` was malformed. (Bare-keyword `q=` is auto-promoted to a
  safe form, so this only fires for explicit CQL.)
- `Upstream: EPO OPS HTTP 403` — quota exceeded or bad creds.
  Weekly cap is 4 GB rolling.
- `NotFound: unknown kind: patent` — the env trio isn't set on
  this server. The kind is hidden at registration time.

## See also

- `precis-search-help` — cross-kind `search` mechanics
- `precis-patent-help` — read patents (`get`, views, slugs)
- `precis-patent-power` — raw OPS CQL grammar for advanced searches
- `precis-paper-help` — comparison: same `~N..M` chunk syntax
- `precis-tags` — tag conventions
- `docs/search-future-filters.md` — deferred filter affordances
