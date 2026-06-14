---
id: precis-stubs-help
title: precis — papers we still need to get
summary: paper acquisition backlog — stub list, fetch state, reason each is waiting
applies-to: search(kind='paper', view='stubs')
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

```python
acquire(identifier='doi:10.1038/nature10352', reason='cited across cluster')
acquire(identifier='arxiv:2401.00001', context_ref_id=<where it came up>)
acquire(title='Some Paper With No DOI Yet')   # backlog-only stub
```

Mints a stub (idempotent — a paper already held or already wanted is a
no-op) and gets out of the way. The fetcher auto-grabs an OA PDF on a
later pass when the stub carries an external id; a title-only stub
waits until someone supplies an identifier. `acquire` never downloads
or ingests inline.

## See also

```python
get(kind='skill', id='precis-paper-help')      # read, cite, search held papers
get(kind='skill', id='precis-finding-help')    # chasing un-ingested DOIs
get(kind='skill', id='precis-search-help')     # search args incl. view=
```
