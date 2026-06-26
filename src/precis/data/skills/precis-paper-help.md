---
id: precis-paper-help
title: precis — find, read, cite papers
summary: scientific paper corpus — find, read, address by handle (pa/pc), views and chunk selectors
applies-to: get/search/tag/link (kind='paper')
status: active
---

# precis-paper-help — find, read, cite papers

Papers are research articles in the store. The canonical address is
the **record handle** `pa<id>` (e.g. `pa40`); individual blocks are
chunk handles `pc<id>` (e.g. `pc512`). Copy the handle straight from
search/get output back into `get`/`tag`/`link`. The legacy slug
(`abazari2024design`) and a bare DOI still resolve on input.

## Look up a paper when I have a handle
## Open a paper by handle (or legacy slug / DOI)
## I have a DOI — how do I read the paper?

```python
get(kind='paper', id='pa40')                                    # full overview, by handle
get(kind='paper', id='pa40', view='toc')                        # TOC — the reading entry point
get(kind='paper', id='abazari2024design')                       # legacy slug still resolves
get(kind='paper', id='10.1038/nature10352')                     # bare DOI resolves via metadata
get(kind='paper', id='10.1038/nature10352', view='abstract')    # DOI + view = kwarg only
get(kind='paper', id='10.1038/nature10352', view='toc')
```

The handle is the thing to keep. The DOI is a *fetch key* — once the
paper is held, `pa<id>` is its canonical address. DOI suffixes can
contain `/`, so DOI + view needs the `view=` kwarg — the `id/view`
path form is ambiguous.

## How do I get a paper's handle?

You don't construct it — you read it off `search`/`get` output (every
row leads with `pa<id>` for the paper and `pc<id>` for a block) and
paste it back into the next call. Don't reconstruct a slug from an
author + year + title; search for the paper and copy the handle:

```python
search(kind='paper', q='<author or topic>')        # → rows carry pa<id>/pc<id>
get(kind='paper', id='<DOI>')                      # DOI → fetches the paper, then use its handle
```

## Find a paper by topic
## Discover papers about a subject I'm researching
## I don't know any slugs — find papers by keyword

```python
search(kind='paper', q='photocatalytic NOx reduction')
search(kind='paper', q='photocatalytic NOx reduction', page_size=20)   # default 10, max 100
```

Hybrid lexical + semantic. Each result is a chunk handle `pc<chunk_id>`
(paste back into `get`/`link`); order is the relevance signal. The legacy
`slug~chunk` form still resolves on input.

Search also matches a paper's **title / authors / abstract** (via its
embedded metadata card), so a title query surfaces the paper even when
the body never repeats the title. When a real body block of the same
paper also matches, that body block is shown instead of the card — the
card is a fallback introducer, not a duplicate hit. A paper whose
metadata is missing (a bad-import stub) can still be unfindable; repair
it with `edit(kind='paper', id='<slug>', title=…, authors=[…], year=…)`,
which rebuilds the card.

## Find a paper that mentions an exact term
## Grep papers for a unique token (compound, DOI, exact string)
## Where does any paper mention this specific string?

```python
search(kind='paper', q='LiBF4')                  # rare tokens rank high via lexical
search(kind='paper', q='10.1038/nature10352')    # finds papers citing this DOI in body text
```

Same hybrid search — there's no pure-lexical mode, but rare tokens
land at the top of the result. Searching a DOI this way finds *citing*
papers; use `get(kind='paper', id='<DOI>')` to fetch the paper itself.

## See additional papers after a search
## Page through more search results
## What if there are more hits than I see?

```python
search(kind='paper', q='photocatalytic NOx reduction', page=2)
search(kind='paper', q='photocatalytic NOx reduction', page=3, page_size=20)
```

`page=1` is the default. Bump `page=` to walk results; `page_size=` sets
the page size (default 10, max 100).

## Filter papers by publication year
## Find recent papers / papers from a date range
## Only papers published after / before a year

```python
search(kind='paper', q='solid-state batteries', after=2019)              # 2019→present
search(kind='paper', q='solid-state batteries', before=2015)             # up to 2015
search(kind='paper', q='solid-state batteries', after=2019, before=2023) # 2019–2023
```

`after=` / `before=` are **inclusive publication-year bounds** (the
corpus stores year, not full dates — `after=2019` means 2019 and later,
`before=2023` means 2023 and earlier). Paper kind only. Papers with **no
year on record are excluded** from a year-filtered search; when that
happens the response appends a `⚠ N matching paper(s) omitted` line so a
sparse result isn't mistaken for "nothing exists" — fix missing years via
`/papers/triage`, or drop `after=`/`before=` to include them.

## Read a paper or one of its sections
## Open a paper's TOC to see what's in it
## I have a handle — what's in this paper?

Start with the TOC — it's the entry point for any non-trivial paper.

```python
get(kind='paper', id='pa40', view='toc')             # start here
get(id='pc512')                                      # single block by chunk handle
get(kind='paper', id='pa40', view='abstract')
get(kind='paper', id='pa40')                         # full overview
get(kind='paper', id='pa40/toc')                     # path form = view='toc'
get(kind='paper', id='<slug>~63..89')                # legacy: drill a slug range
get(kind='paper', id='<slug>~63..89', view='toc')    # legacy: sub-TOC of a range
get(kind='paper', id='<slug>~38')                    # legacy slug~pos still resolves
```

