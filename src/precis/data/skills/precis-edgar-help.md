---
id: precis-edgar-help
title: precis — find, read, compare SEC filings
summary: SEC EDGAR filings — accession ids, fetch-as-ingest, biblio/body/toc/diff views, quarter-to-quarter comparison
applies-to: get/search/tag/link (kind='edgar')
status: active
---

# precis-edgar-help — find, read, compare SEC filings

SEC EDGAR filings (10-K, 10-Q, 8-K, S-1, …) are public-record documents
fetched from the free SEC APIs. Read-only from the agent side. A filing's
canonical address is its **accession number** (`0000320193-23-000106`) —
the fetch key and durable identity. The record handle `ed<id>` and chunk
handles also resolve on input once a filing is held.

The SEC APIs need **no credentials**, only a descriptive `User-Agent`.
The kind is available only when `PRECIS_EDGAR_USER_AGENT` and
`PRECIS_EDGAR_RAW_ROOT` are set.

## Quickstart — get an NVIDIA filing

```python
get(kind='edgar', id='ticker:nvda')                  # list NVIDIA's recent filings
get(kind='edgar', id='cik:1045810')                  # same, by CIK (NVIDIA Corp)
get(kind='edgar', id='<accession-from-the-list>')    # open one (e.g. the latest 10-K)
```

Resolve the company → pick a filing from the list → `get` its accession to
fetch, parse into section-labelled blocks, and read. Then `view='diff'`
compares it to NVIDIA's prior same-form filing.

## Read a filing by accession number

```python
get(kind='edgar', id='0000320193-23-000106')                 # fetch (first time)
get(kind='edgar', id='0000320193-23-000106', view='biblio')  # bibliographic table
get(kind='edgar', id='0000320193-23-000106', view='body')    # full filing text
get(kind='edgar', id='0000320193-23-000106', view='toc')     # clustered table of contents
get(kind='edgar', id='0000320193-23-000106', view='diff')    # quarter-to-quarter changes
```

First `get` for an unknown accession fetches the submissions index + the
primary document from SEC, parses it into **section-labelled** blocks
(each block tagged with its 10-K Item / 8-K item code), persists, and
renders. From then on it's local — `search` lists it and `~chunk`
selectors work.

Accession format is `<10-digit-cik>-<2-digit-year>-<6-digit-seq>`; the
dashless form `000032019323000106` is also accepted (dashes re-inserted).

```python
get(kind='edgar', id='0000320193-23-000106~5')       # single block
get(kind='edgar', id='0000320193-23-000106~5..12')   # block range
```

## List a company's filings

```python
get(kind='edgar', id='ticker:nvda')     # resolve ticker → CIK, list recent filings
get(kind='edgar', id='cik:1045810')     # same by CIK (NVIDIA Corp)
get(kind='edgar', id='ticker:aapl')     # any ticker works
get(kind='edgar', id='cik:320193')      # or a bare CIK
```

Lists the company's recent filings from the submissions index (does not
ingest). Each row is a `get(id='<accession>')` you can open.

## Compare quarter to quarter (the interesting bits)

```python
get(kind='edgar', id='<current-accession>', view='diff')
```

`view='diff'` aligns the filing against the **prior same-form filing**
for that company (10-Q vs previous 10-Q, 10-K vs previous 10-K), section
by section, and reports the material changes: new / removed sections and
paragraph-level additions (new risk factors, changed MD&A language, …).

It also stamps queryable tags on the current filing:

- `changed:item-1a`, `changed:item-7`, … — one per materially changed section;
- `new-risk-factor` — when Item 1A gained paragraphs.

The prior filing must already be ingested. Ingest it first via the
company list above, then re-run the diff. Find all filings that changed
their risk factors:

```python
search(kind='edgar', q='...', tags=['changed:item-1a'])
search(kind='edgar', q='...', tags=['new-risk-factor'])
```

## Find a filing by topic

```python
search(kind='edgar', q='climate risk disclosure')
search(kind='edgar', q='going concern', tags=['form:10-k'])
search(kind='edgar', q='cyber incident', tags=['form:8-k', 'cik:320193'])
search(kind='edgar', q='revenue recognition', source='remote')
```

Returns local + remote EDGAR full-text hits merged by accession;
locally-stored rows are marked `[local]`, remote rows `[edgar]`.
`form:` / `cik:` / `ticker:` tags lift to the EDGAR full-text query;
open prefixes like `topic:` narrow only the local leg.

If you search an accession-shaped string and it misses, the error points
at `get(kind='edgar', id=...)` to fetch it directly.

## Scope search to one filing

```python
search(kind='edgar', q='supply chain', scope='0000320193-23-000106')
```

Same hybrid search, restricted to one filing's blocks — combine with the
section labels to answer "where does this 10-K discuss X?".

## Tag or cross-link a filing

```python
tag(kind='edgar', id='ed40', add=['topic:semiconductors'])
link(kind='edgar', id='ed40', target='pa57', rel='related-to')
```

Closed-prefix axes for edgar: `SRC:`, `CACHE:`. Auto-applied open tags at
ingest: `form:<lower>`, `cik:<digits>`, `fiscal-year:<yyyy>`. Company
name / ticker / form live in `refs.meta`, not as tag rows.

## Write a note about a filing

Filings are read-only — `put(kind='edgar', ...)` raises `Unsupported`.
Park notes on a `memory` and link it:

```python
put(kind='memory', text='<note>', link='ed40', rel='annotates')
```

## See also

```python
get(kind='skill', id='precis-overview')      # verbs and kinds
get(kind='skill', id='precis-patent-help')   # sibling public-record kind
get(kind='skill', id='precis-search-help')   # search mechanics
get(kind='skill', id='precis-tags')          # axis vocabulary
```
