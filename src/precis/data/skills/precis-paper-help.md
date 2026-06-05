---
id: precis-paper-help
title: precis — find, read, cite papers
applies-to: get/search/tag/link (kind='paper')
status: active
---

# precis-paper-help — find, read, cite papers

Papers are research articles in the store. Address by slug
(`abazari2024design`) or by bare DOI.

## Look up a paper when I have an identifier
## Open a paper by slug or DOI
## I have a DOI — how do I read the paper?

```python
get(kind='paper', id='abazari2024design')                       # full overview
get(kind='paper', id='abazari2024design', view='toc')           # TOC — the reading entry point
get(kind='paper', id='10.1038/nature10352')                     # bare DOI resolves via metadata
get(kind='paper', id='10.1038/nature10352', view='abstract')    # DOI + view = kwarg only
get(kind='paper', id='10.1038/nature10352', view='toc')
```

DOI suffixes can contain `/`, so DOI + view needs the `view=` kwarg
— the `slug/view` path form is ambiguous.

## What does a paper slug look like?
## Slug format and uniqueness
## Can I guess a slug from a citation?

A slug is `<surname><year><first-content-word>` — ASCII-folded,
stopwords skipped: `abazari2024design`, `kim2024electrocatalytic`,
`wang2020state`. Collisions append `-2`, `-3`.

Don't guess. Stopword skipping (`a`, `the`, `of`, `on`, `in`, `and`,
`for`, `with`, `to`, `by`, `is`, `are`, `from`, `into`, `as`, `at`,
`new`) and ASCII folding make construction unreliable. Use search
or DOI lookup instead:

```python
search(kind='paper', q='<author or topic>')        # find the slug
get(kind='paper', id='<DOI>')                      # DOI → fetches the paper
```

## Find a paper by topic
## Discover papers about a subject I'm researching
## I don't know any slugs — find papers by keyword

```python
search(kind='paper', q='photocatalytic NOx reduction')
search(kind='paper', q='photocatalytic NOx reduction', page_size=20)   # default 10, max 100
```

Hybrid lexical + semantic. Results are `slug~chunk` handles; order is
the relevance signal.

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

## Read a paper or one of its sections
## Open a paper's TOC to see what's in it
## I have a slug — what's in this paper?

Start with the TOC — it's the entry point for any non-trivial paper.

```python
get(kind='paper', id='<slug>', view='toc')           # start here
get(kind='paper', id='<slug>~63..89')                # drill into a TOC handle
get(kind='paper', id='<slug>~63..89', view='toc')    # sub-TOC of a range
get(kind='paper', id='<slug>', view='abstract')
get(kind='paper', id='<slug>~38')                    # single block
get(kind='paper', id='<slug>~38..42')                # explicit block range
get(kind='paper', id='<slug>')                       # full overview
get(kind='paper', id='<slug>/toc')                   # path form = view='toc'
```

TOC rows are drillable: paste a handle (`slug~A..B`) as `id=`. Each
row shows the segment's most-distinctive keywords. Segments are
clustered dynamically by content at request time.

Views: `abstract`, `toc`, `bibtex` (`cite/bib`), `ris` (`cite/ris`),
`endnote` (`cite/endnote`). The `view=` kwarg and `slug/<view>` path
are equivalent (except for DOIs — see above).

## Find a passage in a paper I have
## Locate where a topic comes up in a specific paper
## Where does this paper discuss X?

```python
search(kind='paper', q='Z-scheme', scope='<slug>')
search(kind='paper', q='Z-scheme', scope='<slug>', page=2)
```

Same hybrid search as cross-corpus, scoped to one paper's blocks.

## Cite a paper
## Get a BibTeX or RIS entry for a paper
## I need to cite this in my manuscript

```python
get(kind='paper', id='<slug>', view='bibtex')
get(kind='paper', id='<slug>', view='ris')
get(kind='paper', id='<slug>', view='endnote')
```

## Cite a figure (caption only — no image binaries)
## Reference a figure by its legend
## How do I cite Figure 3 of this paper?

Image files aren't served. The figure block holds a markdown image
marker; the legend is on the next block.

```python
get(kind='paper', id='<slug>~45')
```

```text
Figure 3. Schematic representation of the structure of NU-1000…
```

Cite as "Figure 3 of `<slug>`" and quote the legend. The
`![](...)` marker in the body is a relative path nothing serves —
don't invent URLs.

## Tag a paper or cross-link it
## Annotate a paper with topic tags or relationships
## How do I mark this paper as topic-X?

```python
tag(kind='paper', id='<slug>', add=['topic:photocatalysis'])
tag(kind='paper', id='<slug>', add=['SRC:primary'])
tag(kind='paper', id='<slug>', remove=['topic:photocatalysis'])

link(kind='paper', id='<slug>',
     target='paper:<other-slug>', rel='cites')
```

Closed-prefix axes for paper: `SRC:`, `CACHE:`. Open tags
(`topic-x`, ...) always allowed. Other axes (`STATUS:`, `PRIO:`, ...)
are rejected. Tag and link operate at the paper level — chunk
selectors and view paths are rejected.

## See also

```python
get(kind='skill', id='precis-overview')         # verbs and kinds
get(kind='skill', id='precis-search-help')      # search mechanics
get(kind='skill', id='precis-relations')        # related-to, contradicts between papers
get(kind='skill', id='precis-tags')             # axis vocabulary
get(kind='skill', id='precis-paper-tag-axes')   # paper-specific axes
get(kind='skill', id='precis-finding-help')     # chasing un-ingested DOIs
get(kind='skill', id='precis-citation-help')    # verifier workflow for writing
get(kind='skill', id='precis-memory-help')      # capturing thoughts from a paper
```