TOC rows are drillable: each row leads with the block handle (`pc<id>`,
or a `pc<id>..pc<id>` range) — paste it back as `id=`. Each row shows
the segment's most-distinctive keywords. Segments are clustered
dynamically by content at request time.

Views: `abstract`, `toc`, `bibtex` (`cite/bib`), `ris` (`cite/ris`),
`endnote` (`cite/endnote`). The `view=` kwarg and `slug/<view>` path
are equivalent (except for DOIs — see above).

## Find a passage in a paper I have
## Locate where a topic comes up in a specific paper
## Where does this paper discuss X?

```python
search(kind='paper', q='Z-scheme', scope='pa40')             # scope by handle
search(kind='paper', q='Z-scheme', scope='pa40', page=2)
search(kind='paper', q='Z-scheme', scope='<slug>')           # legacy slug still resolves
```

Same hybrid search as cross-corpus, scoped to one paper's blocks.

## Cite a paper
## Get a BibTeX or RIS entry for a paper
## I need to cite this in my manuscript

```python
get(kind='paper', id='pa40', view='bibtex')
get(kind='paper', id='pa40', view='ris')
get(kind='paper', id='pa40', view='endnote')
```

## Cite a figure (caption only — no image binaries)
## Reference a figure by its legend
## How do I cite Figure 3 of this paper?

Image files aren't served. The figure block holds a markdown image
marker; the legend is on the next block.

```python
get(id='pc517')                                # the figure block, by handle
get(kind='paper', id='<slug>~45')              # legacy slug~pos still resolves
```

```text
Figure 3. Schematic representation of the structure of NU-1000…
```

Cite as "Figure 3 of `pa40`" and quote the legend. The
`![](...)` marker in the body is a relative path nothing serves —
don't invent URLs.

## Tag a paper or cross-link it
## Annotate a paper with topic tags or relationships
## How do I mark this paper as topic:X?

```python
tag(kind='paper', id='pa40', add=['topic:photocatalysis'])
tag(kind='paper', id='pa40', add=['SRC:primary'])
tag(kind='paper', id='pa40', remove=['topic:photocatalysis'])

link(kind='paper', id='pa40',
     target='pa57', rel='cites')             # target is also a handle (legacy paper:<slug> resolves)
```

Closed-prefix axes for paper: `SRC:`, `CACHE:`. Open tags
(`topic:x`, ...) always allowed. Other axes (`STATUS:`, `PRIO:`, ...)
are rejected. Tag and link operate at the paper level — chunk
selectors and view paths are rejected.

## Request a paper the library doesn't have
## Get a paper we don't hold yet

`put` on a paper mints a **stub** (a request) — it never writes a body
(bodies are import-only). The `fetch_oa` worker chases an OA PDF for
any stub carrying an identifier:

```python
put(kind='paper', doi='10.1038/nature10352')           # best — resolvable id
put(kind='paper', arxiv='2401.00001', title='…')       # or an arXiv id
put(kind='paper', title='Some Paper With No DOI Yet')  # title-only backlog stub
```

Idempotent (already-held / already-wanted is a no-op). Full contract +
backlog view in `precis-stubs-help`.

## Find the paper to request — walk a citation graph
## What does this paper cite? Who cites it?

To find the *right* primary source to stub, navigate a known paper's
citation graph via Semantic Scholar. Each row carries the neighbour's
DOI — feed it straight into a `put(kind='paper', doi=…)` request:

```python
get(kind='semanticscholar', id='refs:10.1038/nature10352')   # papers it cites (its bibliography)
get(kind='semanticscholar', id='cites:10.1038/nature10352')  # papers citing it (forward citations)
get(kind='semanticscholar', id='<title or topic>')           # or search by topic → ranked hits + DOIs
```

`<paper-id>` is any S2-resolvable handle — a bare DOI, an arXiv id
(`2401.00001`), a raw S2 hash, or a prefixed `DOI:` / `ArXiv:` /
`CorpusId:` / `PMID:` form. This is the high-precision way to fill a
citation gap: the held review's reference list almost always contains
the primary source you're missing. (Cached 30 days; capped at 50 rows
per hop.)

## See also

```python
get(kind='skill', id='precis-overview')         # verbs and kinds
get(kind='skill', id='precis-search-help')      # search mechanics
get(kind='skill', id='precis-relations')        # related-to, contradicts between papers
get(kind='skill', id='precis-tags')             # axis vocabulary
get(kind='skill', id='precis-paper-tag-axes')   # paper-specific axes
get(kind='skill', id='precis-finding-help')     # chasing un-ingested DOIs
get(kind='skill', id='precis-stubs-help')       # papers we still need to get
get(kind='skill', id='precis-cite-paper-help')  # how do I cite a paper? (the router)
get(kind='skill', id='precis-check-source-help') # find a citation, read surrounds, judge support
get(kind='skill', id='precis-citation-help')    # verifier workflow for writing
get(kind='skill', id='precis-memory-help')      # capturing thoughts from a paper
```
