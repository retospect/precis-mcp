---
name: paper-structural-navigation
description: >
  Navigate a paper's structure end-to-end: TOC, abstract, summary,
  figures, chunk ranges, links, citations.  Read this when ``/toc`` is
  unavailable, when a paper has only an abstract, or when you need to
  pick the right entry point for a long paper without reading all of it.
user-invocable: true
argument-hint: [slug]
allowed-tools: [get, search]
applies-to: [paper]
kind-onboarding: paper
tags: [papers, navigation, structure]
---

## When to use

- ``get(id='<slug>/toc')`` returned ``ERROR [unavailable]`` — the ref
  has no positional blocks (likely a stub from incomplete ingestion).
- The paper is large (>200 blocks) and a flat read would blow the
  context window — you need to pick a section.
- You need a specific element (figure, equation, abstract, citation
  list) and want the cheapest path to it.
- A search hit pointed at ``slug~N`` and you want to see what came
  before / after that chunk for context.

## Decision table — which entry to pick

|  Goal                            |  Call                                       |
|  ---                             |  ---                                        |
|  Quick gist                      |  ``get(id='slug/abstract')``                |
|  One-line takeaway               |  ``get(id='slug/summary')``                 |
|  Map of sections                 |  ``get(id='slug/toc')``                     |
|  Drill into one section          |  ``get(id='slug~N..M/toc')``                |
|  Read a specific chunk           |  ``get(id='slug~N')``                       |
|  Read a chunk range              |  ``get(id='slug~N..M')``                    |
|  See a figure caption + image    |  ``get(id='slug/fig/3')``                   |
|  Get all figure captions         |  ``get(id='slug/fig')``                     |
|  BibTeX                          |  ``get(id='slug/cite/bib')``                |
|  What this paper cites           |  ``get(id='slug/links')``                   |
|  Who cites this paper            |  ``get(id='slug/links-in')``                |
|  Find a phrase inside the paper  |  ``search(query='...', scope='slug')``      |

## Recovery from /toc unavailable

When ``get(id='slug/toc')`` returns ``ERROR [unavailable]: <slug> has
no positional blocks``, the paper has metadata (abstract / summary)
but no body text in the store.  This is usually an ingestion gap —
the PDF didn't extract cleanly.  Three actions in order:

1. **Confirm with the abstract:** ``get(id='slug/abstract')`` — if
   there's no abstract either, the ref is a near-empty stub.
2. **Read the overview:** ``get(id='slug')`` shows authors, title,
   year, DOI, and any notes — enough to cite the paper at the
   metadata level.
3. **Gripe to log the gap:** ``put(type='gripe', text='<slug> has no
   body — re-ingest needed')``.  This adds it to the maintenance
   queue without blocking your task.

Until reingestion lands, **cite the DOI only** — never fabricate
quotes from a title or abstract pretending they came from the body.

## Long-paper strategy (>200 blocks)

For a 1300-block paper (e.g. a review article), don't read linearly.
The fast path:

```
get(id='slug/toc')                  # section overview
get(id='slug~12..30/toc')           # drill into one section
get(id='slug~18')                   # the specific paragraph
```

The TOC at depth 1 shows section ranges with previews; the
``slug~N..M/toc`` form expands the chosen range into a detailed list.
Three calls, ~600 tokens, instead of 1300 chunks at ~500k tokens.

## Search-anchored navigation

When you searched and got a hit at ``slug~N`` but need surrounding
context:

```
search(query='...', scope='slug')   # in-paper search
get(id='slug~N')                    # the hit chunk
get(id='slug~N..N+5')               # a window after it
get(id='slug~N-3..N')               # a window before it
```

The ``scope=`` filter ensures search results stay within the one
paper — important when an LLM might otherwise be tempted to cite
chunks from related papers as if they were from this one.

## Related skills

- ``skill:find-paper`` — acquire a paper that isn't in the store yet
- ``skill:quest-disambiguate`` — when fetch returns multiple candidates
