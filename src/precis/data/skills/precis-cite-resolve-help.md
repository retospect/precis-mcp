---
id: precis-cite-resolve-help
family: paper
title: precis — resolve an in-text citation ([32], Miller 1921) to its source
summary: follow an in-text citation marker — [32], [3-5], (Smith et al., 2004) — to the cited paper and supporting passage, and record a cites link
answers:
  - what does [32] refer to?
  - how do I resolve an in-text citation marker to the paper it cites?
  - how do I follow a "Miller 1921" author-year citation to its source?
  - how do I link a citation in a paper chunk to the cited paper?
  - a paper says "as shown before [7]" — how do I find what [7] is?
applies-to: get/search/put/link (kind='paper')
status: active
tags: [workflow]
kinds: [paper]
---

# precis-cite-resolve-help — follow a citation marker to its source

A chunk that says "as shown before [32]" or "(Miller 1921)" cites an
entry in the citing paper's bibliography. The `cites` link from the
citing chunk to the cited paper is the followable hop: the
`cite_resolve` worker pass mints it automatically (numeric markers
from the paper's parsed bibliography, author-year matches resolved
in memory), so **the link may already be there — read before you
write**. The pass only covers papers that were marked and swept, so
an unlinked citation is normal, not a failure: this recipe resolves
it by hand, over calls that already exist.

## Is this citation already linked?
## Check the citing paper's cites edges first

```python
get(kind="paper", id="pa<citing>", view="links")
```

Every `cites` row touching the paper, both directions. A
`cite_resolve`-written row carries `meta.marker` (the label as written
— `"32"`, `"[3-5]"`, `"Miller 1921"`) and `meta.marker_order` (the
citation's first-occurrence ordinal in the chunk), and the citing
chunk sits on the row's source side. Hit → done; the write is
idempotent, so re-writing the same edge is a no-op anyway.

## Read the citing chunk and spot the marker

```python
get(kind="paper", id="pc<citing chunk>")
```

The marker is one of:

- `[32]` — one bib entry.
- `[3-5]`, `[7,9]` — a group: each expanded member is its own
  citation, ordered as written.
- `Miller 1921`, `Miller (1921)`, `(Miller, 1921)`,
  `(Smith et al., 2004)`, `Smith and Jones (2004a)` — author-year:
  first-author surname + year.

## Find the bibliography entry for the marker
## Which reference does the marker point at?

The entry lives in the citing paper's tail (reference) chunks — those
are *not* embedded, so semantic search won't find them; go lexical:

```python
# numeric: the entry text contains the marker digits
search(kind="paper", q="[32]", scope="pa<citing>", mode="lexical")
# author-year: the entry text contains the surname (and usually the year)
search(kind="paper", q="Miller 1921", scope="pa<citing>", mode="lexical")
```

Read the hit chunk — the entry carries the cited work's authors,
title, venue, year, and often a DOI.

## Is the cited paper held?
## Get or request the cited paper

```python
get(kind="paper", id="<the DOI>")              # DOI resolves when held
search(kind="paper", q="<title from the entry>")
```

Held → keep its `pa<id>` handle. Not held → request it; the stub
lands and fills in on its own (see [[precis-stubs-help]]):

```python
put(kind="paper", doi="<DOI>")
put(kind="paper", citation="<journal vol, page (year)>")   # no DOI
```

## Find the supporting passage in the cited paper
## Which chunk of the cited paper does it cite?

Body chunks *are* embedded — semantic search scoped to the cited paper,
querying with the citing claim itself:

```python
search(kind="paper", q="<the citing sentence>", scope="pa<cited>")
```

The top `pc<id>` hit is the passage the marker most likely supports.
No confident hit → link to the paper, not a chunk.

## Record the link
## Write the cites edge

`link` writes are ref-level — link the citing *paper* (the `pa` ref);
pin the citing chunk in `meta.evidence` and the cited passage as the
target when you found one:

```python
# cited paper + passage found:
link(kind="paper", id="pa<citing>", target="pc<cited chunk>", rel="cites",
     meta={"evidence": ["pc<citing chunk>"], "why": "<the citing sentence>"})
# cited paper found, no passage:
link(kind="paper", id="pa<citing>", target="pa<cited>", rel="cites",
     meta={"evidence": ["pc<citing chunk>"], "why": "<the citing sentence>"})
```

Your edge is a *different* row from the worker's, not a duplicate:
`cite_resolve` mints chunk→paper links and yours is a ref-level `pa`
edge — the link key is `(src, src_chunk, dst, dst_chunk, rel)`, so
the two coexist rather than deduping away. And a truly identical
tuple still isn't a no-op: `link` merges your `meta` (`evidence`,
`why`) into the existing row, so a repeat call adds provenance. When
the cited paper isn't held yet, link anyway once the stub exists —
the edge resolves to the filled paper when it lands.

## See also

- [[precis-paper-help]] — paper handles, views, `put` requests
- [[precis-stubs-help]] — what a stub is and how it fills in
- [[precis-search-help]] — `scope=`, `mode=`, lexical vs semantic
- [[precis-link-help]] — the `link` verb, `rel=` vocabulary
- [[precis-citation-help]] — citing claims (finding hubs), not papers
