---
id: precis-stubs-help
title: precis — papers we still need to get
summary: paper acquisition backlog — stub list, fetch state, reason each is waiting
applies-to: search(kind='paper', view='stubs'), put (kind='paper')
status: active
---

# precis-stubs-help — papers we still need to get

A *stub* is a paper the corpus knows about by identifier (DOI / arXiv
/ S2) but doesn't hold the PDF for yet. Stubs are the backlog of
papers still to acquire — surface them, see why each is waiting, and
add new ones.

## List the papers we still need to get
## What papers are missing PDFs?
## Show the acquisition backlog

```python
search(kind='paper', view='stubs')
search(kind='paper', view='stubs', n=50)   # default 25
```

Each row shows the ref id, the best external identifier, the cite key,
and a one-line state (`awaiting fetch`, `no OA version available`,
`PDF downloaded; awaiting watcher ingest`, …). Newest stub first.
`q=` is ignored — the view *is* the filter.

## See just the papers a dream decided to chase

```python
search(kind='paper', tags=['DREAM:acquire'])
```

`view='stubs'` is the whole backlog (chase-worker stubs included);
the `DREAM:acquire` tag marks only the ones a dream explicitly wanted.

## Open a stub to see where it came up

```python
get(kind='paper', id=<ref_id>)
```

A stub has no body yet. `get` shows its metadata and inbound links —
the finding or paper that cited it lives on the other end of a
`related-to` link.

## Add a paper to the backlog
## Queue a missing paper for fetch
## Request a paper the library doesn't have

```python
put(kind='paper', doi='10.1038/nature10352')           # best — resolvable id
put(kind='paper', arxiv='2401.00001', title='…')       # or an arXiv id
put(kind='paper', identifier='s2:<id>')                # or a Semantic Scholar id
put(kind='paper', title='Some Paper With No DOI Yet')  # title-only backlog stub
```

`put(kind='paper', …)` mints a **stub only** — it requests the paper
into this backlog; it never writes a body (paper bodies are
import-only, via `.acatome` ingest). Idempotent: a paper already held
or already wanted is a no-op.

A **DOI / arXiv / S2 id is strongly preferred** — the stub carries the
id, the `fetch_oa` worker auto-grabs an OA PDF on a later pass, and a
hallucinated identifier is rejected up front (pass `verify=False` to
force a known-real preprint S2 hasn't indexed). A **title-only** stub
just parks in the backlog until someone supplies an identifier — no
auto-fetch. Optional: `year=` (disambiguates the cite key) and
`reason=` (why it's wanted).

## Don't know the identifier? Find it first

A stub is only auto-fetched when it carries a resolvable id, so find
the DOI before you request. Walk a held paper's citation graph or
search by topic on Semantic Scholar — each hit carries a DOI to stub:

```python
get(kind='semanticscholar', id='refs:<held-doi>')    # papers it cites
get(kind='semanticscholar', id='cites:<held-doi>')   # papers citing it
get(kind='semanticscholar', id='<title or topic>')   # search → ranked hits + DOIs
```

## See also

```python
get(kind='skill', id='precis-paper-help')      # read, cite, search held papers (+ S2 nav)
get(kind='skill', id='precis-finding-help')    # chasing un-ingested DOIs
get(kind='skill', id='precis-search-help')     # search args incl. view=
```
