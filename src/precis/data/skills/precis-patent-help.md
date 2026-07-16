---
id: precis-patent-help
title: precis — find, read, cite patents
summary: patent reading — DOCDB ids, EPO OPS fetch, biblio/claims/description/abstract views
applies-to: get/search/tag/link (kind='patent')
status: active
---

# precis-patent-help — find, read, cite patents

Patents are public-record documents fetched from EPO OPS. Read-only
from the agent side. Once a patent is held, its canonical address is
the **record handle** `pt<id>` (e.g. `pt40`); blocks are chunk handles
`pk<id>`. The DOCDB publication number (`ep1234567b1`) is the *fetch
key* — like a DOI, it's how you pull the patent the first time and a
durable external identity, but `pt<id>` is what you copy back into
`get`/`tag`/`link` afterwards. Both resolve on input.

## Look up a patent when I have a handle or a DOCDB id
## Open a patent by handle (or DOCDB id)
## I have a publication number — how do I read the patent?

```python
get(kind='patent', id='ep1234567b1')                    # fetch by DOCDB id (first time)
get(kind='patent', id='pt40')                           # held patent, by handle
get(kind='patent', id='pt40', view='abstract')
get(kind='patent', id='pt40', view='claims')
get(kind='patent', id='pt40', view='biblio')            # bibliographic table
get(kind='patent', id='pt40', view='description')
get(kind='patent', id='pt40', view='bibtex')
```

First `get` for an unknown DOCDB id fetches from OPS, persists, embeds,
and renders. From then on it's local — `search` lists it (with its
`pt<id>` handle), and `~chunk` selectors work.

`view='claims'` returns only the claim blocks, each prefixed with its
number and whether it is **independent** or which earlier claim(s) it
**depends on** (`view='description'` returns only the description). This
is the freedom-to-operate reading — the independent claims define the
scope a new application must design around. (Patents ingested before this
split render their full body under either view until re-fetched.)

Patent ids only accept the `~` chunk separator. Pass `view=` as a
kwarg; there is no `slug/view` path form.

```python
get(id='pk<chunk_id>')                         # single block by handle
get(kind='patent', id='ep1234567b1~5')         # legacy id~pos still resolves
get(kind='patent', id='ep1234567b1~5..12')     # block range (ranges stay id~N..M)
```

## What does a DOCDB fetch key look like?
## DOCDB id format

The DOCDB number is a *fetch key* (like a DOI), not an address you
construct — you get the `pt<id>` handle back from `get`/`search`
output. But to pull a patent the first time you supply its DOCDB
number, shape `<cc><digits><letter>[<digit>]`, lowercased:

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
search(kind='patent', q='heterojunction', scope='pt40')          # scope by handle
search(kind='patent', q='heterojunction', scope='ep1234567b1')   # DOCDB id also resolves
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
get(kind='patent', id='pt40', view='bibtex')        # or the DOCDB id
```

## Annotate a patent or cross-link it
## Tag a patent with topics
## Link a patent to a paper or memory

```python
tag(kind='patent', id='pt40', add=['topic:photocatalysis'])
tag(kind='patent', id='pt40', add=['cpc:B01J27/24'])
tag(kind='patent', id='pt40', remove=['topic:photocatalysis'])

link(kind='patent', id='pt40',
     target='pa57', rel='cited-by')              # target handle (legacy paper:<slug> resolves)
link(kind='patent', id='pt40',
     target='me88', rel='annotates')             # memory handle (legacy memory:<id> resolves)
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
put(kind='memory', text='<note>', link='pt40', rel='annotates')   # legacy patent:<docdb> resolves
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
