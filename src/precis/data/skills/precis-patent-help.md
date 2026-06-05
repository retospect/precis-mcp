---
id: precis-patent-help
title: precis — find, read, cite patents
applies-to: get/search/tag/link (kind='patent')
status: active
---

# precis-patent-help — find, read, cite patents

Patents are public-record documents fetched from EPO OPS. Read-only
from the agent side: address by DOCDB id (`ep1234567b1`).

## Look up a patent when I have an id
## Open a patent by DOCDB id
## I have a publication number — how do I read the patent?

```python
get(kind='patent', id='ep1234567b1')                    # overview
get(kind='patent', id='ep1234567b1', view='abstract')
get(kind='patent', id='ep1234567b1', view='claims')
get(kind='patent', id='ep1234567b1', view='biblio')     # bibliographic table
get(kind='patent', id='ep1234567b1', view='description')
get(kind='patent', id='ep1234567b1', view='bibtex')
```

First `get` for an unknown id fetches from OPS, persists, embeds,
and renders. From then on it's local — `search` lists it as
`[local]` and `~chunk` selectors work.

Patent ids only accept the `~` chunk separator. Pass `view=` as a
kwarg; there is no `slug/view` path form.

```python
get(kind='patent', id='ep1234567b1~5')        # single block
get(kind='patent', id='ep1234567b1~5..12')    # block range
```

## What does a patent id look like?
## DOCDB id format
## How do I write a patent slug?

DOCDB shape: `<cc><digits><letter>[<digit>]`, lowercased.

```python
get(kind='patent', id='ep1234567b1')       # EP grant
get(kind='patent', id='us20240123456a1')   # US application
get(kind='patent', id='wo2023123456a1')    # PCT
```

Country code (`ep`, `us`, `wo`, `cn`, `jp`, ...) is validated
against WIPO ST.3. Inputs are case-insensitive; internal whitespace
is stripped (`EP 1234567 B1` works).

Dotted form (`ep.1234567.b1`) is rejected with a recovery hint
naming the dot-stripped id — the error tells you exactly what to
retry, so trust it and re-issue.

## Find a patent by topic
## Discover patents about a subject
## I don't know any ids — find patents by keyword

```python
search(kind='patent', q='photocatalytic NOx reduction')
search(kind='patent', q='Z-scheme photocatalysis', page_size=20)
search(kind='patent', q='amine carbon capture', source='remote')
```

Returns local + remote hits merged by DOCDB id; locally-stored
rows are tagged `[local]`. CQL queries, the `source=` matrix,
saved watches, and the OPS-CQL tag lift live in
`precis-patent-search-help`.

If you search a DOCDB-shaped string and it misses, the error
points at `get(kind='patent', id=...)` to fetch it, or
`put(kind='finding', ...)` to register it as a chase target.

## See additional patents after a search
## Page through more search results
## What if there are more hits than I see?

```python
search(kind='patent', q='photocatalysis', page=2)
search(kind='patent', q='photocatalysis', page=3, page_size=20)
```

`page=1` is the default. `page_size=` sets page size (default 10,
max 100).

## Find a passage in a patent I have
## Locate where a topic comes up in a specific patent
## Where does this patent discuss X?

```python
search(kind='patent', q='heterojunction', scope='ep1234567b1')
```

Same hybrid search, scoped to one patent's blocks.

## List patents I've already ingested
## What patents are in my local store?
## Browse locally-stored patents

```python
get(kind='patent')                   # default page (by ingest time)
get(kind='patent', id='/recent')     # newest by ingest time (when YOU added it)
get(kind='patent', id='/published')  # newest by publication date (when EPO published it)
```

`/recent` and `/published` differ: a patent filed in 2002 but
ingested last week is recent-new but published-old.

## Cite a patent
## Get a BibTeX entry for a patent

```python
get(kind='patent', id='ep1234567b1', view='bibtex')
```

## Annotate a patent or cross-link it
## Tag a patent with topics
## Link a patent to a paper or memory

```python
tag(kind='patent', id='ep1234567b1', add=['topic:photocatalysis'])
tag(kind='patent', id='ep1234567b1', add=['cpc:B01J27/24'])
tag(kind='patent', id='ep1234567b1', remove=['topic:photocatalysis'])

link(kind='patent', id='ep1234567b1',
     target='paper:wang2020state', rel='cited-by')
link(kind='patent', id='ep1234567b1',
     target='memory:<slug>', rel='annotates')
```

Closed-prefix axes for patent: `SRC:`, `CACHE:`, `cpc:`, `ipc:`,
`applicant:`, `country:`, `kind:`, `family:`. CPC/IPC/applicant
auto-apply at ingest from the OPS bibliographic data. Two open
status tags ride on patents whose full text wasn't available at
ingest: `awaiting-fulltext` and `fulltext-unavailable`. Open tags
(`topic:photocatalysis`, ...) are always allowed.

## Write a note about a patent
## I want to put a thought on a patent

Patents are read-only — `put(kind='patent', ...)` raises
`Unsupported`. Park notes on a `memory` and link it to the patent:

```python
put(kind='memory', text='<note>', link='patent:ep1234567b1', rel='annotates')
```

## See also

```python
get(kind='skill', id='precis-overview')             # verbs and kinds
get(kind='skill', id='precis-patent-search-help')   # source=, CQL, watches
get(kind='skill', id='precis-patent-power')         # raw OPS CQL grammar
get(kind='skill', id='precis-search-help')          # search mechanics
get(kind='skill', id='precis-paper-help')           # sibling kind, same ~N..M syntax
get(kind='skill', id='precis-tags')                 # axis vocabulary
get(kind='skill', id='precis-finding-help')         # register a chase target
get(kind='skill', id='precis-memory-help')          # notes attached to a patent
```
