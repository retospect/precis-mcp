---
id: precis-doi-resolution
title: precis — resolve a DOI to a paper at the agent boundary
summary: DOI handling — bare DOIs as paper identifiers, slug fallback, view-kwarg discipline
answers:
  - how do I open a paper when all I have is a DOI?
  - why does view= have to be a kwarg when I address by DOI?
  - does a pasted doi.org URL work as an id?
  - what do I do when get(kind='paper', id='<DOI>') raises NotFound?
  - how do I find which ingested papers cite a given DOI?
applies-to: get(kind='paper', id='<DOI>')
status: active
---

# precis-doi-resolution — resolve a DOI to a paper at the agent boundary

A bare DOI works anywhere a paper slug works. `get` and `search`
collapse `10.1038/nature10352` to the underlying paper transparently;
you do not need a separate resolve step.

## Open a paper when all I have is a DOI
## Fetch a paper by DOI instead of slug
## I have a DOI — how do I read the paper?

```python
get(kind="paper", id="10.1038/nature10352")  # full overview
get(kind="paper", id="10.1038/nature10352", view="abstract")  # DOI + view = kwarg only
get(kind="paper", id="10.1038/nature10352", view="toc")
get(kind="paper", id="10.1038/nature10352", view="bibtex")
```

The DOI resolves to the same paper a slug would. The response shows the
paper's **handle** (`pa<id>`) and per-chunk handles (`pc<id>`) — paste those
for any chunk-level work (e.g. `get(id='pc38')`, or a range `pa<id>~38..42`).

## Pass a view with a DOI without breaking on the slash
## Why does view= have to be a kwarg for DOIs?
## DOI plus view — the kwarg-only quirk

DOI suffixes contain `/`, so the `slug/view` path form (which works
for slugs: `id='abazari2024design/toc'`) is ambiguous for DOIs. Use
the `view=` kwarg:

```python
get(kind="paper", id="10.1038/nature10352", view="toc")  # works
get(kind="paper", id="10.1038/nature10352/toc")  # ambiguous — don't
```

Same rule for chunk selectors: `id='<DOI>~38..42'` is fine because
`~` doesn't collide with DOI grammar, but `id='<DOI>/cite/bib'`
collides — use `view='bibtex'`.

## Fetch a paper from a URL-form DOI
## Resolve doi.org links
## I copy-pasted a doi.org URL — does that work?

Strip the URL prefix and pass the bare DOI:

```python
get(
    kind="paper", id="10.1038/s41531-025-01018-8"
)  # from https://doi.org/10.1038/s41531-025-01018-8
```

`https://doi.org/`, `http://doi.org/`, `dx.doi.org/` prefixes all
need stripping. arXiv-flavoured DOIs (`10.48550/arXiv.<id>`) resolve
to the same paper as the bare arXiv id.

## Cite a paper when I only have its DOI
## Get BibTeX from a DOI
## DOI → BibTeX in one call

```python
get(kind="paper", id="10.1038/nature10352", view="bibtex")
get(kind="paper", id="10.1038/nature10352", view="ris")
get(kind="paper", id="10.1038/nature10352", view="endnote")
```

## Handle a DOI that isn't in the corpus
## Recover when a DOI lookup misses
## What if get(kind='paper', id='<DOI>') raises NotFound?

The DOI isn't ingested. Stub it so the fetch worker chases a copy:

```python
put(kind="paper", doi="10.1038/nature10352")
```

`cited_in=` on a `finding` only resolves corpus handles — a bare
`'doi:…'` is rejected. To register a claim that auto-grounds once the
paper lands, mint the finding in **acquisition mode** instead
(`wants=[{'doi': '10.1038/nature10352'}]` + `provenance=<citing
chunk>` in place of `cited_in=`) — see `precis-finding-help`.

## Find which ingested papers cite a given DOI
## Search the corpus for body-text mentions of a DOI
## Who cites 10.x/y?

```python
search(kind="paper", q="10.1038/nature10352")
```

Searching a DOI string finds papers that *mention* it in body text
(typically citing it). To fetch the paper itself, use
`get(kind='paper', id='<DOI>')`.

## See also

```python
get(kind="skill", id="precis-paper-help")  # slug grammar, views, chunk selectors
get(kind="skill", id="precis-finding-help")  # chase pipeline for un-ingested DOIs
get(kind="skill", id="precis-search-help")  # query mechanics
get(kind="skill", id="precis-citation-help")  # verifier workflow for writing
```
