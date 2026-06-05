---
id: precis-toc-help
title: precis — table of contents for any TOC-capable kind
applies-to: get(view='toc'), slug~N / slug~A..B / slug/toc
status: active
---

# precis-toc-help — table of contents for any TOC-capable kind

The TOC is the reading entry point for anything longer than a snippet.
It returns a table of drillable handles, each labelled with the
keywords that distinguish its range.

## Ask for the TOC of something
## Open the table of contents
## How do I see what's inside a paper or skill?

```python
get(kind='paper', id='<slug>', view='toc')       # kwarg form
get(kind='paper', id='<slug>/toc')               # path form, equivalent
get(kind='skill', id='precis-overview/toc')      # same on skills
```

Path form and kwarg form are interchangeable. Pick whichever reads
better. Both work on every TOC-capable kind (today: `paper`, `skill`).

## What the TOC looks like
## Read a TOC table
## What do the columns mean?

```text
# <slug> TOC — 142 chunks, 8 clusters

{handle	keywords}
<slug>~0..14	keyword phrases for this range
<slug>~15..29	keyword phrases for the next range
…
```

Each row is a `slug~A..B` handle. Paste any handle as `id=` to read
that range. Keywords are most-distinctive first — `keywords[0]` is
what makes the range unique, not what the whole document is about.

Short documents render one row per chunk with a text preview instead
of clustered keywords — there's nothing to cluster.

## Drill into a section
## Read a TOC row
## I picked a row — now what?

```python
get(kind='paper', id='<slug>~15..29')              # read the range
get(kind='paper', id='<slug>~15..29', view='toc')  # sub-TOC of the range
get(kind='paper', id='<slug>~15..29/toc')          # path form
```

Sub-TOC re-clusters the chosen range into its own table. Recurse
until rows are small enough to read directly.

## See also

```python
get(kind='skill', id='precis-paper-help')      # paper-specific TOC + drill-in
get(kind='skill', id='precis-overview')        # address grammar (slug~N, /toc)
get(kind='skill', id='precis-search-help')     # search returns slug~chunk handles
get(kind='skill', id='precis-toon')            # the table wire format
```
