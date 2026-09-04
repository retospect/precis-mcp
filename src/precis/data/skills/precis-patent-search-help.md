---
id: precis-patent-search-help
title: precis — search patents (local + EPO OPS)
summary: patent discovery — merge local hits with EPO OPS, reach selection, deduped by DOCDB id
answers:
  - when do I use reach='local' vs reach='remote' for patent search?
  - how do I filter patent search by CPC code, applicant, or country?
  - how do I look up a specific patent that a search didn't return?
  - what's the difference between local and remote patent search hits?
applies-to: search (kind='patent')
status: active
---

# precis-patent-search-help — search × patent

Patent search merges local hits with EPO OPS. The patent-specific
knob is `reach=`; everything else follows the cross-kind search
shape (`page=`, `page_size=`, `tags=`, `scope=`, `exclude=`).

## Search patents
## Find a patent by topic
## Discover patents about a subject

```python
search(kind="patent", q="photocatalytic NOx reduction")
search(kind="patent", q="Z-scheme photocatalysis", page_size=20)
search(kind="patent", q="amine carbon capture", page=2)
```

Default `reach='both'` — merges local + remote, deduped by DOCDB
id. Order is the relevance signal.

## Pick which leg of search runs
## Choose between local store and EPO OPS
## When do I use reach='local' vs reach='remote'?

```python
search(kind="patent", q="amine carbon capture")  # both (default)
search(kind="patent", q="amine carbon capture", reach="local")  # local only
search(kind="patent", q="amine carbon capture", reach="remote")  # OPS, minus local
```

| `reach=` | Returns | Use when |
|---|---|---|
| `'both'` | Local ∪ OPS, merged by DOCDB id | Default discovery |
| `'local'` | Only patents already ingested | Offline; reviewing what's curated |
| `'remote'` | OPS hits **not** already local | Prior-art sweep |

`reach='remote'` is the prior-art sweep: OPS results with locally-
stored DOCDB ids drop out, so you only see patents you haven't
fetched yet. `get(kind='patent', id=...)` on any remote hit fetches
and persists it.

## Read a merged search response
## What do local vs remote hits look like?

```text
1. pt812  ep4123456a1  [local]  Z-scheme photocatalyst for NOx abatement
   Siemens AG · 2024 · cpc=B01J27/24
   > "…visible-light driven Z-scheme heterojunction…"

2. wo2023123456a1  [ops]         Method for selective NOxRR over MOF surfaces
   Univ. Limerick · 2023 · cpc=B01J27/24
   Abstract preview: …
```

`[local]` rows lead with their `pt<id>` **handle** and return
instantly from `get(id='pt812')` — the DOCDB pub-number
(`ep4123456a1`) stays alongside as the OPS fetch key (like a DOI).
`[ops]` rows are remote-only, not yet ingested — no `pt<id>` handle
has been minted for them yet; `get(id='wo2023123456a1')` fetches and
persists them, minting one. (The row marker is `[ops]` — the source
system's name — even though the `reach=` search kwarg spells the same
leg `'remote'`.)

## Filter patents by CPC, applicant, country
## Narrow patent search by tag axis

```python
search(kind="patent", q="photocatalysis", tags=["cpc:B01J27/24"])
search(kind="patent", tags=["cpc:B01J27/24", "country:ep"])
search(kind="patent", q="MOF synthesis", tags=["applicant:siemens-ag", "kind:b1"])
```

Closed-prefix tag axes for patents: `cpc:`, `ipc:`, `applicant:`,
`country:`, `kind:`. These lift to OPS CQL on the remote leg so the
filter applies to both legs. Open tags (`topic:my-project`) only
narrow the local leg.

## Search inside one patent
## Where does this patent mention X?

```python
search(
    kind="patent", q="Z-scheme", scope="pt812"
)  # handle (pub-number ep4123456a1 also resolves)
```

`scope=` restricts to one already-ingested patent's blocks. Lead with
the `pt<id>` handle; the DOCDB pub-number still resolves as the OPS
fetch key.

## Look up a specific patent that didn't match
## I have a publication number — how do I fetch it?

A DOCDB-shaped query (e.g. `ep4123456a1`, `wo2023123456a1`) that
returns no hits gets a recovery trailer:

```text
no patent matches "ep4123456a1"

Next:
  get(kind='patent', id='ep4123456a1')
    → fetch this patent from OPS directly
  put(kind='finding', title='<short claim>', body='<claim + setup>',
      cited_in='patent:ep4123456a1', scope={'...': '...'})
    → register as a chase target if OPS doesn't have it
```

The shape check is lexical (country code + digits + kind code). If
OPS does have it, `get` returns the patent and ingests it. If OPS
doesn't, `put(kind='finding', ...)` records the citation so the
chase can resume when the publication propagates.

## Use raw OPS CQL for power queries
## Boolean operators, date windows, applicant exclusions

```python
search(kind="patent", q='cpc=B01J27/24 and pd within "2020 2025" and not pa=basf')
```

The handler accepts raw OPS CQL in `q=` when Boolean operators,
date windows, citation-graph filters, or wildcard publication
numbers are needed. Bare keywords are auto-promoted; explicit CQL
is passed through.

```python
get(kind="skill", id="precis-patent-power")  # full CQL grammar + saved watches
```

## See also

```python
get(kind="skill", id="precis-search-help")  # cross-kind search mechanics
get(kind="skill", id="precis-patent-help")  # read patents (get, views, slugs)
get(kind="skill", id="precis-patent-power")  # OPS CQL grammar, saved watches
get(kind="skill", id="precis-finding-help")  # register a chase target on a miss
get(kind="skill", id="precis-tags")  # tag axis vocabulary
```
